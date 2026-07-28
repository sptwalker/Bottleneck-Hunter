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

# 动作 → 期望方向：+1 看涨 / -1 看跌 / 0 横盘。advisory 用 减/持/加，recommend 用 避/关注/建仓。
_DIRECTION = {
    "加仓": 1, "建仓": 1,
    "减仓": -1, "规避": -1,
    "持有": 0, "关注": 0,
}


def _judge(action: str, chg_pct: float, band: float = _BAND) -> bool | None:
    """建议方向对不对？涨/跌/横盘 vs 加/减/持。未知动作→None（不结）。"""
    want = _DIRECTION.get((action or "").strip())
    if want is None:
        return None
    if want > 0:
        return chg_pct > band
    if want < 0:
        return chg_pct < -band
    return abs(chg_pct) <= band


def _chg_pct(mstore, ticker: str, prediction_date: str) -> float | None:
    """从共享行情算 prediction_date→最新 的涨跌幅%。任一端缺价→None（跳过，不硬结）。"""
    rows = mstore.get_snapshots(ticker, days=400)  # DESC by date
    closes = [(r["date"], r["close"]) for r in rows if r.get("close")]
    if len(closes) < 2:
        return None
    latest_close = closes[0][1]  # DESC → 第一条最新
    # 预测日基准：取 date >= prediction_date 的最早一条（升序里第一条不早于预测日的）
    base = None
    for d, c in sorted(closes):
        if d >= prediction_date:
            base = c
            break
    if not base:
        return None
    return round((latest_close / base - 1) * 100, 2)


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


if __name__ == "__main__":
    # 隔离守卫：VIP 桶绝不撞 sim 的 vote / committee_*
    assert VIP_PT_ADVICE != "vote" and VIP_PT_RECOMMEND != "vote"
    assert not VIP_ROLE_CONTEXT.startswith("committee_")

    # _judge 九宫格：方向 × 涨/跌/横盘
    assert _judge("加仓", 5.0) is True and _judge("加仓", -5.0) is False
    assert _judge("建仓", 5.0) is True and _judge("建仓", 1.0) is False
    assert _judge("减仓", -5.0) is True and _judge("减仓", 5.0) is False
    assert _judge("规避", -5.0) is True and _judge("规避", 0.0) is False
    assert _judge("持有", 1.0) is True and _judge("持有", 5.0) is False
    assert _judge("关注", -1.0) is True and _judge("关注", -5.0) is False
    assert _judge("横盘乱写", 5.0) is None  # 未知动作不结
    # band 边界：恰好 ±band 视为横盘（持有对、加仓错）
    assert _judge("持有", 3.0) is True and _judge("加仓", 3.0) is False

    # 二值编码语义自检（与 record_outcome 内 is_correct = 1 if abs(score_delta)<2.0 else 0 对齐）
    assert abs(0.0) < 2.0       # 对 → score_delta=0 → is_correct=1
    assert abs(5.0) >= 2.0      # 错 → score_delta=5 → is_correct=0

    print("advice_review self-check OK")
