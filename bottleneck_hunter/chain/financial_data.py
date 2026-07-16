"""财务数据拉取器：从 AKShare（A 股）和 yfinance（美股）获取真实财务数据。"""

from __future__ import annotations

import asyncio
import logging
import re
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Optional

try:
    import akshare as ak
except ImportError:
    ak = None  # type: ignore[assignment]

try:
    import yfinance as yf
except ImportError:
    yf = None  # type: ignore[assignment]

from .models import FinancialSnapshot, FinancialTrend, MarketRegion, QuarterlyDataPoint, SupplierInfo

logger = logging.getLogger(__name__)

_SEMAPHORE = asyncio.Semaphore(4)


def _compute_trend(quarters: list[QuarterlyDataPoint]) -> FinancialTrend:
    """从季度数据点列表计算趋势指标。quarters 按时间降序（最新在前）。"""
    trend = FinancialTrend(quarters=quarters)
    if len(quarters) < 2:
        return trend

    rev_yoys = [q.revenue_yoy_pct for q in quarters if q.revenue_yoy_pct is not None]
    profit_yoys = [q.net_profit_yoy_pct for q in quarters if q.net_profit_yoy_pct is not None]
    gm_vals = [q.gross_margin_pct for q in quarters if q.gross_margin_pct is not None]

    def _avg(vals: list[float]) -> float:
        return sum(vals) / len(vals) if vals else 0.0

    if len(rev_yoys) >= 4:
        recent = _avg(rev_yoys[:2])
        earlier = _avg(rev_yoys[2:4])
        trend.revenue_acceleration = round(recent - earlier, 2)

    if len(profit_yoys) >= 4:
        recent = _avg(profit_yoys[:2])
        earlier = _avg(profit_yoys[2:4])
        trend.profit_acceleration = round(recent - earlier, 2)

    if len(gm_vals) >= 4:
        recent = _avg(gm_vals[:2])
        earlier = _avg(gm_vals[2:4])
        trend.gross_margin_trend = round(recent - earlier, 2)

    count = 0
    for q in quarters:
        if q.revenue_yoy_pct is not None and q.revenue_yoy_pct > 0:
            count += 1
        else:
            break
    trend.consecutive_growth_quarters = count

    parts = []
    if trend.revenue_acceleration is not None:
        direction = "加速" if trend.revenue_acceleration > 2 else "减速" if trend.revenue_acceleration < -2 else "平稳"
        parts.append(f"营收{direction}({trend.revenue_acceleration:+.1f}pp)")
    if trend.gross_margin_trend is not None:
        direction = "扩张" if trend.gross_margin_trend > 0.5 else "收缩" if trend.gross_margin_trend < -0.5 else "持平"
        parts.append(f"毛利率{direction}({trend.gross_margin_trend:+.1f}pp)")
    if trend.consecutive_growth_quarters > 0:
        parts.append(f"连续{trend.consecutive_growth_quarters}季正增长")
    trend.trend_summary = "，".join(parts) if parts else "趋势数据不足"

    return trend


def _safe_float(val, scale: float = 1.0) -> Optional[float]:
    if val is None:
        return None
    try:
        v = float(str(val).replace(",", "").replace("%", ""))
        return round(v * scale, 4)
    except (ValueError, TypeError):
        return None


def _safe_int(val) -> Optional[int]:
    if val is None:
        return None
    try:
        return int(float(str(val)))
    except (ValueError, TypeError):
        return None


def _compute_volume_metrics(
    volumes: list[float], closes: list[float],
) -> tuple[Optional[float], Optional[float], Optional[float], int]:
    """从日线数据计算成交量动量（含异常值过滤）和涨幅。

    Returns:
        (volume_ratio, price_change_3m_pct, price_change_1m_pct, consecutive_volume_days)
    """
    if len(volumes) < 20 or len(closes) < 20:
        return None, None, None, 0

    n = min(len(volumes), 60)
    vol_60d_avg = sum(volumes[-n:]) / n
    if vol_60d_avg <= 0:
        return None, None, None, 0

    recent_10 = list(volumes[-10:])
    for i in range(len(recent_10)):
        if recent_10[i] > vol_60d_avg * 10:
            recent_10[i] = vol_60d_avg * 3
    filtered_10d_avg = sum(recent_10) / len(recent_10)
    volume_ratio = round(filtered_10d_avg / vol_60d_avg, 3)

    consecutive = 0
    daily_ratios = [v / vol_60d_avg for v in volumes[-10:]]
    for i in range(len(daily_ratios) - 2):
        if daily_ratios[i] > 1.3 and daily_ratios[i + 1] > 1.3 and daily_ratios[i + 2] > 1.3:
            consecutive = 3
            break

    chg_3m = None
    if len(closes) >= 60 and closes[-60] != 0:
        chg_3m = round((closes[-1] / closes[-60] - 1) * 100, 1)
    elif closes[0] != 0:
        chg_3m = round((closes[-1] / closes[0] - 1) * 100, 1)

    chg_1m = None
    if len(closes) >= 20 and closes[-20] != 0:
        chg_1m = round((closes[-1] / closes[-20] - 1) * 100, 1)

    return volume_ratio, chg_3m, chg_1m, consecutive


