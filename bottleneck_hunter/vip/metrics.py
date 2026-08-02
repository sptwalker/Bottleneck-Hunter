"""Phase 3 · VIP 收益率 / 风险调整 / 贡献归因 —— 稀疏结算单期末点上的诚实近似。

红线(§8.1/8.2)：数据是周期性稀疏期末点、非逐日 NAV，故：
- 收益率：只能是 Modified Dietz 近似(已剔外部现金流)，链接子期≈TWR；必须显式标「基于 N 期结单·非逐日」。
- 风险调整：Sharpe/Sortino/Calmar 按「实际期均跨度」年化(非假定 252 交易日)，样本极少→指示性趋势，
            <3 个期收益直接不算(None)，绝不用 1~2 个点冒充统计量。
- 贡献归因：相邻两期价格收益 × 期初权重，用 mv/qty 还原单价、剔买卖交割污染；两期缺一则跳过并披露覆盖面。

全部纯函数、无副作用、无 IO；本文件 __main__ 自检。口径标注由调用方 (_perf_summary) 附加。
"""

from __future__ import annotations

import math
from datetime import date

RISK_FREE_ANNUAL = 0.04
_MIN_RET_POINTS = 3   # 期收益样本下限：<3 不算风险调整(统计无意义)，诚实降级 None


def _days(d0: str, d1: str) -> int:
    try:
        return (date.fromisoformat(str(d1)[:10]) - date.fromisoformat(str(d0)[:10])).days
    except (ValueError, TypeError):
        return 0


_MIN_DENOM_FRAC = 0.05      # 分母须 ≥ 5%×|期初|，否则近零/翻负→退化(诚实降级 None)
_MIN_BASE_ABS = 1.0         # 期初本金绝对下限：<1(货币单位)视同空账户，收益率无意义→None(挡近零期初爆表)


def modified_dietz(v_begin: float, v_end: float, flows: list[dict]) -> float | None:
    """单期 Modified Dietz 收益率(小数)。flows: [{amount:带符号(注入+/提取-), weight:期内在场权重∈[0,1]}]。

    R = (期末 − 期初 − Σ净流) / (期初 + Σ(权重×净流))。分母(平均投入本金)≤0 或近零 → None(不可定义)。
    退化守卫(红线 §8.2)：①分母≤0(本金非正,产符号翻转伪收益)；②|分母|<5%×|期初|(近零,产爆表)；
    ③期初本金<1 货币单位(空账户建仓/深度回撤清零,任何跳变都产天文伪收益)——三者一律诚实降级 None。
    """
    net_flow = sum(f["amount"] for f in flows)
    weighted = sum(f["weight"] * f["amount"] for f in flows)
    denom = v_begin + weighted
    if abs(v_begin) < _MIN_BASE_ABS or denom <= 0 or abs(denom) < _MIN_DENOM_FRAC * abs(v_begin):
        return None
    return (v_end - v_begin - net_flow) / denom


def linked_modified_dietz(series: list[dict], flows: list[dict]) -> dict:
    """相邻期末点两两算 Modified Dietz 子期收益，几何链接≈TWR(剔外部现金流)。

    series: 升序 [{as_of_date, total_equity}]（调用方须先剔推算点 is_projected）。
    flows:  外部现金流 [{date, amount(带符号:注入+/提取-)}]，按落在各子期 (d0, d1] 归桶+按日加权。
    返回 {cumulative_pct, annualized_pct, mwr_pct, period_returns:[{period,pct}], n_periods, coverage}。
    coverage = 有效子期数/总子期数(分母为 0 的退化子期被跳过并计入披露)。
    """
    pts = [s for s in series if s.get("as_of_date") and s.get("total_equity") is not None]
    out = {"cumulative_pct": None, "annualized_pct": None, "mwr_pct": None,
           "period_returns": [], "n_periods": 0, "coverage": "0/0"}
    if len(pts) < 2:
        return out
    all_flows = [{"date": str(f["date"])[:10], "amount": float(f["amount"] or 0.0)}
                 for f in (flows or []) if f.get("date")]
    link = 1.0
    valid = 0
    gaps = 0
    for prev, cur in zip(pts, pts[1:], strict=False):
        d0, d1 = prev["as_of_date"], cur["as_of_date"]
        span = _days(d0, d1)
        if span <= 0:
            continue
        gaps += 1
        gap_flows = []
        for f in all_flows:
            if d0 < f["date"] <= d1:
                w = (span - _days(d0, f["date"])) / span   # 期内在场比例(越早注入权重越高)
                gap_flows.append({"amount": f["amount"], "weight": w})
        r = modified_dietz(prev["total_equity"], cur["total_equity"], gap_flows)
        if r is None:
            continue
        valid += 1
        link *= (1.0 + r)
        out["period_returns"].append({"period": d1, "pct": round(r * 100, 2), "span_days": span})
    out["n_periods"] = valid
    out["coverage"] = f"{valid}/{gaps}"
    if valid:
        out["cumulative_pct"] = round((link - 1.0) * 100, 2)
        total_span = _days(pts[0]["as_of_date"], pts[-1]["as_of_date"])
        if total_span >= 30 and link > 0:
            out["annualized_pct"] = round((link ** (365.0 / total_span) - 1.0) * 100, 2)
    # MWR：整段单期 Modified Dietz(把全部外部流按对首末点的权重计一次)——与链接口径互为参照
    total_span = _days(pts[0]["as_of_date"], pts[-1]["as_of_date"])
    if total_span > 0:
        span_flows = [{"amount": f["amount"],
                       "weight": (total_span - _days(pts[0]["as_of_date"], f["date"])) / total_span}
                      for f in all_flows if pts[0]["as_of_date"] < f["date"] <= pts[-1]["as_of_date"]]
        mwr = modified_dietz(pts[0]["total_equity"], pts[-1]["total_equity"], span_flows)
        if mwr is not None:
            out["mwr_pct"] = round(mwr * 100, 2)
    return out


