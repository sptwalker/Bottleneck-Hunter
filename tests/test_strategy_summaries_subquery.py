"""回归：决策中心全量刷新时 get_all_strategy_summaries 曾因 G-4 子查询护栏报 500。

根因：该方法的关联子查询(MAX version)被 _user_filter 的字符串插入护栏拦截报错，
导致 decision_engine._collect/_update 调用它时崩溃，L1-L4 全红。
修复：改为显式对内/外两层 WHERE 做 user_id 过滤，不再走 _user_filter 自动插入。
本测试确保它在带/不带 user 过滤时都能正常执行且正确隔离。
运行：pytest tests/test_strategy_summaries_subquery.py -q
"""
from __future__ import annotations

import shutil
import tempfile
import uuid
from pathlib import Path

import pytest

from bottleneck_hunter.watchlist.store import WatchlistStore


@pytest.fixture
def tmpdir_path():
    # Windows 下 WAL sidecar 文件可能滞留导致自动清理失败，用 ignore_errors 兜底
    d = tempfile.mkdtemp()
    yield d
    shutil.rmtree(d, ignore_errors=True)


def _seed(store, entry_id, ticker, signal, version, user_id=""):
    conn = store._connect()
    try:
        conn.execute(
            "INSERT INTO strategy_records (id, entry_id, ticker, version, signal, confidence, "
            "status, created_at, user_id) VALUES (?,?,?,?,?,?,?,?,?)",
            (uuid.uuid4().hex[:12], entry_id, ticker, version, signal, 7,
             "completed", "2026-07-03T00:00:00", user_id),
        )
        conn.commit()
    finally:
        conn.close()


def test_get_all_strategy_summaries_runs_without_guard_error(tmpdir_path):
    """核心回归：含子查询的该方法不再被 _user_filter 护栏拦截报错。"""
    store = WatchlistStore(db_path=str(Path(tmpdir_path) / "s.db"))
    _seed(store, "e1", "NVDA", "bullish", 1)
    _seed(store, "e1", "NVDA", "neutral", 2)   # e1 最新版本=2 hold
    _seed(store, "e2", "AAPL", "bearish", 1)
    res = store.get_all_strategy_summaries()   # 不应抛异常
    assert res["e1"]["version"] == 2
    assert res["e1"]["signal"] == "neutral"
    assert res["e2"]["signal"] == "bearish"


def test_get_all_strategy_summaries_user_isolation(tmpdir_path):
    """带 user 过滤时只返回该用户的数据，无跨用户泄露。"""
    base = WatchlistStore(db_path=str(Path(tmpdir_path) / "s.db"))
    _seed(base, "e1", "NVDA", "bullish", 1, user_id="userA")
    _seed(base, "e2", "AAPL", "bearish", 1, user_id="userB")

    a = base.for_user("userA").get_all_strategy_summaries()
    b = base.for_user("userB").get_all_strategy_summaries()
    assert set(a.keys()) == {"e1"}
    assert set(b.keys()) == {"e2"}
    assert "e2" not in a and "e1" not in b


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-q"])