# ---------------------------------------------------------------------------
# A 股实时行情：腾讯行情 API（获取 PE / 市值）
# ---------------------------------------------------------------------------

_TENCENT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
}


def _code_to_tencent(code: str) -> str:
    code = code.strip()
    if code.startswith("6"):
        return f"sh{code}"
    if code[:1] in ("4", "8") or code.startswith("920"):  # 北交所
        return f"bj{code}"
    return f"sz{code}"


def _fetch_tencent_pe_mcap(code_6: str) -> tuple[float | None, float | None, float | None]:
    """从腾讯行情获取实时 PE、总市值(亿)、现价。失败返回 (None, None, None)。"""
    symbol = _code_to_tencent(code_6)
    url = f"http://qt.gtimg.cn/q={symbol}"
    req = urllib.request.Request(url, headers=_TENCENT_HEADERS)
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        text = resp.read().decode("gbk", errors="replace")
    except Exception:
        return None, None, None

    for line in text.strip().split(";"):
        line = line.strip()
        if not line:
            continue
        m = re.match(r'v_(\w+)="(.*)"', line)
        if not m:
            continue
        fields = m.group(2).split("~")
        if len(fields) < 46 or not fields[1]:
            continue
        try:
            pe = float(fields[39]) if fields[39] else None
            total_mcap = round(float(fields[45]), 2) if fields[45] else None
            price = float(fields[3]) if fields[3] else None
        except (ValueError, IndexError):
            return None, None, None
        return pe, total_mcap, price

    return None, None, None


def _fetch_astock_consensus(code_6: str) -> tuple[float | None, int | None]:
    """同花顺机构盈利预测 → (最近预测年度一致预期EPS均值, 预测机构家数)。失败返回 (None, None)。"""
    try:
        df = ak.stock_profit_forecast_ths(symbol=code_6, indicator="预测年报每股收益")
    except Exception:
        return None, None
    if df is None or df.empty:
        return None, None
    # 列: 年度 / 预测机构家数 / 最小值 / 均值 / 最大值 / 行业平均值
    mean_col = [c for c in df.columns if c == "均值"]  # 精确匹配，避开"行业平均值"子串
    inst_col = [c for c in df.columns if "机构" in c]
    if not mean_col:
        return None, None
    row = df.iloc[0]  # 最近预测年度 = 前瞻一致预期
    eps = _safe_float(row[mean_col[0]])
    n_inst = _safe_int(row[inst_col[0]]) if inst_col else None
    return eps, n_inst


# ---------------------------------------------------------------------------
# A 股：AKShare (同花顺 + 东方财富)
# ---------------------------------------------------------------------------

