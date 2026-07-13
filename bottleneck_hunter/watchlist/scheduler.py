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

# 时区常量：全系统统一北京时间。APScheduler CronTrigger 使用本地时间 + timezone 参数。
# 中国无夏令时（Asia/Shanghai 恒为 UTC+8），美股任务已在 schedule_config 里换算为对应北京时刻。
_TZ_CN = ZoneInfo("Asia/Shanghai")              # CST (UTC+8)，无夏令时

logger = logging.getLogger(__name__)

_scheduler = None
_wl_store: WatchlistStore | None = None
_budget: BudgetTracker | None = None
_auth_store = None  # AuthStore 实例，用于获取活跃用户列表


def _get_active_user_stores(category: str | None = None) -> list[tuple[str, WatchlistStore, BudgetTracker]]:
    """返回活跃用户的 (user_id, store, budget) 三元组，供定时任务遍历。

    category 非空时按自动更新配置门控：
    - 全局总开关 auto_update_global_enabled='0' → 系统级停用，返回 []。
    - 逐用户读 auto_update_config，master 或该 category 开关为 '0' 的用户被跳过。
    category 为空时保持旧行为（不门控，兼容非分类调用）。
    """
    if not _wl_store:
        return []
    # 全局 kill-switch（管理员级）
    if category is not None:
        try:
            from bottleneck_hunter.watchlist.schedule_config import is_global_enabled
            if not is_global_enabled(_auth_store):
                logger.info("自动更新全局总开关关闭，跳过 category=%s", category)
                return []
        except Exception:
            pass
    if not _auth_store:
        # 未配置认证时，用全局 store（兼容单用户模式）
        store = _wl_store
        if category is not None and not store.is_auto_update_enabled(category):
            return []
        return [("", store, _budget or BudgetTracker(store))]
    user_ids = _auth_store.list_active_user_ids()
    if not user_ids:
        return []
    result = []
    for uid in user_ids:
        user_store = _wl_store.for_user(uid)
        if category is not None and not user_store.is_auto_update_enabled(category):
            logger.debug("用户 %s 关闭了 %s 自动更新，跳过", uid[:8], category)
            continue
        user_budget = BudgetTracker(user_store)
        result.append((uid, user_store, user_budget))
    return result


def _iter_users(category: str | None = None):
    """遍历活跃用户，并在每个用户的处理期间设置请求级「当前用户」上下文。

    供后台任务使用：确保下游 LLM/数据源 KEY 严格按该用户解析（不会误用他人 KEY）。
    """
    from bottleneck_hunter.auth.current_user import set_current_user, reset_current_user
    for uid, store, budget in _get_active_user_stores(category):
        tok = set_current_user(uid)
        try:
            yield uid, store, budget
        finally:
            reset_current_user(tok)


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

    # 调度器统一北京时区；misfire_grace_time=3600 覆盖事件循环晚点/短暂重启（默认仅 1 秒会漏跑），
    # coalesce 防重启后堆积重复跑。
    _scheduler = AsyncIOScheduler(
        timezone="Asia/Shanghai",
        job_defaults={"misfire_grace_time": 3600, "coalesce": True, "max_instances": 1},
    )

    # 从全局时间表配置注册所有任务（无配置时回退默认时间）。触发时间可由管理员改后 reschedule。
    from bottleneck_hunter.watchlist.schedule_config import get_global_schedule
    schedule = get_global_schedule(auth_store)
    for spec in _JOB_SPECS:
        job_id, func, kw, _tz, _kind, name = spec
        _scheduler.add_job(
            func, _make_trigger(spec, schedule),
            id=job_id, name=name, kwargs=(kw or None),
            replace_existing=True, max_instances=1, coalesce=True,
        )

    logger.info("Watchlist scheduler configured with %d jobs (含陈旧兜底刷新 + 全量刷新，可配可开关)",
                len(_JOB_SPECS))
    # 操作日志保留清理：每天 03:30 删 >30 天（不入可配时间表，固定后台维护）
    try:
        from apscheduler.triggers.cron import CronTrigger
        _scheduler.add_job(job_oplog_prune, CronTrigger(hour=3, minute=30, timezone=_TZ_CN),
                           id="oplog_prune", name="Operation log retention (30d)",
                           replace_existing=True, max_instances=1, coalesce=True)
    except Exception as e:  # noqa: BLE001
        logger.warning("注册操作日志清理任务失败: %s", e)
    return _scheduler


