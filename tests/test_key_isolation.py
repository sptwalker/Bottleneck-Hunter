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
from bottleneck_hunter.llm_clients.factory import MissingUserKeyError
from bottleneck_hunter.auth import current_user as CU


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