def _fetch_astock_financial(code_6: str) -> FinancialSnapshot:
    """同步拉取 A 股财务数据。code_6 = 6位纯数字代码。"""
    snap = FinancialSnapshot(data_source="akshare_ths")

    # 1) 财务摘要 — stock_financial_abstract_ths（取近8个季度）
    try:
        df = ak.stock_financial_abstract_ths(symbol=code_6, indicator="按报告期")
        if df is not None and not df.empty:
            cols = df.columns.tolist()

            col_map = {
                "营业总收入": "revenue",
                "归母净利润": "net_profit",
                "销售毛利率": "gross_margin_pct",
                "净资产收益率": "roe_pct",
                "资产负债率": "debt_ratio_pct",
                "每股经营现金流": "cashflow_per_share",
            }
            revenue_yoy_cols = [c for c in cols if "营业总收入" in c and "同比" in c]
            net_profit_yoy_cols = [c for c in cols if "净利润" in c and "同比" in c]

            quarters: list[QuarterlyDataPoint] = []
            for idx, row in df.head(8).iterrows():
                qp = QuarterlyDataPoint(
                    report_date=str(row.iloc[0]) if len(cols) > 0 else "",
                )
                for col_keyword, attr in col_map.items():
                    matched = [c for c in cols if col_keyword in c]
                    if not matched:
                        continue
                    val = row[matched[0]]
                    if attr == "revenue":
                        qp.revenue_yi = _safe_float(val, 1e-8)
                    elif attr == "net_profit":
                        qp.net_profit_yi = _safe_float(val, 1e-8)
                    elif attr == "gross_margin_pct":
                        qp.gross_margin_pct = _safe_float(val)
                    elif attr == "roe_pct":
                        qp.roe_pct = _safe_float(val)

                if revenue_yoy_cols:
                    qp.revenue_yoy_pct = _safe_float(row[revenue_yoy_cols[0]])
                if net_profit_yoy_cols:
                    qp.net_profit_yoy_pct = _safe_float(row[net_profit_yoy_cols[0]])

                quarters.append(qp)

            if quarters:
                latest = quarters[0]
                snap.report_date = latest.report_date
                snap.revenue_yi = latest.revenue_yi
                snap.net_profit_yi = latest.net_profit_yi
                snap.gross_margin_pct = latest.gross_margin_pct
                snap.roe_pct = latest.roe_pct
                snap.revenue_yoy_pct = latest.revenue_yoy_pct
                snap.net_profit_yoy_pct = latest.net_profit_yoy_pct

                # 最新期同比可能为空（年报/季报首期），往前找有数据的期
                if snap.revenue_yoy_pct is None:
                    for q in quarters[1:4]:
                        if q.revenue_yoy_pct is not None:
                            snap.revenue_yoy_pct = q.revenue_yoy_pct
                            break
                if snap.net_profit_yoy_pct is None:
                    for q in quarters[1:4]:
                        if q.net_profit_yoy_pct is not None:
                            snap.net_profit_yoy_pct = q.net_profit_yoy_pct
                            break

                # 从 col_map 中提取 debt_ratio_pct / cashflow_per_share（只需最新期）
                row0 = df.iloc[0]
                debt_cols = [c for c in cols if "资产负债率" in c]
                if debt_cols:
                    snap.debt_ratio_pct = _safe_float(row0[debt_cols[0]])
                cf_cols = [c for c in cols if "每股经营现金流" in c]
                if cf_cols:
                    snap.cashflow_per_share = _safe_float(row0[cf_cols[0]])

                snap.trend = _compute_trend(quarters)

    except Exception as e:
        logger.warning(f"AKShare 财务摘要获取失败 ({code_6}): {e}")

    # 2) 研报数据 — stock_research_report_em（改为近6月去重机构数）
    try:
        df_rpt = ak.stock_research_report_em(symbol=code_6)
        if df_rpt is not None and not df_rpt.empty:
            cols_rpt = df_rpt.columns.tolist()
            date_col = [c for c in cols_rpt if "日期" in c]
            inst_col = [c for c in cols_rpt if "机构" in c]
            if date_col and inst_col:
                cutoff = (datetime.now() - timedelta(days=180)).strftime("%Y-%m-%d")
                df_rpt["_date_str"] = df_rpt[date_col[0]].astype(str)
                recent = df_rpt[df_rpt["_date_str"] >= cutoff]
                snap.analyst_report_count = int(recent[inst_col[0]].nunique()) if not recent.empty else 0
            else:
                snap.analyst_report_count = min(len(df_rpt), 999)
            first = df_rpt.iloc[0]
            rating_cols = [c for c in cols_rpt if "评级" in c or "rating" in c.lower()]
            if rating_cols:
                snap.analyst_rating = str(first[rating_cols[0]])
    except Exception as e:
        logger.debug(f"AKShare 研报数据获取失败 ({code_6}): {e}")

    # 3) 日线数据 → 成交量动量 + 涨幅
    try:
        df_hist = ak.stock_zh_a_hist(
            symbol=code_6, period="daily",
            start_date=(datetime.now() - timedelta(days=180)).strftime("%Y%m%d"),
            end_date=datetime.now().strftime("%Y%m%d"),
            adjust="qfq",
        )
        if df_hist is not None and len(df_hist) >= 20:
            vols = df_hist["成交量"].tolist()
            cls_prices = df_hist["收盘"].tolist()
            vr, c3m, c1m, consec = _compute_volume_metrics(vols, cls_prices)
            snap.volume_ratio = vr
            snap.price_change_3m_pct = c3m
            snap.price_change_1m_pct = c1m
            snap.consecutive_volume_days = consec
    except Exception as e:
        logger.debug(f"AKShare 日线数据获取失败 ({code_6}): {e}")

    # 4) 一致预期 — 同花顺机构盈利预测（真·一致预期EPS）+ 腾讯现价 → forward PE
    #    旧逻辑用腾讯实时 TTM PE 冒充一致预期，已废弃。无预测覆盖时留空（不回退假数据）。
    try:
        eps_cons, _n_inst = _fetch_astock_consensus(code_6)
        _pe, _mcap, price = _fetch_tencent_pe_mcap(code_6)
        if eps_cons is not None and eps_cons > 0:
            snap.consensus_eps = eps_cons
            if price is not None and price > 0:
                snap.consensus_pe = round(price / eps_cons, 2)
    except Exception as e:
        logger.debug(f"A股一致预期获取失败 ({code_6}): {e}")

    return snap


