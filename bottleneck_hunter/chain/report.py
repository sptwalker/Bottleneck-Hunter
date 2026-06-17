"""Report generator for screening results."""

from __future__ import annotations

from bottleneck_hunter.chain.models import ScreeningResult


def generate_report(result: ScreeningResult, language: str = "zh") -> str:
    """Generate a markdown screening report."""
    lines: list[str] = []

    if language == "zh":
        _report_zh(result, lines)
    else:
        _report_en(result, lines)

    return "\n".join(lines)


def _report_zh(result: ScreeningResult, lines: list[str]) -> None:
    lines.append(f"# {result.sector} 产业链选股报告\n")

    # --- 1. Chain overview ---
    lines.append("## 1. 产业链全景\n")
    chain = result.chain
    for layer in range(chain.max_depth + 1):
        nodes = chain.get_nodes_at_layer(layer)
        if nodes:
            indent = "  " * layer
            lines.append(f"**第{layer}层**:")
            for n in nodes:
                lines.append(f"{indent}- {n.name}：{n.description}")
            lines.append("")

    # --- 2. Bottleneck ranking ---
    lines.append("## 2. 瓶颈环节排名\n")
    lines.append("| 排名 | 环节 | 层级 | 稀缺性 | 不可替代 | 供需缺口 | 定价权 | 技术壁垒 | 综合得分 |")
    lines.append("|------|------|------|--------|----------|----------|--------|----------|----------|")
    for r in result.bottleneck_reports:
        score_map = {s.dimension: s.score for s in r.scores}
        lines.append(
            f"| {r.rank} | {r.node_name} | L{r.layer} "
            f"| {score_map.get('scarcity', '-'):.1f} "
            f"| {score_map.get('irreplaceability', '-'):.1f} "
            f"| {score_map.get('supply_demand_gap', '-'):.1f} "
            f"| {score_map.get('pricing_power', '-'):.1f} "
            f"| {score_map.get('tech_barrier', '-'):.1f} "
            f"| **{r.overall_score:.1f}** |"
        )
    lines.append("")

    # Detailed bottleneck analysis
    lines.append("## 3. 瓶颈环节详细分析\n")
    for r in result.bottleneck_reports:
        lines.append(f"### {r.rank}. {r.node_name} (综合得分: {r.overall_score:.1f})\n")
        lines.append(f"{r.node_description}\n")
        lines.append("**关键洞察**:")
        for insight in r.key_insights:
            lines.append(f"- {insight}")
        lines.append("\n**风险提示**:")
        for risk in r.risks:
            lines.append(f"- {risk}")
        lines.append("")

    # --- 4. Supplier scorecards ---
    if result.supplier_scorecards:
        lines.append("## 4. 候选供应商评分\n")

        # Summary table
        lines.append("| 排名 | 公司 | 代码 | 对应瓶颈 | 市值 | 市场地位 | 客户验证 | 产能 | 财务 | 估值 | 综合 |")
        lines.append("|------|------|------|----------|------|----------|----------|------|------|------|------|")
        for i, sc in enumerate(result.supplier_scorecards, 1):
            s = sc.supplier
            cap_str = f"{s.market_cap}亿" if s.market_cap else "-"
            lines.append(
                f"| {i} | {s.name} | {s.ticker} | {sc.bottleneck_node} "
                f"| {cap_str} "
                f"| {sc.market_position} "
                f"| {sc.customer_validation} "
                f"| {sc.capacity_status} "
                f"| {sc.financial_health} "
                f"| {sc.valuation} "
                f"| **{sc.overall_score:.1f}** |"
            )
        lines.append("")

        # Detailed per-supplier
        for i, sc in enumerate(result.supplier_scorecards, 1):
            s = sc.supplier
            lines.append(f"### 4.{i} {s.name} ({s.ticker})\n")
            lines.append(f"- **对应瓶颈**: {sc.bottleneck_node}")
            lines.append(f"- **市值**: {s.market_cap}{'亿' if s.market_cap else ''}")
            lines.append(f"- **行业**: {s.sector}")
            lines.append(f"- **PE**: {s.pe_ratio or '-'}")
            lines.append("")
            lines.append(f"| 维度 | 得分 |")
            lines.append(f"|------|------|")
            lines.append(f"| 市场地位 | {sc.market_position}/10 |")
            lines.append(f"| 客户验证 | {sc.customer_validation}/10 |")
            lines.append(f"| 产能状况 | {sc.capacity_status}/10 |")
            lines.append(f"| 财务健康 | {sc.financial_health}/10 |")
            lines.append(f"| 估值水平 | {sc.valuation}/10 |")
            lines.append(f"| **综合** | **{sc.overall_score:.1f}/10** |")
            lines.append("")
            if sc.strengths:
                lines.append("**优势**:")
                for st in sc.strengths:
                    lines.append(f"- {st}")
            if sc.weaknesses:
                lines.append("\n**风险**:")
                for wk in sc.weaknesses:
                    lines.append(f"- {wk}")
            lines.append("")

    # --- 5. Cross-validation ---
    if result.cross_validations:
        lines.append("## 5. 多模型交叉验证\n")

        # Summary table
        lines.append("| 公司 | 代码 | AI 均分 |")
        lines.append("|------|------|---------|")
        for cv in result.cross_validations:
            lines.append(f"| {cv.supplier_name} | {cv.ticker} | {cv.avg_score:.1f}/10 |")
        lines.append("")

        # Detailed per-supplier
        for cv in result.cross_validations:
            lines.append(f"### {cv.supplier_name} ({cv.ticker})\n")
            for v in cv.validations:
                score_icon = "🟢" if v.score >= 7 else ("🟡" if v.score >= 5 else "🔴")
                lines.append(f"- {score_icon} **{v.model_name}** ({v.score:.0f}/10): {v.reasoning}")
                if v.concerns:
                    for c in v.concerns:
                        lines.append(f"  - ⚡ {c}")
            lines.append(f"\n> **共识**: {cv.consensus_reasoning}\n")

    # --- 6. Final recommendations ---
    if result.top_picks:
        lines.append("## 6. 最终推荐\n")
        lines.append("| 优先级 | 代码 | 公司 | 共识 |")
        lines.append("|--------|------|------|------|")

        # Build lookup for cross-validation consensus
        cv_map = {cv.ticker: cv for cv in result.cross_validations}
        sc_map = {sc.supplier.ticker: sc for sc in result.supplier_scorecards}

        for i, ticker in enumerate(result.top_picks, 1):
            name = ""
            consensus_str = "-"
            cv = cv_map.get(ticker)
            if cv:
                name = cv.supplier_name
                consensus_str = f"{cv.avg_score:.1f}/10"
            else:
                sc = sc_map.get(ticker)
                if sc:
                    name = sc.supplier.name
                    consensus_str = f"评分 {sc.overall_score:.1f}/10"
            lines.append(f"| {i} | {ticker} | {name} | {consensus_str} |")
        lines.append("")

    # --- Disclaimer ---
    lines.append("---")
    lines.append("*本报告由 BottleneckHunter AI 生成，仅供参考，不构成投资建议。*")
    lines.append("")
    lines.append("*方法论：Serenity「三步法」— 产业链拆解 → 供应商检索 → 多模型交叉验证*")


