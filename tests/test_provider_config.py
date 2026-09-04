"""provider_configs 单一真源 + 去写死解析 自检。"""

from bottleneck_hunter.llm_clients import factory as F
from bottleneck_hunter.llm_clients.role_registry import get_role
from bottleneck_hunter.watchlist.store import WatchlistStore


def test_resolve_model_seed_then_override():
    F._PROVIDER_OVERRIDES.clear()
    # 无覆盖 → 种子常量
    assert F.resolve_provider_model("openai") == F.PROVIDER_MODELS["openai"]
    # 全局覆盖（运行时缓存）→ 覆盖值
    F.register_provider_override("openai", "gpt-x", "")
    assert F.resolve_provider_model("openai") == "gpt-x"
    F._PROVIDER_OVERRIDES.clear()


def test_resolve_base_url_seed_then_override():
    F._PROVIDER_OVERRIDES.clear()
    assert F.resolve_provider_base_url("deepseek") == "https://api.deepseek.com"
    assert F.resolve_provider_base_url("openai") is None  # 走 SDK 默认端点
    F.register_provider_override("deepseek", "", "https://proxy.local/v1")
    assert F.resolve_provider_base_url("deepseek") == "https://proxy.local/v1"
    F._PROVIDER_OVERRIDES.clear()


def test_provider_config_store_roundtrip(tmp_path):
    s = WatchlistStore(str(tmp_path / "t.db"))
    s.upsert_provider_config("openai", "gpt-user", "", user_id="u1")
    s.upsert_provider_config("openai", "gpt-global", "https://g/v1", user_id="")
    assert s.get_provider_config("openai", user_id="u1")["default_model"] == "gpt-user"
    g = s.get_provider_config("openai", user_id="")
    assert g["default_model"] == "gpt-global" and g["base_url"] == "https://g/v1"
    assert s.get_provider_config("openai", user_id="nobody") is None
    # upsert 覆盖（不重复插入）
    s.upsert_provider_config("openai", "gpt-user2", "", user_id="u1")
    assert s.get_provider_config("openai", user_id="u1")["default_model"] == "gpt-user2"
    assert len([c for c in s.get_provider_configs(user_id="u1") if c["provider_id"] == "openai"]) == 1


def test_role_defaults_no_hardcoded_model():
    # 角色默认不再写死模型，只保留 provider；模型由 resolve_provider_model 解析
    g = get_role("committee_growth")
    assert g.default_provider == "qwen" and g.default_model == ""
    assert get_role("L1_macro").default_model == ""


def test_keyed_user_blank_model_does_not_inherit_global(monkeypatch):
    """P1 回归：用户自带 kimi Key 但模型留空时，绝不套用管理员/全局 kimi 模型，回退种子。
    根因事故：全局 kimi-k3 被强加到 4f4ec3b54d384135 自己的 Key 上 → 其账户无该模型权限 →
    insufficient balance → 整个 kimi 节点被误判「欠费」熔断。信号取「自有 Key」而非配置行，
    覆盖「有 Key 但无 provider_configs 行」的用户（如 d32a4acd/cd95b8de）。"""
    F._PROVIDER_OVERRIDES.clear()
    F.register_provider_override("kimi", "kimi-k3", "")  # 管理员/全局把 kimi 设成 kimi-k3
    # 用户模型留空（无自有 default_model 行，或有行但留空皆可）
    monkeypatch.setattr(F, "_load_provider_config_from_db", lambda prov, uid: None)

    # 用户自带 kimi Key → 不继承全局 kimi-k3，落到种子
    monkeypatch.setattr(F, "_resolve_user_llm_key",
                        lambda prov, uid: "sk-real-key" if prov == "kimi" else None)
    assert F.resolve_provider_model("kimi", "4f4ec3b54d384135") == F.PROVIDER_MODELS["kimi"]
    # 用户自己填了模型 → 用自己的
    monkeypatch.setattr(F, "_load_provider_config_from_db",
                        lambda prov, uid: {"default_model": "moonshot-v1-8k"})
    assert F.resolve_provider_model("kimi", "4f4ec3b54d384135") == "moonshot-v1-8k"
    # 无自有 Key 的用户 → 仍继承全局覆盖（全局覆盖只服务无 Key 的共享用户 / keyless provider）
    monkeypatch.setattr(F, "_load_provider_config_from_db", lambda prov, uid: None)
    monkeypatch.setattr(F, "_resolve_user_llm_key", lambda prov, uid: None)
    assert F.resolve_provider_model("kimi", "someone") == "kimi-k3"
    F._PROVIDER_OVERRIDES.clear()
