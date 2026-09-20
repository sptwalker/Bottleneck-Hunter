"""聪明钱追踪：从 AKShare / yfinance 获取机构、内部人、资金流向等行为数据。

纯 Python 规则计算，不需要 LLM。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

try:
    import akshare as ak
except ImportError:
    ak = None  # type: ignore[assignment]

try:
    import yfinance as yf
except ImportError:
    yf = None  # type: ignore[assignment]

from . import fetch_budget
from .models import MarketRegion, SmartMoneySignal, SupplierInfo

logger = logging.getLogger(__name__)

_SEMAPHORE = asyncio.Semaphore(4)


def _safe_float(val, scale: float = 1.0) -> float | None:
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

    # 1) 资金流向 — efinance 东财主力净流入优先（akshare 口径 2024 后不稳），失败回退 akshare，
    #    二者皆不可达（机房被墙）时用 Gangtise 兜底
    total_flow = None
    try:
        from bottleneck_hunter.data_provider.efinance_astock import _fetch_history_bill_sync
        mf = _fetch_history_bill_sync(code_6, days=5)
        if mf is not None:
            total_flow = mf["main_net_wan"]
    except Exception as e:
        logger.debug(f"efinance 资金流获取失败 ({code_6}): {e}")
    if total_flow is None:
        try:
            df = ak.stock_individual_fund_flow(stock=code_6, market="sh" if code_6.startswith("6") else "sz")
            if df is not None and not df.empty:
                recent = df.head(5)
                flow_col = [c for c in recent.columns if "主力净流入" in c and "净额" in c]
                if flow_col:
                    total_flow = sum(_safe_float(v, 1e-4) or 0 for v in recent[flow_col[0]])
        except Exception as e:
            logger.debug(f"资金流向获取失败 ({code_6}): {e}")
    if total_flow is None:
        # efinance/akshare 皆不可达（如生产机房被墙）→ Gangtise 兜底（近5日主力净流入，元→万）
        try:
            from bottleneck_hunter.data_provider.data_source_catalog import resolve_gangtise_credentials
            from bottleneck_hunter.data_provider.gangtise_client import fetch_fund_flow
            creds = resolve_gangtise_credentials()
            if creds:
                end = datetime.now()
                start = end - timedelta(days=12)  # 覆盖≥5个交易日（含周末/假日冗余）
                rows = fetch_fund_flow(creds[0], creds[1], code_6,
                                       start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))
                recent = [r for r in rows if r.get("main_net") is not None][-5:]
                if recent:
                    total_flow = sum(r["main_net"] for r in recent) / 1e4  # 元 → 万
        except Exception as e:
            logger.debug(f"Gangtise 资金流兜底失败 ({code_6}): {e}")
    if total_flow is not None:
        signal.fund_flow_net = round(total_flow, 2)
        if total_flow > 500:
            score += 1.5
            details.append(f"近5日主力净流入{total_flow:.0f}万")
        elif total_flow > 0:
            score += 0.5
            details.append("近5日主力小幅净流入")
        elif total_flow < -500:
            score -= 1.5
            details.append(f"近5日主力净流出{abs(total_flow):.0f}万")
        elif total_flow < 0:
            score -= 0.5
            details.append("近5日主力小幅净流出")

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
                    details.append("近5日北向小幅净买入")
                elif total_nb < -2000:
                    score -= 1.5
                    details.append(f"近5日北向净卖出{abs(total_nb):.0f}万")
                elif total_nb < 0:
                    score -= 0.5
                    details.append("近5日北向小幅净卖出")
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


def _load_us_smart_money_bundle(ticker: str, stock, info: dict) -> dict:
    """取聪明钱链所需的 Yahoo 数据（`.info` 由调用方经共享缓存传入，不在此重复取）。

    此前机构持仓/内部人交易/分析师评级/`.info` 四处在一个 `throttle()` 槽位里连打，
    且 `.info` 与财务链重复。现合并为「一次取数 + 轮内共享」：同一只票只产生 1 次请求，
    两条链各自命中缓存。分项独立 try：某一项失败只丢那一项（与改动前容错语义一致）。
    """
    bundle: dict = {"info": info, "inst": None, "insider": None, "recs": None}

    def _grab(key: str, fn, label: str) -> None:
        try:
            bundle[key] = fn()
        except Exception as e:  # noqa: BLE001
            logger.debug(f"{label}获取失败 ({ticker}): {e}")

    _grab("inst", lambda: stock.institutional_holders, "机构持仓")
    _grab("insider", lambda: stock.insider_transactions, "内部人交易")
    _grab("recs", lambda: stock.recommendations, "分析师评级")
    return bundle


def _track_us_stock(ticker: str) -> SmartMoneySignal | None:
    """同步获取美股聪明钱信号。**一次取数都没成功则返回 None**，交给批量层计入失败。

    ponytail: 与 financial_data._fetch_us_financial 同一处伪装——外层 `except` 兜住 429 后
    照样 `return signal`（`smart_money_score=5.0` 的中性壳），于是「253 家只成功 85 家」在
    日志里记成「85/253 成功, 0 失败」。中性 5.0 与「真的没信号」不可区分，会用假数据参与评分。
    """
    signal = SmartMoneySignal()
    details: list[str] = []
    score = 5.0
    got_anything = False

    try:
        from bottleneck_hunter.chain.quotes_cache import get as _cache_get
        from bottleneck_hunter.data_provider import yf_gate
        yf_gate.throttle()  # 全局限速：聪明钱查询也均匀错峰打 Yahoo
        stock = yf.Ticker(ticker)
        # us_info 与财务链共用同一缓存键——两条链并行跑同一批 ticker，各打一遍等于凭空翻倍等待
        info = _cache_get("us_info", ticker, lambda: stock.info or {}) or {}
        bundle = _cache_get("us_smart_money", ticker, lambda: _load_us_smart_money_bundle(ticker, stock, info))
        yf_gate.observe(None)
        got_anything = True

        # 1) 机构持仓 — 按机构数量区分
        try:
            inst = bundle.get("inst")
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
            insider = bundle.get("insider")
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
            recs = bundle.get("recs")
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
        # 与财务链共用同一份降级计数：两条链各失败一次就记两次，分母（attempted）只数财务链发起数，
        # 所以取数全灭时比率可以 >1——这是有意的，它表达的是「这一轮有多少次取数是白跑的」。
        try:
            from bottleneck_hunter.chain.financial_data import _note_yf_degraded
            _note_yf_degraded(ticker, f"聪明钱取数失败: {e}")
        except Exception:  # noqa: BLE001
            pass
        return None  # 空壳中性分不许冒充成功——那正是「0 失败」的来源

    signal.smart_money_score = round(min(10.0, max(0.0, score)), 1)
    signal.details = details
    if signal.signal_direction == "neutral":
        signal.signal_direction = "bullish" if score >= 6.5 else "bearish" if score <= 3.5 else "neutral"

    return signal if got_anything else None


def _extract_astock_code(ticker: str) -> str | None:
    # 全系统唯一 A股代码提取器（见 store_base）；容纳 600519 / 600519.SH/.SS / SH600519 等全部形态
    from bottleneck_hunter.watchlist.store_base import extract_astock_code
    return extract_astock_code(ticker)


async def track_smart_money(supplier: SupplierInfo) -> SmartMoneySignal | None:
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


def _global_rate_limited_skip() -> bool:
    """闸门已熔断 → 本次不再打 Yahoo（快速降级为「无数据」，而非睡 30s 再失败）。"""
    try:
        from bottleneck_hunter.data_provider import yf_gate
        return yf_gate.is_tripped()
    except Exception:  # noqa: BLE001
        return False


async def _budgeted_track(supplier: SupplierInfo) -> SmartMoneySignal | None:
    """带总预算的单票取数：预算用尽则不再发起，直接返回 None（计入失败）。

    与 financial_data._budgeted_fetch 同构——两条链共用同一个 Phase 2 取数预算，
    谁先到点谁停，余下时间留给评估环节而不是继续撞 Yahoo。
    """
    if fetch_budget.expired():
        return None
    return await track_smart_money(supplier)


async def track_batch(suppliers: list[SupplierInfo]) -> tuple[dict[str, SmartMoneySignal], list[str]]:
    """批量获取聪明钱信号。返回 ({ticker: SmartMoneySignal}, [failed_tickers])，失败的自动重试一次。

    ponytail: 原实现把全部协程建好后**串行 await**，名字叫 batch 实则零并发（还触发
    "coroutine was never awaited" 警告）；改为 asyncio.gather 真并发，限流交给
    track_smart_money 内的 _SEMAPHORE(4) 与 yf_gate 全局节流。第二轮重试照旧，但闸门熔断
    或取数预算用尽（`fetch_budget`）时跳过——那时重试只是把注定失败的一轮再打一遍。
    """
    results: dict[str, SmartMoneySignal] = {}
    failed_suppliers: list[SupplierInfo] = []

    sigs = await asyncio.gather(*[_budgeted_track(s) for s in suppliers])
    for supplier, sig in zip(suppliers, sigs, strict=True):
        if sig is not None:
            results[supplier.ticker] = sig
        else:
            failed_suppliers.append(supplier)
    budget_hit = fetch_budget.expired()

    if failed_suppliers:
        if budget_hit:
            logger.warning(
                f"聪明钱数据跳过重试: 取数总预算用尽，{len(failed_suppliers)} 个 ticker 直接降级为无数据"
            )
        elif _global_rate_limited_skip():
            logger.warning(
                f"聪明钱数据跳过重试: Yahoo 限流熔断中，{len(failed_suppliers)} 个 ticker 直接降级为无数据"
            )
        else:
            logger.info(f"聪明钱数据重试: {len(failed_suppliers)} 个失败的 ticker")
            retries = await asyncio.gather(*[track_smart_money(s) for s in failed_suppliers])
            for s, sig in zip(failed_suppliers, retries, strict=True):
                if sig is not None:
                    results[s.ticker] = sig

    failed_tickers = [s.ticker for s in failed_suppliers if s.ticker not in results]
    logger.info(f"聪明钱数据批量获取完成: {len(results)}/{len(suppliers)} 成功, {len(failed_tickers)} 失败")
    return results, failed_tickers
