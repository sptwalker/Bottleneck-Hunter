"""Phase 5b · VIP 建议复盘闭环（C-3）——把 C-1 打点的 pending 预测「再评即结」成准确率信号。

设计边界（与路线一致）：
- VIP 是 advice-only 参谋，本模块只读共享行情 + 结 model_accuracy 的 VIP 桶，**绝不写 sim_***。
- 隔离唯一干净维度是 role_context=vip_advisor + prediction_type∈{vip_advice,vip_recommend}；
  逐条结算按 (ticker, prediction_type, prediction_date) 匹配，不误结 sim 的 vote 行。
- 无行情则跳过（保持 pending，不硬结）；结算幂等（已结的 is_correct!=-1 不再命中）。
"""

from __future__ import annotations

import logging

from bottleneck_hunter.vip.advisory import VIP_PT_ADVICE, VIP_PT_RECOMMEND, VIP_ROLE_CONTEXT

logger = logging.getLogger(__name__)

# 方向判定阈值（ponytail: 校准旋钮，物理世界需 tuning；默认 ±3%）。
_BAND = 3.0

# 固定持有期（交易日）：评判视野统一，否则「预测日→结算日最新价」会让视野随结算时点膨胀，
# 早出的建议视野长、晚出的视野短，命中率把不同时间窗混为一谈不可比。默认 5 个交易日。
# ponytail: 5 日是校准旋钮，若日后想按周/双周评判改这一处即可。
_HOLD_DAYS = 5

# 动作 → 期望方向：+1 看涨 / -1 看跌 / 0 中性（持有/关注无明确方向）。
# advisory 用 减/持/加，recommend 用 避/关注/建仓。
_DIRECTION = {
    "加仓": 1, "建仓": 1,
    "减仓": -1, "规避": -1,
    "持有": 0, "关注": 0,
}


def _judge(action: str, chg_pct: float, band: float = _BAND) -> bool | None:
    """建议方向对不对？涨/跌 vs 加/减；持有/关注只要没大幅下跌即算合理。未知动作→None（不结）。"""
    want = _DIRECTION.get((action or "").strip())
    if want is None:
        return None
    if want > 0:
        return chg_pct > band
    if want < 0:
        return chg_pct < -band
    # 持有/关注：无明确方向，涨了也是对（持有本就获利），只有大幅下跌（该减仓却没减）才算错。
    return chg_pct >= -band


def _chg_pct(mstore, ticker: str, prediction_date: str, hold_days: int = _HOLD_DAYS) -> float | None:
    """预测日→持有 hold_days 个交易日后的涨跌幅%。持有期未满或缺价→None（保持 pending，不硬结）。"""
    rows = mstore.get_snapshots(ticker, days=400)
    closes = sorted((r["date"], r["close"]) for r in rows if r.get("close"))  # ASC by date
    # 预测日基准：升序里第一条不早于预测日的
    base_idx = next((i for i, (d, _) in enumerate(closes) if d >= prediction_date), None)
    if base_idx is None:
        return None
    exit_idx = base_idx + hold_days
    if exit_idx >= len(closes):
        return None  # 持有期未满，保持 pending 待下轮再评
    base = closes[base_idx][1]
    if not base:
        return None
    return round((closes[exit_idx][1] / base - 1) * 100, 2)


def review_pending_advice(mstore, *, band: float = _BAND) -> dict:
    """结算当前市场下 VIP 的 pending 建议预测。返回 {reviewed, correct, skipped, no_price}。
    mstore 须已 .for_user().for_market()。同步纯逻辑，无 LLM 调用。"""
    pending = mstore.list_pending_predictions(
        role_context=VIP_ROLE_CONTEXT,
        prediction_types=[VIP_PT_ADVICE, VIP_PT_RECOMMEND],
        market=getattr(mstore, "_market", "") or "",
    )
    stats = {"reviewed": 0, "correct": 0, "skipped": 0, "no_price": 0}
    for row in pending:
        ticker = row["ticker"]
        pt = row["prediction_type"]
        pdate = row["prediction_date"]
        action = row.get("prediction_value", "")
        chg = _chg_pct(mstore, ticker, pdate)
        if chg is None:
            stats["no_price"] += 1
            continue
        ok = _judge(action, chg, band)
        if ok is None:
            stats["skipped"] += 1
            continue
        # 二值编码：对→score_delta=0(is_correct=1)，错→5(is_correct=0)。按预测日逐条结。
        mstore.record_outcome(
            ticker, pt, outcome_value=f"chg={chg:+.1f}%",
            score_delta=0.0 if ok else 5.0, prediction_date=pdate)
        stats["reviewed"] += 1
        if ok:
            stats["correct"] += 1
    return stats


def _parse_chg(outcome_value: str) -> float | None:
    """从 'chg=+5.0%' 抠出 5.0；解析不出→None（缺价/异常条目诚实留空，不臆造 0）。"""
    s = (outcome_value or "").strip()
    if not s.startswith("chg="):
        return None
    try:
        return float(s[4:].rstrip("%"))
    except ValueError:
        return None


