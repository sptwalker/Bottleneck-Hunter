"""组合层风险预算与约束（P1-4）

在「整本组合」维度上，对 单票 / 行业 / 因子 / 链条 / 流动性 / CVaR / 现金 七类风险预算逐项体检，
每项超限都给出**明确的拒绝或降级**结论（附建议缩仓比例），绝不静默放行。

与既有约束层的分工：
- `constraint_validator.validate_execution_plan` 是**逐笔交易**生成期的硬校验（单票/行业/现金/换手/beta），
  回答「这一笔能不能下」；本模块是**组合已装配后**的整体预算体检，回答「这本组合整体是否超预算」。
- `risk_metrics.compute_portfolio_risk` 只产出 VaR/CVaR/HHI 等**描述性**摘要（warnings，不否决）；
  本模块把其中的 CVaR / 集中度变成**可拒绝/可降级的预算约束**。

纯 stdlib、确定可复现，不引入 numpy/scipy。**诊断层，不接线进生产决策链**（回退=不调用，
现有风险摘要入口 `compute_portfolio_risk` 与逐笔校验 `validate_execution_plan` 完全不变）。
"""

from __future__ import annotations

from dataclasses import dataclass

# 状态严重度排序：拒绝 > 降级 > 通过（取组合最坏项用）
_RANK = {"ok": 0, "degrade": 1, "reject": 2}


@dataclass(frozen=True)
class BudgetLimits:
    """各维度风险预算。单票/行业/现金/因子默认对齐 constraint_validator 的 balanced regime；
    链条/流动性/CVaR 为本层新增维度，默认值是校准旋钮而非硬事实。"""

    max_single_pct: float = 25.0        # 单票权重上限 %
    max_sector_pct: float = 40.0        # 单一行业权重上限 %
    max_chain_pct: float = 35.0         # 单一产业链环节（瓶颈节点）权重上限 %——本系统核心：跨行业但同链的隐性聚集
    max_factor_exposure: float = 1.1    # 任一因子净暴露绝对值上限（含市场 beta，对齐 max_portfolio_beta）
    max_liquidity_days: float = 5.0     # 单票按安全参与率清仓所需天数上限
    max_cvar_pct: float = 8.0           # 组合 CVaR（尾部日损）占权益上限 %
    min_cash_pct: float = 15.0          # 现金下限 %
    participation_rate: float = 0.2     # 流动性口径：每日最多吃掉标的日均成交额（ADV）的比例
    degrade_band: float = 0.15          # 超限但 ≤limit×(1+band) 记「降级」，超出才「拒绝」（现金下限对称按 ×(1−band)）

    @classmethod
    def from_constraints(cls, c: dict, **overrides) -> BudgetLimits:
        """用既有 constraint 字典（如 REGIME_CONSTRAINTS['balanced']）填充对应维度，不 import 生产模块（调用方传字典）。

        市场 beta 视作「市场因子」的暴露上限，映射到 max_factor_exposure；chain/liquidity/cvar 走默认或 overrides。
        """
        base = dict(
            max_single_pct=c.get("max_single_position_pct", cls.max_single_pct),
            max_sector_pct=c.get("max_sector_pct", cls.max_sector_pct),
            min_cash_pct=c.get("min_cash_pct", cls.min_cash_pct),
            max_factor_exposure=c.get("max_portfolio_beta", cls.max_factor_exposure),
        )
        base.update(overrides)
        return cls(**base)


@dataclass(frozen=True)
class BudgetCheck:
    """单维度单主体的预算体检结论。"""

    dimension: str                    # single_name|sector|factor|chain|liquidity|cvar|cash|coverage
    subject: str                      # 具体标的/行业/因子/环节；组合级或覆盖率项为 ""
    usage: float                      # 实际值（% 或 天 或 暴露绝对值）
    limit: float                      # 预算上限（ceiling）或下限（cash floor）
    status: str                       # ok|degrade|reject
    suggested_scale: float | None     # 超限 ceiling 项：缩到合规需乘的比例（<1）；下限/覆盖项为 None
    detail: str                       # 中文说明


