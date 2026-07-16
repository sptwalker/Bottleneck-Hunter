"""Price data pipeline — fetch daily OHLCV + compute technical indicators.

Uses FetcherManager for auto-failover across data sources:
  A-stock: efinance → akshare → pytdx
  US-stock: yfinance → finnhub
"""

from __future__ import annotations

import asyncio
import logging
import math
from datetime import datetime, timedelta, timezone

import pandas as pd
import yfinance as yf

try:
    import akshare as ak
except ImportError:
    ak = None  # type: ignore[assignment]

from bottleneck_hunter.watchlist.retry import with_retry
from bottleneck_hunter.watchlist.store import WatchlistStore

logger = logging.getLogger(__name__)

_SEM: asyncio.Semaphore | None = None


def _get_sem() -> asyncio.Semaphore:
    global _SEM
    if _SEM is None:
        _SEM = asyncio.Semaphore(4)
    return _SEM


# ---------------------------------------------------------------------------
# Technical indicators
# ---------------------------------------------------------------------------

def _compute_rsi(closes: list[float], period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100.0 - 100.0 / (1.0 + rs), 2)


def _compute_macd(closes: list[float], fast: int = 12, slow: int = 26, signal: int = 9) -> tuple[float, float, float] | None:
    if len(closes) < slow + signal:
        return None

    def ema(data: list[float], period: int) -> list[float]:
        result = [sum(data[:period]) / period]
        k = 2.0 / (period + 1)
        for v in data[period:]:
            result.append(v * k + result[-1] * (1 - k))
        return result

    ema_fast = ema(closes, fast)
    ema_slow = ema(closes, slow)
    offset = slow - fast
    macd_line = [ema_fast[i + offset] - ema_slow[i] for i in range(len(ema_slow))]
    if len(macd_line) < signal:
        return None
    signal_line = ema(macd_line, signal)
    hist = macd_line[-1] - signal_line[-1]
    return round(macd_line[-1], 4), round(signal_line[-1], 4), round(hist, 4)


def _compute_sma(closes: list[float], period: int) -> float | None:
    if len(closes) < period:
        return None
    return round(sum(closes[-period:]) / period, 4)


def _safe(v) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
        return None if (math.isnan(f) or math.isinf(f)) else f
    except (ValueError, TypeError):
        return None


def _extract_astock_code(ticker: str) -> str | None:
    """从 ticker (如 '600519.SH', 'SH600519', '688012') 中提取 6 位代码。
    全系统唯一 A股代码提取器（见 store_base），供本模块及 chain/reverse 等复用。"""
    from bottleneck_hunter.watchlist.store_base import extract_astock_code
    return extract_astock_code(ticker)


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------

@with_retry(max_retries=3, base_delay=1.0)
def _fetch_daily_data(ticker: str, days: int = 180) -> tuple[list[dict], dict]:
    """Fetch OHLCV from yfinance and compute RSI/MACD/SMA. Synchronous.

    返回 (snapshots, company_info)——与 A 股路径一致的二元组，空数据也返回 ([], {})，
    避免调用方解包失败。
    """
    t = yf.Ticker(ticker)
    period = "1y" if days > 180 else "6mo"
    df: pd.DataFrame = t.history(period=period)
    if df is None or df.empty:
        logger.warning("No price data for %s", ticker)
        return [], {}

    closes = df["Close"].tolist()
    volumes = df["Volume"].tolist()
    opens = df["Open"].tolist()
    highs = df["High"].tolist()
    lows = df["Low"].tolist()

    rsi = _compute_rsi(closes)
    macd_result = _compute_macd(closes)
    sma_20 = _compute_sma(closes, 20)
    sma_50 = _compute_sma(closes, 50)

    info = {}
    try:
        info = t.info or {}
    except Exception as e:
        logger.debug("获取 %s info 失败: %s", ticker, e)

    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    result = []
    for i in range(max(0, len(df) - days), len(df)):
        date_str = df.index[i].strftime("%Y-%m-%d")
        prev_close = closes[i - 1] if i > 0 else closes[i]
        change_pct = ((closes[i] - prev_close) / prev_close * 100) if prev_close else 0.0
        snap = {
            "ticker": ticker,
            "date": date_str,
            "open": _safe(opens[i]),
            "high": _safe(highs[i]),
            "low": _safe(lows[i]),
            "close": _safe(closes[i]),
            "volume": int(volumes[i]) if volumes[i] else None,
            "change_pct": round(change_pct, 2),
            "fetched_at": now_iso,
        }
        if i == len(df) - 1:
            snap["rsi_14"] = rsi
            snap["sma_20"] = sma_20
            snap["sma_50"] = sma_50
            snap["market_cap"] = _safe(info.get("marketCap"))
            snap["pe_ratio"] = _safe(info.get("forwardPE") or info.get("trailingPE"))
            if macd_result:
                snap["macd"], snap["macd_signal"], snap["macd_hist"] = macd_result
        result.append(snap)
    return result, info


