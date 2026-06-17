"""财务数据拉取器：从 AKShare（A 股）和 yfinance（美股）获取真实财务数据。"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Optional

try:
    import akshare as ak
except ImportError:
    ak = None  # type: ignore[assignment]

try:
    import yfinance as yf
except ImportError:
    yf = None  # type: ignore[assignment]

from .models import FinancialSnapshot, MarketRegion, SupplierInfo

logger = logging.getLogger(__name__)

_SEMAPHORE = asyncio.Semaphore(4)


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


# ---------------------------------------------------------------------------
# A 股：AKShare (同花顺 + 东方财富)
# ---------------------------------------------------------------------------

def _fetch_astock_financial(code_6: str) -> FinancialSnapshot:
    """同步拉取 A 股财务数据。code_6 = 6位纯数字代码。"""
    snap = FinancialSnapshot(data_source="akshare_ths")

    # 1) 财务摘要 — stock_financial_abstract_ths
    try:
        df = ak.stock_financial_abstract_ths(symbol=code_6, indicator="按报告期")
        if df is not None and not df.empty:
            row = df.iloc[0]
            cols = df.columns.tolist()
            snap.report_date = str(row.iloc[0]) if len(cols) > 0 else ""

            col_map = {
                "营业总收入": "revenue",
                "归母净利润": "net_profit",
                "销售毛利率": "gross_margin_pct",
                "净资产收益率": "roe_pct",
                "资产负债率": "debt_ratio_pct",
                "每股经营现金流": "cashflow_per_share",
            }
            for col_keyword, attr in col_map.items():
                matched = [c for c in cols if col_keyword in c]
                if not matched:
                    continue
                val = row[matched[0]]
                if attr == "revenue":
                    snap.revenue_yi = _safe_float(val, 1e-8)
                elif attr == "net_profit":
                    snap.net_profit_yi = _safe_float(val, 1e-8)
                elif attr in ("gross_margin_pct", "roe_pct", "debt_ratio_pct"):
                    setattr(snap, attr, _safe_float(val))
                elif attr == "cashflow_per_share":
                    snap.cashflow_per_share = _safe_float(val)

            # 同比增速
            revenue_yoy_cols = [c for c in cols if "营业总收入" in c and "同比" in c]
            if revenue_yoy_cols:
                snap.revenue_yoy_pct = _safe_float(row[revenue_yoy_cols[0]])
            net_profit_yoy_cols = [c for c in cols if "净利润" in c and "同比" in c]
            if net_profit_yoy_cols:
                snap.net_profit_yoy_pct = _safe_float(row[net_profit_yoy_cols[0]])
    except Exception as e:
        logger.warning(f"AKShare 财务摘要获取失败 ({code_6}): {e}")

    # 2) 研报数据 — stock_research_report_em
    try:
        df_rpt = ak.stock_research_report_em(symbol=code_6)
        if df_rpt is not None and not df_rpt.empty:
            snap.analyst_report_count = min(len(df_rpt), 999)
            first = df_rpt.iloc[0]
            cols_rpt = df_rpt.columns.tolist()
            rating_cols = [c for c in cols_rpt if "评级" in c or "rating" in c.lower()]
            if rating_cols:
                snap.analyst_rating = str(first[rating_cols[0]])
    except Exception as e:
        logger.debug(f"AKShare 研报数据获取失败 ({code_6}): {e}")

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

        # 报告日期
        fiscal = info.get("mostRecentQuarter")
        if fiscal:
            snap.report_date = datetime.fromtimestamp(fiscal).strftime("%Y-%m-%d")
    except Exception as e:
        logger.warning(f"yfinance 数据获取失败 ({ticker}): {e}")

    return snap


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------

def _extract_astock_code(ticker: str) -> Optional[str]:
    """从 ticker (如 '600519.SH' 或 '688012') 中提取 6 位纯数字代码。"""
    code = ticker.split(".")[0].strip()
    if code.isdigit() and len(code) == 6:
        return code
    return None


async def fetch_financial_snapshot(supplier: SupplierInfo) -> Optional[FinancialSnapshot]:
    """为单个供应商拉取财务快照。失败返回 None。"""
    async with _SEMAPHORE:
        try:
            if supplier.market == MarketRegion.A_STOCK:
                code = _extract_astock_code(supplier.ticker)
                if not code:
                    logger.debug(f"无法提取 A 股代码: {supplier.ticker}")
                    return None
                return await asyncio.to_thread(_fetch_astock_financial, code)
            elif supplier.market == MarketRegion.US_STOCK:
                ticker = supplier.ticker.split(".")[0].strip()
                if not ticker:
                    return None
                return await asyncio.to_thread(_fetch_us_financial, ticker)
            else:
                return None
        except Exception as e:
            logger.warning(f"财务数据拉取异常 ({supplier.name}/{supplier.ticker}): {e}")
            return None


async def fetch_batch(suppliers: list[SupplierInfo]) -> dict[str, FinancialSnapshot]:
    """批量拉取。返回 {ticker: FinancialSnapshot}，失败的自动跳过。"""
    tasks = {s.ticker: fetch_financial_snapshot(s) for s in suppliers}
    results: dict[str, FinancialSnapshot] = {}
    for ticker, coro in tasks.items():
        snap = await coro
        if snap is not None:
            results[ticker] = snap
    logger.info(f"财务数据批量拉取完成: {len(results)}/{len(suppliers)} 成功")
    return results