def reschedule_all_from_config() -> None:
    """按最新全局时间表重排所有任务（管理员改时间后免重启生效）。"""
    if not _scheduler:
        return
    from bottleneck_hunter.watchlist.schedule_config import get_global_schedule
    schedule = get_global_schedule(_auth_store)
    for spec in _JOB_SPECS:
        job_id = spec[0]
        try:
            _scheduler.reschedule_job(job_id, trigger=_make_trigger(spec, schedule))
        except Exception as e:
            logger.warning("reschedule %s 失败: %s", job_id, e)
    logger.info("已按最新配置重排 %d 个定时任务", len(_JOB_SPECS))


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

def _oplog(uid: str, title: str, *, market: str = "", detail: str = "已完成",
           result: str = "success", error: str = "") -> None:
    """记一条「系统自动更新」操作日志（供用户在实时操作日志里看到）。失败不影响 job。"""
    if not uid:
        return
    try:
        from bottleneck_hunter.web.oplog import record_operation
        if error:
            record_operation(uid, title, category="error", detail=error[:200], result="fail", market=market)
        else:
            record_operation(uid, title, category="auto_update", detail=detail, result=result, market=market)
    except Exception:  # noqa: BLE001
        pass


async def job_price_update(market: str = "us_stock") -> dict[str, str]:
    """Fetch prices for watchlist tickers of a specific market (multi-user)."""
    all_results: dict[str, str] = {}
    for uid, store, budget in _iter_users("watchlist_data"):
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
            _oplog(uid, "行情自动更新", market=market, result=("fail" if status == "error" else status),
                   detail=f"成功 {ok_count}/{len(tickers)}" + (f"，失败 {fail_count}" if fail_count else ""))
            all_results.update(results)
        except Exception as e:
            logger.error("Price update (%s/user=%s) failed: %s", market, uid[:8] if uid else "global", e)
            _oplog(uid, "行情自动更新", market=market, error=str(e))
    return all_results


async def job_daily_scan(market: str = "us_stock") -> dict:
    """Daily scan: news (+ SEC/options for US), iterates all users."""
    all_results: dict[str, dict] = {}
    for uid, store, budget in _iter_users("watchlist_data"):
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
    from bottleneck_hunter.watchlist.schedule_config import is_global_enabled
    if not is_global_enabled(_auth_store):
        logger.info("自动更新全局总开关关闭，跳过宏观数据更新")
        return
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
    for uid, store, budget in _iter_users("daily_decision"):
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
            _oplog(uid, "日常决策", market=market, detail=f"{len(tickers)} 只标的完成 L1→L4+投委会")
        except Exception as e:
            logger.error("Daily decision (%s/user=%s) failed: %s", market, uid[:8] if uid else "global", e)
            _oplog(uid, "日常决策", market=market, error=str(e))


async def job_catalyst_scan() -> None:
    """催化剂扫描：清理过期 + 检测新催化剂（多用户）。"""
    for uid, store, budget in _iter_users("catalyst"):
        try:
            label = f"user={uid[:8]}" if uid else "global"
            logger.info("Catalyst scan (%s) starting", label)
            from bottleneck_hunter.watchlist.catalyst_monitor import check_catalyst_expiry, detect_catalysts, judge_expired_catalysts
            await _drain_sse(check_catalyst_expiry(store))
            await _drain_sse(judge_expired_catalysts(store))
            await _drain_sse(detect_catalysts(store, budget))
            logger.info("Catalyst scan (%s) completed", label)
            _oplog(uid, "催化剂扫描", detail="已检测新催化剂并清理过期项")
        except Exception as e:
            logger.error("Catalyst scan (user=%s) failed: %s", uid[:8] if uid else "global", e)
            _oplog(uid, "催化剂扫描", error=str(e))