def _fetch_company_info_us(ticker: str) -> dict:
    """获取美股企业基本面信息 (yfinance Ticker.info)。同步。"""
    try:
        t = yf.Ticker(ticker)
        return t.info or {}
    except Exception as e:
        logger.debug("获取 %s company info 失败: %s", ticker, e)
        return {}


def _fetch_astock_profile(ticker: str) -> dict:
    """通过 baostock 获取 A 股基本面，映射成 yfinance 风格 info dict——基本信息页遂能复用
    与美股同一套字段(估值/盈利/财务/成长)。baostock 走独立服务器，东财系(akshare/efinance)
    挂掉时仍可用。baostock 的 roe/margin/growth 已是分数(0.32=32%)，与 yfinance 约定一致，原样存。

    ponytail: 每次刷新都重新拉(与美股 .info 一致，非高频)；若吞吐吃紧再按季度缓存。
    """
    try:
        import baostock as bs
    except ImportError:
        return {}
    from bottleneck_hunter.data_provider.fetchers.baostock_fetcher import _bs_code, _bs_lock

    bcode = _bs_code(ticker)
    if not bcode:
        return {}

    def _query(fn, **kw):
        rs = fn(**kw)
        if rs.error_code != "0":
            return {}
        rows = []
        while rs.next():
            rows.append(rs.get_row_data())
        return dict(zip(rs.fields, rows[0])) if rows else {}

    def _rows_latest(fn, **kw):
        rs = fn(**kw)
        if rs.error_code != "0":
            return {}
        last = None
        while rs.next():
            last = rs.get_row_data()
        return dict(zip(rs.fields, last)) if last else {}

    info: dict = {}
    now = datetime.now()
    with _bs_lock:  # baostock 全局会话非线程安全，复用 K线 fetcher 的同一把锁
        if bs.login().error_code != "0":
            return {}
        try:
            # 估值(日频最新)：peTTM/pbMRQ/psTTM + close(算市值)
            start = (now - timedelta(days=15)).strftime("%Y-%m-%d")
            v = _rows_latest(bs.query_history_k_data_plus, code=bcode,
                             fields="close,peTTM,pbMRQ,psTTM",
                             start_date=start, end_date=now.strftime("%Y-%m-%d"),
                             frequency="d", adjustflag="3")
            close = _safe(v.get("close"))
            info["trailingPE"] = _safe(v.get("peTTM"))
            info["priceToBook"] = _safe(v.get("pbMRQ"))
            info["priceToSalesTrailing12Months"] = _safe(v.get("psTTM"))

            # 盈利/成长/偿债(季频)：从当前季度回溯，找到最近一期已披露
            y, q = now.year, (now.month - 1) // 3 + 1
            prof = {}
            for _ in range(6):
                prof = _query(bs.query_profit_data, code=bcode, year=y, quarter=q)
                if prof:
                    break
                q -= 1
                if q == 0:
                    q, y = 4, y - 1
            if prof:
                info["returnOnEquity"] = _safe(prof.get("roeAvg"))
                info["profitMargins"] = _safe(prof.get("npMargin"))
                info["grossMargins"] = _safe(prof.get("gpMargin"))
                info["trailingEps"] = _safe(prof.get("epsTTM"))
                shares = _safe(prof.get("totalShare"))
                if close and shares:
                    info["marketCap"] = close * shares

                growth = _query(bs.query_growth_data, code=bcode, year=y, quarter=q)
                info["earningsGrowth"] = _safe(growth.get("YOYNI"))

                bal = _query(bs.query_balance_data, code=bcode, year=y, quarter=q)
                info["currentRatio"] = _safe(bal.get("currentRatio"))
                info["quickRatio"] = _safe(bal.get("quickRatio"))
                a2e = _safe(bal.get("assetToEquity"))
                if a2e and a2e > 0:  # 资产负债率% = 1 - 权益/资产；存进 debtToEquity 槽(前端标签即"资产负债率")
                    info["debtToEquity"] = round((1 - 1 / a2e) * 100, 1)
        finally:
            bs.logout()

    if info:
        info["currency"] = "CNY"
        info["country"] = "中国"
        info["exchange"] = "上交所" if bcode.startswith("sh") else "深交所"
    # 全空(None)视为无数据，避免写入空 profile
    return info if any(v is not None for v in info.values()) else {}


