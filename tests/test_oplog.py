"""实时操作日志：路径→白话映射 + store 记录/隔离/过滤/30天清理。"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from bottleneck_hunter.watchlist.store import WatchlistStore
from bottleneck_hunter.web.oplog import _resolve_action


def test_resolve_action():
    assert _resolve_action("POST", "/api/watchlist") == "加入观察池"
    assert _resolve_action("DELETE", "/api/watchlist/abc123") == "移出观察池"
    assert _resolve_action("POST", "/api/decision/full-refresh") == "全量刷新决策"
    assert _resolve_action("POST", "/api/custom-providers") == "新增 AI 接口"
    assert _resolve_action("POST", "/api/unknown/x").startswith("POST /api/unknown")


def test_store_roundtrip_isolation_filter(tmp_path):
    s = WatchlistStore(str(tmp_path / "t.db"))
    s.record_operation("u1", "加入观察池", category="user_action", detail="AAPL")
    s.record_operation("u1", "行情自动更新", category="auto_update", detail="成功 12/12")
    s.record_operation("u2", "别人的操作", category="user_action")
    assert len(s.get_operations("u1")) == 2          # 按用户隔离
    assert len(s.get_operations("u2")) == 1
    assert len(s.get_operations("u1", category="auto_update")) == 1   # 类别过滤
    assert s.get_operations("u1")[0]["ts"] >= s.get_operations("u1")[1]["ts"]  # 时间倒序


def test_rapid_records_distinct_ts(tmp_path):
    """微秒级时间戳：连发多条 ts 各不相同，避免 `ts <` 分页整秒吃掉历史。"""
    s = WatchlistStore(str(tmp_path / "t.db"))
    for i in range(5):
        s.record_operation("u1", f"操作{i}")
    tss = [r["ts"] for r in s.get_operations("u1")]
    assert len(set(tss)) == 5   # 全不重复


def test_prune_30_days(tmp_path):
    dbp = str(tmp_path / "t.db")
    s = WatchlistStore(dbp)
    s.record_operation("u1", "新记录")
    old = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat(timespec="seconds")
    conn = sqlite3.connect(dbp)
    conn.execute("INSERT INTO operation_log (id,user_id,ts,category,title) VALUES (?,?,?,?,?)",
                 ("old1", "u1", old, "user_action", "40天前"))
    conn.commit(); conn.close()
    assert len(s.get_operations("u1")) == 2
    assert s.prune_operations(30) == 1      # 只删旧的
    assert len(s.get_operations("u1")) == 1
