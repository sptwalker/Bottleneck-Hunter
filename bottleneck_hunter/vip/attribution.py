"""Phase 5b · VIP 轻量归因底座（C-4）——结算单导入时做确定性的仓位事件检测。

设计边界（与路线一致）：
- 只做**确定性 diff**（上一份结算单 sim 持仓 vs 本次导入）：平仓 / 大幅增减仓。
  不调 LLM、不猜因果——事件一律标「推断·非确认」，是备忘录而非结论。
- 只写 vip_account_log（event_type 复用 'calibration'，避开 CHECK 重建），绝不写 sim_*。
- 阈值开关：LLM 归因+经验卡沉淀是**第二段（C-4b，未接入）**，默认关；
  攒够样本(_CARD_THRESHOLD) 且手动开(_CARD_STAGE_ENABLED) 才升级。当前仅打一条「待接入」提示。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# ponytail: 校准旋钮——「大变动」按数量变化幅度定，默认 ±30%（物理世界需 tuning）。
_LARGE_PCT = 30.0
_EPS = 1e-6

# 阈值开关（第二段 LLM 经验卡的闸）。默认关：底座先跑，攒够样本再谈沉淀。
# ponytail: 手动开=改这个常量即可；等 C-4b 真要接 LLM 时再接 system_config，别提前造配置。
_CARD_STAGE_ENABLED = False
_CARD_THRESHOLD = 12  # 累计归因事件数，过线才有沉淀价值

_TAG = "推断·非确认"
_TITLE_PREFIX = "归因·"  # list_account_log 无 title 过滤，用前缀数累计事件


def _norm(positions: list[dict], *, qty_key: str, mv_key: str) -> dict[str, dict]:
    """归一成 {ticker: {qty, mv}}。old: ticker/shares/market_value；new: symbol/quantity/market_value_base。"""
    out: dict[str, dict] = {}
    for p in positions or []:
        t = str(p.get("ticker") or p.get("symbol") or "").strip()
        if not t:
            continue
        out[t] = {"qty": float(p.get(qty_key) or 0.0), "mv": float(p.get(mv_key) or 0.0)}
    return out


def detect_position_events(old_positions: list[dict], new_rows: list[dict],
                           *, large_pct: float = _LARGE_PCT) -> list[dict]:
    """确定性 diff：返回平仓/大幅减仓/大幅加仓事件。新建仓不计（无既往建议可归因）。

    每个事件：{ticker, event: closed|reduced|increased, old_qty, new_qty, chg_pct, old_mv, new_mv}。
    纯函数，无副作用。
    """
    old = _norm(old_positions, qty_key="shares", mv_key="market_value")
    new = _norm(new_rows, qty_key="quantity", mv_key="market_value_base")
    events: list[dict] = []
    for t, o in old.items():
        oq = o["qty"]
        if oq <= _EPS:  # 旧本就空仓，无从谈变动
            continue
        n = new.get(t)
        nq = n["qty"] if n else 0.0
        chg = round((nq / oq - 1) * 100, 2)  # oq>_EPS，安全
        if nq <= _EPS:
            ev = "closed"
        elif chg <= -large_pct:
            ev = "reduced"
        elif chg >= large_pct:
            ev = "increased"
        else:
            continue
        events.append({"ticker": t, "event": ev, "old_qty": oq, "new_qty": nq,
                       "chg_pct": chg, "old_mv": o["mv"], "new_mv": n["mv"] if n else 0.0})
    return events


def _should_escalate(event_count: int, *, enabled: bool = _CARD_STAGE_ENABLED,
                     threshold: int = _CARD_THRESHOLD) -> bool:
    """阈值开关：手动开 且 累计样本过线 → 该升级到 LLM 经验卡沉淀（C-4b）。"""
    return bool(enabled) and event_count >= threshold


def _accumulated_count(wl_store, account_ref: str) -> int:
    """该账户累计归因事件数（含本次前）——按 title 前缀数 calibration 日志。"""
    logs = wl_store.list_account_log(account_ref=account_ref, event_type="calibration", limit=500)
    return sum(1 for r in logs if str(r.get("title") or "").startswith(_TITLE_PREFIX))


_LABEL = {"closed": "平仓", "reduced": "大幅减仓", "increased": "大幅加仓"}


def run_attribution(wl_store, account_ref: str, old_positions: list[dict],
                    new_rows: list[dict], *, large_pct: float = _LARGE_PCT) -> dict:
    """结算单导入后调用：检测事件 → 写轻量账户日志（标注推断）。返回统计。

    wl_store 须已 .for_user().for_market()；account_ref 为真实账户（非空）。
    第二段 LLM 归因/经验卡默认不触发，仅在阈值过线时打「待接入」提示。
    """
    events = detect_position_events(old_positions, new_rows, large_pct=large_pct)
    for e in events:
        label = _LABEL.get(e["event"], e["event"])
        wl_store.log_account_event(
            account_ref=account_ref, event_type="calibration",
            title=f"{_TITLE_PREFIX}{label} {e['ticker']}",
            detail=f"{_TAG}：数量 {e['old_qty']:g}→{e['new_qty']:g}（{e['chg_pct']:+.1f}%）",
            severity="info",
            payload={"kind": "attribution", **e, "confirmed": False})

    total = _accumulated_count(wl_store, account_ref)  # 已含刚写入的本批
    escalate = _should_escalate(total)
    if escalate:
        # ponytail: C-4b LLM 归因经验卡挂这里（读近段归因日志→生成卡→store_research.create_experience_card）。
        # 现仅提示，未接入；接入时把 _CARD_STAGE_ENABLED 接 system_config 并在此调用生成。
        logger.info("VIP 归因事件累计 %d 过阈值(%d)，经验卡沉淀待接入(C-4b) acct=%s",
                    total, _CARD_THRESHOLD, account_ref)
    return {"events": len(events),
            "closed": sum(1 for e in events if e["event"] == "closed"),
            "changed": sum(1 for e in events if e["event"] != "closed"),
            "accumulated": total, "escalated": escalate}


if __name__ == "__main__":
    old = [{"ticker": "NVDA", "shares": 100, "market_value": 10000.0},
           {"ticker": "AMD", "shares": 50, "market_value": 5000.0},
           {"ticker": "TSM", "shares": 200, "market_value": 20000.0}]
    new = [{"symbol": "NVDA", "quantity": 0, "market_value_base": 0.0},      # 平仓
           {"symbol": "AMD", "quantity": 30, "market_value_base": 3000.0},   # -40% 大幅减仓
           {"symbol": "TSM", "quantity": 210, "market_value_base": 21000.0}, # +5% 未过阈值
           {"symbol": "AVGO", "quantity": 10, "market_value_base": 9000.0}]  # 新建仓，不计
    evs = {e["ticker"]: e for e in detect_position_events(old, new)}
    assert evs["NVDA"]["event"] == "closed"
    assert evs["AMD"]["event"] == "reduced" and evs["AMD"]["chg_pct"] == -40.0
    assert "TSM" not in evs and "AVGO" not in evs  # 小变动/新建仓都不产事件
    assert detect_position_events([], new) == []  # 首次导入无旧仓 → 无事件
    # 阈值开关
    assert _should_escalate(20, enabled=False) is False   # 开关关，多少样本都不升级
    assert _should_escalate(5, enabled=True) is False      # 样本不足
    assert _should_escalate(12, enabled=True) is True      # 恰好过线
    print("attribution self-check OK")