@dataclass(frozen=True)
class BudgetReport:
    """整本组合的预算体检报告。"""

    checks: tuple[BudgetCheck, ...]
    status: str                       # 全组合最坏状态

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    @property
    def approved(self) -> bool:
        """非拒绝即放行（degrade 仍放行，但调用方须按 suggested_scale 缩仓降级）。"""
        return self.status != "reject"

    @property
    def breaches(self) -> tuple[BudgetCheck, ...]:
        return tuple(c for c in self.checks if c.status != "ok")

    @property
    def rejected(self) -> tuple[BudgetCheck, ...]:
        return tuple(c for c in self.checks if c.status == "reject")

    @property
    def degraded(self) -> tuple[BudgetCheck, ...]:
        return tuple(c for c in self.checks if c.status == "degrade")


def _ceiling_status(usage: float, limit: float, band: float) -> tuple[str, float | None]:
    """上限型判定：usage≤limit 通过；≤limit×(1+band) 降级；再高拒绝。返回 (status, 建议缩仓比例)。"""
    if limit <= 0:
        # 预算=0 表示禁止该敞口：任何正暴露即拒绝
        return ("reject", 0.0) if usage > 0 else ("ok", None)
    if usage <= limit:
        return ("ok", None)
    scale = round(limit / usage, 4)          # 缩到上限需乘的比例（<1）
    return ("degrade" if usage <= limit * (1 + band) else "reject", scale)


def _floor_status(usage: float, limit: float, band: float) -> str:
    """下限型判定（现金）：usage≥limit 通过；≥limit×(1−band) 降级；再低拒绝。"""
    if limit <= 0 or usage >= limit:
        return "ok"
    return "degrade" if usage >= limit * (1 - band) else "reject"


