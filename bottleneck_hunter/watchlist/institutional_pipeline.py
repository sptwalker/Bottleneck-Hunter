"""机构持仓 & 分析师评级数据管道。

- 美股机构持仓：FMP 优先（有 Key 的用户上下文，多季 per-holder → 喂活 committee/QoQ），yfinance 兜底。
- 美股分析师评级：yfinance。
- A股：efinance（东财十大流通股东，免费无 Key），落同一 institutional_holders 共享表。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import yfinance as yf

from bottleneck_hunter.watchlist.store import WatchlistStore

logger = logging.getLogger(__name__)

_SEM: asyncio.Semaphore | None = None


def _get_sem() -> asyncio.Semaphore:
    """延迟创建信号量，避免在导入时绑定事件循环。"""
    global _SEM
    if _SEM is None:
        _SEM = asyncio.Semaphore(5)
    return _SEM


_HOLDERS_COOLDOWN_DAYS = 30  # 13F 季度申报：近 30 天已有持仓则跳过(周频/按需重拉季度级数据纯浪费)


def _holders_fresh(store: WatchlistStore, ticker: str) -> bool:
    """近 30 天内已抓到 13F 机构持仓 → 本轮跳过重拉(季度级数据，周频刷新 3/4 是重复)。

    分析师评级(周频更新)不套此冷却，仍每轮刷新。
    """
    latest = store.get_institutional_holders(ticker, limit=1)
    if not latest:
        return False
    ts = (latest[0].get("fetched_at") or "").strip()
    try:
        fetched = datetime.fromisoformat(ts)
    except ValueError:
        return False
    if fetched.tzinfo is None:
        fetched = fetched.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - fetched) <= timedelta(days=_HOLDERS_COOLDOWN_DAYS)


# ---------------------------------------------------------------------------
# 机构持仓（FMP 优先，多季 per-holder → 喂活 QoQ；yfinance 兜底）
# ---------------------------------------------------------------------------

_FMP_STABLE = "https://financialmodelingprep.com/stable"


def _fmp_recent_quarters(n: int = 2) -> list[tuple[int, int]]:
    """返回近 n 个「已披露」的 13F 财季 (year, quarter)，新→旧。

    13F 申报截止在季末后约 45 天，故当前季通常尚未披露——从「上一个季末」起回溯。
    """
    now = datetime.now(timezone.utc)
    # 退到上一个完整季度（q = 当前季 - 1），再往前数
    q = (now.month - 1) // 3  # 0..3；当前季序号-1 即「上一完整季」的 0-based
    y = now.year
    if q == 0:
        q, y = 4, y - 1
    out: list[tuple[int, int]] = []
    for _ in range(n):
        out.append((y, q))
        q -= 1
        if q == 0:
            q, y = 4, y - 1
    return out


def _fetch_institutional_holders_fmp_sync(ticker: str, key: str) -> list[dict]:
    """FMP per-holder 机构持仓：抓近 2 个财季，落成与 yfinance 同形 holder dict（多季共存喂 QoQ）。

    映射：investorName→holder_name / sharesNumber→shares / marketValue→value /
    ownershipPercent→pct_held（FMP 已是百分比口径，勿用 weight=组合权重）/ date→date。
    """
    from bottleneck_hunter.data_provider.providers import _get_json_soft
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    holders: list[dict] = []
    seen_dates: set[str] = set()
    for year, quarter in _fmp_recent_quarters(2):
        url = (f"{_FMP_STABLE}/institutional-ownership/extract-analytics/holder"
               f"?symbol={ticker}&year={year}&quarter={quarter}&page=0&apikey={key}")
        rows = _get_json_soft(url)  # 402/403/429→None（付费/限流软失败，退 yfinance）
        if not isinstance(rows, list) or not rows:
            continue
        for row in rows:
            name = str(row.get("investorName", "") or "").strip()
            if not name:
                continue
            date_reported = str(row.get("date", "") or "")[:10]
            seen_dates.add(date_reported)
            holders.append({
                "holder_name": name,
                "shares": int(row.get("sharesNumber") or 0),
                "value": float(row.get("marketValue") or 0.0),
                "pct_held": round(float(row.get("ownershipPercent") or 0.0), 4),
                "date": date_reported,
                "fetched_at": now_iso,
            })
    # ponytail: 只有拿到 ≥2 个季度才算解锁 QoQ；单季或空则返回，让上层退回 yfinance（更稳的当前季）
    if len(seen_dates) < 2:
        return []
    return holders


def _fetch_institutional_holders_sync(ticker: str) -> list[dict]:
    """同步获取机构持仓数据（在线程池中运行）。"""
    from bottleneck_hunter.data_provider import yf_gate
    yf_gate.throttle()  # 全局限速：均匀错峰打 Yahoo，避免 429
    try:
        t = yf.Ticker(ticker)
        df = t.institutional_holders
        yf_gate.observe(None)
        if df is None or df.empty:
            logger.info("无机构持仓数据: %s", ticker)
            return []
    except Exception as e:
        yf_gate.observe(e)  # 命中 429 则闸门自适应退避
        logger.warning("获取 %s 机构持仓失败: %s", ticker, e)
        return []

    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    holders: list[dict] = []
    for _, row in df.iterrows():
        # yfinance 列名: Holder, Shares, Date Reported, % Out, Value
        holder_name = str(row.get("Holder", ""))
        if not holder_name:
            continue
        date_reported = ""
        raw_date = row.get("Date Reported")
        if raw_date is not None:
            try:
                date_reported = str(raw_date)[:10]
            except Exception:
                pass
        shares = 0
        raw_shares = row.get("Shares")
        if raw_shares is not None:
            try:
                shares = int(raw_shares)
            except (ValueError, TypeError):
                pass
        value = 0.0
        raw_value = row.get("Value")
        if raw_value is not None:
            try:
                value = float(raw_value)
            except (ValueError, TypeError):
                pass
        pct_held = 0.0
        raw_pct = row.get("% Out")
        if raw_pct is not None:
            try:
                pct_held = float(raw_pct) * 100  # 转为百分比
            except (ValueError, TypeError):
                pass
        holders.append({
            "holder_name": holder_name,
            "shares": shares,
            "value": value,
            "pct_held": round(pct_held, 4),
            "date": date_reported,
            "fetched_at": now_iso,
        })
    return holders


async def fetch_institutional_holders(
    ticker: str, store: WatchlistStore
) -> str:
    """异步获取单个 ticker 的机构持仓并保存到 store。

    Returns:
        "ok" / "no_data" / "error: ..."
    """
    async with _get_sem():
        try:
            if _holders_fresh(store, ticker):
                return "cached"  # 13F 季度级数据近 30 天已抓，跳过重拉
            from bottleneck_hunter.data_provider.data_source_catalog import resolve_data_source_key
            from bottleneck_hunter.data_provider.hub import CAP_INSTITUTIONAL, get_hub
            # FMP 优先：仅当「当前用户上下文」配了 fmp key 才走
            # （全局周更 job 无用户上下文 → key="" → 退 yfinance，不回归）
            fmp_key = resolve_data_source_key("fmp")
            if fmp_key:
                async with get_hub().track("fmp", CAP_INSTITUTIONAL, "us_stock") as _sink:
                    holders = await asyncio.to_thread(
                        _fetch_institutional_holders_fmp_sync, ticker, fmp_key
                    )
                    if holders:
                        store.save_institutional_holders(ticker, holders)
                        logger.info("FMP 机构持仓保存成功: %s (%d 条/多季)", ticker, len(holders))
                        _sink["rows"] = len(holders)
                        return "ok"
                    # FMP 无多季数据（付费档软失败/单季）→ 落回 yfinance 当前季
            async with get_hub().track("yfinance", CAP_INSTITUTIONAL, "us_stock") as _sink:
                holders = await asyncio.to_thread(
                    _fetch_institutional_holders_sync, ticker
                )
                if holders:
                    store.save_institutional_holders(ticker, holders)
                    logger.info("机构持仓保存成功: %s (%d 条)", ticker, len(holders))
                    _sink["rows"] = len(holders)
                    return "ok"
                return "no_data"
        except Exception as e:
            logger.error("机构持仓管道错误 %s: %s", ticker, e)
            return f"error: {e}"


# ---------------------------------------------------------------------------
# A股机构持仓（efinance 十大流通股东，免费无 Key，落同一共享表）
# ---------------------------------------------------------------------------

async def fetch_astock_holders(ticker: str, store: WatchlistStore) -> str:
    """异步获取单个 A股 ticker 的十大流通股东并保存。Returns "ok"/"cached"/"no_data"/"error:..."。"""
    async with _get_sem():
        try:
            if _holders_fresh(store, ticker):
                return "cached"  # 季度级数据，近 30 天已抓则跳过（同 13F 冷却）
            from bottleneck_hunter.data_provider.efinance_astock import fetch_astock_holders as _ef_holders
            from bottleneck_hunter.data_provider.hub import CAP_INSTITUTIONAL, get_hub
            async with get_hub().track("efinance", CAP_INSTITUTIONAL, "a_stock") as _sink:
                holders = await _ef_holders(ticker)
                if holders:
                    store.save_institutional_holders(ticker, holders)
                    logger.info("A股股东保存成功: %s (%d 条)", ticker, len(holders))
                    _sink["rows"] = len(holders)
                    return "ok"
                return "no_data"
        except Exception as e:  # noqa: BLE001
            logger.error("A股股东管道错误 %s: %s", ticker, e)
            return f"error: {e}"


async def fetch_astock_holders_batch(tickers: list[str], store: WatchlistStore) -> dict[str, str]:
    """批量获取 A股十大流通股东。返回 {ticker: status}。"""
    if not tickers:
        return {}
    tasks = {t: asyncio.create_task(fetch_astock_holders(t, store)) for t in tickers}
    return {t: await task for t, task in tasks.items()}


# ---------------------------------------------------------------------------
# 分析师评级
# ---------------------------------------------------------------------------

def _fetch_analyst_ratings_sync(ticker: str) -> list[dict]:
    """同步获取分析师评级 / 推荐数据（在线程池中运行）。"""
    from bottleneck_hunter.data_provider import yf_gate
    yf_gate.throttle()  # 全局限速：均匀错峰打 Yahoo，避免 429
    try:
        t = yf.Ticker(ticker)
        df = t.recommendations
        yf_gate.observe(None)
        if df is None or df.empty:
            logger.info("无分析师评级数据: %s", ticker)
            return []
    except Exception as e:
        yf_gate.observe(e)  # 命中 429 则闸门自适应退避
        logger.warning("获取 %s 分析师评级失败: %s", ticker, e)
        return []

    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    ratings: list[dict] = []
    for idx, row in df.iterrows():
        # yfinance recommendations 列名: period, strongBuy, buy, hold, sell, strongSell
        # 或者较新版 yfinance: Firm, To Grade, From Grade, Action, Date
        firm = str(row.get("Firm", ""))
        rating = str(row.get("To Grade", ""))
        # 如果没有 Firm 列（汇总格式），使用 period 作为标识
        if not firm and "period" in row.index:
            firm = f"consensus_{row.get('period', '')}"
            # 汇总格式：strongBuy/buy/hold/sell/strongSell 列
            buy_count = int(row.get("strongBuy", 0) or 0) + int(row.get("buy", 0) or 0)
            hold_count = int(row.get("hold", 0) or 0)
            sell_count = int(row.get("sell", 0) or 0) + int(row.get("strongSell", 0) or 0)
            total = buy_count + hold_count + sell_count
            if total > 0:
                if buy_count > hold_count and buy_count > sell_count:
                    rating = "Buy"
                elif sell_count > hold_count:
                    rating = "Sell"
                else:
                    rating = "Hold"
            else:
                rating = "N/A"

        # 日期
        date_str = ""
        if isinstance(idx, datetime):
            date_str = idx.strftime("%Y-%m-%d")
        else:
            raw_date = row.get("Date")
            if raw_date is not None:
                try:
                    date_str = str(raw_date)[:10]
                except Exception:
                    pass

        # 目标价（如果有）
        target_price = None
        raw_tp = row.get("Target Price")
        if raw_tp is not None:
            try:
                target_price = float(raw_tp)
            except (ValueError, TypeError):
                pass

        if not firm:
            continue
        ratings.append({
            "firm": firm,
            "rating": rating,
            "target_price": target_price,
            "date": date_str,
            "fetched_at": now_iso,
        })
    return ratings


async def fetch_analyst_ratings(
    ticker: str, store: WatchlistStore
) -> str:
    """异步获取单个 ticker 的分析师评级并保存到 store。

    Returns:
        "ok" / "no_data" / "error: ..."
    """
    async with _get_sem():
        try:
            from bottleneck_hunter.data_provider.hub import CAP_INSTITUTIONAL, get_hub
            async with get_hub().track("yfinance", CAP_INSTITUTIONAL, "us_stock") as _sink:
                ratings = await asyncio.to_thread(
                    _fetch_analyst_ratings_sync, ticker
                )
                if ratings:
                    store.save_analyst_ratings(ticker, ratings)
                    logger.info("分析师评级保存成功: %s (%d 条)", ticker, len(ratings))
                    _sink["rows"] = len(ratings)
                    return "ok"
                return "no_data"
        except Exception as e:
            logger.error("分析师评级管道错误 %s: %s", ticker, e)
            return f"error: {e}"


# ---------------------------------------------------------------------------
# 批量接口
# ---------------------------------------------------------------------------

async def fetch_institutional_batch(
    tickers: list[str], store: WatchlistStore
) -> dict[str, str]:
    """批量获取机构持仓。返回 {ticker: status}。"""
    if not tickers:
        return {}
    tasks = {
        t: asyncio.create_task(fetch_institutional_holders(t, store))
        for t in tickers
    }
    results: dict[str, str] = {}
    for ticker, task in tasks.items():
        results[ticker] = await task
    return results


async def fetch_analyst_batch(
    tickers: list[str], store: WatchlistStore
) -> dict[str, str]:
    """批量获取分析师评级。返回 {ticker: status}。"""
    if not tickers:
        return {}
    tasks = {
        t: asyncio.create_task(fetch_analyst_ratings(t, store))
        for t in tickers
    }
    results: dict[str, str] = {}
    for ticker, task in tasks.items():
        results[ticker] = await task
    return results


# ---------------------------------------------------------------------------
# 自检（离线，无网络/LLM）：验证 FMP 解析 + 字段映射 + 单季降级
# ---------------------------------------------------------------------------

def demo() -> None:
    import bottleneck_hunter.data_provider.providers as prov

    # ① 近 2 财季推算：新→旧、皆为已披露的完整季（q∈1..4），无重复
    qs = _fmp_recent_quarters(2)
    assert len(qs) == 2 and all(1 <= q <= 4 for _, q in qs) and qs[0] != qs[1], qs

    # 两季合成 payload（键对齐推算出的季度）；ownershipPercent 是持股比例，weight 是组合权重(勿混)
    two_q = {
        qs[0]: [{"investorName": "VANGUARD", "sharesNumber": 1000, "marketValue": 5e5,
                 "ownershipPercent": 8.12, "weight": 3.3, "date": "2025-03-31"}],
        qs[1]: [{"investorName": "BLACKROCK", "sharesNumber": 900, "marketValue": 4e5,
                 "ownershipPercent": 7.50, "weight": 3.1, "date": "2024-12-31"},
                {"investorName": "", "sharesNumber": 5, "date": "2024-12-31"}],  # 空名跳过
    }

    def _fake_soft_factory(payloads):
        import re

        def _fake(url, headers=None):
            y = int(re.search(r"year=(\d+)", url).group(1))
            q = int(re.search(r"quarter=(\d+)", url).group(1))
            return payloads.get((y, q))
        return _fake

    orig_soft = prov._get_json_soft
    try:
        # ② FMP 解析：两季 → 2 条有效持有人（空名跳过），映射 ownershipPercent→pct_held，不落 weight
        prov._get_json_soft = _fake_soft_factory(two_q)
        rows = _fetch_institutional_holders_fmp_sync("TEST", "k")
        assert len(rows) == 2, rows
        assert rows[0]["holder_name"] == "VANGUARD" and rows[0]["pct_held"] == 8.12, rows[0]
        assert "weight" not in rows[0], "不得落 weight（组合权重≠持股比例）"
        assert {r["date"] for r in rows} == {"2025-03-31", "2024-12-31"}, rows

        # ③ 单季降级：只有最新季有数据 → 返回空（让上层退回 yfinance 当前季）
        prov._get_json_soft = _fake_soft_factory({qs[0]: two_q[qs[0]]})
        assert _fetch_institutional_holders_fmp_sync("TEST", "k") == [], "单季应降级为空"

        # ④ 全空/付费软失败 → 空（不抛）
        prov._get_json_soft = _fake_soft_factory({})
        assert _fetch_institutional_holders_fmp_sync("TEST", "k") == [], "无数据应为空"
    finally:
        prov._get_json_soft = orig_soft

    print("institutional_pipeline demo OK: FMP 多季解析 / 映射 ownershipPercent / 单季降级 全通过")


if __name__ == "__main__":
    demo()
