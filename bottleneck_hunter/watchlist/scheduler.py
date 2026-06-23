"""APScheduler integration for watchlist data pipelines.

Provides scheduled jobs (price update 2x/day, daily scan) and manual triggers.
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

    _scheduler = AsyncIOScheduler(timezone="US/Eastern")

    # 盘前价格更新：8:30 AM ET 周一至周五
    _scheduler.add_job(
        job_price_update, CronTrigger(hour=8, minute=30, day_of_week="mon-fri"),
        id="price_premarket", name="Pre-market price update",
        replace_existing=True,
    )
    # 盘后价格更新：5:00 PM ET 周一至周五
    _scheduler.add_job(
        job_price_update, CronTrigger(hour=17, minute=0, day_of_week="mon-fri"),
        id="price_postmarket", name="Post-market price update",
        replace_existing=True,
    )
    # 每日扫描（新闻/SEC/期权）：7:00 PM ET 周一至周五
    _scheduler.add_job(
        job_daily_scan, CronTrigger(hour=19, minute=0, day_of_week="mon-fri"),
        id="daily_scan", name="Daily news/SEC/options scan",
        replace_existing=True,
    )

    logger.info("Watchlist scheduler configured with 3 jobs")
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

async def job_price_update() -> dict[str, str]:
    """Fetch prices for all watchlist tickers."""
    if not _wl_store:
        return {}
    from bottleneck_hunter.watchlist.price_pipeline import fetch_price_batch

    tickers = _wl_store.get_tickers()
    if not tickers:
        return {}

    logger.info("Price update starting for %d tickers", len(tickers))
    _wl_store.update_pipeline_status("price", last_status="running", stocks_total=len(tickers))

    try:
        results = await fetch_price_batch(tickers, _wl_store)
        ok_count = sum(1 for v in results.values() if v == "ok")
        _wl_store.update_pipeline_status(
            "price",
            last_run_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            last_status="success",
            stocks_processed=ok_count,
            stocks_total=len(tickers),
        )
        logger.info("Price update done: %d/%d succeeded", ok_count, len(tickers))
        return results
    except Exception as e:
        _wl_store.update_pipeline_status("price", last_status="error", last_error=str(e))
        logger.error("Price update failed: %s", e)
        return {}


async def job_daily_scan() -> dict:
    """Daily scan: news + SEC filings + options."""
    if not _wl_store:
        return {}

    tickers = _wl_store.get_tickers()
    if not tickers:
        return {}

    logger.info("Daily scan starting for %d tickers", len(tickers))
    results = {"news": {}, "sec": {}, "options": {}}

    # News
    try:
        _wl_store.update_pipeline_status("news", last_status="running", stocks_total=len(tickers))
        from bottleneck_hunter.watchlist.news_pipeline import fetch_news_batch
        results["news"] = await fetch_news_batch(tickers, _wl_store, budget=_budget)
        _wl_store.update_pipeline_status(
            "news",
            last_run_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            last_status="success",
            stocks_processed=len(results["news"]),
        )
    except Exception as e:
        _wl_store.update_pipeline_status("news", last_status="error", last_error=str(e))
        logger.error("News scan failed: %s", e)

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

    logger.info("Daily scan complete")
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

    if pipeline is None or pipeline == "price":
        yield _sse("step_start", step="price", message="正在更新价格数据...")
        result = await job_price_update()
        ok = sum(1 for v in result.values() if v == "ok")
        yield _sse("step_done", step="price", message=f"价格更新完成: {ok}/{len(result)}")

    if pipeline is None or pipeline == "news":
        yield _sse("step_start", step="news", message="正在抓取新闻...")
        if not _wl_store:
            yield _sse("step_done", step="news", message="Store not initialized")
        else:
            from bottleneck_hunter.watchlist.news_pipeline import fetch_news_batch
            tickers = _wl_store.get_tickers()
            result = await fetch_news_batch(tickers, _wl_store, budget=_budget)
            total = sum(result.values())
            yield _sse("step_done", step="news", message=f"新闻抓取完成: {total} 条")

    if pipeline is None or pipeline == "sec":
        yield _sse("step_start", step="sec", message="正在查询 SEC 文件...")
        if not _wl_store:
            yield _sse("step_done", step="sec", message="Store not initialized")
        else:
            from bottleneck_hunter.watchlist.sec_pipeline import fetch_sec_batch
            tickers = _wl_store.get_tickers()
            result = await fetch_sec_batch(tickers, _wl_store)
            total_filings = sum(v.get("filings", 0) for v in result.values())
            yield _sse("step_done", step="sec", message=f"SEC 查询完成: {total_filings} 份文件")

    if pipeline is None or pipeline == "options":
        yield _sse("step_start", step="options", message="正在分析期权数据...")
        if not _wl_store:
            yield _sse("step_done", step="options", message="Store not initialized")
        else:
            from bottleneck_hunter.watchlist.options_pipeline import fetch_options_batch
            tickers = _wl_store.get_tickers()
            result = await fetch_options_batch(tickers, _wl_store)
            ok = sum(1 for v in result.values() if v == "ok")
            yield _sse("step_done", step="options", message=f"期权分析完成: {ok}/{len(result)}")

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
