"""严格按用户隔离 API Key —— 单元测试。

验证：
- 无当前用户 / 用户无 KEY → create_llm 抛 MissingUserKeyError（不兜底）
- 设置了用户且该用户有 KEY → 正常构建，且用的是该用户的 KEY
- KEYLESS provider（ollama）无 KEY 也可用
- 数据源：resolve_data_source_key 严格按当前用户，any_data_source_key_encrypted 恒 None
- factory 不再读 os.environ / 全局明文缓存
"""

import pytest

import bottleneck_hunter.llm_clients.factory as F
from bottleneck_hunter.auth import current_user as CU
from bottleneck_hunter.llm_clients.factory import MissingUserKeyError


@pytest.fixture(autouse=True)
def _clear_ctx():
    tok = CU.set_current_user("")
    yield
    CU.reset_current_user(tok)


class _Fake:
    def __init__(self, key): self.key = key


class TestLLMKeyIsolation:
    def test_no_user_no_key_raises(self, monkeypatch):
        # 无当前用户 → 无 KEY → 严格失败
        monkeypatch.setattr(F, "_resolve_user_llm_key", lambda p, u: None)
        with pytest.raises(MissingUserKeyError):
            F.create_llm("deepseek", "deepseek-chat", with_fallback=False)

    def test_user_without_key_raises(self, monkeypatch):
        CU.set_current_user("userA")
        monkeypatch.setattr(F, "_resolve_user_llm_key", lambda p, u: None)
        with pytest.raises(MissingUserKeyError):
            F.create_llm("deepseek", "deepseek-chat", with_fallback=False)

    def test_user_with_own_key_used(self, monkeypatch):
        CU.set_current_user("userA")
        captured = {}
        monkeypatch.setattr(F, "_resolve_user_llm_key",
                            lambda p, u: f"key-of-{u}-{p}")

        def fake_chatopenai(model, api_key, **kw):
            captured["key"] = api_key
            return _Fake(api_key)
        monkeypatch.setattr("langchain_openai.ChatOpenAI", fake_chatopenai)
        F.create_llm("deepseek", "deepseek-chat", with_fallback=False)
        assert captured["key"] == "key-of-userA-deepseek"

    def test_no_cross_user_borrow(self, monkeypatch):
        # userB 没配 KEY，即使 userA 配了也拿不到
        keys = {("userA", "deepseek"): "A-key"}
        monkeypatch.setattr(F, "_resolve_user_llm_key",
                            lambda p, u: keys.get((u, p)))
        CU.set_current_user("userB")
        with pytest.raises(MissingUserKeyError):
            F.create_llm("deepseek", "deepseek-chat", with_fallback=False)

    def test_keyless_provider_allowed(self, monkeypatch):
        monkeypatch.setattr(F, "_resolve_user_llm_key", lambda p, u: None)

        def fake_chatopenai(model, api_key, base_url=None, **kw):
            return _Fake(api_key)
        monkeypatch.setattr("langchain_openai.ChatOpenAI", fake_chatopenai)
        # ollama 在 KEYLESS 白名单，且有内置/覆盖 base_url 时应可构建
        monkeypatch.setattr(F, "resolve_provider_base_url", lambda p, u="": "http://localhost:11434/v1")
        llm = F.create_llm("ollama", "llama3", with_fallback=False)
        assert llm is not None

    def test_factory_does_not_read_env(self, monkeypatch):
        # 设置了 env KEY，但当前用户无 KEY → 仍必须失败（证明不读 env）
        monkeypatch.setenv("DEEPSEEK_API_KEY", "env-global-key")
        monkeypatch.setattr(F, "_resolve_user_llm_key", lambda p, u: None)
        CU.set_current_user("userA")
        with pytest.raises(MissingUserKeyError):
            F.create_llm("deepseek", "deepseek-chat", with_fallback=False)


class TestDataSourceKeyIsolation:
    def test_any_data_source_key_disabled(self):
        from bottleneck_hunter.auth.store import AuthStore
        store = AuthStore()
        assert store.any_data_source_key_encrypted("finnhub") is None

    def test_resolve_data_source_key_no_user_returns_empty(self):
        from bottleneck_hunter.data_provider.data_source_catalog import resolve_data_source_key
        CU.set_current_user("")  # 无当前用户
        assert resolve_data_source_key("finnhub") == ""

    def test_resolve_data_source_key_uses_current_user(self, monkeypatch):
        import bottleneck_hunter.data_provider.data_source_catalog as cat
        from bottleneck_hunter.auth import store as store_mod

        captured = {}

        class _Store:
            def get_data_source_key_encrypted(self, uid, sid):
                captured["uid"] = uid
                captured["sid"] = sid
                return "enc" if uid == "userA" else None
        monkeypatch.setattr(store_mod, "AuthStore", lambda: _Store())
        monkeypatch.setattr("bottleneck_hunter.auth.crypto.decrypt", lambda e: "decrypted-key")

        CU.set_current_user("userA")
        assert cat.resolve_data_source_key("finnhub") == "decrypted-key"
        assert captured == {"uid": "userA", "sid": "finnhub"}

        CU.set_current_user("userB")  # userB 无 KEY → 空，不借 userA
        assert cat.resolve_data_source_key("finnhub") == ""


class TestHasUsableLLM:
    """自动化门控核心谓词：无 Key / Key 全硬死 → False（冻结）；有效 Key / keyless → True。"""

    def test_no_user_id_false(self):
        assert F.has_usable_llm("") is False

    def test_user_with_valid_key_true(self, monkeypatch):
        monkeypatch.setattr(F, "resolve_primary_for_user", lambda u: "deepseek")
        monkeypatch.setattr(F, "list_custom_provider_ids", lambda: [])
        monkeypatch.setattr(F, "_resolve_user_llm_key", lambda p, u: "sk-xxx" if u == "U" else None)
        monkeypatch.setattr(F, "resolve_provider_model", lambda p, u="": "deepseek-chat")
        assert F.has_usable_llm("U") is True

    def test_all_keys_hard_disabled_false(self, monkeypatch):
        """达涅利实况：配了 Key 但全部认证失效/欠费硬死 → 决策链每轮必冻 → 冻结自动化。"""
        from bottleneck_hunter.llm_clients import provider_gate
        monkeypatch.setattr(F, "resolve_primary_for_user", lambda u: "deepseek")
        monkeypatch.setattr(F, "list_custom_provider_ids", lambda: [])
        monkeypatch.setattr(F, "_resolve_user_llm_key", lambda p, u: "sk-dead")  # 有 Key
        monkeypatch.setattr(F, "resolve_provider_model", lambda p, u="": "m")
        monkeypatch.setattr(provider_gate, "is_hard_disabled", lambda u, p: True)  # 但全硬死
        assert F.has_usable_llm("U") is False

    def test_no_key_no_keyless_false(self, monkeypatch):
        monkeypatch.setattr(F, "resolve_primary_for_user", lambda u: "")
        monkeypatch.setattr(F, "list_custom_provider_ids", lambda: [])
        monkeypatch.setattr(F, "_resolve_user_llm_key", lambda p, u: None)  # 没配任何 Key
        # KEYLESS provider（ollama 等）仍会被 _user_has_llm_key 视为可用，但需能解析出模型；
        # 这里让模型解析恒空，确保没有任何节点满足 → False
        monkeypatch.setattr(F, "resolve_provider_model", lambda p, u="": "")
        assert F.has_usable_llm("U") is False
