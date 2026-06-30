"""质量门控引擎 — 决策链路关键节点的质量检查

借鉴 Anthropic financial-services 的 Portfolio Monitoring 设计模式：
- Green/Yellow/Red 三级偏差阈值
- 数据新鲜度检查
- 论点一致性验证
- 数据源优先级分层
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from bottleneck_hunter.watchlist.store import WatchlistStore

logger = logging.getLogger(__name__)


SOURCE_TIERS = {
    "market_snapshots": {"tier": 1, "label": "实时行情"},
    "sec_filings":      {"tier": 1, "label": "SEC公告"},
    "earnings_reports":  {"tier": 1, "label": "财报数据"},
    "news_digest":      {"tier": 2, "label": "新闻聚合"},
    "options_activity":  {"tier": 2, "label": "期权异动"},
    "scorecard":        {"tier": 3, "label": "产业链评分"},
}

DEFAULT_FRESHNESS_THRESHOLDS = {
    "market_snapshots": {"green": 2, "yellow": 5},
    "news_digest":      {"green": 3, "yellow": 7},
    "sec_filings":      {"green": 30, "yellow": 90},
    "strategy_records":  {"green": 7, "yellow": 14},
    "stock_intelligence": {"green": 3, "yellow": 7},
}

DEFAULT_DEVIATION_THRESHOLDS = {
    "green": 5.0,
    "yellow": 15.0,
}


def _sse(event: str, **data) -> dict:
    return {"event": event, "data": data}


def validate_deviation(
    actual: float, expected: float,
    thresholds: dict | None = None,
) -> str:
    """通用偏差检查，返回 'green' / 'yellow' / 'red'"""
    if expected == 0:
        return "green"
    t = thresholds or DEFAULT_DEVIATION_THRESHOLDS
    pct = abs(actual - expected) / abs(expected) * 100
    if pct <= t["green"]:
        return "green"
    if pct <= t["yellow"]:
        return "yellow"
    return "red"


def validate_data_freshness(
    last_updated: str | None,
    source_type: str,
    thresholds: dict | None = None,
) -> tuple[str, int]:
    """检查数据新鲜度，返回 (color, days_stale)"""
    if not last_updated:
        return "red", 999

    try:
        updated = datetime.fromisoformat(last_updated.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return "red", 999

    now = datetime.now(timezone.utc)
    days = (now - updated).days

    t = (thresholds or DEFAULT_FRESHNESS_THRESHOLDS).get(source_type, {"green": 7, "yellow": 30})
    if days <= t["green"]:
        return "green", days
    if days <= t["yellow"]:
        return "yellow", days
    return "red", days


async def run_quality_checks(
    store: WatchlistStore,
    stage: str,
):
    """在决策链路的关键节点执行质量检查。

    Args:
        stage: 'pre_l2' / 'pre_l3' / 'pre_l4' / 'pre_committee'

    Yields:
        SSE 事件: quality_check_pass / quality_check_warning / quality_check_block
    """
    checks = []
    warnings = []

    entries = store.list_all()
    if not entries:
        yield _sse("quality_check_pass", stage=stage,
                    message=f"[{stage}] 质量检查通过（无标的）")
        return

    # 1. 数据新鲜度检查
    stale_tickers = []
    for entry in entries[:10]:
        ticker = entry["ticker"]
        snapshots = store.get_snapshots(ticker, days=5)
        if snapshots:
            latest_date = snapshots[-1].get("fetched_at", "")
            color, days = validate_data_freshness(latest_date, "market_snapshots")
            if color != "green":
                stale_tickers.append(f"{ticker}({days}天)")
        else:
            stale_tickers.append(f"{ticker}(无数据)")

    if stale_tickers:
        msg = f"数据过期: {', '.join(stale_tickers[:5])}"
        severity = "red" if len(stale_tickers) > len(entries) / 2 else "yellow"
        warnings.append({"type": "data_freshness", "severity": severity, "message": msg})

    # P2.3 上游决策层 staleness 守卫（L1/L2 过期则警告）
    if stage in ("pre_l3", "pre_l4"):
        STALE_DAYS = 7
        now = datetime.now(timezone.utc)
        for layer_name, getter in (("L1宏观", store.get_latest_macro_strategy),
                                    ("L2组合", store.get_latest_strategic_plan)):
            try:
                rec = getter()
            except Exception:
                rec = None
            if not rec:
                warnings.append({"type": "layer_missing", "severity": "yellow",
                                 "message": f"{layer_name}策略缺失，建议先全量刷新"})
                continue
            created = rec.get("created_at") or rec.get("updated_at") or ""
            try:
                dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                age = (now - dt).days
                if age > STALE_DAYS:
                    warnings.append({"type": "layer_stale", "severity": "yellow",
                                     "message": f"{layer_name}策略已 {age} 天未更新，建议用全量刷新 revalidate"})
            except (ValueError, AttributeError):
                pass

    # 2. 论点一致性检查（仅在 pre_l3 和 pre_l4 阶段）
    if stage in ("pre_l3", "pre_l4"):
        inconsistencies = []
        for entry in entries[:10]:
            theses = store.get_theses_for_entry(entry["id"], active_only=True)
            strategy = store.get_latest_strategy(entry["id"])

            for thesis in theses:
                if thesis["status"] in ("weakened", "invalidated"):
                    signal = strategy.get("signal", "neutral") if strategy else "neutral"
                    if signal in ("bullish", "strong_buy"):
                        inconsistencies.append(
                            f"{entry['ticker']}: 论点{thesis['status']}但策略仍{signal}"
                        )

        if inconsistencies:
            warnings.append({
                "type": "thesis_consistency",
                "severity": "yellow",
                "message": f"论点与策略不一致: {'; '.join(inconsistencies[:3])}",
            })

    # 3. 催化剂时效检查
    if stage in ("pre_l3", "pre_l4"):
        expired_count = store.expire_past_catalysts()
        if expired_count > 3:
            warnings.append({
                "type": "catalyst_expiry",
                "severity": "yellow",
                "message": f"{expired_count} 个催化剂刚过期，需关注触发情况",
            })

    # 4. 仓位偏离检查（仅 pre_l4）
    if stage == "pre_l4":
        try:
            account = store.get_sim_account()
            if account:
                positions = store.get_sim_positions()
                total = account.get("total_equity", 0) or account.get("current_capital", 100000)
                if total > 0:
                    for pos in positions:
                        weight = pos.get("market_value", 0) / total * 100
                        if weight > 20:
                            warnings.append({
                                "type": "position_concentration",
                                "severity": "red" if weight > 30 else "yellow",
                                "message": f"{pos.get('ticker', '?')} 仓位 {weight:.1f}% 超过 20% 上限",
                            })
        except Exception as e:
            logger.debug("仓位检查跳过: %s", e)

    # 输出结果
    if not warnings:
        yield _sse("quality_check_pass", stage=stage,
                    checks=len(checks),
                    message=f"[{stage}] 质量检查全部通过")
    else:
        max_severity = "green"
        for w in warnings:
            if w["severity"] == "red":
                max_severity = "red"
                break
            if w["severity"] == "yellow":
                max_severity = "yellow"

        event = "quality_check_warning" if max_severity != "red" else "quality_check_block"
        yield _sse(event, stage=stage,
                    severity=max_severity,
                    warnings=warnings,
                    message=f"[{stage}] {len(warnings)} 项质量预警")

    for w in warnings:
        logger.info("质量门控 [%s] %s: %s", stage, w["severity"].upper(), w["message"])