async def job_weekly_strategy(market: str = "us_stock") -> None:
    """每周策略刷新：L1 宏观策略 + L2 组合策略（多用户）。"""
    for uid, store, budget in _iter_users("weekly_strategy"):
        try:
            label = f"user={uid[:8]}" if uid else "global"
            logger.info("Weekly strategy refresh (%s/%s) starting", market, label)
            from bottleneck_hunter.watchlist.decision_engine import run_macro_strategy, run_strategic_plan
            await _drain_sse(run_macro_strategy(store, budget, market=market))
            await _drain_sse(run_strategic_plan(store, budget, market=market))
            logger.info("Weekly strategy refresh (%s/%s) completed", market, label)
            _oplog(uid, "每周策略刷新", market=market, detail="L1 宏观 + L2 组合策略已更新")
        except Exception as e:
            logger.error("Weekly strategy refresh (%s/user=%s) failed: %s", market, uid[:8] if uid else "global", e)
            _oplog(uid, "每周策略刷新", market=market, error=str(e))


async def job_auto_review(market: str = "us_stock") -> None:
    """自动复盘：批量复盘未复盘的卖出交易（多用户）。"""
    for uid, store, budget in _iter_users("auto_review"):
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
    for uid, store, budget in _iter_users("auto_review"):
        try:
            from bottleneck_hunter.watchlist.trade_reviewer import scan_missed_opportunities
            async for evt in scan_missed_opportunities(store, market=market):
                data = evt.get("data", {})
                if data.get("event") == "missed_scan_done":
                    logger.info("机会成本扫描 (user=%s) — 发现 %d 条",
                                uid[:8] if uid else "global", data.get("found", 0))
        except Exception as e:
            logger.debug("机会成本扫描 (user=%s) 失败: %s", uid[:8] if uid else "global", e)

    # P1.6 用户偏好学习：从确认/拒绝历史归纳偏好写入 user_preferences，供 L4 参考。
    # 需累计足够样本才有意义（交易+反馈 ≥3），否则跳过避免噪声偏好。
    for uid, store, budget in _iter_users("auto_review"):
        try:
            from bottleneck_hunter.watchlist.preference_learner import learn_preferences
            sample = len(store.get_sim_trades(limit=50)) + len(store.get_rejection_patterns(limit=50))
            if sample < 3:
                logger.debug("偏好学习 (user=%s) 跳过 — 样本不足 (%d<3)", uid[:8] if uid else "global", sample)
                continue
            prefs = learn_preferences(store)
            logger.info("偏好学习 (user=%s) 完成 — %d 项: %s",
                        uid[:8] if uid else "global", len(prefs), list(prefs.keys()))
        except Exception as e:
            logger.error("偏好学习 (user=%s) 失败: %s", uid[:8] if uid else "global", e)


async def job_institutional_update() -> None:
    """每周更新美股机构持仓 & 分析师评级数据（多用户）。"""
    for uid, store, budget in _iter_users("watchlist_data"):
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


async def job_earnings_update(market: str = "us_stock") -> None:
    """周度更新财报数据（经 DataHub：FMP 美股含一致预期 / Tushare A股），填 earnings_reports。"""
    for uid, store, budget in _iter_users("watchlist_data"):
        try:
            by_market = store.get_tickers_by_market()
            tickers = by_market.get(market, [])
            if not tickers:
                continue
            label = f"user={uid[:8]}" if uid else "global"
            logger.info("Earnings update (%s/%s) starting for %d tickers", market, label, len(tickers))
            store.update_pipeline_status("earnings", last_status="running", stocks_total=len(tickers))

            from bottleneck_hunter.watchlist.earnings_pipeline import fetch_earnings_batch
            results = await fetch_earnings_batch(tickers, store, market=market, user_id=uid)
            ok = sum(1 for v in results.values() if v == "ok")
            total = len(tickers)
            status = "success" if ok == total else ("partial" if ok > 0 else "error")
            store.update_pipeline_status(
                "earnings",
                last_run_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                last_status=status,
                last_error="" if ok == total else f"earnings: {ok}/{total}",
                stocks_processed=ok, stocks_total=total,
            )
            logger.info("Earnings update (%s/%s) done: %d/%d", market, label, ok, total)
        except Exception as e:
            logger.error("Earnings update (user=%s) failed: %s", uid[:8] if uid else "global", e)


