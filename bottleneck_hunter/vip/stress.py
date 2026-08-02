"""Phase 4 · 组合级压力测试 + 净 Greeks —— 让「就绪未接线」的 payoff/BS 引擎首次落地生产。

红线(§8.3/§8.6)：衍生品是**呈现敞口**、绝不并回头条权益；覆盖口径必须披露。故本文件：
- 股票：线性 delta 冲击（shocked_mv = mv × (1+shock)，delta≈1）——精确、无需 BS。
- 累购/累沽/MLI：**重放已验证的 payoff 引擎**在冲击后的现价上（final_price = spot×(1+shock)），
  绝不对路径依赖 exotic 用 BS 臆造。净 Greeks 亦是对同一 payoff 重估做有限差分（bump ±1%）。
- FCN(equity_fcn)：无到期 payoff 模型(仅条款薄记录、无 payoff_fcn) → 诚实标 not_modeled、剔出 P&L、计入未建模覆盖披露。

全部纯函数、无 IO。价源(spot)由调用方(_stress_summary)从 get_latest_snapshot 注入；缺价标的诚实降级。
本文件 __main__ 自检。
"""

from __future__ import annotations

from bottleneck_hunter.vip.derivatives import (
    DerivativeTerm,
    payoff_accumulator,
    payoff_mli_booster,
)

# 可重放定价的衍生品族（有到期 payoff 引擎）；其余(FCN)诚实标未建模、剔出压测 P&L。
_REVALUABLE = {"equity_accumulator", "equity_decumulator", "equity_mli_booster"}
_GREEK_BUMP = 0.01     # 净 Greeks 有限差分现价扰动幅度（±1%）
# 币种口径：非美元腿绝不折进 *_usd 汇总（HKD~7.8×/JPY~150× 虚高），与 portfolio._derivative_notional_usd 同规矩。
# "" = 未知币种，按美元处理（结单多为美元名义；未知不硬拦，与 number_guard._USD_CCY 一致）。
_USD_CCY = {"", "usd", "us$", "$"}


def _deriv_value(term: DerivativeTerm, spot: float) -> float | None:
    """单笔衍生品在给定现价下的**经济盈亏**(USD 口径，相对 AFP/名义的 P&L，静态近似)；
    无 payoff 模型/参数不全/非美元币种 → None(不臆造、不混币)。

    ★关键：累购/累沽是远期式义务，价值相对 AFP 记（payoff 引擎的 **pnl** 字段 = shares×(现价−AFP)），
    绝不用 market_value(=shares×现价) —— 后者把累购跌破 AFP「加倍套牢」的巨亏反转成大额收益(§8.6 死线)。
    累购/累沽：payoff_accumulator.pnl（days_observed=None → 整段 tenor 满敞口近似，压测保守口径，非逐日路径）。
    MLI：payoff_mli_booster.redemption − notional（相对投入名义的到期盈亏，与累购 pnl 口径可通约）。
    非美元腿(HKD/JPY 累购、港币 FCN)：现价为原币种、无 FX 换算 → 返回 None，由调用方剔入 coverage.unmodeled 披露。
    """
    fam = term.product_family
    if fam not in _REVALUABLE or spot <= 0:
        return None
    if (term.currency or "").strip().lower() not in _USD_CCY:
        return None   # 非美元：现价原币种、无 FX → 剔出(不把原币量级写进 *_usd)
    t = term.terms or {}
    if fam in ("equity_accumulator", "equity_decumulator"):
        if not (t.get("afp") and (t.get("daily_shares") or t.get("step_up_daily_shares"))):
            return None   # 缺 AFP/日股数无法折算 → 诚实 None
        r = payoff_accumulator(term, spot)     # 满 tenor 敞口(days_observed=None)，压测保守口径
        return round(float(r.get("pnl") or 0.0), 2)     # 经济 P&L(相对 AFP)，非裸市值
    # MLI：无本金参数无法折算 → None（与 project_derivative_accrual 一致跳过）
    notional = t.get("notional") or 0.0
    if fam == "equity_mli_booster" and notional:
        r = payoff_mli_booster(term, spot, investment_amount=float(notional))
        return round(float(r.get("redemption") or 0.0) - float(notional), 2)   # 相对名义的到期盈亏
    return None


