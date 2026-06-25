"""APScheduler integration for watchlist data pipelines.

Provides scheduled jobs for dual-market support (US stocks + A-stocks).
Uses AsyncIOScheduler to run inside FastAPI's event loop.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from bottleneck_hunter.watchlist.budget import BudgetTracker
from bottleneck_hunter.watchlist.store import WatchlistStore

logger = logging.getLogger(__name__)

_scheduler = None
_wl_store: WatchlistStore | None = None
_budget: BudgetTracker | None = None


def init_scheduler(store: WatchlistStore) -> object | None:
    """Create and configure scheduler. Returns the scheduler or None if APScheduler not installed."""
    global _scheduler, _wl_store, _budget
    _wl_store = store
    _budget = BudgetTracker(store)

    try:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError:
        logger.warning("APScheduler not installed, scheduler disabled. pip install apscheduler")
        return None

    _scheduler = AsyncIOScheduler(timezone="UTC")

    # === 美股定时任务 (US/Eastern) ===
    # 盘前价格更新：8:30 AM ET = 12:30 UTC (夏令时) / 13:30 UTC (冬令时)
    _scheduler.add_job(
        job_price_update, CronTrigger(hour=12, minute=30, day_of_week="mon-fri"),
        id="us_price_premarket", name="US pre-market price update",
        kwargs={"market": "us_stock"},
        replace_existing=True,
    )
    # 盘后价格更新：5:00 PM ET = 21:00 UTC
    _scheduler.add_job(
        job_price_update, CronTrigger(hour=21, minute=0, day_of_week="mon-fri"),
        id="us_price_postmarket", name="US post-market price update",
        kwargs={"market": "us_stock"},
        replace_existing=True,
    )
    # 每日扫描：7:00 PM ET = 23:00 UTC
    _scheduler.add_job(
        job_daily_scan, CronTrigger(hour=23, minute=0, day_of_week="mon-fri"),
        id="us_daily_scan", name="US daily news/SEC/options scan",
        kwargs={"market": "us_stock"},
        replace_existing=True,
    )

    # === A股定时任务 (Asia/Shanghai, UTC+8) ===
    # 盘前价格更新：9:00 AM CST = 01:00 UTC
    _scheduler.add_job(
        job_price_update, CronTrigger(hour=1, minute=0, day_of_week="mon-fri"),
        id="cn_price_premarket", name="A-stock pre-market price update",
        kwargs={"market": "a_stock"},
        replace_existing=True,
    )
    # 盘后价格更新：16:00 CST = 08:00 UTC
    _scheduler.add_job(
        job_price_update, CronTrigger(hour=8, minute=0, day_of_week="mon-fri"),
        id="cn_price_postmarket", name="A-stock post-market price update",
        kwargs={"market": "a_stock"},
        replace_existing=True,
    )
    # 每日扫描：18:00 CST = 10:00 UTC
    _scheduler.add_job(
        job_daily_scan, CronTrigger(hour=10, minute=0, day_of_week="mon-fri"),
        id="cn_daily_scan", name="A-stock daily news scan",
        kwargs={"market": "a_stock"},
        replace_existing=True,
    )

    logger.info("Watchlist scheduler configured with 6 jobs (US + A-stock)")
    return _scheduler


def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler:
        try:
            _scheduler.shutdown(wait=False)
        except Exception:
            pass
        _scheduler = None


# ---------------------------------------------------------------------------
# Scheduled jobs
# ---------------------------------------------------------------------------

async def job_price_update(market: str = "us_stock") -> dict[str, str]:
    """Fetch prices for watchlist tickers of a specific market."""
    if not _wl_store:
        return {}
    from bottleneck_hunter.watchlist.price_pipeline import fetch_price_batch

    by_market = _wl_store.get_tickers_by_market()
    tickers = by_market.get(market, [])
    if not tickers:
        return {}

    logger.info("Price update (%s) starting for %d tickers", market, len(tickers))
    _wl_store.update_pipeline_status("price", last_status="running", stocks_total=len(tickers))

    try:
        results = await fetch_price_batch(tickers, _wl_store, market=market)
        ok_count = sum(1 for v in results.values() if v == "ok")
        _wl_store.update_pipeline_status(
            "price",
            last_run_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            last_status="success",
            stocks_processed=ok_count,
            stocks_total=len(tickers),
        )
        logger.info("Price update (%s) done: %d/%d succeeded", market, ok_count, len(tickers))
        return results
    except Exception as e:
        _wl_store.update_pipeline_status("price", last_status="error", last_error=str(e))
        logger.error("Price update (%s) failed: %s", market, e)
        return {}


async def job_daily_scan(market: str = "us_stock") -> dict:
    """Daily scan: news (+ SEC/options for US)."""
    if not _wl_store:
        return {}

    by_market = _wl_store.get_tickers_by_market()
    tickers = by_market.get(market, [])
    if not tickers:
        return {}

    logger.info("Daily scan (%s) starting for %d tickers", market, len(tickers))
    results: dict[str, dict] = {"news": {}}

    # News
    try:
        _wl_store.update_pipeline_status("news", last_status="running", stocks_total=len(tickers))
        from bottleneck_hunter.watchlist.news_pipeline import fetch_news_batch
        results["news"] = await fetch_news_batch(tickers, _wl_store, budget=_budget, market=market)
        _wl_store.update_pipeline_status(
            "news",
            last_run_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            last_status="success",
            stocks_processed=len(results["news"]),
        )
    except Exception as e:
        _wl_store.update_pipeline_status("news", last_status="error", last_error=str(e))
        logger.error("News scan (%s) failed: %s", market, e)

    # SEC + Options only for US stocks
    if market == "us_stock":
        # SEC
        try:
            _wl_store.update_pipeline_status("sec", last_status="running", stocks_total=len(tickers))
            from bottleneck_hunter.watchlist.sec_pipeline import fetch_sec_batch
            results["sec"] = await fetch_sec_batch(tickers, _wl_store)
            _wl_store.update_pipeline_status(
                "sec",
                last_run_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                last_status="success",
                stocks_processed=len(results["sec"]),
            )
        except Exception as e:
            _wl_store.update_pipeline_status("sec", last_status="error", last_error=str(e))
            logger.error("SEC scan failed: %s", e)

        # Options
        try:
            _wl_store.update_pipeline_status("options", last_status="running", stocks_total=len(tickers))
            from bottleneck_hunter.watchlist.options_pipeline import fetch_options_batch
            results["options"] = await fetch_options_batch(tickers, _wl_store)
            _wl_store.update_pipeline_status(
                "options",
                last_run_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                last_status="success",
                stocks_processed=len(results["options"]),
            )
        except Exception as e:
            _wl_store.update_pipeline_status("options", last_status="error", last_error=str(e))
            logger.error("Options scan failed: %s", e)

    # A 股公告（对标 SEC EDGAR）
    if market == "a_stock":
        try:
            _wl_store.update_pipeline_status("notice", last_status="running", stocks_total=len(tickers))
            from bottleneck_hunter.watchlist.notice_pipeline import fetch_notice_batch
            results["notice"] = await fetch_notice_batch(tickers, _wl_store)
            _wl_store.update_pipeline_status(
                "notice",
                last_run_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                last_status="success",
                stocks_processed=len(results["notice"]),
            )
        except Exception as e:
            _wl_store.update_pipeline_status("notice", last_status="error", last_error=str(e))
            logger.error("A-stock notice scan failed: %s", e)

    logger.info("Daily scan (%s) complete", market)
    return results


# ---------------------------------------------------------------------------
# Manual triggers
# ---------------------------------------------------------------------------

async def run_manual_refresh(pipeline: str | None = None):
    """Manual trigger for one or all pipelines. Yields SSE events."""
    import json

    def _sse(event: str, **data):
        return {"event": event, "data": json.dumps(data, ensure_ascii=False)}

    yield _sse("refresh_start", pipeline=pipeline or "all")

    if not _wl_store:
        yield _sse("refresh_done", pipeline=pipeline or "all")
        return

    by_market = _wl_store.get_tickers_by_market()

    if pipeline is None or pipeline == "price":
        yield _sse("step_start", step="price", message="正在更新价格数据...")
        total_ok = 0
        total_count = 0
        for market, tickers in by_market.items():
            if not tickers:
                continue
            from bottleneck_hunter.watchlist.price_pipeline import fetch_price_batch
            result = await fetch_price_batch(tickers, _wl_store, market=market)
            total_ok += sum(1 for v in result.values() if v == "ok")
            total_count += len(result)
        _wl_store.update_pipeline_status(
            "price",
            last_run_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            last_status="success",
            stocks_processed=total_ok,
            stocks_total=total_count,
        )
        yield _sse("step_done", step="price", message=f"价格更新完成: {total_ok}/{total_count}")

    if pipeline is None or pipeline == "news":
        yield _sse("step_start", step="news", message="正在抓取新闻...")
        total_news = 0
        for market, tickers in by_market.items():
            if not tickers:
                continue
            from bottleneck_hunter.watchlist.news_pipeline import fetch_news_batch
            result = await fetch_news_batch(tickers, _wl_store, budget=_budget, market=market)
            total_news += sum(result.values())
        yield _sse("step_done", step="news", message=f"新闻抓取完成: {total_news} 条")

    if pipeline is None or pipeline == "sec":
        yield _sse("step_start", step="sec", message="正在查询 SEC 文件...")
        us_tickers = by_market.get("us_stock", [])
        if us_tickers:
            from bottleneck_hunter.watchlist.sec_pipeline import fetch_sec_batch
            result = await fetch_sec_batch(us_tickers, _wl_store)
            total_filings = sum(v.get("filings", 0) for v in result.values())
            yield _sse("step_done", step="sec", message=f"SEC 查询完成: {total_filings} 份文件")
        else:
            yield _sse("step_done", step="sec", message="无美股标的，跳过 SEC")

    if pipeline is None or pipeline == "options":
        yield _sse("step_start", step="options", message="正在分析期权数据...")
        us_tickers = by_market.get("us_stock", [])
        if us_tickers:
            from bottleneck_hunter.watchlist.options_pipeline import fetch_options_batch
            result = await fetch_options_batch(us_tickers, _wl_store)
            ok = sum(1 for v in result.values() if v == "ok")
            yield _sse("step_done", step="options", message=f"期权分析完成: {ok}/{len(result)}")
        else:
            yield _sse("step_done", step="options", message="无美股标的，跳过期权")

    if pipeline is None or pipeline == "notice":
        yield _sse("step_start", step="notice", message="正在查询 A 股公告...")
        cn_tickers = by_market.get("a_stock", [])
        if cn_tickers:
            from bottleneck_hunter.watchlist.notice_pipeline import fetch_notice_batch
            result = await fetch_notice_batch(cn_tickers, _wl_store)
            total_filings = sum(v.get("filings", 0) for v in result.values())
            yield _sse("step_done", step="notice", message=f"A 股公告查询完成: {total_filings} 条")
        else:
            yield _sse("step_done", step="notice", message="无 A 股标的，跳过公告")

    yield _sse("refresh_done", pipeline=pipeline or "all")


# ---------------------------------------------------------------------------
# Status query
# ---------------------------------------------------------------------------

def get_job_statuses() -> list[dict]:
    """Return scheduled job statuses."""
    if not _scheduler:
        return []
    jobs = []
    for job in _scheduler.get_jobs():
        next_run = job.next_run_time
        jobs.append({
            "id": job.id,
            "name": job.name,
            "next_run_at": next_run.isoformat() if next_run else None,
        })
    return jobs