def risk_adjusted(period_returns: list[float], span_days: list[int], mdd_pct: float | None,
                  rf_annual: float = RISK_FREE_ANNUAL) -> dict:
    """稀疏期收益上的 Sharpe/Sortino/Calmar，按「实际期均跨度」年化(非假定 252 交易日)。

    period_returns: 各子期收益(小数)。span_days: 各子期天数(与 period_returns 同序，用于年化频率)。
    mdd_pct: 最大回撤(负值%，来自 _perf_summary 稀疏近似)。<3 个期收益 → 全 None(样本不足，诚实降级)。
    """
    out = {"sharpe": None, "sortino": None, "calmar": None, "n_returns": len(period_returns),
           "note": ""}
    if len(period_returns) < _MIN_RET_POINTS:
        out["note"] = f"样本不足(仅 {len(period_returns)} 期收益, 需≥{_MIN_RET_POINTS}), 不算风险调整"
        return out
    avg_span = (sum(span_days) / len(span_days)) if span_days else 0
    if avg_span <= 0:
        out["note"] = "跨度缺失, 无法年化"
        return out
    ppy = 365.0 / avg_span                     # 每年期数(按实际期均跨度)
    rf_per = rf_annual * avg_span / 365.0       # 每期无风险利率
    excess = [r - rf_per for r in period_returns]
    mean_ex = sum(excess) / len(excess)
    var = sum((r - mean_ex) ** 2 for r in excess) / (len(excess) - 1)   # 样本方差(ddof=1)
    std = math.sqrt(var) if var > 0 else 0.0
    if std > 0:
        out["sharpe"] = round(mean_ex / std * math.sqrt(ppy), 2)
    downside = [r for r in excess if r < 0]
    if downside:
        dvar = sum(r ** 2 for r in downside) / (len(excess) - 1)
        dstd = math.sqrt(dvar)
        if dstd > 0:
            out["sortino"] = round(mean_ex / dstd * math.sqrt(ppy), 2)
    # Calmar = 年化收益 / |最大回撤|（几何链接年化）
    link = 1.0
    for r in period_returns:
        link *= (1.0 + r)
    total_days = sum(span_days)
    if total_days >= 30 and link > 0 and mdd_pct not in (None, 0):
        ann = (link ** (365.0 / total_days) - 1.0) * 100
        out["calmar"] = round(ann / abs(mdd_pct), 2)
    return out


def contribution_attribution(prev_holdings: list[dict], cur_holdings: list[dict],
                             prev_total: float) -> list[dict]:
    """标的贡献归因：期初权重 × 单标的价格收益(mv/qty 还原单价，剔买卖交割污染)。

    prev/cur_holdings: [{symbol, quantity, market_value_base}]。prev_total: 期初总权益(权重分母)。
    contribution_pct = 期初权重 × 单价收益；两期缺一/数量为 0/单价不可还原 → 跳过(不臆造)。
    返回按 |贡献| 降序 [{symbol, weight_pct, price_return_pct, contribution_pct}]。
    """
    def _px(h: dict) -> float | None:
        q = float(h.get("quantity") or 0.0)
        mv = float(h.get("market_value_base") or 0.0)
        return (mv / q) if q else None

    prev = {str(h.get("symbol") or "").strip(): h for h in (prev_holdings or []) if h.get("symbol")}
    cur = {str(h.get("symbol") or "").strip(): h for h in (cur_holdings or []) if h.get("symbol")}
    rows = []
    for sym, ph in prev.items():
        ch = cur.get(sym)
        if not ch or not prev_total:
            continue
        px0, px1 = _px(ph), _px(ch)
        if px0 is None or px1 is None or px0 == 0:
            continue
        w0 = float(ph.get("market_value_base") or 0.0) / prev_total
        r = px1 / px0 - 1.0
        rows.append({"symbol": sym, "weight_pct": round(w0 * 100, 2),
                     "price_return_pct": round(r * 100, 2),
                     "contribution_pct": round(w0 * r * 100, 2)})
    rows.sort(key=lambda x: abs(x["contribution_pct"]), reverse=True)
    return rows