def _is_empty(v) -> bool:
    """字段视空：None / 空串 / 占位符 '-'。"""
    return v is None or (isinstance(v, str) and v.strip() in ("", "-", "—", "N/A"))


def _merge_fill(*sources: dict, prefer: dict | None = None) -> dict:
    """免费源智能融合：每个字段取"第一个非空来源"，某些字段可用 prefer 指定优先源。

    纯函数、确定性。sources 顺序=默认优先级(前者优先)；prefer[field]=某个源 dict → 该字段
    优先取该源(仍要求非空)。任一源可用即产出，全空→{}。见方案A：免费源补缺。
    """
    prefer = prefer or {}
    out: dict = {}
    keys: list = []
    for s in sources:
        for k in (s or {}):
            if k not in out:
                keys.append(k)
    for k in keys:
        # prefer 指定源优先(非空才用)，否则按 sources 顺序取首个非空
        pv = prefer.get(k, {}).get(k) if isinstance(prefer.get(k), dict) else None
        if not _is_empty(pv):
            out[k] = pv
            continue
        for s in sources:
            v = (s or {}).get(k)
            if not _is_empty(v):
                out[k] = v
                break
    return out


def _fetch_astock_extras(code: str) -> dict:
    """akshare stock_individual_info_em 里 baostock 缺的字段 → yfinance 风格。best-effort。

    补 baostock 没有的：行业(sector/industry)、总市值(marketCap，东财实时)、员工(fullTimeEmployees)、
    上市时间(ipoDate)、动态PE(trailingPE 兜底)。东财挂/无 akshare → {}。
    """
    if ak is None or not code:
        return {}
    try:
        df = ak.stock_individual_info_em(symbol=code)
        if df is None or df.empty:
            return {}
        m = dict(zip(df["item"], df["value"]))
    except Exception as e:  # noqa: BLE001
        logger.debug("A股 extras(%s) akshare 失败: %s", code, e)
        return {}

    out: dict = {}
    mc = _safe(m.get("总市值"))
    if mc is not None:
        out["marketCap"] = mc
    ind = m.get("行业")
    if ind and str(ind).strip() not in ("", "-", "—"):
        out["sector"] = str(ind).strip()
        out["industry"] = str(ind).strip()
    emp = _safe(m.get("员工人数"))
    if emp is not None:
        out["fullTimeEmployees"] = int(emp)
    ipo = m.get("上市时间")
    if ipo and str(ipo).strip() not in ("", "-", "0"):
        out["ipoDate"] = str(ipo).strip()
    pe = _safe(m.get("市盈率(动态)"))
    if pe is not None:
        out["trailingPE"] = pe
    return out