# ---------------------------------------------------------------------------
# 美股：yfinance
# ---------------------------------------------------------------------------

def _fetch_us_financial(ticker: str) -> FinancialSnapshot:
    """同步拉取美股财务数据。"""
    snap = FinancialSnapshot(data_source="yfinance")

    try:
        stock = yf.Ticker(ticker)
        info = stock.info or {}

        snap.revenue_yi = _safe_float(info.get("totalRevenue"), 1e-8)
        snap.revenue_yoy_pct = _safe_float(info.get("revenueGrowth"), 100)
        snap.net_profit_yi = _safe_float(info.get("netIncomeToCommon"), 1e-8)
        snap.gross_margin_pct = _safe_float(info.get("grossMargins"), 100)
        snap.roe_pct = _safe_float(info.get("returnOnEquity"), 100)
        snap.debt_ratio_pct = _safe_float(info.get("debtToEquity"))
        snap.cashflow_per_share = _safe_float(info.get("operatingCashflow"))

        # 机构持仓
        inst_pct = info.get("heldPercentInstitutions")
        if inst_pct is not None:
            snap.institution_holding_pct = round(float(inst_pct) * 100, 2)

        # IPO 日期
        ipo_ts = info.get("firstTradeDateEpochUtc")
        if ipo_ts:
            snap.days_since_ipo = (datetime.now(timezone.utc) - datetime.fromtimestamp(ipo_ts, timezone.utc)).days

        eps_fwd = info.get("forwardEps")
        pe_fwd = info.get("forwardPE")
        if eps_fwd is not None:
            snap.consensus_eps = _safe_float(eps_fwd)
        if pe_fwd is not None:
            snap.consensus_pe = _safe_float(pe_fwd)

        rec = info.get("recommendationKey")
        if rec:
            snap.analyst_rating = rec
        n_analysts = info.get("numberOfAnalystOpinions")
        if n_analysts is not None:
            snap.analyst_report_count = _safe_int(n_analysts)

        fiscal = info.get("mostRecentQuarter")
        if fiscal:
            snap.report_date = datetime.fromtimestamp(fiscal).strftime("%Y-%m-%d")

        # 多季度趋势数据
        try:
            qf = stock.quarterly_financials
            qi = stock.quarterly_income_stmt
            if qf is not None and not qf.empty:
                quarters: list[QuarterlyDataPoint] = []
                for col_date in list(qf.columns)[:8]:
                    qp = QuarterlyDataPoint(
                        report_date=col_date.strftime("%Y-%m-%d") if hasattr(col_date, "strftime") else str(col_date),
                    )
                    rev = qf.at["Total Revenue", col_date] if "Total Revenue" in qf.index else None
                    qp.revenue_yi = _safe_float(rev, 1e-8)
                    ni = qf.at["Net Income", col_date] if "Net Income" in qf.index else None
                    qp.net_profit_yi = _safe_float(ni, 1e-8)
                    gp = qf.at["Gross Profit", col_date] if "Gross Profit" in qf.index else None
                    if rev and gp and float(str(rev)) != 0:
                        qp.gross_margin_pct = round(float(str(gp)) / float(str(rev)) * 100, 2)
                    quarters.append(qp)

                # 计算 YoY（需要至少5季度：当前+4季度前）
                for i, qp in enumerate(quarters):
                    if i + 4 < len(quarters):
                        prev = quarters[i + 4]
                        if qp.revenue_yi and prev.revenue_yi and prev.revenue_yi != 0:
                            qp.revenue_yoy_pct = round((qp.revenue_yi / prev.revenue_yi - 1) * 100, 2)
                        if qp.net_profit_yi and prev.net_profit_yi and prev.net_profit_yi != 0:
                            qp.net_profit_yoy_pct = round((qp.net_profit_yi / prev.net_profit_yi - 1) * 100, 2)

                if quarters:
                    snap.trend = _compute_trend(quarters)
        except Exception as e:
            logger.debug(f"yfinance 季度数据获取失败 ({ticker}): {e}")

        # 日线数据 → 成交量动量 + 涨幅
        try:
            hist = stock.history(period="6mo")
            if hist is not None and len(hist) >= 20:
                vols = hist["Volume"].tolist()
                cls_prices = hist["Close"].tolist()
                vr, c3m, c1m, consec = _compute_volume_metrics(vols, cls_prices)
                snap.volume_ratio = vr
                snap.price_change_3m_pct = c3m
                snap.price_change_1m_pct = c1m
                snap.consecutive_volume_days = consec
        except Exception as e:
            logger.debug(f"yfinance 日线数据获取失败 ({ticker}): {e}")

    except Exception as e:
        logger.warning(f"yfinance 数据获取失败 ({ticker}): {e}")

    return snap


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------

