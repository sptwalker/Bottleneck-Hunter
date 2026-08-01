"""宏观市场数据采集模块 — VIX、美债收益率、DXY、北向资金等。

为 L1 宏观策略层提供真实宏观数据输入，替代空 macro 字典。
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

import yfinance as yf

try:
    import akshare as ak
except ImportError:
    ak = None  # type: ignore[assignment]

from bottleneck_hunter.watchlist.retry import with_retry
from bottleneck_hunter.watchlist.store import WatchlistStore

logger = logging.getLogger(__name__)


@with_retry(max_retries=2, base_delay=1.0)
def _fetch_yf_quote(symbol: str) -> dict | None:
    """从 yfinance 获取单个指标的最新价格和变动。"""
    t = yf.Ticker(symbol)
    hist = t.history(period="5d")
    if hist is None or hist.empty:
        return None
    closes = hist["Close"].tolist()
    latest = closes[-1]
    prev = closes[-2] if len(closes) >= 2 else latest
    change = ((latest - prev) / prev * 100) if prev else 0.0
    return {"value": round(latest, 4), "change_pct": round(change, 2)}


@with_retry(max_retries=2, base_delay=1.0)
def _fetch_northbound_flow() -> dict | None:
    """通过 akshare 获取北向资金净流入（最近交易日）。"""
    if ak is None:
        return None
    try:
        df = ak.stock_hsgt_north_net_flow_in_em()
        if df is None or df.empty:
            return None
        latest = df.iloc[-1]
        value = float(latest.get("value", latest.iloc[-1]))
        return {"value": round(value, 2), "change_pct": 0.0}
    except Exception as e:
        logger.debug("北向资金获取失败: %s", e)
        return None


@with_retry(max_retries=2, base_delay=1.0)
def _fetch_sge_gold() -> dict | None:
    """上海金 Au99.99 收盘价（人民币/克），akshare 国内可达，替代已停更的 FRED 伦敦金。

    价格锚是人民币/克而非美元/盎司，但用于「跨资产风险印证」看的是黄金**走势方向**，
    单位差异不影响判断；label 已注明单位避免误读。
    """
    if ak is None:
        return None
    try:
        df = ak.spot_hist_sge(symbol="Au99.99")
        if df is None or df.empty or len(df) < 1:
            return None
        latest = float(df.iloc[-1]["close"])
        prev = float(df.iloc[-2]["close"]) if len(df) >= 2 else latest
        change_pct = round((latest / prev - 1) * 100, 2) if prev else 0.0
        return {"value": round(latest, 2), "change_pct": change_pct}
    except Exception as e:
        logger.debug("上海金获取失败: %s", e)
        return None


@with_retry(max_retries=2, base_delay=1.0)
def _fetch_cn_macro() -> dict:
    """中国本土宏观：CPI同比 / M2同比 / 1年期LPR / 中债10Y / 社融增量（akshare，国内可达）。

    A股 L1 宏观口径的『本土锚』——此前 A股 的 macro 段只有美联储/美国指标(偏美股口径)，
    切到 A股 仍满屏美国数据。补上本土宏观后，A股 以本土为主、美国 FRED 退为全球外溢参考。
    逐项容错：单个接口失败不影响其余；akshare 不可用则整体返回空。
    """
    if ak is None:
        return {}
    out: dict[str, dict] = {}

    def _report_yoy(fn, key: str, label: str) -> None:
        # akshare「商品/日期/今值/预测值/前值」型月度报告：今值=最新同比%，change=今-前(百分点)
        try:
            df = fn().dropna(subset=["今值", "前值"])
            if df.empty:
                return
            last = df.iloc[-1]
            val, prev = float(last["今值"]), float(last["前值"])
            out[key] = {"value": round(val, 2), "change_pct": round(val - prev, 2),
                        "label": label, "date": str(last["日期"])}
        except Exception as e:  # noqa: BLE001
            logger.debug("%s 采集失败: %s", key, e)

    _report_yoy(ak.macro_china_cpi_yearly, "cn_cpi_yoy", "中国CPI同比(%)")
    _report_yoy(ak.macro_china_m2_yearly, "cn_m2_yoy", "中国M2同比(%)")

    try:  # LPR：TRADE_DATE / LPR1Y / LPR5Y；change 用相邻两次报价差(百分点)
        df = ak.macro_china_lpr().dropna(subset=["LPR1Y"])
        if not df.empty:
            val = float(df.iloc[-1]["LPR1Y"])
            prev = float(df.iloc[-2]["LPR1Y"]) if len(df) >= 2 else val
            out["cn_lpr_1y"] = {"value": round(val, 2), "change_pct": round(val - prev, 2),
                                "label": "中国1年期LPR(%)", "date": str(df.iloc[-1]["TRADE_DATE"])}
    except Exception as e:  # noqa: BLE001
        logger.debug("LPR 采集失败: %s", e)

    try:  # 中债10Y：bond_zh_us_rate，列「中国国债收益率10年」；change 用相邻交易日差(百分点)
        col = "中国国债收益率10年"
        df = ak.bond_zh_us_rate().dropna(subset=[col])
        if not df.empty:
            val = float(df.iloc[-1][col])
            prev = float(df.iloc[-2][col]) if len(df) >= 2 else val
            out["cn_10y_yield"] = {"value": round(val, 3), "change_pct": round(val - prev, 3),
                                   "label": "中国10Y国债收益率(%)", "date": str(df.iloc[-1]["日期"])}
    except Exception as e:  # noqa: BLE001
        logger.debug("中债10Y 采集失败: %s", e)

    try:  # 社融增量：macro_china_shrzgm，列「社会融资规模增量」(亿元)，change 用环比%
        col = "社会融资规模增量"
        df = ak.macro_china_shrzgm().dropna(subset=[col])
        if not df.empty:
            val = float(df.iloc[-1][col])
            prev = float(df.iloc[-2][col]) if len(df) >= 2 else val
            out["cn_social_financing"] = {"value": round(val, 1),
                                          "change_pct": round((val / prev - 1) * 100, 2) if prev else 0.0,
                                          "label": "中国社融增量(亿元)", "date": str(df.iloc[-1]["月份"])}
    except Exception as e:  # noqa: BLE001
        logger.debug("社融 采集失败: %s", e)

    return out


# 宏观指标定义：(显示名, yfinance 代码, 市场标签)
# 全球风险因子：VIX/美债/美元指数——各市场都合理参考（人民币/资本流动/联储外溢）
_GLOBAL_INDICATORS = [
    ("vix", "^VIX", "VIX 恐慌指数"),
    ("us_10y_yield", "^TNX", "10Y 美债收益率"),
    ("dxy", "DX-Y.NYB", "美元指数"),
]
# 美股专属股指：仅美股市场纳入，避免 sp500/nasdaq 污染 A股/港股宏观口径
_US_INDICATORS = [
    ("sp500", "^GSPC", "标普500"),
    ("nasdaq", "^IXIC", "纳斯达克综指"),
]

_CN_INDICATORS = [
    ("cny_usd", "CNY=X", "人民币汇率"),
    ("sse_index", "000001.SS", "上证综指"),
    ("csi300", "000300.SS", "沪深300"),
]

_HK_INDICATORS = [
    ("hsi", "^HSI", "恒生指数"),
    ("hstech", "^HSTECH", "恒生科技指数"),
]

# 各市场用于填充"大盘指数"的真实指数键（区别于 VIX/汇率等宏观指标）
MARKET_INDEX_KEYS: dict[str, list[str]] = {
    "us_stock": ["sp500", "nasdaq"],
    "a_stock": ["sse_index", "csi300"],
    "hk_stock": ["hsi", "hstech"],
}

# 各市场净值曲线的默认对照基准（宽基指数）。code 复用上方指数元组、不另立代码表；
# 从元组反查保证代码变更时不漂移。A股用沪深300、美股标普、港股恒指。
_INDEX_CODE_MAP: dict[str, tuple[str, str]] = {
    k: (code, label)
    for k, code, label in (_GLOBAL_INDICATORS + _US_INDICATORS + _CN_INDICATORS + _HK_INDICATORS)
}
_BENCHMARK_KEY: dict[str, str] = {"us_stock": "sp500", "a_stock": "csi300", "hk_stock": "hsi"}


# 观察池顶栏「市场主要指数」——随所选市场切换展示的指数 (key, yfinance 代码, 中文名)。
# 独立于 L1 宏观口径：这里只为顶栏展示，深成/中证500 不进 L1 宏观策略，避免污染。
_WATCH_INDEX_BAR: dict[str, list[tuple[str, str, str]]] = {
    "us_stock": [
        ("sp500", "^GSPC", "标普500"),
        ("nasdaq", "^IXIC", "纳斯达克"),
        ("vix", "^VIX", "恐慌指数VIX"),
    ],
    "a_stock": [
        ("sse_index", "000001.SS", "上证指数"),
        ("szse_component", "399001.SZ", "深证成指"),
        ("csi500", "000905.SS", "中证500"),
        ("csi300", "000300.SS", "沪深300"),
    ],
}

_index_bar_cache: dict[str, tuple[float, dict]] = {}  # market -> (取数时刻, payload)
_INDEX_BAR_TTL = 600  # 秒；指数按日更新，10 分钟缓存足够，避免每次进池都打 yfinance


async def fetch_market_indices(store: WatchlistStore, market: str = "us_stock") -> dict:
    """观察池顶栏所需的市场主要指数（随市场切换）。TTL 缓存 + 单指标快照兜底。

    返回 {"market", "updated_at", "indices": [{key,label,value,change_pct,stale,fetched_at}]}。
    yfinance 取不到（国内被墙/限流）时回落到 macro_snapshots 最近一条并标 stale。
    """
    market = market or "us_stock"
    specs = _WATCH_INDEX_BAR.get(market)
    if not specs:
        return {"market": market, "updated_at": None, "indices": []}

    now = time.time()
    hit = _index_bar_cache.get(market)
    if hit and now - hit[0] < _INDEX_BAR_TTL:
        return hit[1]

    cached = {r["indicator"]: r for r in store.get_latest_macro_snapshots()}
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")

    async def _one(key: str, symbol: str, label: str) -> dict:
        data = None
        try:
            data = await asyncio.to_thread(_fetch_yf_quote, symbol)
        except Exception as e:  # noqa: BLE001
            logger.debug("指数 %s 获取失败: %s", key, e)
        if data:
            store.save_macro_snapshot(key, today, data["value"], now_iso,
                                      change_pct=data.get("change_pct", 0.0))
            return {"key": key, "label": label, "value": data["value"],
                    "change_pct": data.get("change_pct", 0.0), "stale": False, "fetched_at": now_iso}
        row = cached.get(key)  # 实时取不到 → 最近快照兜底
        if row:
            return {"key": key, "label": label, "value": row["value"],
                    "change_pct": row.get("change_pct", 0.0) or 0.0, "stale": True,
                    "fetched_at": row.get("fetched_at") or row.get("date")}
        return {"key": key, "label": label, "value": None, "change_pct": None,
                "stale": True, "fetched_at": None}

    indices = list(await asyncio.gather(*[_one(k, s, l) for k, s, l in specs]))
    stamps = [i["fetched_at"] for i in indices if i["fetched_at"]]
    payload = {"market": market, "updated_at": max(stamps) if stamps else None, "indices": indices}
    _index_bar_cache[market] = (now, payload)
    return payload


def default_benchmark_ticker(market: str) -> tuple[str, str]:
    """返回该市场净值对照用的默认基准 (yfinance 代码, 显示名)。未知市场退美股标普。"""
    key = _BENCHMARK_KEY.get(market or "us_stock", "sp500")
    return _INDEX_CODE_MAP.get(key, ("^GSPC", "标普500"))

# 各市场「专属」宏观指标 key（_FRED_GLOBAL 为全球共享、任何市场都可用，不在此列）。
# 用于缓存兜底时剔除「他市专属」指标，避免 sp500/北向资金/美国CPI/中国LPR 等串味进另一市场的 L1 宏观口径。
_MARKET_EXCLUSIVE_KEYS: dict[str, set[str]] = {
    "us_stock": {"sp500", "nasdaq", "unemployment_rate", "cpi_yoy"},  # cpi_yoy=美国CPI(FRED)
    "a_stock": {"cny_usd", "sse_index", "csi300", "northbound_flow",
                "cn_cpi_yoy", "cn_m2_yoy", "cn_lpr_1y", "cn_10y_yield", "cn_social_financing"},
    "hk_stock": {"hsi", "hstech"},
}


def foreign_indicator_keys(markets: list[str]) -> set[str]:
    """返回不属于给定市场的『他市专属』宏观指标 key 集合（缓存兜底应剔除，防串味）。"""
    keep: set[str] = set()
    for m in markets or []:
        keep |= _MARKET_EXCLUSIVE_KEYS.get(m, set())
    all_exclusive: set[str] = set()
    for ks in _MARKET_EXCLUSIVE_KEYS.values():
        all_exclusive |= ks
    return all_exclusive - keep


# ── FRED（美联储经济数据）：真宏观经济指标，补齐 yfinance 只有行情价格的缺口 ──
# 国内服务器 yfinance(Yahoo)常被墙，而 FRED(api.stlouisfed.org)在桌面借道白名单 → 更可靠。
# 故利率/曲线/信用利差/缩表/VIX/金油全部走 FRED；VIX/10Y 与 yfinance 同 key，FRED 作兜底。
# (显示名 key, FRED series_id, 中文标签, kind)
#   kind="level"  : 取最新值 + 环比绝对变动（利率/利差/VIX/油金等价格型）
#   kind="cpi"    : 按 13 个月算同比通胀
#   kind="walcl"  : 美联储总资产（百万美元）→ 换算万亿 + 周环比%（看缩表节奏）
#
# _FRED_GLOBAL：全球风险/流动性因子（利率/曲线/缩表/美元/VIX/信用利差/油）——对各市场都有外溢，
#   全市场纳入；但语义是「全球外溢参考」而非主市场本土宏观（咨询/L1 提示词已据此标注，A股 另有本土锚）。
_FRED_GLOBAL = [
    ("fed_funds_rate", "FEDFUNDS", "美国联邦基金利率(%)", "level"),
    ("us_10y_yield", "DGS10", "10Y 美债收益率(%)", "level"),
    ("yield_curve_2s10s", "T10Y2Y", "美债2s10s利差(%,负=倒挂)", "level"),
    ("fed_balance_sheet", "WALCL", "美联储总资产(万亿$,降=缩表QT)", "walcl"),
    ("vix", "VIXCLS", "VIX 恐慌指数", "level"),
    ("hy_oas", "BAMLH0A0HYM2", "美国高收益债信用利差 HY OAS(%)", "level"),
    ("wti_oil", "DCOILWTICO", "WTI 原油($/桶)", "level"),
    ("dxy", "DTWEXBGS", "美元指数", "pct"),  # 广义贸易加权美元；yfinance DX-Y.NYB 被墙时借道 FRED 兜底
    # 黄金：FRED 的 GOLDAMGBD228NLBM 已停更(返回400)，改用 akshare 上海金(见 _fetch_sge_gold)。
]

# _FRED_US_DOMESTIC：美国本土数据（失业率/CPI）——仅美股纳入，不灌进 A股/港股主宏观口径。
_FRED_US_DOMESTIC = [
    ("unemployment_rate", "UNRATE", "美国失业率(%)", "level"),
    ("cpi_yoy", "CPIAUCSL", "美国CPI同比(%)", "cpi"),
]

# 美股大盘指数的 FRED 兜底：yfinance(^GSPC/^IXIC)被 Yahoo 限流(429)时用 FRED 补。
# 仅当 markets 含 us_stock 时才取(sp500/nasdaq 是美股专属，绝不进 A股/港股宏观口径)。
_FRED_US_EQUITY = [
    ("sp500", "SP500", "标普500", "pct"),
    ("nasdaq", "NASDAQCOM", "纳斯达克综指", "pct"),
]


async def _fred_series(key: str, series_id: str, limit: int = 1) -> list[dict]:
    # 走共享 httpx 客户端(带桌面借道 transport)：api.stlouisfed.org 在借道白名单，国内服务器可经桌面取
    from bottleneck_hunter.watchlist.retry import get_http_client
    client = get_http_client()
    r = await client.get(
        "https://api.stlouisfed.org/fred/series/observations"
        f"?series_id={series_id}&api_key={key}&file_type=json&sort_order=desc&limit={limit}",
        timeout=10, headers={"User-Agent": "BottleneckHunter/1.0"})
    r.raise_for_status()
    return [o for o in (r.json().get("observations") or []) if o.get("value") not in (None, "", ".")]


async def _fetch_fred_indicators(extra: list | None = None) -> dict:
    """拉取 FRED 关键宏观指标（利率/曲线/信用利差/缩表/VIX/金油等）。无 Key 则返回空。

    extra: 追加的 (key, series_id, label, kind) 列表（如按市场条件加的美股指数兜底）。
    """
    from bottleneck_hunter.data_provider.data_source_catalog import resolve_data_source_key
    key = resolve_data_source_key("fred")
    if not key:
        return {}
    out: dict[str, dict] = {}
    for k, series_id, label, kind in (_FRED_GLOBAL + (extra or [])):
        try:
            if kind == "cpi":
                obs = await _fred_series(key, series_id, limit=13)  # 需 13 个月算同比
                if len(obs) >= 13:
                    latest, year_ago = float(obs[0]["value"]), float(obs[12]["value"])
                    yoy = round((latest / year_ago - 1) * 100, 2) if year_ago else 0.0
                    prev_yoy = None
                    if len(obs) >= 14:
                        prev_yoy = round((float(obs[1]["value"]) / float(obs[13]["value"]) - 1) * 100, 2)
                    out[k] = {"value": yoy, "change_pct": round(yoy - prev_yoy, 2) if prev_yoy is not None else 0.0,
                              "label": label, "date": obs[0].get("date", "")}
            elif kind == "walcl":
                # 美联储总资产：原始单位百万美元 → 万亿；change_pct 用周环比%（看缩表/扩表趋势）
                obs = await _fred_series(key, series_id, limit=2)
                if obs:
                    val_m = float(obs[0]["value"])
                    prev_m = float(obs[1]["value"]) if len(obs) >= 2 else val_m
                    trillions = round(val_m / 1_000_000, 3)
                    wow = round((val_m / prev_m - 1) * 100, 2) if prev_m else 0.0
                    out[k] = {"value": trillions, "change_pct": wow, "label": label, "date": obs[0].get("date", "")}
            elif kind == "pct":  # 价格/指数型：环比百分比变动（股指等大数值，绝对点差当%会得出 -433%）
                obs = await _fred_series(key, series_id, limit=2)
                if obs:
                    val = float(obs[0]["value"])
                    prev = float(obs[1]["value"]) if len(obs) >= 2 else val
                    out[k] = {"value": round(val, 2),
                              "change_pct": round((val / prev - 1) * 100, 2) if prev else 0.0,
                              "label": label, "date": obs[0].get("date", "")}
            else:  # kind == "level"：最新值 + 环比绝对变动（利率/利差等，点差即百分点）
                obs = await _fred_series(key, series_id, limit=2)
                if obs:
                    val = float(obs[0]["value"])
                    prev = float(obs[1]["value"]) if len(obs) >= 2 else val
                    out[k] = {"value": round(val, 2), "change_pct": round(val - prev, 2),
                              "label": label, "date": obs[0].get("date", "")}
        except Exception as e:  # noqa: BLE001
            logger.warning("FRED 指标 %s 采集失败: %s", series_id, e)
    return out


async def fetch_macro_data(store: WatchlistStore, markets: list[str] | None = None) -> dict:
    """采集宏观数据并存入 macro_snapshots 表，返回整合的宏观数据字典。

    返回格式:
    {
        "vix": {"value": 18.5, "change_pct": -2.1, "label": "VIX 恐慌指数"},
        "us_10y_yield": {"value": 4.25, "change_pct": 0.5, "label": "10Y 美债收益率"},
        ...
    }
    """
    if markets is None:
        markets = ["us_stock"]

    indicators = list(_GLOBAL_INDICATORS)  # 全球风险因子各市场都取
    if "us_stock" in markets:
        indicators.extend(_US_INDICATORS)   # 美股股指仅美股纳入
    if "a_stock" in markets:
        indicators.extend(_CN_INDICATORS)
    if "hk_stock" in markets:
        indicators.extend(_HK_INDICATORS)

    results: dict[str, dict] = {}
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")

    async def _fetch_one(key: str, symbol: str, label: str):
        if key in results:
            return  # FRED 已取到该 key(可靠源) → yfinance 不再重复打(省 Yahoo 限流预算)
        try:
            data = await asyncio.to_thread(_fetch_yf_quote, symbol)
            if data:
                results[key] = {**data, "label": label}
                store.save_macro_snapshot(key, today, data["value"], now_iso,
                                          change_pct=data.get("change_pct", 0.0))
        except Exception as e:
            logger.warning("宏观指标 %s 采集失败: %s", key, e)

    tasks = [_fetch_one(key, symbol, label) for key, symbol, label in indicators]

    if "a_stock" in markets:
        async def _fetch_north():
            try:
                data = await asyncio.to_thread(_fetch_northbound_flow)
                if data:
                    results["northbound_flow"] = {**data, "label": "北向资金净流入(亿)"}
                    store.save_macro_snapshot("northbound_flow", today, data["value"], now_iso,
                                              change_pct=data.get("change_pct", 0.0))
            except Exception as e:
                logger.warning("北向资金采集失败: %s", e)
        tasks.append(_fetch_north())

        # A股 本土宏观锚（CPI/M2/LPR/中债10Y/社融）——让 A股 macro 段以本土为主，非满屏美国数据。
        # ponytail: 仅 a_stock 取；港股同受中国内地政策影响，需要时再把 cn_* 扩到 hk_stock。
        async def _fetch_cn():
            try:
                data = await asyncio.to_thread(_fetch_cn_macro)
                for k, v in data.items():
                    results[k] = v
                    store.save_macro_snapshot(k, today, v["value"], now_iso,
                                              change_pct=v.get("change_pct", 0.0))
            except Exception as e:
                logger.warning("中国本土宏观采集失败: %s", e)
        tasks.append(_fetch_cn())

    # FRED 真宏观指标 —— Fed 政策对各市场都有外溢，全局纳入；无 Key 自动跳过。
    # vix/us_10y_yield/dxy/sp500/nasdaq 与 yfinance 同 key：**FRED 优先**（借道白名单可靠，
    # 国内直连 yfinance 必失败）。故先跑 FRED 填满其覆盖的 key，yfinance 只补 FRED 没覆盖的
    # (港股恒指/A股相关等 FRED 无的)。其余(曲线/信用利差/缩表/金油等)本就 FRED 独有。
    async def _fetch_fred():
        try:
            # 美股市场额外把 美国本土(失业率/CPI) + sp500/nasdaq 纳入 FRED；
            # 非美股市场不加，避免美国本土数据/美股指数串味进 A股/港股主宏观口径。
            extra = (_FRED_US_DOMESTIC + _FRED_US_EQUITY) if "us_stock" in markets else None
            fred = await _fetch_fred_indicators(extra=extra)  # 已改异步(走共享 httpx，可借道)，不再 to_thread
            for k, v in fred.items():
                if k in results:
                    continue  # 已取到(cn_macro 等) → 不覆盖
                results[k] = v
                store.save_macro_snapshot(k, today, v["value"], now_iso,
                                          change_pct=v.get("change_pct", 0.0))
        except Exception as e:
            logger.warning("FRED 宏观指标采集失败: %s", e)
    # 先跑 FRED(可靠)填满其覆盖的 key，再并发跑 yfinance 只补 FRED 缺的，确定去重语义
    await _fetch_fred()
    await asyncio.gather(*tasks, return_exceptions=True)

    # 黄金（上海金，akshare 国内可达）——全局风险资产，各市场都参考
    async def _fetch_gold():
        try:
            data = await asyncio.to_thread(_fetch_sge_gold)
            if data:
                results["gold"] = {**data, "label": "上海金 Au99.99(¥/克)"}
                store.save_macro_snapshot("gold", today, data["value"], now_iso,
                                          change_pct=data.get("change_pct", 0.0))
        except Exception as e:
            logger.warning("黄金采集失败: %s", e)
    await _fetch_gold()

    if not results:
        foreign = foreign_indicator_keys(markets)  # 剔除他市专属指标，防缓存兜底串味
        cached = store.get_latest_macro_snapshots()
        for row in cached:
            if row["indicator"] in foreign:
                continue
            results[row["indicator"]] = {
                "value": row["value"], "change_pct": row.get("change_pct", 0.0) or 0.0,
                "label": row["indicator"],
            }

    return results
