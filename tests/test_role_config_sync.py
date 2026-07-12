"""provider 默认模型变更 → 同步角色矩阵里钉着旧模型的条目（修"改了模型不生效")。"""

from __future__ import annotations

from bottleneck_hunter.watchlist.store import WatchlistStore


def test_sync_role_config_model(tmp_path):
    store = WatchlistStore(str(tmp_path / "t.db"))
    # 三条矩阵：kimi 钉旧默认8k、kimi 显式128k、deepseek 无关
    store.upsert_role_config("L1_macro", 0, "kimi", "moonshot-v1-8k", user_id="u1")
    store.upsert_role_config("committee_value", 0, "kimi", "moonshot-v1-128k", user_id="u1")
    store.upsert_role_config("L2_strategic", 0, "deepseek", "deepseek-chat", user_id="u1")

    # kimi 默认模型 8k→32k
    n = store.sync_role_config_model("kimi", "moonshot-v1-8k", "moonshot-v1-32k")
    assert n == 1  # 只同步钉着旧默认的那条

    cfgs = {c["role_key"]: c["model"] for c in store.get_role_configs(user_id="u1")}
    assert cfgs["L1_macro"] == "moonshot-v1-32k"          # 同步到新模型
    assert cfgs["committee_value"] == "moonshot-v1-128k"  # 显式选的他模型不动
    assert cfgs["L2_strategic"] == "deepseek-chat"        # 别的 provider 不动


def test_sync_noops(tmp_path):
    store = WatchlistStore(str(tmp_path / "t.db"))
    assert store.sync_role_config_model("kimi", "m", "m") == 0    # 新旧相同
    assert store.sync_role_config_model("", "a", "b") == 0        # 空 provider
    assert store.sync_role_config_model("kimi", "", "b") == 0     # 空旧模型
    assert store.sync_role_config_model("kimi", "x", "y") == 0    # 无匹配条目