def _fetch_astock_profile_fused(ticker: str) -> dict:
    """A股基本面多免费源融合：baostock(比率/盈利/成长，精确) ⊕ akshare(行业/市值/员工/上市，补缺)。

    每字段取有值的最优来源；marketCap 优先 akshare(东财实时)而非 baostock(close×股本估算)。
    任一源可用即产出——比原来 baostock 单源更全更稳(某源抖动时另一源兜底)。
    """
    code = _extract_astock_code(ticker)
    baostock = _fetch_astock_profile(ticker)   # 主：比率/盈利/成长
    extras = _fetch_astock_extras(code) if code else {}  # 补：行业/市值/员工/上市
    if not baostock and not extras:
        return {}
    return _merge_fill(baostock, extras, prefer={"marketCap": extras})


def _fetch_astock_fundamentals(code: str) -> dict:
    """通过 akshare 获取 A 股基本面数据（PE/PB/总市值）。

    优先使用 stock_individual_info_em（单只股票，轻量）；
    若失败则回退到 stock_zh_a_spot_em（全市场快照过滤）。
    两者都失败时返回空 dict，不影响价格数据采集。
    """
    result: dict = {}
    if ak is None:
        return result

    # ── 方案 1: stock_individual_info_em（单股信息，字段丰富） ──
    try:
        df_info = ak.stock_individual_info_em(symbol=code)
        if df_info is not None and not df_info.empty:
            info = dict(zip(df_info["item"], df_info["value"]))
            result["market_cap"] = _safe(info.get("总市值"))
            result["pe_ratio"] = _safe(info.get("市盈率(动态)"))
            # 如果 PE 已获取，直接返回
            if result.get("pe_ratio") is not None:
                logger.debug("A股基本面(%s): 通过 stock_individual_info_em 获取成功", code)
                return result
    except Exception as e:
        logger.debug("stock_individual_info_em(%s) 失败: %s", code, e)

    # ── 方案 2: stock_zh_a_spot_em（全市场快照，按代码过滤） ──
    try:
        df_spot = ak.stock_zh_a_spot_em()
        if df_spot is not None and not df_spot.empty:
            row = df_spot[df_spot["代码"] == code]
            if not row.empty:
                r = row.iloc[0]
                pe_val = r.get("市盈率-动态")
                if pe_val not in ("", "-", None):
                    result["pe_ratio"] = _safe(pe_val)
                pb_val = r.get("市净率")
                if pb_val not in ("", "-", None):
                    result["pb"] = _safe(pb_val)
                if not result.get("market_cap"):
                    result["market_cap"] = _safe(r.get("总市值"))
                logger.debug("A股基本面(%s): 通过 stock_zh_a_spot_em 获取成功", code)
    except Exception as e:
        logger.debug("stock_zh_a_spot_em(%s) 失败: %s", code, e)

    return result