def _report_en(result: ScreeningResult, lines: list[str]) -> None:
    lines.append(f"# {result.sector} Supply Chain Screening Report\n")

    # Chain overview
    chain = result.chain
    for layer in range(chain.max_depth + 1):
        nodes = chain.get_nodes_at_layer(layer)
        if nodes:
            lines.append(f"**Layer {layer}**:")
            for n in nodes:
                lines.append(f"- {n.name}: {n.description}")
            lines.append("")

    # Bottleneck ranking
    lines.append("## Bottleneck Ranking\n")
    lines.append("| Rank | Node | Layer | Scarcity | Irreplaceability | Gap | Pricing | Tech | Overall |")
    lines.append("|------|------|-------|----------|------------------|-----|---------|------|---------|")
    for r in result.bottleneck_reports:
        sm = {s.dimension: s.score for s in r.scores}
        lines.append(
            f"| {r.rank} | {r.node_name} | L{r.layer} "
            f"| {sm.get('scarcity', '-'):.1f} "
            f"| {sm.get('irreplaceability', '-'):.1f} "
            f"| {sm.get('supply_demand_gap', '-'):.1f} "
            f"| {sm.get('pricing_power', '-'):.1f} "
            f"| {sm.get('tech_barrier', '-'):.1f} "
            f"| **{r.overall_score:.1f}** |"
        )
    lines.append("")

    # Supplier scorecards
    if result.supplier_scorecards:
        lines.append("## Supplier Scorecards\n")
        lines.append("| # | Company | Ticker | Bottleneck | MktPos | Customer | Capacity | Finance | Valuation | Overall |")
        lines.append("|---|---------|--------|------------|--------|----------|----------|---------|-----------|---------|")
        for i, sc in enumerate(result.supplier_scorecards, 1):
            lines.append(
                f"| {i} | {sc.supplier.name} | {sc.supplier.ticker} | {sc.bottleneck_node} "
                f"| {sc.market_position} | {sc.customer_validation} | {sc.capacity_status} "
                f"| {sc.financial_health} | {sc.valuation} | **{sc.overall_score:.1f}** |"
            )
        lines.append("")

    # Cross-validation
    if result.cross_validations:
        lines.append("## Cross-Validation\n")
        for cv in result.cross_validations:
            lines.append(f"### {cv.supplier_name} ({cv.ticker})\n")
            for v in cv.validations:
                score_icon = "🟢" if v.score >= 7 else ("🟡" if v.score >= 5 else "🔴")
                lines.append(f"- {score_icon} **{v.model_name}** ({v.score:.0f}/10): {v.reasoning}")
            lines.append(f"\n> **Consensus**: {cv.consensus_reasoning}\n")

    # Final picks
    if result.top_picks:
        lines.append("## Top Picks\n")
        for i, ticker in enumerate(result.top_picks, 1):
            lines.append(f"{i}. **{ticker}**")
        lines.append("")

    lines.append("---")
    lines.append("*Generated by BottleneckHunter AI. For reference only, not investment advice.*")