def _extract_astock_code(ticker: str) -> Optional[str]:
    """从 ticker (如 '600519.SH' 或 '688012') 中提取 6 位纯数字代码。"""
    # 全系统唯一 A股代码提取器（见 store_base）；容纳 600519 / 600519.SH/.SS / SH600519 等全部形态
    from bottleneck_hunter.watchlist.store_base import extract_astock_code
    return extract_astock_code(ticker)


def _overlay_hub_financials(base: FinancialSnapshot, rec: dict) -> None:
    """把 hub 多源深财务/真一致预期覆盖到免费基线（仅覆盖非空字段，保留基线的量价/IPO）。"""
    for field in ("revenue_yi", "revenue_yoy_pct", "net_profit_yi", "net_profit_yoy_pct",
                  "gross_margin_pct", "roe_pct", "debt_ratio_pct", "cashflow_per_share",
                  "consensus_eps", "consensus_pe", "analyst_rating", "analyst_report_count",
                  "report_date"):
        val = rec.get(field)
        if val is not None and val != "":
            setattr(base, field, val)
    qs = rec.get("quarters") or []
    if qs:
        pts = [QuarterlyDataPoint(**{k: q.get(k) for k in (
            "report_date", "revenue_yi", "net_profit_yi", "gross_margin_pct",
            "roe_pct", "revenue_yoy_pct", "net_profit_yoy_pct") if q.get(k) is not None}) for q in qs]
        if pts:
            base.trend = _compute_trend(pts)
    src = rec.get("data_source")
    if src and src not in (base.data_source or ""):
        base.data_source = f"{base.data_source}+{src}" if base.data_source else src


async def fetch_financial_snapshot(supplier: SupplierInfo, user_id: str = "") -> Optional[FinancialSnapshot]:
    """为单个供应商拉取财务快照。失败返回 None。

    免费直连做基线（含量价/IPO/A股一致预期），再用 DataHub 多源（FMP/Tiingo/AV/Tushare）
    覆盖深财务/真一致预期——无 key 时 provider 立即返 None（不发请求），有 key 才生效。
    """
    async with _SEMAPHORE:
        try:
            if supplier.market == MarketRegion.A_STOCK:
                code = _extract_astock_code(supplier.ticker)
                if not code:
                    logger.debug(f"无法提取 A 股代码: {supplier.ticker}")
                    return None
                base = await asyncio.to_thread(_fetch_astock_financial, code)
                tk, market = code, "a_stock"
            elif supplier.market == MarketRegion.US_STOCK:
                tk = supplier.ticker.replace(".", "-").strip()  # 美股类别股 BRK.B→BRK-B（yfinance 约定），勿去后缀
                if not tk:
                    return None
                base = await asyncio.to_thread(_fetch_us_financial, tk)
                market = "us_stock"
            else:
                return None

            try:
                from bottleneck_hunter.data_provider.hub import CAP_FINANCIALS, get_hub
                rec = await get_hub().fetch(CAP_FINANCIALS, tk, market, user_id)
                if rec:
                    _overlay_hub_financials(base, rec)
            except Exception as e:  # noqa: BLE001
                logger.debug(f"DataHub 深财务覆盖跳过 ({supplier.ticker}): {e}")

            return base
        except Exception as e:
            logger.warning(f"财务数据拉取异常 ({supplier.name}/{supplier.ticker}): {e}")
            return None