@with_retry(max_retries=3, base_delay=1.0)
def _fetch_astock_daily(ticker: str, days: int = 180) -> tuple[list[dict], dict]:
    """Fetch A-stock OHLCV via akshare + compute RSI/MACD/SMA + PE/市值. Synchronous.

    返回 (snapshots, company_info)——与成功路径一致的二元组，避免调用方
    `snapshots, company_info = fetch_fn(...)` 在空数据时 ValueError 解包失败。
    """
    if ak is None:
        logger.warning("akshare not installed, cannot fetch A-stock data")
        return [], {}
    code = _extract_astock_code(ticker)
    if not code:
        logger.warning("Cannot extract A-stock code from %s", ticker)
        return [], {}
    start_date = (datetime.now() - timedelta(days=max(days, 365))).strftime("%Y%m%d")
    end_date = datetime.now().strftime("%Y%m%d")
    df = ak.stock_zh_a_hist(
        symbol=code, period="daily",
        start_date=start_date, end_date=end_date,
        adjust="qfq",
    )
    if df is None or df.empty:
        logger.warning("No A-stock price data for %s", ticker)
        return [], {}

    closes = [float(v) for v in df["收盘"]]
    volumes = [int(v) for v in df["成交量"]]
    opens = [float(v) for v in df["开盘"]]
    highs = [float(v) for v in df["最高"]]
    lows = [float(v) for v in df["最低"]]

    rsi = _compute_rsi(closes)
    macd_result = _compute_macd(closes)
    sma_20 = _compute_sma(closes, 20)
    sma_50 = _compute_sma(closes, 50)

    # 获取 A 股基本面数据（PE/PB/总市值）
    fundamentals = _fetch_astock_fundamentals(code)

    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    result = []
    for i in range(max(0, len(df) - days), len(df)):
        date_str = str(df.iloc[i]["日期"])[:10]
        prev_close = closes[i - 1] if i > 0 else closes[i]
        change_pct = ((closes[i] - prev_close) / prev_close * 100) if prev_close else 0.0
        snap = {
            "ticker": ticker,
            "date": date_str,
            "open": _safe(opens[i]),
            "high": _safe(highs[i]),
            "low": _safe(lows[i]),
            "close": _safe(closes[i]),
            "volume": volumes[i],
            "change_pct": round(change_pct, 2),
            "fetched_at": now_iso,
        }
        if i == len(df) - 1:
            snap["rsi_14"] = rsi
            snap["sma_20"] = sma_20
            snap["sma_50"] = sma_50
            snap["market_cap"] = fundamentals.get("market_cap")
            snap["pe_ratio"] = fundamentals.get("pe_ratio")
            if macd_result:
                snap["macd"], snap["macd_signal"], snap["macd_hist"] = macd_result
        result.append(snap)
    return result, {}


async def _fetch_one(ticker: str, store: WatchlistStore, days: int = 180, market: str = "us_stock",
                     cache: dict | None = None) -> str:
    """Fetch one ticker asynchronously with semaphore. Returns status string.

    优先通过 FetcherManager 获取（自动降级），若失败再走原有直连逻辑。

    cache: 可选的「周期内拉取缓存」，key=(market, ticker)。命中则跳过网络拉取、
    直接用缓存的 (snapshots, company_info) 做本用户的校验+落库——公共信息层阶段1：
    多用户共享同一支票只拉一次网络（缓解限流、提速）。cache=None 时行为完全不变。
    """
    from bottleneck_hunter.watchlist.data_validator import validate_snapshot

    async with _get_sem():
        try:
            ck = (market, ticker)
            if cache is not None and ck in cache:
                snapshots, company_info = cache[ck]
            else:
                company_info = {}
                snapshots = await _fetch_via_manager(ticker, days, market)
                if not snapshots:
                    fetch_fn = _fetch_astock_daily if market == "a_stock" else _fetch_daily_data
                    snapshots, company_info = await asyncio.to_thread(fetch_fn, ticker, days)

                if not company_info:
                    info_fn = _fetch_astock_profile_fused if market == "a_stock" else _fetch_company_info_us
                    company_info = await asyncio.to_thread(info_fn, ticker)
                if cache is not None:
                    cache[ck] = (snapshots, company_info)

            if company_info:
                try:
                    store.save_company_profile(ticker, company_info)
                except Exception as e:
                    logger.debug("保存 %s company profile 失败: %s", ticker, e)

            if snapshots:
                # 浅拷贝每条快照：校验会往 snap 里写 market/data_quality/quality_notes，
                # 缓存被多用户共享时不能就地改同一份 dict（否则互相污染）。
                snapshots = [dict(s) for s in snapshots]
                prev_snap = None
                valid_snaps = []
                is_st = False
                if market == "a_stock":
                    wl_entry = store.get_by_ticker(ticker)
                    name = (wl_entry or {}).get("company_name_cn", "") or (wl_entry or {}).get("company_name", "")
                    is_st = "ST" in name.upper()
                for snap in snapshots:
                    snap["market"] = market
                    vr = validate_snapshot(snap, prev_snap, market, is_st=is_st)
                    snap["data_quality"] = vr.data_quality
                    snap["quality_notes"] = "; ".join(vr.warnings + vr.errors)
                    if vr.valid:
                        valid_snaps.append(snap)
                        prev_snap = snap
                    else:
                        logger.warning("跳过异常数据 %s %s: %s",
                                       ticker, snap.get("date"), vr.errors)
                store.save_snapshots(valid_snaps)
                return "ok"
            return "no_data"
        except Exception as e:
            logger.error("Price pipeline error for %s: %s", ticker, e)
            return f"error: {e}"


