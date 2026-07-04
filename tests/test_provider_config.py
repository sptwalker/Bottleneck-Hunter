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
