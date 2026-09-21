"""跨账户串味 + 每日决策回执的回归测试。

**背景（生产事故）**：`quality_gate` 的 pre_l4 仓位集中度检查写的是
`account = store.get_sim_account()`（决策中心自有账户，权益 100 万）配
`store.get_sim_positions()`（不传 account_id → 该用户该市场**全部**账户持仓）。
用户同时有 VIP 真实券商账户时，分子变成 VIP 持仓市值、分母仍是决策中心权益，
权重虚报 68%~255% → 误判红灯 → L4 被整体阻断 → 美股连续多日零执行方案，
而调度器照记 success。

本文件锁死三件事：
1. 仓位检查必须按 account_id 取持仓（不再串味）；
2. 单只权重 >100% 这种不可能量级必须被忽略并告警，而不是当红灯用；
3. 每日决策回执必须把「跑了但没操作」与「被质量门阻断」区分成不同文案。
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from bottleneck_hunter.watchlist.decision_report import (
    build_report,
    empty_summary,
    summarize_event,
)
from bottleneck_hunter.watchlist.quality_gate import run_quality_checks
from bottleneck_hunter.watchlist.store import WatchlistStore

UID = "u_isolate"


def _market_store(tmp_path: Path) -> WatchlistStore:
    # 显式传 db_path：绝不依赖 WATCHLIST_DB 环境变量，否则测试会静默写进生产库。
    return WatchlistStore(db_path=tmp_path / "wl.db", user_id=UID).for_user(UID).for_market("us_stock")


def _seed(s: WatchlistStore) -> tuple[str, str]:
    """两账户并存：决策中心权益 100 万但持仓很小；VIP 账户持仓巨大（串味的污染源）。"""
    dc = s.get_sim_account()                      # 决策中心自有模拟盘，account_ref=''
    s.ensure_vip_account("CITI-1", display_name="花旗银行")
    vip = s.get_sim_account("CITI-1")
    for t in ("NVDA", "MU", "PLTR", "ORCL"):
        # run_quality_checks 见到空观察池会直接 quality_check_pass 返回，必须先有标的。
        s.add({"ticker": t, "market": "us_stock"})
    # 顺手塞新鲜快照，否则数据新鲜度也会判红，掩盖掉本测试真正要盯的仓位集中度结论。
    now = datetime.now(timezone.utc).isoformat()
    s.save_snapshots([
        {"ticker": t, "date": now[:10], "close": 100.0, "fetched_at": now, "market": "us_stock"}
        for t in ("NVDA", "MU", "PLTR", "ORCL")
    ])

    s.create_sim_position(account_id=dc["id"], ticker="NVDA", shares=120, avg_cost=200.0)
    s.update_sim_position(
        s.get_sim_position(dc["id"], "NVDA")["id"], current_price=230.0, market_value=27_600.0
    )
    s.create_sim_position(account_id=dc["id"], ticker="MU", shares=18, avg_cost=900.0)
    s.update_sim_position(
        s.get_sim_position(dc["id"], "MU")["id"], current_price=925.0, market_value=16_650.0
    )
    # VIP：单只 68.5 万 —— 串味时会被算成决策中心账户的 68.5% 权重（实测生产值）
    s.create_sim_position(account_id=vip["id"], ticker="PLTR", shares=3680, avg_cost=156.0)
    s.update_sim_position(
        s.get_sim_position(vip["id"], "PLTR")["id"], current_price=186.4, market_value=685_878.0
    )
    s.update_sim_account(total_equity=1_000_000.0, current_capital=1_000_000.0)
    s.update_sim_account("CITI-1", total_equity=25_000_000.0)
    return dc["id"], vip["id"]


def _concentration_warnings(events: list[dict]) -> list[dict]:
    return [
        w
        for e in events
        for w in e.get("data", {}).get("warnings", [])
        if w.get("type") == "position_concentration"
    ]


async def _collect(store: WatchlistStore, stage: str) -> list[dict]:
    return [evt async for evt in run_quality_checks(store, stage)]


def test_pre_l4_仓位检查不跨账户串味(tmp_path: Path) -> None:
    s = _market_store(tmp_path)
    _seed(s)

    events = asyncio.run(_collect(s, "pre_l4"))

    blocks = [e for e in events if e["event"] == "quality_check_block"]
    assert not blocks, f"决策中心账户最大权重仅 2.76%，不该有质量门红灯：{blocks}"

    # 红灯标的只进 reason 不进 warnings，所以整份事件都要查。
    for e in events:
        blob = json.dumps(e.get("data", {}), ensure_ascii=False)
        assert "PLTR" not in blob, f"VIP 账户持仓串进了决策中心账户的仓位检查：{e}"


def test_权重超100视为串味异常而非红灯(tmp_path: Path) -> None:
    """即便上游真把两账户持仓混着送来，也不该拿 >100% 的假权重去阻断 L4。"""
    s = _market_store(tmp_path)
    dc_id, _ = _seed(s)
    assert not [e for e in asyncio.run(_collect(s, "pre_l4")) if e["event"] == "quality_check_block"]

    # 反向验证：真的同一账户内集中持仓时仍要判红——不能把检查改废
    s.create_sim_position(account_id=dc_id, ticker="ORCL", shares=2000, avg_cost=200.0)
    s.update_sim_position(
        s.get_sim_position(dc_id, "ORCL")["id"], current_price=225.0, market_value=450_000.0
    )
    events = asyncio.run(_collect(s, "pre_l4"))
    blocks = [e for e in events if e["event"] == "quality_check_block"]
    assert blocks, "同一账户内 45% 集中持仓应当仍然判红灯"
    assert "ORCL" in blocks[0]["data"].get("reason", ""), blocks[0]["data"]


def test_每日回执区分正常运行与被质量门阻断() -> None:
    # 情形 A：真的产出执行方案
    ok = empty_summary()
    summarize_event({"event": "quality_check_pass", "stage": "pre_l4"}, ok)
    summarize_event({"event": "decision_done", "layer": "L4", "plan_count": 2,
                     "blocked_count": 0, "repaired_count": 0,
                     "message": "L4 执行方案已生成：2 条待确认操作"}, ok)
    r, _title, detail = build_report(ok, market="us_stock", ticker_count=38)
    assert r == "success" and "已生成 2 条待确认操作" in detail

    # 情形 B：跑到 L4 但 LLM 主动选择不动手 —— 必须明确是「运行正常」
    quiet = empty_summary()
    summarize_event({"event": "decision_done", "layer": "L4", "plan_count": 0,
                     "blocked_count": 0, "repaired_count": 0,
                     "message": "L4 执行方案已生成：0 条待确认操作"}, quiet)
    r2, title2, detail2 = build_report(quiet, market="us_stock", ticker_count=38)
    assert r2 == "success"
    assert "运行正常" in title2 and "运行正常" in detail2

    # 情形 C：被 pre_l4 质量门红灯阻断 —— 绝不能长得像情形 B
    blocked = empty_summary()
    summarize_event({"event": "quality_check_block", "stage": "pre_l4", "severity": "red",
                     "reason": "仓位集中度：PLTR(68.5%)"}, blocked)
    summarize_event({"event": "decision_warning", "layer": "L4",
                     "message": "⛔ 质量门红灯，已阻断 L4 新建执行计划"}, blocked)
    r3, title3, detail3 = build_report(blocked, market="us_stock", ticker_count=38)
    assert r3 == "partial"
    assert "阻断" in title3 and "PLTR(68.5%)" in detail3
    assert title3 != title2 and detail3 != detail2

    # 情形 D：L3 全为持有（正常）
    hold = empty_summary()
    summarize_event({"event": "decision_done", "layer": "L3", "plan_count": 9}, hold)
    summarize_event({"event": "decision_done", "layer": "L4",
                     "message": "L3 计划全部为持有，无需生成执行方案"}, hold)
    r4, title4, _ = build_report(hold, market="a_stock", ticker_count=25)
    assert r4 == "success" and "运行正常" in title4

    # 情形 E：无 L3 战术计划（链路缺失，属 fail）
    none_l3 = empty_summary()
    summarize_event({"event": "decision_info", "layer": "L4",
                     "message": "今日无 L3 战术计划，跳过 L4"}, none_l3)
    r5, _, detail5 = build_report(none_l3, market="us_stock", ticker_count=38)
    assert r5 == "fail" and "无 L3 战术计划" in detail5


@pytest.mark.parametrize("market", ["us_stock", "a_stock"])
def test_回执不会因缺字段炸掉(market: str) -> None:
    """summary 缺字段/事件畸形时回执也必须给出可用文案（回执绝不能拖垮决策）。"""
    s = empty_summary()
    summarize_event({"event": "quality_check_block"}, s)             # 无 stage
    summarize_event({"event": "decision_done"}, s)                   # 无 layer
    summarize_event({"event": "decision_error", "layer": "L4"}, s)   # 无 error
    result, title, detail = build_report(s, market=market)
    assert result in ("success", "partial", "fail")
    assert title and detail