async def _fetch_via_manager(ticker: str, days: int, market: str) -> list[dict]:
    """通过 FetcherManager 获取 OHLCV + 计算技术指标。返回 snapshot list。"""
    try:
        from bottleneck_hunter.data_provider import get_fetcher_manager
        mgr = get_fetcher_manager()
    except Exception as e:
        logger.debug("FetcherManager 不可用，将回退到直连: %s", e)
        return []

    df = await mgr.fetch_daily(ticker, market, days)
    if df is None or df.empty:
        return []

    if "close" not in df.columns:
        return []

    closes = df["close"].tolist()
    volumes = df["volume"].tolist() if "volume" in df.columns else [0] * len(df)
    opens = df["open"].tolist() if "open" in df.columns else closes
    highs = df["high"].tolist() if "high" in df.columns else closes
    lows = df["low"].tolist() if "low" in df.columns else closes

    rsi = _compute_rsi(closes)
    macd_result = _compute_macd(closes)
    sma_20 = _compute_sma(closes, 20)
    sma_50 = _compute_sma(closes, 50)

    fundamentals = {}
    try:
        quote = await mgr.fetch_realtime(ticker, market)
        if quote:
            fundamentals["market_cap"] = quote.market_cap
            fundamentals["pe_ratio"] = quote.pe_ratio
    except Exception:
        pass

    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    result = []
    start_idx = max(0, len(df) - days)
    for i in range(start_idx, len(df)):
        date_str = str(df.iloc[i].get("date", ""))[:10]
        prev_close = closes[i - 1] if i > 0 else closes[i]
        change_pct = ((closes[i] - prev_close) / prev_close * 100) if prev_close else 0.0
        snap = {
            "ticker": ticker,
            "date": date_str,
            "open": _safe(opens[i]),
            "high": _safe(highs[i]),
            "low": _safe(lows[i]),
            "close": _safe(closes[i]),
            "volume": int(volumes[i]) if volumes[i] else None,
            "change_pct": round(change_pct, 2),
            "fetched_at": now_iso,
        }
        if i == len(df) - 1:
            snap["rsi_14"] = rsi
            snap["sma_20"] = sma_20
            snap["sma_50"] = sma_50
            snap["market_cap"] = fundamentals.get("market_cap")
            snap["pe_ratio"] = fundamentals.get("pe_ratio")
            if macd_result:
                snap["macd"], snap["macd_signal"], snap["macd_hist"] = macd_result
        result.append(snap)

    logger.info("通过 FetcherManager 获取 %s 成功: %d 条数据", ticker, len(result))
    return result


async def fetch_price_batch(tickers: list[str], store: WatchlistStore, days: int = 180, market: str = "us_stock",
                            cache: dict | None = None) -> dict[str, str]:
    """Batch-fetch daily prices for all watchlist tickers. Returns {ticker: status}.

    cache: 传入则多用户共享同一支票的网络拉取（周期内去重，见 _fetch_one）。
    """
    if not tickers:
        return {}
    tasks = {t: asyncio.create_task(_fetch_one(t, store, days, market, cache=cache)) for t in tickers}
    results = {}
    for ticker, task in tasks.items():
        results[ticker] = await task
    return results
