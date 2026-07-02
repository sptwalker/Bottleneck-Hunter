"""用户管理中心自检 —— 守住两个关键行为：账户只读不创建 / AI配置跨用户拷贝。"""

from bottleneck_hunter.watchlist.store import WatchlistStore


def test_list_sim_accounts_readonly(tmp_path):
    """list_sim_accounts 只读：无账户用户返回 []，且不会像 get_sim_account 那样自动创建。"""
    store = WatchlistStore(str(tmp_path / "t.db"))
    assert store.for_user("u_empty").list_sim_accounts("u_empty") == []

    # 另一用户显式建账户（get_sim_account 会创建）
    acct = store.for_user("u_has").for_market("us_stock").get_sim_account()
    assert acct["id"]
    got = store.for_user("u_has").list_sim_accounts("u_has")
    assert len(got) == 1 and got[0]["market"] == "us_stock"

    # 关键：多次只读查询 u_empty 仍无账户（未被污染）
    assert store.for_user("u_empty").list_sim_accounts("u_empty") == []


def test_role_config_cross_user_copy(tmp_path):
    """copy-to-me 核心：源用户配置逐条 upsert 到目标 user_id，覆盖不重复，源不受影响。"""
    store = WatchlistStore(str(tmp_path / "t.db"))
    store.upsert_role_config("L1_macro", 0, "deepseek", "deepseek-chat",
                             "L1 宏观策略", "decision", user_id="src")
    store.upsert_role_config("L1_macro", 1, "openai", "gpt-4o",
                             "L1 宏观策略", "decision", user_id="src")
    src = store.get_role_configs(user_id="src")
    assert len(src) == 2

    def _copy():
        for c in src:
            store.upsert_role_config(c["role_key"], c["slot_index"], c["provider"], c["model"],
                                     c["role_label"], c["role_group"], user_id="admin")

    _copy()
    admin = store.get_role_configs(user_id="admin")
    assert len(admin) == 2
    assert {c["slot_index"] for c in admin} == {0, 1}
    assert {c["provider"] for c in admin} == {"deepseek", "openai"}

    _copy()  # 再拷一次：UNIQUE(role_key,slot_index,user_id) 覆盖，不新增
    assert len(store.get_role_configs(user_id="admin")) == 2
    assert len(store.get_role_configs(user_id="src")) == 2  # 源不受影响


def test_count_by_tier_and_configs_isolated(tmp_path):
    """overview 计数的隔离：不同用户的观察池/配置互不串。"""
    store = WatchlistStore(str(tmp_path / "t.db"))
    store.for_user("a").add({"ticker": "NVDA", "tier": "focus", "market": "us_stock"})
    store.upsert_role_config("L1_macro", 0, "deepseek", "deepseek-chat", "", "", user_id="a")

    assert len(store.for_user("a").list_all()) == 1
    assert len(store.for_user("a").get_role_configs(user_id="a")) == 1
    # 用户 b 干净
    assert store.for_user("b").list_all() == []
    assert store.for_user("b").get_role_configs(user_id="b") == []