async def fetch_batch(suppliers: list[SupplierInfo], user_id: str = "") -> tuple[dict[str, FinancialSnapshot], list[str]]:
    """批量拉取。返回 ({ticker: FinancialSnapshot}, [failed_tickers])，失败的自动重试一次。

    user_id 透传给 hub.fetch，用本用户自己的付费 key，避免跨用户借用（D-1）。
    """
    results: dict[str, FinancialSnapshot] = {}
    failed_suppliers: list[SupplierInfo] = []

    # 真并发：fetch_financial_snapshot 内部 _SEMAPHORE(4) 负责限流（此前串行 await 使信号量形同虚设）
    snaps = await asyncio.gather(*[fetch_financial_snapshot(s, user_id) for s in suppliers])
    for supplier, snap in zip(suppliers, snaps):
        if snap is not None:
            results[supplier.ticker] = snap
        else:
            failed_suppliers.append(supplier)

    if failed_suppliers:
        logger.info(f"财务数据重试: {len(failed_suppliers)} 个失败的 ticker")
        retries = await asyncio.gather(*[fetch_financial_snapshot(s, user_id) for s in failed_suppliers])
        for s, snap in zip(failed_suppliers, retries):
            if snap is not None:
                results[s.ticker] = snap

    failed_tickers = [s.ticker for s in failed_suppliers if s.ticker not in results]
    logger.info(f"财务数据批量拉取完成: {len(results)}/{len(suppliers)} 成功, {len(failed_tickers)} 失败")
    return results, failed_tickers


# ---------------------------------------------------------------------------
# K 线数据
# ---------------------------------------------------------------------------

def _fetch_astock_kline(code_6: str, days: int = 365) -> list[dict]:
    if ak is None:
        return []
    df = ak.stock_zh_a_hist(
        symbol=code_6, period="daily",
        start_date=(datetime.now() - timedelta(days=days)).strftime("%Y%m%d"),
        end_date=datetime.now().strftime("%Y%m%d"),
        adjust="qfq",
    )
    if df is None or df.empty:
        return []
    rows = []
    for _, r in df.iterrows():
        rows.append({
            "date": str(r["日期"])[:10],
            "open": round(float(r["开盘"]), 2),
            "high": round(float(r["最高"]), 2),
            "low": round(float(r["最低"]), 2),
            "close": round(float(r["收盘"]), 2),
            "volume": int(r["成交量"]),
        })
    return rows


def _fetch_us_kline(ticker: str, period: str = "1y") -> list[dict]:
    if yf is None:
        return []
    stock = yf.Ticker(ticker)
    hist = stock.history(period=period)
    if hist is None or hist.empty:
        return []
    rows = []
    for date, r in hist.iterrows():
        rows.append({
            "date": str(date.date()),
            "open": round(float(r["Open"]), 2),
            "high": round(float(r["High"]), 2),
            "low": round(float(r["Low"]), 2),
            "close": round(float(r["Close"]), 2),
            "volume": int(r["Volume"]),
        })
    return rows


async def fetch_kline(ticker: str, market: str = "us_stock") -> list[dict]:
    """获取近一年 K 线 OHLCV 数据。优先通过 FetcherManager 自动降级。"""
    # 优先通过 FetcherManager（自动降级）
    try:
        from bottleneck_hunter.data_provider import get_fetcher_manager
        mgr = get_fetcher_manager()
        df = await mgr.fetch_daily(ticker, market, 365)
        if df is not None and not df.empty and "close" in df.columns:
            rows = []
            for _, r in df.iterrows():
                rows.append({
                    "date": str(r.get("date", ""))[:10],
                    "open": round(float(r.get("open", 0)), 2),
                    "high": round(float(r.get("high", 0)), 2),
                    "low": round(float(r.get("low", 0)), 2),
                    "close": round(float(r.get("close", 0)), 2),
                    "volume": int(r.get("volume", 0)),
                })
            return rows
    except Exception as e:
        logger.debug(f"FetcherManager K线获取失败 ({ticker}): {e}")

    # 后备：原有直连逻辑
    try:
        if market == "a_stock":
            code = _extract_astock_code(ticker)
            if not code:
                return []
            return await asyncio.to_thread(_fetch_astock_kline, code)
        else:
            t = ticker.replace(".", "-").strip()  # 美股类别股 BRK.B→BRK-B，勿去后缀
            if not t:
                return []
            return await asyncio.to_thread(_fetch_us_kline, t)
    except Exception as e:
        logger.warning(f"K线数据获取失败 ({ticker}): {e}")
        return []