if __name__ == "__main__":
    # Modified Dietz 单期：期初 100 期末 150，期中(半程)注入 20 → R = (150-100-20)/(100+0.5*20)=30/110
    r = modified_dietz(100.0, 150.0, [{"amount": 20.0, "weight": 0.5}])
    assert abs(r - 30.0 / 110.0) < 1e-9, r
    # 无现金流退化为简单收益
    assert abs(modified_dietz(100.0, 110.0, []) - 0.10) < 1e-9
    # 分母为 0 → None
    assert modified_dietz(0.0, 10.0, []) is None
    # 负分母(提取×权重 > 期初，平均投入本金<0) → None，不返回符号翻转伪收益
    assert modified_dietz(100.0, 10.0, [{"amount": -120.0, "weight": 0.9}]) is None
    # 近零正分母(期初极小致爆表) → None，不返回 +499900% 伪收益
    assert modified_dietz(0.01, 50.0, []) is None
    # 分母恰在下限之上(6%>5%)仍可算，守卫不误杀正常单期
    assert modified_dietz(100.0, 110.0, [{"amount": -94.0, "weight": 1.0}]) is not None

    # 链接：两期各 +10%(无流) → 累计 21%
    series = [{"as_of_date": "2026-01-31", "total_equity": 100.0},
              {"as_of_date": "2026-02-28", "total_equity": 110.0},
              {"as_of_date": "2026-03-31", "total_equity": 121.0}]
    lk = linked_modified_dietz(series, [])
    assert lk["cumulative_pct"] == 21.0, lk
    assert lk["n_periods"] == 2 and lk["coverage"] == "2/2"
    assert [p["pct"] for p in lk["period_returns"]] == [10.0, 10.0]
    # 期中注资不该被当成收益：期初100→期末120但期初日后注入20 → 真实收益≈0
    s2 = [{"as_of_date": "2026-01-31", "total_equity": 100.0},
          {"as_of_date": "2026-02-28", "total_equity": 120.0}]
    lk2 = linked_modified_dietz(s2, [{"date": "2026-02-01", "amount": 20.0}])
    assert lk2["cumulative_pct"] is not None and abs(lk2["cumulative_pct"]) < 1.0, lk2

    # 风险调整：<3 期 → None + 样本不足
    ra_thin = risk_adjusted([0.1, 0.1], [30, 30], -5.0)
    assert ra_thin["sharpe"] is None and "样本不足" in ra_thin["note"]
    # ≥3 期 → 出数
    ra = risk_adjusted([0.02, -0.01, 0.03, 0.01], [30, 31, 30, 31], -8.0)
    assert ra["sharpe"] is not None and ra["n_returns"] == 4

    # 贡献归因：期初 A 权重高、涨得多 → 贡献最大且排最前
    prev_h = [{"symbol": "A", "quantity": 10, "market_value_base": 800.0},
              {"symbol": "B", "quantity": 10, "market_value_base": 200.0}]
    cur_h = [{"symbol": "A", "quantity": 10, "market_value_base": 960.0},   # 单价 +20%
             {"symbol": "B", "quantity": 10, "market_value_base": 190.0}]   # 单价 -5%
    contrib = contribution_attribution(prev_h, cur_h, 1000.0)
    assert contrib[0]["symbol"] == "A" and abs(contrib[0]["contribution_pct"] - 16.0) < 1e-6, contrib
    # 买卖污染剔除：B 期末买入翻倍(qty 10→20, mv 400)但单价其实 -5% → 贡献按单价算不受买入影响
    cur_b_bought = [{"symbol": "A", "quantity": 10, "market_value_base": 960.0},
                    {"symbol": "B", "quantity": 20, "market_value_base": 380.0}]  # 单价 380/20=19 → -5%
    c2 = {r["symbol"]: r for r in contribution_attribution(prev_h, cur_b_bought, 1000.0)}
    assert abs(c2["B"]["price_return_pct"] - (-5.0)) < 1e-6, c2
    print("vip.metrics self-check OK")