def net_greeks(derivs: list[dict]) -> dict:
    """组合净 Greeks 聚合：对每笔可重放衍生品做现价 ±1% 有限差分，聚合净 delta/gamma(单位:USD/1%现价变动)。

    derivs: [{term:DerivativeTerm, spot:float}]。仅可重放且价齐的计入；FCN/缺价/非美元剔出并计入 coverage。
    delta_usd = (V(+1%)−V(−1%)) / 2，对「现价变动 1%」的经济 P&L 一阶敏感(USD)；
    gamma_usd = V(+1%)−2V(0)+V(−1%)，二阶(凸性)。vega/theta 对 exotic 无解析定义 → 留空(不臆造)。
    净 delta_usd 正=多头方向(现价涨→组合盈)、负=空头。返回带覆盖披露。

    ★天花板(诚实披露)：累购/累沽的股数在 AFP 处 step-up 阶跃不连续；当现价贴近 AFP、±1% 扰动跨越折点时，
    有限差分反映的是「step-up 敞口跃变」而非平滑导数(gamma 可极大)——这是「跌破 AFP 加倍套牢」的**真实**凸性，
    非算法失真，但须提示 near_afp 标的的 Greeks 是阶跃口径。
    """
    total = len(derivs)
    net_d = net_g = 0.0
    priced = 0
    unmodeled: list[str] = []
    near_afp: list[str] = []   # ±1% 跨越 AFP 折点的标的：Greeks 为 step-up 阶跃口径，非平滑导数
    for d in derivs:
        term, spot = d.get("term"), float(d.get("spot") or 0.0)
        if not isinstance(term, DerivativeTerm) or spot <= 0:
            unmodeled.append(getattr(term, "underlying_symbol", "?") or "?")
            continue
        v0 = _deriv_value(term, spot)
        vu = _deriv_value(term, spot * (1 + _GREEK_BUMP))
        vd = _deriv_value(term, spot * (1 - _GREEK_BUMP))
        if v0 is None or vu is None or vd is None:
            unmodeled.append(term.underlying_symbol or "?")
            continue
        afp = (term.terms or {}).get("afp") or 0.0
        if afp and spot * (1 - _GREEK_BUMP) < afp <= spot * (1 + _GREEK_BUMP):
            near_afp.append(term.underlying_symbol or "?")
        net_d += (vu - vd) / 2.0
        net_g += vu - 2.0 * v0 + vd
        priced += 1
    basis = "有限差分(±1%现价)重放 payoff 引擎的经济 P&L；FCN/缺价/非美元剔出。exotic 无解析 vega/theta，未列。"
    if near_afp:
        _syms = "/".join(sorted(set(near_afp)))
        basis += f" ⚠ {_syms} 现价贴近 AFP，±1% 跨 step-up 折点，Greeks 为阶跃口径非平滑导数。"
    return {
        "net_delta_usd": round(net_d, 2),      # USD / 1% 现价变动
        "net_gamma_usd": round(net_g, 2),      # 凸性(USD)
        "coverage": {"priced": priced, "total": total, "unmodeled": sorted(set(unmodeled)),
                     "near_afp_step": sorted(set(near_afp))},
        "basis": basis,
    }


def stress_test(stock_mv_total: float, derivs: list[dict], scenarios: list[dict]) -> dict:
    """组合级压力测试：各情景下的组合 P&L = 股票线性 delta + 衍生品 payoff 重放增量。

    stock_mv_total: 组合股票总市值(USD)——线性 delta 冲击(shocked = mv×(1+market_shock))。
    derivs: [{term:DerivativeTerm, spot:float}]（同 net_greeks）。
    scenarios: [{name, market_shock}]（market_shock 小数，如 -0.2=市场跌20%）。
    衍生品增量 = V(冲击后现价) − V(基准现价)（V=经济 P&L，相对 AFP/名义）；不可重放/缺价/非美元的剔出并计入
    未建模披露(不把 exotic 的尾部风险伪装成0)。★累购跌破 AFP「加倍套牢」如实呈现为更大损失(用 pnl 非裸市值)。
    返回 {scenarios:[{name, market_shock_pct, stock_pnl, deriv_pnl, total_pnl}], derivative_coverage, basis}。
    """
    # 衍生品基准价值(基准现价)——仅可重放的算基准，未建模的统一剔出
    base_vals: dict[int, float] = {}
    unmodeled: list[str] = []
    for i, d in enumerate(derivs):
        term, spot = d.get("term"), float(d.get("spot") or 0.0)
        v = _deriv_value(term, spot) if isinstance(term, DerivativeTerm) and spot > 0 else None
        if v is None:
            unmodeled.append(getattr(term, "underlying_symbol", "?") or "?")
            continue
        base_vals[i] = v

    rows = []
    for sc in scenarios:
        shock = float(sc.get("market_shock") or 0.0)
        stock_pnl = round(stock_mv_total * shock, 2)
        deriv_pnl = 0.0
        for i, base in base_vals.items():
            spot = float(derivs[i].get("spot") or 0.0)
            v_sh = _deriv_value(derivs[i]["term"], spot * (1 + shock))
            if v_sh is not None:
                deriv_pnl += v_sh - base
        deriv_pnl = round(deriv_pnl, 2)
        rows.append({"name": sc.get("name", ""), "market_shock_pct": round(shock * 100, 1),
                     "stock_pnl": stock_pnl, "deriv_pnl": deriv_pnl,
                     "total_pnl": round(stock_pnl + deriv_pnl, 2)})
    return {
        "scenarios": rows,
        "derivative_coverage": {"priced": len(base_vals), "total": len(derivs),
                                "unmodeled": sorted(set(unmodeled))},
        "basis": "股票线性delta + 衍生品经济P&L(pnl,相对AFP/名义)重放增量；FCN/缺价/非美元剔出并披露。静态近似。",
    }


