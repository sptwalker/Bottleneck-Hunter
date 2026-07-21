"""聪明钱追踪：从 AKShare / yfinance 获取机构、内部人、资金流向等行为数据。

纯 Python 规则计算，不需要 LLM。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional

try:
    import akshare as ak
except ImportError:
    ak = None  # type: ignore[assignment]

try:
    import yfinance as yf
except ImportError:
    yf = None  # type: ignore[assignment]

from .models import MarketRegion, SmartMoneySignal, SupplierInfo

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


def _track_astock(code_6: str) -> SmartMoneySignal:
    """同步获取 A 股聪明钱信号。"""
    signal = SmartMoneySignal()
    details: list[str] = []
    score = 5.0

    # 1) 资金流向 — stock_individual_fund_flow
    try:
        df = ak.stock_individual_fund_flow(stock=code_6, market="sh" if code_6.startswith("6") else "sz")
        if df is not None and not df.empty:
            recent = df.head(5)
            flow_col = [c for c in recent.columns if "主力净流入" in c and "净额" in c]
            if flow_col:
                total_flow = sum(_safe_float(v, 1e-4) or 0 for v in recent[flow_col[0]])
                signal.fund_flow_net = round(total_flow, 2)
                if total_flow > 500:
                    score += 1.5
                    details.append(f"近5日主力净流入{total_flow:.0f}万")
                elif total_flow > 0:
                    score += 0.5
                    details.append(f"近5日主力小幅净流入")
                elif total_flow < -500:
                    score -= 1.5
                    details.append(f"近5日主力净流出{abs(total_flow):.0f}万")
                elif total_flow < 0:
                    score -= 0.5
                    details.append(f"近5日主力小幅净流出")
    except Exception as e:
        logger.debug(f"资金流向获取失败 ({code_6}): {e}")

    # 2) 融资融券余额
    try:
        exchange = "sh" if code_6.startswith("6") else "sz"
        func = ak.stock_margin_detail_sse if exchange == "sh" else ak.stock_margin_detail_szse
        df_margin = func(code=code_6)
        if df_margin is not None and len(df_margin) >= 2:
            balance_col = [c for c in df_margin.columns if "融资余额" in c]
            if balance_col:
                latest = _safe_float(df_margin.iloc[0][balance_col[0]])
                prev = _safe_float(df_margin.iloc[-1][balance_col[0]])
                if latest and prev and prev != 0:
                    change_pct = round((latest - prev) / prev * 100, 2)
                    signal.margin_balance_change = change_pct
                    if change_pct > 5:
                        score += 1.0
                        details.append(f"融资余额增长{change_pct:.1f}%")
                    elif change_pct < -5:
                        score -= 1.0
                        details.append(f"融资余额下降{abs(change_pct):.1f}%")
    except Exception as e:
        logger.debug(f"融资融券数据获取失败 ({code_6}): {e}")

    # 3) 北向资金 — stock_hsgt_individual_em（2024-08后数据可能不可用）
    try:
        df_nb = ak.stock_hsgt_individual_em(symbol=code_6)
        if df_nb is not None and not df_nb.empty:
            recent = df_nb.tail(5)
            flow_col = [c for c in recent.columns if "增持资金" in c]
            if flow_col:
                total_nb = sum(_safe_float(v, 1e-4) or 0 for v in recent[flow_col[0]])
                signal.northbound_net_buy = round(total_nb, 2)
                if total_nb > 2000:
                    score += 1.5
                    details.append(f"近5日北向净买入{total_nb:.0f}万")
                elif total_nb > 0:
                    score += 0.5
                    details.append(f"近5日北向小幅净买入")
                elif total_nb < -2000:
                    score -= 1.5
                    details.append(f"近5日北向净卖出{abs(total_nb):.0f}万")
                elif total_nb < 0:
                    score -= 0.5
                    details.append(f"近5日北向小幅净卖出")
    except Exception as e:
        logger.debug(f"北向资金数据获取失败 ({code_6}): {e}")

    # 4) 龙虎榜机构净买入 — stock_lhb_jgmmtj_em
    try:
        end_date = datetime.now().strftime("%Y%m%d")
        start_date = (datetime.now() - timedelta(days=30)).strftime("%Y%m%d")
        df_lhb = ak.stock_lhb_jgmmtj_em(start_date=start_date, end_date=end_date)
        if df_lhb is not None and not df_lhb.empty:
            code_col = [c for c in df_lhb.columns if "代码" in c]
            net_col = [c for c in df_lhb.columns if "净额" in c or "净买额" in c]
            if code_col and net_col:
                matches = df_lhb[df_lhb[code_col[0]].astype(str).str.strip() == code_6]
                if not matches.empty:
                    total_lhb = sum(_safe_float(v, 1e-4) or 0 for v in matches[net_col[0]])
                    signal.lhb_net_buy = round(total_lhb, 2)
                    if total_lhb > 0:
                        score += 1.0
                        details.append(f"近30天龙虎榜机构净买入{total_lhb:.0f}万")
                    elif total_lhb < 0:
                        score -= 0.5
                        details.append(f"近30天龙虎榜机构净卖出{abs(total_lhb):.0f}万")
    except Exception as e:
        logger.debug(f"龙虎榜数据获取失败 ({code_6}): {e}")

    signal.smart_money_score = round(min(10.0, max(0.0, score)), 1)
    signal.details = details
    signal.signal_direction = "bullish" if score >= 6.5 else "bearish" if score <= 3.5 else "neutral"

    return signal


def _track_us_stock(ticker: str) -> SmartMoneySignal:
    """同步获取美股聪明钱信号。"""
    signal = SmartMoneySignal()
    details: list[str] = []
    score = 5.0

    try:
        from bottleneck_hunter.data_provider import yf_gate
        yf_gate.throttle()  # 全局限速：聪明钱查询也均匀错峰打 Yahoo
        stock = yf.Ticker(ticker)
        info = stock.info or {}
        yf_gate.observe(None)

        # 1) 机构持仓 — 按机构数量区分
        try:
            inst = stock.institutional_holders
            if inst is not None and not inst.empty:
                n_inst = len(inst)
                signal.institution_count = n_inst
                if n_inst >= 8:
                    score += 1.0
                    details.append(f"前{n_inst}大机构重仓持有")
                elif n_inst >= 3:
                    score += 0.5
                    details.append(f"{n_inst}家机构持有")
        except Exception as e:
            logger.debug(f"机构持仓获取失败 ({ticker}): {e}")

        # 2) 内部人交易
        try:
            insider = stock.insider_transactions
            if insider is not None and not insider.empty:
                recent = insider.head(10)
                buy_count = 0
                sell_count = 0
                for _, row in recent.iterrows():
                    text = str(row.get("Text", "")).lower()
                    if "purchase" in text or "buy" in text:
                        buy_count += 1
                    elif "sale" in text or "sell" in text:
                        sell_count += 1
                net = buy_count - sell_count
                if net > 0:
                    score += 1.5
                    details.append(f"近期内部人净买入{net}笔")
                    signal.signal_direction = "bullish"
                elif net < 0:
                    score -= 1.0
                    details.append(f"近期内部人净卖出{abs(net)}笔")
        except Exception as e:
            logger.debug(f"内部人交易获取失败 ({ticker}): {e}")

        # 3) 分析师评级趋势
        try:
            recs = stock.recommendations
            if recs is not None and not recs.empty:
                recent_recs = recs.tail(5)
                grade_map = {"strong buy": 2, "buy": 1, "hold": 0, "sell": -1, "strong sell": -2}
                grades = []
                for _, row in recent_recs.iterrows():
                    grade = str(row.get("To Grade", "")).lower().strip()
                    if grade in grade_map:
                        grades.append(grade_map[grade])
                if grades:
                    avg_grade = sum(grades) / len(grades)
                    if avg_grade > 0.5:
                        score += 0.5
                        details.append("分析师整体看多")
                    elif avg_grade < -0.5:
                        score -= 0.5
                        details.append("分析师整体看空")
        except Exception as e:
            logger.debug(f"分析师评级获取失败 ({ticker}): {e}")

        # 4) 做空比例
        try:
            short_pct = info.get("shortPercentOfFloat")
            if short_pct is not None:
                signal.short_interest_pct = round(float(short_pct) * 100, 2)
                if short_pct > 0.20:
                    score -= 1.5
                    details.append(f"做空占比{signal.short_interest_pct:.1f}%，市场极度看空")
                elif short_pct > 0.10:
                    score -= 0.8
                    details.append(f"做空占比{signal.short_interest_pct:.1f}%，空头压力大")
                elif short_pct < 0.03:
                    score += 0.5
                    details.append(f"做空占比仅{signal.short_interest_pct:.1f}%，空头稀少")
        except Exception as e:
            logger.debug(f"做空比例获取失败 ({ticker}): {e}")

    except Exception as e:
        try:
            from bottleneck_hunter.data_provider import yf_gate
            yf_gate.observe(e)
        except Exception:
            pass
        logger.warning(f"yfinance 聪明钱数据获取失败 ({ticker}): {e}")

    signal.smart_money_score = round(min(10.0, max(0.0, score)), 1)
    signal.details = details
    if signal.signal_direction == "neutral":
        signal.signal_direction = "bullish" if score >= 6.5 else "bearish" if score <= 3.5 else "neutral"

    return signal


def _extract_astock_code(ticker: str) -> Optional[str]:
    # 全系统唯一 A股代码提取器（见 store_base）；容纳 600519 / 600519.SH/.SS / SH600519 等全部形态
    from bottleneck_hunter.watchlist.store_base import extract_astock_code
    return extract_astock_code(ticker)


async def track_smart_money(supplier: SupplierInfo) -> Optional[SmartMoneySignal]:
    """为单个供应商获取聪明钱信号。"""
    async with _SEMAPHORE:
        try:
            from bottleneck_hunter.data_provider.hub import CAP_SMARTMONEY, get_hub
            if supplier.market == MarketRegion.A_STOCK:
                code = _extract_astock_code(supplier.ticker)
                if not code:
                    return None
                async with get_hub().track("akshare", CAP_SMARTMONEY, "a_stock") as _sink:
                    sig = await asyncio.to_thread(_track_astock, code)
                    _sink["rows"] = 1 if sig else 0
                    return sig
            elif supplier.market == MarketRegion.US_STOCK:
                ticker = supplier.ticker.replace(".", "-").strip()  # 美股类别股 BRK.B→BRK-B，勿去后缀
                if not ticker:
                    return None
                async with get_hub().track("yfinance", CAP_SMARTMONEY, "us_stock") as _sink:
                    sig = await asyncio.to_thread(_track_us_stock, ticker)
                    _sink["rows"] = 1 if sig else 0
                    return sig
            else:
                return None
        except Exception as e:
            logger.warning(f"聪明钱数据获取异常 ({supplier.name}/{supplier.ticker}): {e}")
            return None


async def track_batch(suppliers: list[SupplierInfo]) -> tuple[dict[str, SmartMoneySignal], list[str]]:
    """批量获取聪明钱信号。返回 ({ticker: SmartMoneySignal}, [failed_tickers])，失败的自动重试一次。"""
    results: dict[str, SmartMoneySignal] = {}
    failed_suppliers: list[SupplierInfo] = []

    tasks = {s.ticker: (s, track_smart_money(s)) for s in suppliers}
    for ticker, (supplier, coro) in tasks.items():
        signal = await coro
        if signal is not None:
            results[ticker] = signal
        else:
            failed_suppliers.append(supplier)

    if failed_suppliers:
        logger.info(f"聪明钱数据重试: {len(failed_suppliers)} 个失败的 ticker")
        for s in failed_suppliers:
            signal = await track_smart_money(s)
            if signal is not None:
                results[s.ticker] = signal

    failed_tickers = [s.ticker for s in failed_suppliers if s.ticker not in results]
    logger.info(f"聪明钱数据批量获取完成: {len(results)}/{len(suppliers)} 成功, {len(failed_tickers)} 失败")
    return results, failed_tickers