def check_portfolio_budget(
    positions: list[dict],
    *,
    total_equity: float,
    limits: BudgetLimits | None = None,
    cash: float | None = None,
    factor_exposures: dict[str, float] | None = None,
    portfolio_cvar: float | None = None,
) -> BudgetReport:
    """对整本组合逐维度体检风险预算。

    positions: [{"ticker","market_value","sector"?,"chain_node"?,"adv"?}, ...]
      - market_value 取毛敞口（abs），本系统多头为主；chain_node=产业链环节标签；adv=日均成交额（账户本币）。
    total_equity: 组合总权益（>0，否则无法定义权重——直接抛错，绝不用默认值冒充体检通过）。
    cash: 现金余额（给出才查现金下限）。
    factor_exposures: {因子名: 组合净暴露}（由调用方的因子模型算好；本层只查上限，不建因子模型）。
    portfolio_cvar: 组合 CVaR 金额（如 compute_portfolio_risk().cvar_95，账户本币；给出才查 CVaR 预算）。

    只对「有数据」的维度下结论：未提供 cash/factor/cvar 的维度不体检、也不谎报通过；
    单票/行业恒可从权重算，恒体检。流动性维度若部分标的缺 ADV，额外给一条覆盖率降级（缺口不静默放行）。
    """
    if total_equity <= 0:
        raise ValueError("total_equity 必须为正，无法在非正权益上定义组合预算")
    lim = limits or BudgetLimits()
    band = lim.degrade_band
    checks: list[BudgetCheck] = []

    # 毛敞口权重（%）：市值取绝对值，兼容潜在空头的敞口口径
    weights = [(p, abs(p.get("market_value", 0) or 0) / total_equity * 100) for p in positions]

    # 1. 单票
    for p, w in weights:
        st, scale = _ceiling_status(w, lim.max_single_pct, band)
        if st != "ok":
            checks.append(BudgetCheck(
                "single_name", p.get("ticker", ""), round(w, 2), lim.max_single_pct, st, scale,
                f"{p.get('ticker','')} 权重 {w:.1f}% {'超限拒绝' if st == 'reject' else '触限降级'}"
                f"（上限 {lim.max_single_pct:.0f}%）"))

    # 2. 行业（无标签归「未知」桶，与 compute_portfolio_risk 口径一致；大未知桶本身即真实集中度）
    sector_w: dict[str, float] = {}
    for p, w in weights:
        sec = p.get("sector") or "未知"
        sector_w[sec] = sector_w.get(sec, 0.0) + w
    for sec, w in sector_w.items():
        st, scale = _ceiling_status(w, lim.max_sector_pct, band)
        if st != "ok":
            checks.append(BudgetCheck(
                "sector", sec, round(w, 2), lim.max_sector_pct, st, scale,
                f"行业 '{sec}' 合计 {w:.1f}% {'超限拒绝' if st == 'reject' else '触限降级'}"
                f"（上限 {lim.max_sector_pct:.0f}%）"))

    # 3. 链条（产业链环节聚集）：仅对已标注 chain_node 的标的聚类，未标注不参与（避免凭空造簇）
    chain_w: dict[str, float] = {}
    for p, w in weights:
        node = p.get("chain_node")
        if node:
            chain_w[node] = chain_w.get(node, 0.0) + w
    for node, w in chain_w.items():
        st, scale = _ceiling_status(w, lim.max_chain_pct, band)
        if st != "ok":
            checks.append(BudgetCheck(
                "chain", node, round(w, 2), lim.max_chain_pct, st, scale,
                f"产业链环节 '{node}' 合计 {w:.1f}% {'超限拒绝' if st == 'reject' else '触限降级'}"
                f"（上限 {lim.max_chain_pct:.0f}%，跨行业同链的隐性聚集）"))

    # 4. 因子净暴露（含市场 beta）：只查上限，因子模型由调用方负责
    for name, val in (factor_exposures or {}).items():
        st, scale = _ceiling_status(abs(val), lim.max_factor_exposure, band)
        if st != "ok":
            checks.append(BudgetCheck(
                "factor", name, round(abs(val), 4), lim.max_factor_exposure, st, scale,
                f"因子 '{name}' 净暴露 {val:+.2f} {'超限拒绝' if st == 'reject' else '触限降级'}"
                f"（|暴露| 上限 {lim.max_factor_exposure:.2f}）"))

    # 5. 流动性：清仓天数 = 市值 /（ADV×参与率）；缺 ADV 的标的无法评估→覆盖率缺口另记降级
    has_adv = any((p.get("adv") or 0) > 0 for p in positions)
    if has_adv:
        missing = 0
        for p, _w in weights:
            adv = p.get("adv") or 0
            if adv <= 0:
                missing += 1
                continue
            days = abs(p.get("market_value", 0) or 0) / (adv * lim.participation_rate)
            st, scale = _ceiling_status(days, lim.max_liquidity_days, band)
            if st != "ok":
                checks.append(BudgetCheck(
                    "liquidity", p.get("ticker", ""), round(days, 2), lim.max_liquidity_days, st, scale,
                    f"{p.get('ticker','')} 清仓需 {days:.1f} 天 {'超限拒绝' if st == 'reject' else '触限降级'}"
                    f"（上限 {lim.max_liquidity_days:.0f} 天 @ 参与率 {lim.participation_rate:.0%}）"))
        if missing:
            checks.append(BudgetCheck(
                "coverage", "liquidity", float(missing), float(len(positions)), "degrade", None,
                f"{missing}/{len(positions)} 只无 ADV 数据，流动性预算未覆盖，缺口不作通过论"))

    # 6. CVaR 预算（尾部日损占权益）
    if portfolio_cvar is not None:
        cvar_pct = abs(portfolio_cvar) / total_equity * 100
        st, scale = _ceiling_status(cvar_pct, lim.max_cvar_pct, band)
        if st != "ok":
            checks.append(BudgetCheck(
                "cvar", "", round(cvar_pct, 2), lim.max_cvar_pct, st, scale,
                f"组合 CVaR {cvar_pct:.1f}% {'超限拒绝' if st == 'reject' else '触限降级'}"
                f"（尾部日损上限 {lim.max_cvar_pct:.0f}% 权益）"))

    # 7. 现金下限
    if cash is not None:
        cash_pct = cash / total_equity * 100
        st = _floor_status(cash_pct, lim.min_cash_pct, band)
        if st != "ok":
            checks.append(BudgetCheck(
                "cash", "", round(cash_pct, 2), lim.min_cash_pct, st, None,
                f"现金比例 {cash_pct:.1f}% {'跌破下限拒绝' if st == 'reject' else '逼近下限降级'}"
                f"（下限 {lim.min_cash_pct:.0f}%）"))

    overall = "ok"
    for c in checks:
        if _RANK[c.status] > _RANK[overall]:
            overall = c.status
    return BudgetReport(tuple(checks), overall)