async def job_datasource_report() -> None:
    """每日数据源健康巡检：对已配置的 testable 付费源做连通探测，落 pipeline_status(ds_health:*)。"""
    if not _wl_store:
        return
    from bottleneck_hunter.watchlist.schedule_config import is_global_enabled
    if not is_global_enabled(_auth_store):
        logger.info("自动更新全局总开关关闭，跳过数据源健康巡检")
        return
    import asyncio as _asyncio

    from bottleneck_hunter.data_provider.data_source_catalog import (
        get_catalog,
        probe_source,
        resolve_data_source_key,
    )
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for src in get_catalog():
        sid = src["id"]
        if not src.get("testable"):
            continue
        name = f"ds_health:{sid}"
        key = resolve_data_source_key(sid)
        if not key:
            _wl_store.update_pipeline_status(name, last_status="idle",
                                             last_error="未配置 Key", last_run_at=now_iso)
            continue
        try:
            ok, msg = await _asyncio.to_thread(probe_source, sid, key, "")
            _wl_store.update_pipeline_status(
                name, last_status="success" if ok else "error",
                last_error="" if ok else msg[:200], last_run_at=now_iso)
        except Exception as e:
            _wl_store.update_pipeline_status(name, last_status="error",
                                             last_error=str(e)[:200], last_run_at=now_iso)
    logger.info("Data-source health check completed")


async def job_model_calibration() -> None:
    """周度 AI 模型准确率校准。"""
    if not _wl_store:
        return
    from bottleneck_hunter.watchlist.schedule_config import is_global_enabled
    if not is_global_enabled(_auth_store):
        logger.info("自动更新全局总开关关闭，跳过模型准确率校准")
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


# ── 模型能力分保鲜（月度）：对各用户已配、能力分已过期的模型重跑综合测试 ──
# 新鲜度 45 天 > 30 天月度间隔：某用户本月刷过的模型下月仍算新鲜 → 全局配额自然
# 轮换到其他用户，避免靠前用户每月吃满 cap、靠后用户永不刷新。
_CAP_REFRESH_STALE_DAYS = 45
_CAP_REFRESH_MAX_MODELS = 10    # 每次任务全局最多重测的模型数（防失控，真实 LLM 调用）


def _select_stale_models(uid: str, store, days: int = _CAP_REFRESH_STALE_DAYS) -> list[tuple[str, str]]:
    """返回该用户需要重测的 [(provider, model)]：已配 KEY 的 provider × 其默认模型，
    过滤掉 days 天内已测过的（按 model_capability_test.tested_at 判新鲜度）。"""
    from datetime import datetime, timedelta, timezone

    from bottleneck_hunter.llm_clients.factory import resolve_provider_model

    if not _auth_store:
        return []
    try:
        keyed = {k["provider"] for k in _auth_store.get_user_api_keys(uid)}
    except Exception:  # noqa: BLE001
        return []
    # 与 _now_iso() 一致：UTC + 秒级，保证与 tested_at 同格式可比
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    fresh = set()
    try:
        for r in store.get_test_results(user_id=uid):
            if (r.get("tested_at") or "") >= cutoff:
                fresh.add((r["provider"], r["model"]))
    except Exception:  # noqa: BLE001
        pass
    out = []
    for prov in sorted(keyed):
        model = resolve_provider_model(prov, uid)
        if model and (prov, model) not in fresh:
            out.append((prov, model))
    return out