if __name__ == "__main__":
    # 累购：AFP 100、日 10 股、365 天满敞口。_deriv_value 返回**经济 P&L**(pnl=shares×(现价−AFP))，非裸市值。
    acc = DerivativeTerm(product_family="equity_accumulator", underlying_symbol="AAA", currency="USD",
                         tenor_days=365, terms={"afp": 100.0, "daily_shares": 10, "step_up_daily_shares": 20})
    assert _deriv_value(acc, 120.0) == round(365 * 10 * (120.0 - 100.0), 2)   # 现价≥AFP，赚(20/股)
    # ★核心红线守卫：现价 80<AFP → step_up 20 股「加倍套牢」，P&L=365×20×(80−100)=−146000 应为**负**(巨亏)
    assert _deriv_value(acc, 80.0) == round(365 * 20 * (80.0 - 100.0), 2) < 0

    # 累沽：AFP 100，现价 120（踏空）→ 被迫按 100 卖、市价 120 → pnl=shares×(afp−spot)<0 应为负
    dec = DerivativeTerm(product_family="equity_decumulator", underlying_symbol="DDD", currency="USD",
                         tenor_days=365, terms={"afp": 100.0, "daily_shares": 10, "step_up_daily_shares": 20})
    assert _deriv_value(dec, 120.0) < 0     # 累沽上涨踏空受损，方向正确

    # FCN 无 payoff 模型 → None（诚实剔出，不 BS 臆造）
    fcn = DerivativeTerm(product_family="equity_fcn", underlying_symbol="BBB", currency="USD",
                         tenor_days=365, terms={"strike": 90.0, "afp": 90.0, "knock_out_price": 105.0})
    assert _deriv_value(fcn, 100.0) is None

    # 非美元累购 → None（现价原币种、无 FX，剔出不混入 *_usd）
    hk = DerivativeTerm(product_family="equity_accumulator", underlying_symbol="0700.HK", currency="HKD",
                        tenor_days=365, terms={"afp": 100.0, "daily_shares": 10, "step_up_daily_shares": 20})
    assert _deriv_value(hk, 400.0) is None

    # 压测：纯股票 100k、市场 -20% → 股票 P&L -20k；无衍生品
    st = stress_test(100000.0, [], [{"name": "股灾", "market_shock": -0.20}])
    assert st["scenarios"][0]["total_pnl"] == -20000.0, st
    assert st["scenarios"][0]["stock_pnl"] == -20000.0 and st["scenarios"][0]["deriv_pnl"] == 0.0

    # ★压测含累购：市场 -20% → 现价 100→80 跌破 AFP、step-up 加倍套牢 → deriv_pnl 必须为**负**(尾部损失如实，不反转)
    st2 = stress_test(0.0, [{"term": acc, "spot": 100.0}], [{"name": "跌", "market_shock": -0.20}])
    # base pnl(100)=365×10×0=0；shocked pnl(80)=365×20×(80−100)=−146000 → deriv_pnl=−146000<0
    assert st2["scenarios"][0]["stock_pnl"] == 0.0
    assert st2["scenarios"][0]["deriv_pnl"] < 0, st2      # 股灾中累购呈现为损失、非收益(§8.6 死线)
    assert st2["derivative_coverage"]["priced"] == 1

    # 净 Greeks：累购在现价 120（远离 AFP，同 regime）→ net_delta_usd>0（多头方向，现价涨→盈）
    ng = net_greeks([{"term": acc, "spot": 120.0}])
    assert ng["coverage"]["priced"] == 1 and ng["net_delta_usd"] > 0, ng
    assert not ng["coverage"]["near_afp_step"]             # 远离 AFP，非阶跃口径
    # 现价贴近 AFP（100）→ ±1% 跨 step-up 折点 → near_afp_step 披露该标的（阶跃口径非平滑导数）
    ng_afp = net_greeks([{"term": acc, "spot": 100.0}])
    assert "AAA" in ng_afp["coverage"]["near_afp_step"], ng_afp
    # FCN 计入未建模、不污染净 Greeks
    ng2 = net_greeks([{"term": acc, "spot": 120.0}, {"term": fcn, "spot": 100.0}])
    assert ng2["coverage"]["priced"] == 1 and "BBB" in ng2["coverage"]["unmodeled"], ng2

    print("vip.stress self-check OK")