if __name__ == "__main__":
    # ponytail: 自检 —— 七维各造一个超限场景，验证拒绝/降级/通过与最坏态聚合
    E = 1_000_000

    # 通过：均衡组合，全维度不超限
    ok = check_portfolio_budget(
        [{"ticker": "A", "market_value": 200_000, "sector": "半导体", "chain_node": "晶圆制造", "adv": 5_000_000},
         {"ticker": "B", "market_value": 150_000, "sector": "医药", "chain_node": "CRO", "adv": 3_000_000}],
        total_equity=E, cash=300_000, factor_exposures={"market": 0.9}, portfolio_cvar=50_000)
    assert ok.ok, ok

    # 单票超限拒绝：40% > 25%×1.15
    r = check_portfolio_budget([{"ticker": "BIG", "market_value": 400_000}], total_equity=E)
    assert r.status == "reject" and r.rejected[0].dimension == "single_name", r
    assert r.rejected[0].suggested_scale == round(25.0 / 40.0, 4), r.rejected[0]

    # 单票触限降级：27% 在 25%~28.75% 之间
    d = check_portfolio_budget([{"ticker": "MID", "market_value": 270_000}], total_equity=E)
    assert d.status == "degrade" and d.degraded[0].dimension == "single_name", d

    # 行业聚集拒绝：三只同业各 20% = 60% > 40%
    sec = check_portfolio_budget(
        [{"ticker": t, "market_value": 200_000, "sector": "半导体"} for t in ("X", "Y", "Z")],
        total_equity=E)
    assert any(c.dimension == "sector" and c.status == "reject" for c in sec.rejected), sec

    # 链条聚集：跨两个行业但同属「先进封装」环节，合计 50% > 35%
    ch = check_portfolio_budget(
        [{"ticker": "P", "market_value": 250_000, "sector": "半导体", "chain_node": "先进封装"},
         {"ticker": "Q", "market_value": 250_000, "sector": "设备", "chain_node": "先进封装"}],
        total_equity=E)
    assert any(c.dimension == "chain" and c.status == "reject" for c in ch.rejected), ch

    # 因子暴露拒绝：market beta 1.5 > 1.1×1.15
    fa = check_portfolio_budget([{"ticker": "H", "market_value": 100_000}],
                                total_equity=E, factor_exposures={"market": 1.5})
    assert any(c.dimension == "factor" and c.status == "reject" for c in fa.rejected), fa

    # 流动性：大仓位小成交额，清仓天数远超 5 天
    lq = check_portfolio_budget([{"ticker": "ILLIQ", "market_value": 100_000, "adv": 50_000}],
                                total_equity=E)
    assert any(c.dimension == "liquidity" and c.status == "reject" for c in lq.rejected), lq

    # 流动性覆盖率缺口：有的有 ADV 有的没有 → 覆盖率降级
    cov = check_portfolio_budget(
        [{"ticker": "L", "market_value": 10_000, "adv": 9_000_000},
         {"ticker": "NOADV", "market_value": 10_000}],
        total_equity=E)
    assert any(c.dimension == "coverage" for c in cov.degraded), cov

    # CVaR 预算拒绝：尾部日损 12% > 8%×1.15
    cv = check_portfolio_budget([{"ticker": "R", "market_value": 100_000}],
                                total_equity=E, portfolio_cvar=120_000)
    assert any(c.dimension == "cvar" and c.status == "reject" for c in cv.rejected), cv

    # 现金下限：现金 5% < 15%×0.85 → 拒绝
    csh = check_portfolio_budget([{"ticker": "S", "market_value": 100_000}],
                                 total_equity=E, cash=50_000)
    assert any(c.dimension == "cash" and c.status == "reject" for c in csh.rejected), csh

    # 现金逼近下限降级：13% 在 12.75%~15% 之间
    csh2 = check_portfolio_budget([{"ticker": "S", "market_value": 100_000}],
                                  total_equity=E, cash=130_000)
    assert any(c.dimension == "cash" and c.status == "degrade" for c in csh2.degraded), csh2

    # 非正权益直接抛错，绝不冒充通过
    try:
        check_portfolio_budget([{"ticker": "A", "market_value": 1}], total_equity=0)
        raise AssertionError("total_equity=0 应抛 ValueError")
    except ValueError:
        pass

    # from_constraints 复用既有 balanced 词汇
    lim = BudgetLimits.from_constraints(
        {"max_single_position_pct": 30.0, "max_sector_pct": 50.0, "min_cash_pct": 10.0, "max_portfolio_beta": 1.3})
    assert lim.max_single_pct == 30.0 and lim.max_factor_exposure == 1.3, lim

    print("portfolio_budget 自检通过：单票/行业/链条/因子/流动性/覆盖率/CVaR/现金 拒绝·降级·通过·抛错")