async def job_model_capability_refresh() -> None:
    """月度：对各用户已配、且能力分已过期的模型重跑综合测试，刷新调度器的质量先验。

    每次任务全局最多重测 _CAP_REFRESH_MAX_MODELS 个模型（防失控）。**发起真实 LLM 调用**，
    多重成本护栏：①自动更新全局总开关门控（admin 可一键停）②各用户自动更新主开关
    ③budget.can_spend 门控（已超月度预算的用户跳过）④全局封顶 10。_iter_users 设
    current_user → KEY 严格按用户。

    计费口径：本任务属**有上限的诊断类维护**（月度、封顶 10 个模型、多用 免费档 模型），
    其花费不写入 BudgetTracker 台账（不计入用户日/月使用量）；由上述 can_spend 前置门控
    与全局封顶共同兜底，不会失控。如需纳入硬预算，让 model_tester 回传 token 用量后
    在此 budget.record(...) 即可。
    """
    if not _wl_store or not _auth_store:
        return
    try:
        from bottleneck_hunter.watchlist.schedule_config import is_global_enabled
        if not is_global_enabled(_auth_store):
            logger.info("自动更新全局总开关关闭，跳过模型能力分刷新")
            return
    except Exception as e:  # noqa: BLE001
        # 读开关失败 → **fail-closed**：宁可不刷新，也不在开关状态未知时发起真实付费调用
        logger.warning("读取全局总开关失败，安全起见跳过模型能力分刷新: %s", e)
        return

    from bottleneck_hunter.web.model_tester import run_comprehensive_test

    tested = 0
    # category="model_capability" → 遵守全局总开关 + 各用户自动更新主开关（关了自动更新的用户不被计费）
    for uid, store, budget in _iter_users("model_capability"):
        if tested >= _CAP_REFRESH_MAX_MODELS:
            break
        for prov, model in _select_stale_models(uid, store):
            if tested >= _CAP_REFRESH_MAX_MODELS:
                break
            if not budget.can_spend(estimated_tokens=6 * 1500, provider=prov):
                logger.info("用户 %s 预算不足，跳过其能力分刷新", uid[:8] if uid else "global")
                break   # 该用户预算耗尽 → 跳到下一用户
            try:
                results = await run_comprehensive_test(prov, model)
                for dim, res in results.items():
                    store.save_test_result(prov, model, dim, res.get("score", 0),
                                           json.dumps(res, ensure_ascii=False), user_id=uid)
                tested += 1
                logger.info("能力分已刷新: %s/%s (user=%s)", prov, model, uid[:8] if uid else "global")
            except Exception as e:  # noqa: BLE001
                logger.warning("能力分刷新失败 %s/%s: %s", prov, model, e)
    logger.info("模型能力分刷新完成：本次重测 %d 个模型", tested)


async def job_stale_refresh() -> None:
    """陈旧兜底刷新：刷新每个用户超过其阈值(默认24h)未更新的观察池标的。

    安全网——覆盖"当天定时任务没跑成/新加入/临时停用后恢复"的标的。
    刷价 + 逐只刷情报与策略（受预算门控）。category=watchlist_data。
    """
    from bottleneck_hunter.watchlist.price_pipeline import fetch_price_batch
    from bottleneck_hunter.watchlist.strategy_engine import refresh_intelligence_one, refresh_strategy_one
    for uid, store, budget in _iter_users("watchlist_data"):
        try:
            label = f"user={uid[:8]}" if uid else "global"
            threshold = int(store.get_auto_update_config().get("stale_threshold_hours", "24") or 24)
            stale = store.get_stale_tickers(max_age_hours=threshold)
            if not stale:
                continue
            logger.info("Stale refresh (%s): %d 个标的超过 %dh 未更新", label, len(stale), threshold)
            # 按市场分组刷价
            by_market: dict[str, list[str]] = {}
            for s in stale:
                by_market.setdefault(s.get("market") or "us_stock", []).append(s["ticker"])
            for market, tickers in by_market.items():
                try:
                    await fetch_price_batch(tickers, store, market=market)
                except Exception as e:
                    logger.warning("Stale 刷价失败 (%s/%s): %s", label, market, e)
            # 逐只刷情报 + 策略（预算不足时静默跳过）
            for s in stale:
                if budget and not budget.can_spend():
                    logger.info("Stale refresh (%s) 预算不足，停止情报/策略刷新", label)
                    break
                entry = store.get_by_ticker(s["ticker"])
                if not entry:
                    continue
                try:
                    await _drain_sse(refresh_intelligence_one(s["ticker"], entry["id"], store, budget))
                    await _drain_sse(refresh_strategy_one(s["ticker"], entry["id"], store, budget))
                except Exception as e:
                    logger.warning("Stale 情报/策略刷新失败 %s: %s", s["ticker"], e)
            store.update_pipeline_status(
                "stale_refresh",
                last_run_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                last_status="success", stocks_processed=len(stale), stocks_total=len(stale),
            )
        except Exception as e:
            logger.error("Stale refresh (user=%s) failed: %s", uid[:8] if uid else "global", e)


