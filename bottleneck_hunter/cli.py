"""CLI entry point for BottleneckHunter."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path

import questionary
import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

from bottleneck_hunter.chain.graph import run_screening
from bottleneck_hunter.chain.hot_sector import HotSectorDetector, HotSectorResult
from bottleneck_hunter.chain.models import MarketRegion
from bottleneck_hunter.chain.report import generate_report

load_dotenv()

app = typer.Typer(
    name="bottleneck-hunter",
    help="AI-powered supply chain bottleneck stock screener",
    no_args_is_help=True,
)
console = Console()
logger = logging.getLogger(__name__)

PRESET_CHAINS = {
    "GPU / AI算力": {"sector": "GPU/AI算力", "product": "GPU"},
    "人形机器人": {"sector": "人形机器人", "product": "人形机器人"},
    "商业航天": {"sector": "商业航天", "product": "商业运载火箭"},
    "新能源车": {"sector": "新能源车", "product": "电动汽车"},
    "自定义输入": None,
}

MARKET_OPTIONS = {
    "A 股": MarketRegion.A_STOCK,
    "美股": MarketRegion.US_STOCK,
    "全部市场": MarketRegion.ALL,
}

ENTRY_MODES = [
    "自动检测热点板块（东方财富实时数据）",
    "手动选择产业链方向",
]


@app.command()
def screen():
    """Interactive industry chain screening."""
    asyncio.run(_screen_async())


@app.command()
def hot():
    """Quick scan: show current hot sectors from East Money."""
    _show_hot_sectors()


@app.command()
def serve(
    port: int = typer.Option(8000, help="Server port"),
    host: str = typer.Option("127.0.0.1", help="Server host"),
):
    """Start the web UI server."""
    import uvicorn

    from bottleneck_hunter.web.app import create_app

    console.print(Panel(
        f"[bold cyan]BottleneckHunter Web UI[/bold cyan]\n"
        f"http://{host}:{port}",
        style="cyan",
    ))
    web_app = create_app()
    uvicorn.run(web_app, host=host, port=port)


def _show_hot_sectors() -> None:
    """Fetch and display hot sector rankings without LLM analysis."""
    console.print(Panel(
        "[bold cyan]BottleneckHunter[/bold cyan] — 热点板块扫描",
        style="cyan",
    ))

    try:
        detector = HotSectorDetector(top_n=20)
        result = detector.detect()
        _display_hot_sectors(result, console)

        # Save to file
        output_dir = Path("output")
        output_dir.mkdir(exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        text = detector.format_result(result)
        path = output_dir / f"hot_sectors_{timestamp}.md"
        path.write_text(text, encoding="utf-8")
        console.print(f"\n[green]已保存: {path}[/green]")

    except Exception as e:
        console.print(f"[red]热点板块检测失败: {e}[/red]")
        logger.exception("Hot sector detection failed")


async def _screen_async():
    console.print(Panel(
        "[bold cyan]BottleneckHunter[/bold cyan]\n"
        "AI 产业链瓶颈选股器 — 三步法：拆解 → 检索 → 验证",
        style="cyan",
    ))

    # --- Entry mode ---
    mode = questionary.select(
        "选择模式:",
        choices=ENTRY_MODES,
    ).ask()
    if not mode:
        return

    sector: str | None = None
    end_product: str | None = None

    if mode == ENTRY_MODES[0]:
        # Auto-detect hot sectors first
        try:
            detector = HotSectorDetector(top_n=15)
            result = detector.detect()
            _display_hot_sectors(result, console)

            # Let user pick from detected sectors
            sector_names = [s.name for s in result.all_ranked[:15]] + ["自定义输入"]
            sector_choice = questionary.select(
                "选择要分析的板块:",
                choices=sector_names,
            ).ask()

            if sector_choice == "自定义输入":
                sector = questionary.text("产业名称:").ask()
                end_product = questionary.text("终端产品:").ask()
            else:
                sector = sector_choice
                end_product = sector_choice

        except Exception as e:
            console.print(f"[yellow]热点板块检测失败: {e}，切换为手动模式[/yellow]")
            mode = ENTRY_MODES[1]

    if mode == ENTRY_MODES[1]:
        # Manual selection
        chain_choice = questionary.select(
            "选择产业链方向:",
            choices=list(PRESET_CHAINS.keys()),
        ).ask()
        if not chain_choice:
            return

        if chain_choice == "自定义输入":
            sector = questionary.text("产业名称:").ask()
            end_product = questionary.text("终端产品:").ask()
            if not sector or not end_product:
                console.print("[red]产业名称和终端产品不能为空[/red]")
                return
        else:
            preset = PRESET_CHAINS[chain_choice]
            sector = preset["sector"]
            end_product = preset["product"]

    if not sector or not end_product:
        return

    # --- Analysis parameters ---
    max_depth_str = questionary.select(
        "产业链拆解深度:",
        choices=["3层", "4层", "5层"],
    ).ask()
    max_depth = int(max_depth_str[0]) if max_depth_str else 3

    top_n_str = questionary.select(
        "返回 Top-N 瓶颈环节:",
        choices=["3", "5", "8", "10"],
    ).ask()
    top_n = int(top_n_str) if top_n_str else 5

    language_str = questionary.select(
        "输出语言:",
        choices=["中文", "English"],
    ).ask()
    language = "zh" if language_str == "中文" else "en"

    # --- Market & supplier settings ---
    market_str = questionary.select(
        "搜索市场:",
        choices=list(MARKET_OPTIONS.keys()),
    ).ask()
    market = MARKET_OPTIONS.get(market_str, MarketRegion.A_STOCK)

    max_cap_str = questionary.text(
        "市值上限（亿元，留空不限）:",
        default="200",
    ).ask()
    max_market_cap_yi = float(max_cap_str) if max_cap_str and max_cap_str.strip() else None

    max_suppliers_str = questionary.text(
        "每个瓶颈环节最大供应商数:",
        default="20",
    ).ask()
    max_suppliers = int(max_suppliers_str) if max_suppliers_str else 20

    # --- LLM setup ---
    from bottleneck_hunter.llm_clients.factory import create_llm

    # 严格按用户隔离：CLI 无 web 登录态，需显式指定用户（其 KEY 存于加密表）。
    import os as _os
    _cli_uid = _os.getenv("BH_CLI_USER_ID", "").strip()
    if not _cli_uid:
        console.print("[red]严格隔离模式：CLI 需设置 BH_CLI_USER_ID 指向你的用户 ID（其 API Key 存于配置中心）。[/red]")
        raise typer.Exit(1)
    from bottleneck_hunter.auth.current_user import set_current_user
    set_current_user(_cli_uid)

    provider = questionary.text(
        "LLM Provider:",
        default="openai",
    ).ask()
    model = questionary.text(
        "Model ID:",
        default="gpt-5.5",
    ).ask()
    deep_llm = create_llm(provider, model)

    # --- Cross-validation models ---
    enable_cv = questionary.confirm(
        "启用多模型交叉验证？",
        default=False,
    ).ask()

    validation_models = []
    if enable_cv:
        cv_model_str = questionary.text(
            "验证模型列表 (provider:model,逗号分隔):\n"
            "  例: openai:gpt-5.4,anthropic:claude-sonnet-4-6,deepseek:deepseek-v4-pro",
            default="",
        ).ask()
        if cv_model_str:
            for item in cv_model_str.split(","):
                item = item.strip()
                if ":" in item:
                    p, m = item.split(":", 1)
                    validation_models.append({"provider": p.strip(), "model": m.strip()})

    # --- Run ---
    console.print(f"\n[bold green]开始分析: {sector} — {end_product}[/bold green]")
    console.print(f"  拆解深度: {max_depth}层 | 瓶颈Top-{top_n} | 市场: {market_str}")
    if validation_models:
        console.print(f"  交叉验证: {len(validation_models)} 个模型")
    console.print("")

    try:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
        ) as progress:
            progress.add_task(description="正在拆解产业链...", total=None)

            result = await run_screening(
                sector=sector,
                end_product=end_product,
                deep_llm=deep_llm,
                max_depth=max_depth,
                top_n=top_n,
                language=language,
                market=market,
                max_market_cap_yi=max_market_cap_yi,
                max_suppliers=max_suppliers,
                validation_models=validation_models or None,
            )

        # --- Display results ---
        _display_results(result, console)

        # --- Save report ---
        report = generate_report(result, language)
        output_dir = Path("output")
        output_dir.mkdir(exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_name = sector.replace("/", "_").replace(" ", "")
        report_path = output_dir / f"{safe_name}_{timestamp}_report.md"
        report_path.write_text(report, encoding="utf-8")

        console.print(f"\n[green]报告已保存: {report_path}[/green]")

    except Exception as e:
        console.print(f"\n[red]分析失败: {e}[/red]")
        logger.exception("Screening failed")


def _display_hot_sectors(result: HotSectorResult, console: Console) -> None:
    """Display hot sector detection results."""

    if result.all_ranked:
        table = Table(title="当前热点板块综合排名", show_lines=True)
        table.add_column("排名", style="bold", width=5)
        table.add_column("板块", width=18)
        table.add_column("类型", width=6)
        table.add_column("涨幅%", width=8)
        table.add_column("资金流入(亿)", width=12)
        table.add_column("换手率%", width=8)
        table.add_column("信号", width=5)
        table.add_column("热度", style="bold green", width=6)

        for i, s in enumerate(result.all_ranked[:15], 1):
            change_str = f"{s.price_change_pct:.2f}" if s.price_change_pct is not None else "-"
            flow_str = f"{s.main_net_inflow:.2f}" if s.main_net_inflow is not None else "-"
            turnover_str = f"{s.turnover_rate:.2f}" if s.turnover_rate is not None else "-"
            type_str = "概念" if s.sector_type == "concept" else "行业"
            table.add_row(
                str(i),
                s.name,
                type_str,
                change_str,
                flow_str,
                turnover_str,
                str(s.signal_count),
                f"[bold]{s.composite_score:.1f}[/bold]",
            )
        console.print(table)

    if result.emerging_themes:
        console.print("\n[bold yellow]新兴题材轮动信号:[/bold yellow]")
        for s in result.emerging_themes[:5]:
            change_str = f" 涨{s.price_change_pct:.2f}%" if s.price_change_pct else ""
            flow_str = f" 资金{s.main_net_inflow:.2f}亿" if s.main_net_inflow else ""
            console.print(f"  - [bold]{s.name}[/bold]{change_str}{flow_str}")


def _display_results(result, console: Console) -> None:
    """Display screening results in rich tables."""

    # Bottleneck ranking table
    if result.bottleneck_reports:
        table = Table(title="瓶颈环节排名", show_lines=True)
        table.add_column("排名", style="bold", width=5)
        table.add_column("环节", width=20)
        table.add_column("层级", width=5)
        table.add_column("稀缺性", width=8)
        table.add_column("不可替代", width=8)
        table.add_column("供需缺口", width=8)
        table.add_column("定价权", width=8)
        table.add_column("技术壁垒", width=8)
        table.add_column("综合", style="bold green", width=8)

        for r in result.bottleneck_reports:
            sm = {s.dimension: s.score for s in r.scores}
            table.add_row(
                str(r.rank),
                r.node_name,
                f"L{r.layer}",
                f"{sm.get('scarcity', 0):.1f}",
                f"{sm.get('irreplaceability', 0):.1f}",
                f"{sm.get('supply_demand_gap', 0):.1f}",
                f"{sm.get('pricing_power', 0):.1f}",
                f"{sm.get('tech_barrier', 0):.1f}",
                f"[bold]{r.overall_score:.1f}[/bold]",
            )
        console.print(table)

    # Supplier scorecards table
    if result.supplier_scorecards:
        console.print("")
        table = Table(title="候选供应商评分", show_lines=True)
        table.add_column("#", style="bold", width=3)
        table.add_column("公司", width=15)
        table.add_column("代码", width=12)
        table.add_column("瓶颈环节", width=15)
        table.add_column("市值", width=10)
        table.add_column("地位", width=5)
        table.add_column("客户", width=5)
        table.add_column("产能", width=5)
        table.add_column("财务", width=5)
        table.add_column("估值", width=5)
        table.add_column("综合", style="bold green", width=6)

        for i, sc in enumerate(result.supplier_scorecards, 1):
            s = sc.supplier
            cap_str = f"{s.market_cap}亿" if s.market_cap else "-"
            table.add_row(
                str(i),
                s.name,
                s.ticker,
                sc.bottleneck_node,
                cap_str,
                str(sc.market_position),
                str(sc.customer_validation),
                str(sc.capacity_status),
                str(sc.financial_health),
                str(sc.valuation),
                f"[bold]{sc.overall_score:.1f}[/bold]",
            )
        console.print(table)

    # Cross-validation summary
    if result.cross_validations:
        console.print("")
        table = Table(title="多模型交叉验证", show_lines=True)
        table.add_column("公司", width=15)
        table.add_column("代码", width=12)
        table.add_column("共识", width=10)
        table.add_column("AI 均分", width=8)
        table.add_column("共识摘要", width=50)

        for cv in result.cross_validations:
            score_icon = "🟢" if cv.avg_score >= 7 else ("🟡" if cv.avg_score >= 5 else "🔴")
            table.add_row(
                cv.supplier_name,
                cv.ticker,
                f"{score_icon} {cv.avg_score:.1f}",
                cv.consensus_reasoning[:50] + "..." if len(cv.consensus_reasoning) > 50 else cv.consensus_reasoning,
            )
        console.print(table)

    # Top picks
    if result.top_picks:
        console.print(f"\n[bold green]最终推荐: {', '.join(result.top_picks)}[/bold green]")


if __name__ == "__main__":
    app()
