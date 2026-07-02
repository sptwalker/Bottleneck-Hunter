"""APScheduler integration for watchlist data pipelines.

Provides scheduled jobs for dual-market support (US stocks + A-stocks).
Uses AsyncIOScheduler to run inside FastAPI's event loop.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncGenerator
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from bottleneck_hunter.watchlist.budget import BudgetTracker
from bottleneck_hunter.watchlist.store import WatchlistStore

# 时区常量：APScheduler CronTrigger 使用本地时间 + timezone 参数，自动适应夏令时/冬令时
_TZ_US_EASTERN = ZoneInfo("America/New_York")  # EDT (UTC-4) / EST (UTC-5)
_TZ_CN = ZoneInfo("Asia/Shanghai")              # CST (UTC+8)，无夏令时

logger = logging.getLogger(__name__)

_scheduler = None
_wl_store: WatchlistStore | None = None
_budget: BudgetTracker | None = None
_auth_store = None  # AuthStore 实例，用于获取活跃用户列表


def _get_active_user_stores() -> list[tuple[str, WatchlistStore, BudgetTracker]]:
    """返回所有活跃用户的 (user_id, store, budget) 三元组。

    定时任务遍历该列表，为每个用户独立执行数据管道。
    """
    if not _wl_store:
        return []
    if not _auth_store:
        # 未配置认证时，用全局 store（兼容单用户模式）
        return [("", _wl_store, _budget or BudgetTracker(_wl_store))]
    user_ids = _auth_store.list_active_user_ids()
    if not user_ids:
        return []
    result = []
    for uid in user_ids:
        user_store = _wl_store.for_user(uid)
        user_budget = BudgetTracker(user_store)
        result.append((uid, user_store, user_budget))
    return result


def init_scheduler(store: WatchlistStore, auth_store=None) -> object | None:
    """Create and configure scheduler. Returns the scheduler or None if APScheduler not installed."""
    global _scheduler, _wl_store, _budget, _auth_store
    _wl_store = store
    _budget = BudgetTracker(store)
    _auth_store = auth_store

    try:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError:
        logger.warning("APScheduler not installed, scheduler disabled. pip install apscheduler")
        return None

    _scheduler = AsyncIOScheduler(timezone="UTC")

    # === 美股定时任务 (America/New_York, 自动适应 EDT/EST) ===
    # 盘前数据更新：美东 9:00（开盘前 30 分钟）
    _scheduler.add_job(
        job_price_update, CronTrigger(hour=9, minute=0, day_of_week="mon-fri", timezone=_TZ_US_EASTERN),
        id="us_price_premarket", name="US pre-market price update",
        kwargs={"market": "us_stock"},
        replace_existing=True, max_instances=1, coalesce=True,
    )
    # 盘后数据更新：美东 16:30（收盘后 30 分钟）
    _scheduler.add_job(
        job_price_update, CronTrigger(hour=16, minute=30, day_of_week="mon-fri", timezone=_TZ_US_EASTERN),
        id="us_price_postmarket", name="US post-market price update",
        kwargs={"market": "us_stock"},
        replace_existing=True, max_instances=1, coalesce=True,
    )
    # 日报扫描：美东 18:00
    _scheduler.add_job(
        job_daily_scan, CronTrigger(hour=18, minute=0, day_of_week="mon-fri", timezone=_TZ_US_EASTERN),
        id="us_daily_scan", name="US daily news/SEC/options scan",
        kwargs={"market": "us_stock"},
        replace_existing=True, max_instances=1, coalesce=True,
    )

    # === A股定时任务 (Asia/Shanghai, UTC+8) ===
    # 盘前数据更新：北京时间 9:00
    _scheduler.add_job(
        job_price_update, CronTrigger(hour=9, minute=0, day_of_week="mon-fri", timezone=_TZ_CN),
        id="cn_price_premarket", name="A-stock pre-market price update",
        kwargs={"market": "a_stock"},
        replace_existing=True, max_instances=1, coalesce=True,
    )
    # 盘后数据更新：北京时间 16:00
    _scheduler.add_job(
        job_price_update, CronTrigger(hour=16, minute=0, day_of_week="mon-fri", timezone=_TZ_CN),
        id="cn_price_postmarket", name="A-stock post-market price update",
        kwargs={"market": "a_stock"},
        replace_existing=True, max_instances=1, coalesce=True,
    )
    # 日报扫描：北京时间 18:00
    _scheduler.add_job(
        job_daily_scan, CronTrigger(hour=18, minute=0, day_of_week="mon-fri", timezone=_TZ_CN),
        id="cn_daily_scan", name="A-stock daily news scan",
        kwargs={"market": "a_stock"},
        replace_existing=True, max_instances=1, coalesce=True,
    )

    # === 美股决策自动化任务 (America/New_York) ===
    # 宏观数据更新：美东 18:30（日报扫描后 30 分钟采集 VIX/美债/DXY 等）
    _scheduler.add_job(
        job_macro_update, CronTrigger(hour=18, minute=30, day_of_week="mon-fri", timezone=_TZ_US_EASTERN),
        id="macro_update", name="Macro data update (VIX/Treasury/DXY)",
        replace_existing=True, max_instances=1, coalesce=True,
    )
    # 每日决策：美东 19:00（L1→L3→L4→投委会）
    _scheduler.add_job(
        job_daily_decision, CronTrigger(hour=19, minute=0, day_of_week="mon-fri", timezone=_TZ_US_EASTERN),
        id="us_daily_decision", name="Daily decision (L1→L4+committee)",
        kwargs={"market": "us_stock"},
        replace_existing=True, max_instances=1, coalesce=True,
    )
    # 催化剂扫描：美东 8:00
    _scheduler.add_job(
        job_catalyst_scan, CronTrigger(hour=8, minute=0, day_of_week="mon-fri", timezone=_TZ_US_EASTERN),
        id="us_catalyst_scan", name="Catalyst scan & expiry check",
        replace_existing=True, max_instances=1, coalesce=True,
    )
    # 每周策略刷新：美东周六 10:00（L1 全面生成 + L2 组合策略）
    _scheduler.add_job(
        job_weekly_strategy, CronTrigger(hour=10, minute=0, day_of_week="sat", timezone=_TZ_US_EASTERN),
        id="us_weekly_strategy", name="Weekly macro strategy (L1+L2)",
        replace_existing=True, max_instances=1, coalesce=True,
    )
    # 自动复盘：美东 20:00（批量复盘未复盘的卖出交易）
    _scheduler.add_job(
        job_auto_review, CronTrigger(hour=20, minute=0, day_of_week="mon-fri", timezone=_TZ_US_EASTERN),
        id="us_auto_review", name="Auto review unreviewed sells",
        replace_existing=True, max_instances=1, coalesce=True,
    )

    # === A股决策自动化任务 (Asia/Shanghai) ===
    # 每日决策：北京时间 18:30（cn_daily_scan 后 30 分钟）
    _scheduler.add_job(
        job_daily_decision, CronTrigger(hour=18, minute=30, day_of_week="mon-fri", timezone=_TZ_CN),
        id="cn_daily_decision", name="A-stock daily decision (L1→L4+committee)",
        kwargs={"market": "a_stock"},
        replace_existing=True, max_instances=1, coalesce=True,
    )
    # 催化剂扫描：北京时间 8:00
    _scheduler.add_job(
        job_catalyst_scan, CronTrigger(hour=8, minute=0, day_of_week="mon-fri", timezone=_TZ_CN),
        id="cn_catalyst_scan", name="A-stock catalyst scan & expiry",
        replace_existing=True, max_instances=1, coalesce=True,
    )
    # 每周策略刷新：北京时间周六 10:00
    _scheduler.add_job(
        job_weekly_strategy, CronTrigger(hour=10, minute=0, day_of_week="sat", timezone=_TZ_CN),
        id="cn_weekly_strategy", name="A-stock weekly macro strategy (L1+L2)",
        kwargs={"market": "a_stock"},
        replace_existing=True, max_instances=1, coalesce=True,
    )
    # 自动复盘：北京时间 20:15
    _scheduler.add_job(
        job_auto_review, CronTrigger(hour=20, minute=15, day_of_week="mon-fri", timezone=_TZ_CN),
        id="cn_auto_review", name="A-stock auto review unreviewed sells",
        kwargs={"market": "a_stock"},
        replace_existing=True, max_instances=1, coalesce=True,
    )

    # === 机构持仓 & 分析师评级（每周六美东 11:00，仅美股） ===
    _scheduler.add_job(
        job_institutional_update, CronTrigger(hour=11, minute=0, day_of_week="sat", timezone=_TZ_US_EASTERN),
        id="us_institutional_update", name="Weekly institutional holders & analyst ratings",
        replace_existing=True, max_instances=1, coalesce=True,
    )

    _scheduler.add_job(
        job_model_calibration, CronTrigger(hour=12, minute=0, day_of_week="sun", timezone=_TZ_US_EASTERN),
        id="model_calibration", name="Weekly AI model accuracy calibration",
        replace_existing=True, max_instances=1, coalesce=True,
    )

    logger.info("Watchlist scheduler configured with 17 jobs (6 data + 1 institutional + 1 macro + 8 decision + 1 calibration)")
    return _scheduler


def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler:
        try:
            _scheduler.shutdown(wait=False)
        except Exception:
            logger.warning("Scheduler shutdown error", exc_info=True)
        _scheduler = None


# ---------------------------------------------------------------------------
# Scheduled jobs
# ---------------------------------------------------------------------------

async def job_price_update(market: str = "us_stock") -> dict[str, str]:
    """Fetch prices for watchlist tickers of a specific market (multi-user)."""
    all_results: dict[str, str] = {}
    for uid, store, budget in _get_active_user_stores():
        try:
            from bottleneck_hunter.watchlist.price_pipeline import fetch_price_batch

            by_market = store.get_tickers_by_market()
            tickers = by_market.get(market, [])
            if not tickers:
                continue

            label = f"user={uid[:8]}" if uid else "global"
            logger.info("Price update (%s/%s) starting for %d tickers", market, label, len(tickers))
            store.update_pipeline_status("price", last_status="running", stocks_total=len(tickers))

            results = await fetch_price_batch(tickers, store, market=market)
            ok_count = sum(1 for v in results.values() if v == "ok")
            fail_count = sum(1 for v in results.values() if isinstance(v, str) and v.startswith("error"))
            status = "success" if fail_count == 0 else ("partial" if ok_count > 0 else "error")
            error_msg = f"{fail_count}/{len(results)} tickers failed" if fail_count > 0 else ""
            store.update_pipeline_status(
                "price",
                last_run_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                last_status=status,
                last_error=error_msg,
                stocks_processed=ok_count,
                stocks_total=len(tickers),
            )
            logger.info("Price update (%s/%s) done: %d/%d succeeded", market, label, ok_count, len(tickers))
            all_results.update(results)
        except Exception as e:
            logger.error("Price update (%s/user=%s) failed: %s", market, uid[:8] if uid else "global", e)
    return all_results


async def job_daily_scan(market: str = "us_stock") -> dict:
    """Daily scan: news (+ SEC/options for US), iterates all users."""
    all_results: dict[str, dict] = {}
    for uid, store, budget in _get_active_user_stores():
        try:
            by_market = store.get_tickers_by_market()
            tickers = by_market.get(market, [])
            if not tickers:
                continue

            label = f"user={uid[:8]}" if uid else "global"
            logger.info("Daily scan (%s/%s) starting for %d tickers", market, label, len(tickers))
            results: dict[str, dict] = {"news": {}}

            # News
            try:
                store.update_pipeline_status("news", last_status="running", stocks_total=len(tickers))
                from bottleneck_hunter.watchlist.news_pipeline import fetch_news_batch
                news_results = await fetch_news_batch(tickers, store, budget=budget, market=market)
                results["news"] = news_results
                ok_count = sum(1 for v in news_results.values() if isinstance(v, int) and v >= 0)
                fail_count = sum(1 for v in news_results.values() if isinstance(v, int) and v < 0)
                status = "success" if fail_count == 0 else ("partial" if ok_count > 0 else "error")
                error_msg = f"{fail_count}/{len(news_results)} tickers failed" if fail_count > 0 else ""
                store.update_pipeline_status(
                    "news",
                    last_run_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    last_status=status,
                    last_error=error_msg,
                    stocks_processed=ok_count,
                    stocks_total=len(tickers),
                )
            except Exception as e:
                store.update_pipeline_status("news", last_status="error", last_error=str(e))
                logger.error("News scan (%s/%s) failed: %s", market, label, e)

            # 市场/主题级新闻（供 L1 宏观决策读库；失败不影响个股新闻结果）
            try:
                from bottleneck_hunter.watchlist.news_pipeline import refresh_market_news
                mkt_n = await refresh_market_news(store, market, budget=budget)
                logger.info("Market news (%s/%s): %d 条已落库", market, label, mkt_n)
            except Exception as e:
                logger.warning("Market news scan (%s/%s) failed: %s", market, label, e)

            # SEC + Options only for US stocks
            if market == "us_stock":
                # SEC
                try:
                    store.update_pipeline_status("sec", last_status="running", stocks_total=len(tickers))
                    from bottleneck_hunter.watchlist.sec_pipeline import fetch_sec_batch
                    sec_results = await fetch_sec_batch(tickers, store)
                    results["sec"] = sec_results
                    ok_count = sum(1 for v in sec_results.values() if isinstance(v, dict) and v.get("filings", 0) >= 0)
                    fail_count = len(sec_results) - ok_count
                    status = "success" if fail_count == 0 else ("partial" if ok_count > 0 else "error")
                    error_msg = f"{fail_count}/{len(sec_results)} tickers failed" if fail_count > 0 else ""
                    store.update_pipeline_status(
                        "sec",
                        last_run_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        last_status=status,
                        last_error=error_msg,
                        stocks_processed=ok_count,
                        stocks_total=len(tickers),
                    )
                except Exception as e:
                    store.update_pipeline_status("sec", last_status="error", last_error=str(e))
                    logger.error("SEC scan (%s/%s) failed: %s", market, label, e)

                # Options
                try:
                    store.update_pipeline_status("options", last_status="running", stocks_total=len(tickers))
                    from bottleneck_hunter.watchlist.options_pipeline import fetch_options_batch
                    opt_results = await fetch_options_batch(tickers, store)
                    results["options"] = opt_results
                    ok_count = sum(1 for v in opt_results.values() if v in ("ok", "no_data"))
                    fail_count = sum(1 for v in opt_results.values() if isinstance(v, str) and v.startswith("error"))
                    status = "success" if fail_count == 0 else ("partial" if ok_count > 0 else "error")
                    error_msg = f"{fail_count}/{len(opt_results)} tickers failed" if fail_count > 0 else ""
                    store.update_pipeline_status(
                        "options",
                        last_run_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        last_status=status,
                        last_error=error_msg,
                        stocks_processed=ok_count,
                        stocks_total=len(tickers),
                    )
                except Exception as e:
                    store.update_pipeline_status("options", last_status="error", last_error=str(e))
                    logger.error("Options scan (%s/%s) failed: %s", market, label, e)

            # A 股公告（对标 SEC EDGAR）
            if market == "a_stock":
                try:
                    store.update_pipeline_status("notice", last_status="running", stocks_total=len(tickers))
                    from bottleneck_hunter.watchlist.notice_pipeline import fetch_notice_batch
                    notice_results = await fetch_notice_batch(tickers, store)
                    results["notice"] = notice_results
                    ok_count = sum(1 for v in notice_results.values() if isinstance(v, dict) and v.get("filings", 0) >= 0)
                    fail_count = sum(1 for v in notice_results.values() if isinstance(v, dict) and v.get("filings", 0) < 0)
                    status = "success" if fail_count == 0 else ("partial" if ok_count > 0 else "error")
                    error_msg = f"{fail_count}/{len(notice_results)} tickers failed" if fail_count > 0 else ""
                    store.update_pipeline_status(
                        "notice",
                        last_run_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        last_status=status,
                        last_error=error_msg,
                        stocks_processed=ok_count,
                        stocks_total=len(tickers),
                    )
                except Exception as e:
                    store.update_pipeline_status("notice", last_status="error", last_error=str(e))
                    logger.error("A-stock notice scan (%s) failed: %s", label, e)

            logger.info("Daily scan (%s/%s) complete", market, label)
            all_results.update(results)
        except Exception as e:
            logger.error("Daily scan (%s/user=%s) failed: %s", market, uid[:8] if uid else "global", e)

    return all_results


# ---------------------------------------------------------------------------
# Manual triggers
# ---------------------------------------------------------------------------

async def run_manual_refresh(pipeline: str | None = None, user_store: WatchlistStore | None = None):
    """Manual trigger for one or all pipelines. Yields SSE events.

    如果传入 user_store，则只刷新该用户的数据；否则用全局 store。
    """
    store = user_store or _wl_store
    budget = BudgetTracker(store) if store else None

    def _sse(event: str, **data):
        return {"event": event, "data": json.dumps(data, ensure_ascii=False)}

    yield _sse("refresh_start", pipeline=pipeline or "all")

    if not store:
        yield _sse("refresh_done", pipeline=pipeline or "all")
        return

    by_market = store.get_tickers_by_market()

    if pipeline is None or pipeline == "price":
        yield _sse("step_start", step="price", message="正在更新价格数据...")
        total_ok = 0
        total_count = 0
        for market, tickers in by_market.items():
            if not tickers:
                continue
            from bottleneck_hunter.watchlist.price_pipeline import fetch_price_batch
            result = await fetch_price_batch(tickers, store, market=market)
            total_ok += sum(1 for v in result.values() if v == "ok")
            total_count += len(result)
        store.update_pipeline_status(
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
            result = await fetch_news_batch(tickers, store, budget=budget, market=market)
            total_news += sum(result.values())
        yield _sse("step_done", step="news", message=f"新闻抓取完成: {total_news} 条")

    if pipeline is None or pipeline == "sec":
        yield _sse("step_start", step="sec", message="正在查询 SEC 文件...")
        us_tickers = by_market.get("us_stock", [])
        if us_tickers:
            from bottleneck_hunter.watchlist.sec_pipeline import fetch_sec_batch
            result = await fetch_sec_batch(us_tickers, store)
            total_filings = sum(v.get("filings", 0) for v in result.values())
            yield _sse("step_done", step="sec", message=f"SEC 查询完成: {total_filings} 份文件")
        else:
            yield _sse("step_done", step="sec", message="无美股标的，跳过 SEC")

    if pipeline is None or pipeline == "options":
        yield _sse("step_start", step="options", message="正在分析期权数据...")
        us_tickers = by_market.get("us_stock", [])
        if us_tickers:
            from bottleneck_hunter.watchlist.options_pipeline import fetch_options_batch
            result = await fetch_options_batch(us_tickers, store)
            ok = sum(1 for v in result.values() if v == "ok")
            yield _sse("step_done", step="options", message=f"期权分析完成: {ok}/{len(result)}")
        else:
            yield _sse("step_done", step="options", message="无美股标的，跳过期权")

    if pipeline is None or pipeline == "notice":
        yield _sse("step_start", step="notice", message="正在查询 A 股公告...")
        cn_tickers = by_market.get("a_stock", [])
        if cn_tickers:
            from bottleneck_hunter.watchlist.notice_pipeline import fetch_notice_batch
            result = await fetch_notice_batch(cn_tickers, store)
            total_filings = sum(v.get("filings", 0) for v in result.values())
            yield _sse("step_done", step="notice", message=f"A 股公告查询完成: {total_filings} 条")
        else:
            yield _sse("step_done", step="notice", message="无 A 股标的，跳过公告")

    if pipeline is None or pipeline == "institutional":
        yield _sse("step_start", step="institutional", message="正在获取机构持仓与分析师评级...")
        us_tickers = by_market.get("us_stock", [])
        if us_tickers:
            from bottleneck_hunter.watchlist.institutional_pipeline import (
                fetch_institutional_batch,
                fetch_analyst_batch,
            )
            inst_result = await fetch_institutional_batch(us_tickers, store)
            analyst_result = await fetch_analyst_batch(us_tickers, store)
            ok_inst = sum(1 for v in inst_result.values() if v == "ok")
            ok_analyst = sum(1 for v in analyst_result.values() if v == "ok")
            yield _sse(
                "step_done", step="institutional",
                message=f"机构持仓 {ok_inst}/{len(inst_result)}, 分析师评级 {ok_analyst}/{len(analyst_result)}"
            )
        else:
            yield _sse("step_done", step="institutional", message="无美股标的，跳过机构持仓")

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


# ---------------------------------------------------------------------------
# SSE drain helper (for background scheduled jobs)
# ---------------------------------------------------------------------------

async def _drain_sse(gen: AsyncGenerator) -> None:
    """消费 SSE AsyncGenerator 到底，仅记录关键事件。"""
    async for evt in gen:
        data = evt.get("data", {})
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except (json.JSONDecodeError, TypeError):
                continue
        if isinstance(data, dict):
            event_type = data.get("event", evt.get("event", ""))
            if "error" in event_type:
                logger.warning("Scheduled task SSE error: %s", data)
            elif "done" in event_type:
                logger.info("Scheduled task completed: %s", data.get("message", ""))


# ---------------------------------------------------------------------------
# Decision automation jobs
# ---------------------------------------------------------------------------

async def job_macro_update() -> None:
    """定时采集宏观数据（VIX/美债/DXY/北向资金等），存入 macro_snapshots 表。"""
    try:
        from bottleneck_hunter.watchlist.macro_data import fetch_macro_data
        if not _wl_store:
            return
        store = _wl_store
        by_market = store.get_tickers_by_market()
        markets = list(by_market.keys()) or ["us_stock"]
        result = await fetch_macro_data(store, markets)
        logger.info("Macro update completed: %d indicators fetched", len(result))
    except Exception as e:
        logger.error("Macro update failed: %s", e)


async def job_daily_decision(market: str = "us_stock") -> None:
    """每日自动决策：运行完整 L1→L4→投委会流程（多用户）。"""
    for uid, store, budget in _get_active_user_stores():
        try:
            by_market = store.get_tickers_by_market()
            tickers = by_market.get(market, [])
            if not tickers:
                continue

            label = f"user={uid[:8]}" if uid else "global"
            logger.info("Daily decision (%s/%s) starting for %d tickers", market, label, len(tickers))
            from bottleneck_hunter.watchlist.decision_engine import run_daily_decision
            await _drain_sse(run_daily_decision(store, budget, scope="full", market=market))
            logger.info("Daily decision (%s/%s) completed", market, label)
        except Exception as e:
            logger.error("Daily decision (%s/user=%s) failed: %s", market, uid[:8] if uid else "global", e)


async def job_catalyst_scan() -> None:
    """催化剂扫描：清理过期 + 检测新催化剂（多用户）。"""
    for uid, store, budget in _get_active_user_stores():
        try:
            label = f"user={uid[:8]}" if uid else "global"
            logger.info("Catalyst scan (%s) starting", label)
            from bottleneck_hunter.watchlist.catalyst_monitor import check_catalyst_expiry, detect_catalysts, judge_expired_catalysts
            await _drain_sse(check_catalyst_expiry(store))
            await _drain_sse(judge_expired_catalysts(store))
            await _drain_sse(detect_catalysts(store, budget))
            logger.info("Catalyst scan (%s) completed", label)
        except Exception as e:
            logger.error("Catalyst scan (user=%s) failed: %s", uid[:8] if uid else "global", e)


async def job_weekly_strategy(market: str = "us_stock") -> None:
    """每周策略刷新：L1 宏观策略 + L2 组合策略（多用户）。"""
    for uid, store, budget in _get_active_user_stores():
        try:
            label = f"user={uid[:8]}" if uid else "global"
            logger.info("Weekly strategy refresh (%s/%s) starting", market, label)
            from bottleneck_hunter.watchlist.decision_engine import run_macro_strategy, run_strategic_plan
            await _drain_sse(run_macro_strategy(store, budget, market=market))
            await _drain_sse(run_strategic_plan(store, budget, market=market))
            logger.info("Weekly strategy refresh (%s/%s) completed", market, label)
        except Exception as e:
            logger.error("Weekly strategy refresh (%s/user=%s) failed: %s", market, uid[:8] if uid else "global", e)


async def job_auto_review(market: str = "us_stock") -> None:
    """自动复盘：批量复盘未复盘的卖出交易（多用户）。"""
    for uid, store, budget in _get_active_user_stores():
        try:
            label = f"user={uid[:8]}" if uid else "global"
            market_store = store.for_market(market)
            unreviewed = market_store.get_trades_without_review()
            if not unreviewed:
                logger.debug("Auto review (%s) skipped — no unreviewed trades", label)
                continue
            logger.info("Auto review (%s) starting — %d unreviewed sell trades", label, len(unreviewed))
            from bottleneck_hunter.watchlist.trade_reviewer import run_batch_review
            reviewed_count = 0
            error_count = 0
            async for evt in run_batch_review(store, budget):
                data = evt.get("data", {})
                event_name = data.get("event", "")
                if event_name == "review_done":
                    reviewed_count += 1
                    logger.info("Auto review (%s) — %s 复盘完成 (%d/%d)",
                                label, data.get("ticker", "?"), reviewed_count, len(unreviewed))
                elif event_name == "review_error":
                    error_count += 1
                    logger.warning("Auto review (%s) — %s 复盘失败: %s",
                                   label, data.get("ticker", "?"), data.get("error", "unknown"))
            logger.info("Auto review (%s) completed — reviewed=%d, errors=%d, total=%d",
                        label, reviewed_count, error_count, len(unreviewed))
        except Exception as e:
            logger.error("Auto review (user=%s) failed: %s", uid[:8] if uid else "global", e)

    # P3.1 机会成本扫描（踏空/错误持有），与复盘同周期运行
    for uid, store, budget in _get_active_user_stores():
        try:
            from bottleneck_hunter.watchlist.trade_reviewer import scan_missed_opportunities
            async for evt in scan_missed_opportunities(store, market=market):
                data = evt.get("data", {})
                if data.get("event") == "missed_scan_done":
                    logger.info("机会成本扫描 (user=%s) — 发现 %d 条",
                                uid[:8] if uid else "global", data.get("found", 0))
        except Exception as e:
            logger.debug("机会成本扫描 (user=%s) 失败: %s", uid[:8] if uid else "global", e)


async def job_institutional_update() -> None:
    """每周更新美股机构持仓 & 分析师评级数据（多用户）。"""
    for uid, store, budget in _get_active_user_stores():
        try:
            by_market = store.get_tickers_by_market()
            us_tickers = by_market.get("us_stock", [])
            if not us_tickers:
                continue

            label = f"user={uid[:8]}" if uid else "global"
            logger.info("Institutional update (%s) starting for %d tickers", label, len(us_tickers))
            store.update_pipeline_status("institutional", last_status="running", stocks_total=len(us_tickers))

            from bottleneck_hunter.watchlist.institutional_pipeline import (
                fetch_institutional_batch,
                fetch_analyst_batch,
            )

            inst_results = await fetch_institutional_batch(us_tickers, store)
            analyst_results = await fetch_analyst_batch(us_tickers, store)

            ok_inst = sum(1 for v in inst_results.values() if v == "ok")
            ok_analyst = sum(1 for v in analyst_results.values() if v == "ok")
            total = len(us_tickers)
            fail_count = total - max(ok_inst, ok_analyst)
            status = "success" if fail_count == 0 else ("partial" if (ok_inst + ok_analyst) > 0 else "error")
            error_msg = ""
            if fail_count > 0:
                error_msg = f"inst: {ok_inst}/{total}, analyst: {ok_analyst}/{total}"
            store.update_pipeline_status(
                "institutional",
                last_run_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                last_status=status,
                last_error=error_msg,
                stocks_processed=max(ok_inst, ok_analyst),
                stocks_total=total,
            )
            logger.info(
                "Institutional update (%s) done: inst %d/%d, analyst %d/%d",
                label, ok_inst, total, ok_analyst, total,
            )
        except Exception as e:
            logger.error("Institutional update (user=%s) failed: %s", uid[:8] if uid else "global", e)


async def job_model_calibration() -> None:
    """周度 AI 模型准确率校准。"""
    if not _wl_store:
        return
    from bottleneck_hunter.watchlist.model_calibrator import ModelCalibrator

    user_ids = [(uid, f"user={uid[:8]}" if uid else "global")
                for uid, _store, _budget in _get_active_user_stores()]
    for uid, label in user_ids:
        try:
            store = _wl_store.for_user(uid) if uid else _wl_store
            calibrator = ModelCalibrator(store)
            for mkt in ("us_stock", "a_stock"):
                count = calibrator.recalibrate(market=mkt)
                logger.info("Model calibration (%s/%s): %d recalibrated", mkt, label, count)
        except Exception as e:
            logger.error("Model calibration (user=%s) failed: %s", uid[:8] if uid else "global", e)