async def job_full_refresh(market: str = "us_stock") -> None:
    """周期性全量刷新：数据管道 → 宏观 → 完整决策 → 复盘，一条龙。

    对应"周期性自动全量更新，刷新所有数据和决策环节"。category=full_refresh。
    复用现有各 job 函数，串行执行保证顺序（先数据后决策）。
    """
    if not _get_active_user_stores("full_refresh"):
        return  # 无启用用户 / 全局关闭
    logger.info("Full refresh (%s) 开始：数据→宏观→决策→复盘", market)
    try:
        await job_price_update(market)
        await job_daily_scan(market)
        if market == "us_stock":
            await job_macro_update()
        await job_catalyst_scan()
        # 完整决策（L1→L4+投委会）
        from bottleneck_hunter.watchlist.decision_engine import run_daily_decision
        for uid, store, budget in _iter_users("full_refresh"):
            try:
                await _drain_sse(run_daily_decision(store, budget, scope="full", market=market))
            except Exception as e:
                logger.error("Full refresh 决策 (user=%s) failed: %s", uid[:8] if uid else "global", e)
        await job_auto_review(market)
        logger.info("Full refresh (%s) 完成", market)
    except Exception as e:
        logger.error("Full refresh (%s) failed: %s", market, e)


# ---------------------------------------------------------------------------
# 任务规格表（单一真值源）：id, func, kwargs, timezone, kind, name
# init_scheduler 与 reschedule_all_from_config 都据此从时间表配置构建触发器。
# 定义在文件末尾——此时所有 job 函数已定义。
# kind: "daily"(mon-fri) | "weekly"(day_of_week 来自配置) | "interval"(interval_hours)
# ---------------------------------------------------------------------------
async def job_oplog_prune() -> None:
    """操作日志保留清理：删除超过 30 天的记录。"""
    try:
        n = _wl_store.prune_operations(30) if _wl_store else 0
        if n:
            logger.info("操作日志清理：删除 %d 条(>30天)", n)
    except Exception as e:  # noqa: BLE001
        logger.warning("操作日志清理失败: %s", e)


_JOB_SPECS = [
    ("us_price_premarket",     job_price_update,        {"market": "us_stock"}, _TZ_CN        , "daily",    "US pre-market price update"),
    ("us_price_postmarket",    job_price_update,        {"market": "us_stock"}, _TZ_CN        , "daily",    "US post-market price update"),
    ("us_daily_scan",          job_daily_scan,          {"market": "us_stock"}, _TZ_CN        , "daily",    "US daily news/SEC/options scan"),
    ("cn_price_premarket",     job_price_update,        {"market": "a_stock"},  _TZ_CN,         "daily",    "A-stock pre-market price update"),
    ("cn_price_postmarket",    job_price_update,        {"market": "a_stock"},  _TZ_CN,         "daily",    "A-stock post-market price update"),
    ("cn_daily_scan",          job_daily_scan,          {"market": "a_stock"},  _TZ_CN,         "daily",    "A-stock daily news scan"),
    ("macro_update",           job_macro_update,        {},                     _TZ_CN        , "daily",    "Macro data update (VIX/Treasury/DXY)"),
    ("us_daily_decision",      job_daily_decision,      {"market": "us_stock"}, _TZ_CN        , "daily",    "Daily decision (L1-L4+committee)"),
    ("us_catalyst_scan",       job_catalyst_scan,       {},                     _TZ_CN        , "daily",    "Catalyst scan & expiry check"),
    ("us_weekly_strategy",     job_weekly_strategy,     {"market": "us_stock"}, _TZ_CN        , "weekly",   "Weekly macro strategy (L1+L2)"),
    ("us_auto_review",         job_auto_review,         {"market": "us_stock"}, _TZ_CN        , "daily",    "Auto review unreviewed sells"),
    ("cn_daily_decision",      job_daily_decision,      {"market": "a_stock"},  _TZ_CN,         "daily",    "A-stock daily decision (L1-L4+committee)"),
    ("cn_catalyst_scan",       job_catalyst_scan,       {},                     _TZ_CN,         "daily",    "A-stock catalyst scan & expiry"),
    ("cn_weekly_strategy",     job_weekly_strategy,     {"market": "a_stock"},  _TZ_CN,         "weekly",   "A-stock weekly macro strategy (L1+L2)"),
    ("cn_auto_review",         job_auto_review,         {"market": "a_stock"},  _TZ_CN,         "daily",    "A-stock auto review unreviewed sells"),
    ("us_institutional_update", job_institutional_update, {},                   _TZ_CN        , "weekly",   "Weekly institutional holders & analyst ratings"),
    ("us_earnings_update",     job_earnings_update,     {"market": "us_stock"}, _TZ_CN        , "weekly",   "Weekly earnings update (FMP, incl. consensus)"),
    ("cn_earnings_update",     job_earnings_update,     {"market": "a_stock"},  _TZ_CN,         "weekly",   "A-stock weekly earnings update (Tushare)"),
    ("datasource_report",      job_datasource_report,   {},                     _TZ_CN,         "everyday", "Data source health check & usage report"),
    ("model_calibration",      job_model_calibration,   {},                     _TZ_CN        , "weekly",   "Weekly AI model accuracy calibration"),
    ("model_capability_refresh", job_model_capability_refresh, {},               _TZ_CN        , "monthly",  "Monthly AI model capability re-test (刷新能力分)"),
    ("stale_refresh",          job_stale_refresh,       {},                     None,           "interval", "Stale watchlist refresh (safety net)"),
    ("us_full_refresh",        job_full_refresh,        {"market": "us_stock"}, _TZ_CN        , "weekly",   "US full refresh (data+decision)"),
    ("cn_full_refresh",        job_full_refresh,        {"market": "a_stock"},  _TZ_CN,         "weekly",   "A-stock full refresh (data+decision)"),
]