def build_review_ledger(mstore, *, limit: int = 200) -> dict:
    """特性三 Phase1 · 面向用户的复盘对错台账（只读呈现，不结算不写库）。

    五列直读 model_accuracy 的 settled 孪生（无 join vip_advisory）：逐条 建议动作/实际涨跌/
    对错徽标；命中率 KPI 直接调 get_model_accuracy_stats 取 vip_advisor 桶（勿在此重算，口径唯一）。
    mstore 须已 .for_user().for_market()。
    """
    rows = mstore.list_settled_predictions(
        role_context=VIP_ROLE_CONTEXT,
        prediction_types=[VIP_PT_ADVICE, VIP_PT_RECOMMEND],
        market=getattr(mstore, "_market", "") or "",
        limit=limit,
    )
    ledger = [{
        "date": r.get("prediction_date"),
        "ticker": r.get("ticker"),
        "action": r.get("prediction_value", ""),
        "kind": "荐新" if r.get("prediction_type") == VIP_PT_RECOMMEND else "持仓建议",
        "chg_pct": _parse_chg(r.get("outcome_value", "")),
        "correct": None if r.get("is_correct", -1) == -1 else bool(r.get("is_correct") == 1),
    } for r in rows]
    # 命中率 KPI：取 vip_advisor 桶汇总（跨模型合并 total/correct，与逐条明细同一数据源）
    total = correct = pending = 0
    for s in mstore.get_model_accuracy_stats(market=getattr(mstore, "_market", "") or ""):
        if s.get("role_context") == VIP_ROLE_CONTEXT:
            total += s.get("total", 0) or 0
            correct += s.get("correct", 0) or 0
            pending += s.get("pending", 0) or 0
    settled = total - pending
    return {
        "ledger": ledger,
        "kpi": {
            "settled": settled,
            "correct": correct,
            "pending": pending,
            "hit_rate_pct": round(correct / settled * 100, 1) if settled else None,
        },
    }


if __name__ == "__main__":
    # 隔离守卫：VIP 桶绝不撞 sim 的 vote / committee_*
    assert VIP_PT_ADVICE != "vote" and VIP_PT_RECOMMEND != "vote"
    assert not VIP_ROLE_CONTEXT.startswith("committee_")

    # _judge：方向 × 涨/跌，加/减仓要求踩对方向
    assert _judge("加仓", 5.0) is True and _judge("加仓", -5.0) is False
    assert _judge("建仓", 5.0) is True and _judge("建仓", 1.0) is False
    assert _judge("减仓", -5.0) is True and _judge("减仓", 5.0) is False
    assert _judge("规避", -5.0) is True and _judge("规避", 0.0) is False
    # 持有/关注：涨了也对（本就获利），只有大幅下跌才算错——不再苛求横盘
    assert _judge("持有", 5.0) is True and _judge("持有", 1.0) is True
    assert _judge("持有", -5.0) is False and _judge("持有", -1.0) is True
    assert _judge("关注", 8.0) is True and _judge("关注", -8.0) is False
    assert _judge("横盘乱写", 5.0) is None  # 未知动作不结
    # band 边界：恰好 -band 仍算「没大幅下跌」→ 持有对；加仓恰好 +band 不算涨透→错
    assert _judge("持有", -3.0) is True and _judge("加仓", 3.0) is False

    # _chg_pct 固定持有期：预测日 +5 交易日的涨跌，持有期未满→None
    class _MS:
        def __init__(self, rows): self._rows = rows
        def get_snapshots(self, t, days=400): return self._rows
    # 8 天序列，预测日=d0，第 5 交易日(d5) close=110 → +10%
    days = [{"date": f"2026-07-{d:02d}", "close": c}
            for d, c in [(1, 100), (2, 101), (3, 102), (4, 103), (5, 104), (6, 110), (7, 111), (8, 112)]]
    assert _chg_pct(_MS(days), "X", "2026-07-01") == 10.0  # d0=100 → d5=110
    # 持有期未满：预测日在倒数第 3 天，凑不满 5 个交易日 → None（保持 pending）
    assert _chg_pct(_MS(days), "X", "2026-07-06") is None

    # 二值编码语义自检（与 record_outcome 内 is_correct = 1 if abs(score_delta)<2.0 else 0 对齐）
    assert abs(0.0) < 2.0       # 对 → score_delta=0 → is_correct=1
    assert abs(5.0) >= 2.0      # 错 → score_delta=5 → is_correct=0

    # _parse_chg：正常/缺前缀/垃圾
    assert _parse_chg("chg=+5.0%") == 5.0 and _parse_chg("chg=-2.3%") == -2.3
    assert _parse_chg("") is None and _parse_chg("n/a") is None

    # build_review_ledger stitch：喂 fake mstore（settled 明细 + stats 桶）→ 台账逐行 + 命中率
    class _FakeStore:
        _market = "us_stock"
        def list_settled_predictions(self, **_):
            return [
                {"prediction_date": "2026-07-01", "ticker": "AAPL", "prediction_value": "加仓",
                 "prediction_type": VIP_PT_ADVICE, "outcome_value": "chg=+5.0%", "is_correct": 1},
                {"prediction_date": "2026-07-02", "ticker": "NVDA", "prediction_value": "减仓",
                 "prediction_type": VIP_PT_RECOMMEND, "outcome_value": "chg=+8.0%", "is_correct": 0},
            ]
        def get_model_accuracy_stats(self, **_):
            return [{"role_context": VIP_ROLE_CONTEXT, "total": 3, "correct": 1, "pending": 1},
                    {"role_context": "committee_bull", "total": 99, "correct": 99, "pending": 0}]  # 别桶不得混入
    out = build_review_ledger(_FakeStore())
    assert len(out["ledger"]) == 2
    assert out["ledger"][0] == {"date": "2026-07-01", "ticker": "AAPL", "action": "加仓",
                                "kind": "持仓建议", "chg_pct": 5.0, "correct": True}
    assert out["ledger"][1]["kind"] == "荐新" and out["ledger"][1]["correct"] is False
    # KPI 只吃 vip_advisor 桶：settled=total-pending=2、correct=1、命中率 50%（committee_bull 的 99 不得污染）
    assert out["kpi"] == {"settled": 2, "correct": 1, "pending": 1, "hit_rate_pct": 50.0}

    print("advice_review self-check OK")
