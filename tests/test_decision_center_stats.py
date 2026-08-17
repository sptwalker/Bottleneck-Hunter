"""决策中心运行统计 get_decision_center_stats：

计数正确 + 战术按市场拆分（「两个市场分别」）+ 投委会终裁分桶（通过/否决/待议）
+ MIN(created_at) 起点 + 跨用户隔离（u2 绝不见 u1 数据）。
"""
import pytest

from bottleneck_hunter.watchlist.store import WatchlistStore


@pytest.fixture
def db(tmp_path, monkeypatch):
    from bottleneck_hunter.auth import store as auth_store_mod
    monkeypatch.setattr(auth_store_mod, "_DEFAULT_DB", tmp_path / "auth.db")
    return tmp_path / "wl.db"


def _seed(base, table, cols, rows):
    """裸 SQL 批量插入（显式带 user_id/market，绕过 _filtered，用于构造计数样本）。"""
    ph = ",".join("?" * len(cols))
    with base._write_conn() as conn:
        for r in rows:
            conn.execute(f"INSERT INTO {table} ({','.join(cols)}) VALUES ({ph})", r)


def test_decision_center_stats_counts_split_and_isolation(db):
    base = WatchlistStore(db)  # 建库+迁移(补 user_id/market 列)；仅用其连接做裸插入
    NOW = "2026-08-01T00:00:00"
    EARLIER = "2026-07-01T00:00:00"

    _seed(base, "macro_strategies", ["id", "version", "created_at", "user_id", "market"], [
        ("m1", 1, EARLIER, "u1", "us_stock"),
        ("m2", 2, NOW, "u1", "us_stock"),
        ("m3", 1, NOW, "u1", "a_stock"),
    ])
    _seed(base, "tactical_plans", ["id", "ticker", "plan_date", "created_at", "user_id", "market"], [
        ("t1", "AAPL", "2026-08-01", NOW, "u1", "us_stock"),
        ("t2", "MSFT", "2026-08-01", NOW, "u1", "us_stock"),
        ("t3", "600519.SS", "2026-08-01", NOW, "u1", "a_stock"),
    ])
    _seed(base, "committee_consensus",
          ["id", "execution_plan_id", "final_verdict", "created_at", "user_id", "market"], [
        ("c1", "e1", "approved", NOW, "u1", "us_stock"),
        ("c2", "e2", "approved_with_modifications", NOW, "u1", "us_stock"),
        ("c3", "e3", "rejected", NOW, "u1", "a_stock"),
        ("c4", "e4", "needs_review", NOW, "u1", "us_stock"),   # 归入待议
    ])
    _seed(base, "sim_trades",
          ["id", "account_id", "ticker", "side", "shares", "price", "amount", "created_at", "user_id", "market"], [
        ("s1", "acc", "AAPL", "buy", 10, 100.0, 1000.0, NOW, "u1", "us_stock"),
        ("s2", "acc", "AAPL", "sell", 5, 110.0, 550.0, NOW, "u1", "us_stock"),
    ])
    _seed(base, "experience_cards", ["id", "title", "content", "created_at", "user_id", "market"], [
        ("x1", "教训A", "内容", NOW, "u1", "us_stock"),
    ])
    # u2：只有宏观，用于证明隔离（绝不进 u1 的计数）
    _seed(base, "macro_strategies", ["id", "version", "created_at", "user_id", "market"], [
        ("u2m1", 1, NOW, "u2", "us_stock"),
        ("u2m2", 1, NOW, "u2", "a_stock"),
    ])

    st = WatchlistStore(db).for_user("u1").get_decision_center_stats()
    assert st["macro_rounds"] == 3                                     # 跨两市场汇总
    assert st["tactical_total"] == 3
    assert st["tactical_by_market"] == {"us_stock": 2, "a_stock": 1}   # 「两个市场分别」
    assert st["committee_total"] == 4
    assert st["committee_approved"] == 2                # approved + approved_with_modifications
    assert st["committee_rejected"] == 1
    assert st["committee_pending"] == 1                 # needs_review
    assert st["trades"] == 2
    assert st["experiences"] == 1
    assert st["since"] == EARLIER                       # 最早一条 L1 宏观

    # 跨用户隔离：u2 只见自己的 2 条宏观，绝不含 u1 的任何数据
    st2 = WatchlistStore(db).for_user("u2").get_decision_center_stats()
    assert st2["macro_rounds"] == 2
    assert st2["tactical_total"] == 0
    assert st2["committee_total"] == 0