def _make_trigger(spec, schedule):
    """按 spec 的 kind 和时间表配置构建 APScheduler 触发器。"""
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger
    job_id, _func, _kw, tz, kind, _name = spec
    s = schedule.get(job_id, {}) or {}
    if kind == "interval":
        return IntervalTrigger(hours=int(s.get("interval_hours", 6) or 6))
    hour = int(s.get("hour", 0))
    minute = int(s.get("minute", 0))
    if kind == "monthly":
        # 每月 1 号（能力分保鲜等低频任务）
        return CronTrigger(day=1, hour=hour, minute=minute, timezone=tz)
    if kind == "everyday":
        # 每日含周末（数据源健康巡检：市场不开也要监控连通性）
        return CronTrigger(hour=hour, minute=minute, timezone=tz)
    dow = s.get("day_of_week", "sat") if kind == "weekly" else "mon-fri"
    return CronTrigger(day_of_week=dow, hour=hour, minute=minute, timezone=tz)


def list_job_categories() -> dict[str, str]:
    """job_id → 所属自动更新分类（供前端展示分组）。"""
    return {
        "us_price_premarket": "watchlist_data", "us_price_postmarket": "watchlist_data",
        "us_daily_scan": "watchlist_data", "cn_price_premarket": "watchlist_data",
        "cn_price_postmarket": "watchlist_data", "cn_daily_scan": "watchlist_data",
        "macro_update": "watchlist_data", "us_institutional_update": "watchlist_data",
        "us_earnings_update": "watchlist_data", "cn_earnings_update": "watchlist_data",
        "datasource_report": "",
        "us_daily_decision": "daily_decision", "cn_daily_decision": "daily_decision",
        "us_catalyst_scan": "catalyst", "cn_catalyst_scan": "catalyst",
        "us_weekly_strategy": "weekly_strategy", "cn_weekly_strategy": "weekly_strategy",
        "us_auto_review": "auto_review", "cn_auto_review": "auto_review",
        "stale_refresh": "watchlist_data",
        "us_full_refresh": "full_refresh", "cn_full_refresh": "full_refresh",
        "model_calibration": "",
        "model_capability_refresh": "",
    }


def list_job_labels() -> dict[str, dict]:
    """job_id → 中文说明（前端时间配置逐项显示）。含名称/触发时机/时区/频率。"""
    return {
        # 美股（北京时刻，已按美股时段前后换算；中国无夏令时故固定不漂移）
        "us_price_premarket":  {"label": "美股·盘前行情更新",      "desc": "开盘前采集行情快照",           "tz": "北京", "freq": "工作日"},
        "us_price_postmarket": {"label": "美股·盘后行情更新",      "desc": "收盘后更新行情与技术指标",     "tz": "北京", "freq": "工作日"},
        "us_daily_scan":       {"label": "美股·日报扫描",          "desc": "新闻 / SEC 文件 / 期权异动",    "tz": "北京", "freq": "工作日"},
        "macro_update":        {"label": "宏观数据更新",          "desc": "VIX / 美债 / 美元指数 / 北向资金", "tz": "北京", "freq": "工作日"},
        "us_daily_decision":   {"label": "美股·日常决策",          "desc": "L1-L4 分层决策 + 投委会评审",   "tz": "北京", "freq": "工作日"},
        "us_catalyst_scan":    {"label": "美股·催化剂扫描",        "desc": "检测/判定催化剂事件",           "tz": "北京", "freq": "工作日"},
        "us_weekly_strategy":  {"label": "美股·每周策略重生成",    "desc": "L1 宏观 + L2 组合策略全面重算", "tz": "北京", "freq": "每周"},
        "us_auto_review":      {"label": "美股·自动复盘",          "desc": "卖出复盘 + 机会成本 + 偏好学习", "tz": "北京", "freq": "工作日"},
        "us_institutional_update": {"label": "机构持仓 & 分析师评级", "desc": "13F 机构持仓与评级（仅美股）", "tz": "北京", "freq": "每周"},
        "us_earnings_update":  {"label": "美股·财报更新",          "desc": "FMP 财报（含机构一致预期）",   "tz": "北京", "freq": "每周"},
        "cn_earnings_update":  {"label": "A股·财报更新",           "desc": "Tushare 业绩快报/预告",        "tz": "北京", "freq": "每周"},
        "datasource_report":   {"label": "数据源健康巡检",        "desc": "付费源连通探测 + 用量汇总",     "tz": "北京", "freq": "每日"},
        "model_calibration":   {"label": "AI 模型准确率校准",      "desc": "对比历史预测与实际，更新权重", "tz": "北京", "freq": "每周"},
        "model_capability_refresh": {"label": "AI 模型能力分刷新", "desc": "月度重测各模型能力分",         "tz": "北京", "freq": "每月1号"},
        # A股（北京时区）
        "cn_price_premarket":  {"label": "A股·盘前行情更新",       "desc": "开盘前采集行情快照",           "tz": "北京", "freq": "工作日"},
        "cn_price_postmarket": {"label": "A股·盘后行情更新",       "desc": "收盘后更新行情与技术指标",     "tz": "北京", "freq": "工作日"},
        "cn_daily_scan":       {"label": "A股·日报扫描",           "desc": "新闻 / 公告",                   "tz": "北京", "freq": "工作日"},
        "cn_daily_decision":   {"label": "A股·日常决策",           "desc": "L1-L4 分层决策 + 投委会评审",   "tz": "北京", "freq": "工作日"},
        "cn_catalyst_scan":    {"label": "A股·催化剂扫描",         "desc": "检测/判定催化剂事件",           "tz": "北京", "freq": "工作日"},
        "cn_weekly_strategy":  {"label": "A股·每周策略重生成",     "desc": "L1 宏观 + L2 组合策略全面重算", "tz": "北京", "freq": "每周"},
        "cn_auto_review":      {"label": "A股·自动复盘",           "desc": "卖出复盘 + 机会成本 + 偏好学习", "tz": "北京", "freq": "工作日"},
        # 新增
        "stale_refresh":       {"label": "陈旧兜底刷新",           "desc": "刷新超过阈值未更新的观察池标的", "tz": "轮询", "freq": "每隔N小时"},
        "us_full_refresh":     {"label": "美股·周期性全量刷新",    "desc": "数据+宏观+完整决策+复盘一条龙", "tz": "北京", "freq": "每周"},
        "cn_full_refresh":     {"label": "A股·周期性全量刷新",     "desc": "数据+宏观+完整决策+复盘一条龙", "tz": "北京", "freq": "每周"},
    }

