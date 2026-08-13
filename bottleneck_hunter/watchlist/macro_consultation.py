"""L1 宏观咨询互动 — 两位分析师（宏观市场 / 产业动向）流式多轮对话。

复用 L1_macro 的两个模型分饰两角，每市场一条滚动会话（meeting_records，
meeting_type="macro_consult"）。用户提问 → round1 各自独立作答 → round2 互评辩论。
超两周历史消息 UI 折叠 + 由 LLM 压成滚动摘要留在上下文（_maybe_compress）。

纯咨询、只读不回写 —— 不改动已生成的 L1 策略。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from bottleneck_hunter.llm_clients.factory import get_models_for_role
from bottleneck_hunter.watchlist.budget import BudgetTracker

# 复用 decision_engine 的工具：_sse 会把 event 名同时写进 data（前端 dcSSE 依赖此约定）
from bottleneck_hunter.watchlist.decision_engine import (
    _collect_market_context,
    _get_market_context_text,
    _inject_market_news,
    _load_prompt,
    _sse,
)
from bottleneck_hunter.watchlist.models import DegradationMode
from bottleneck_hunter.watchlist.store import WatchlistStore
from bottleneck_hunter.watchlist.store_base import normalize_market

logger = logging.getLogger(__name__)

MEETING_TYPE = "macro_consult"
FOLD_DAYS = 14          # 超此天数的对话消息在 UI 折叠并进入滚动摘要
SUMMARY_TRIGGER = 40    # 待摘消息数阈值：低于此不触发压缩，避免每轮烧钱
MAX_RECENT = 20         # 拼进 prompt 上下文的最近未折叠消息条数
CONTENT_CAP = 800       # 单条消息拼进上下文时的截断长度

ANALYSTS = [
    {"slot": 0, "role": "macro_market",   "label": "🌐 宏观市场分析师", "prompt": "macro_consult_market"},
    {"slot": 1, "role": "industry_trend", "label": "🏭 产业动向分析师", "prompt": "macro_consult_industry"},
]

# snapshot 顶部的「本次咨询主市场」标注：点明宏观段哪些是本土锚、哪些是全球外溢参考，
# 防止切到 A股/港股 后分析师仍把美联储/美国CPI 当本土基本面主线解读。
_SNAPSHOT_MARKET_NOTE = {
    "a_stock": "本次咨询主市场：A股。宏观段以中国本土指标(中国CPI/M2/1年LPR/社融/中债10Y)为主锚；"
               "美联储利率/美债/VIX/美元指数等为『全球外溢参考』，勿当作 A股 本土基本面主线。",
    "us_stock": "本次咨询主市场：美股。宏观段以美国本土指标(联储利率/美国CPI/失业率/美债曲线/VIX)为主线。",
    "hk_stock": "本次咨询主市场：港股。宏观段兼看美联储(联系汇率下利率同步)与中国内地政策外溢，两头均为外部驱动。",
}


# ── 小工具 ────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _iso_days_ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")


def _analyst_llm(models: list[tuple], slot: int) -> tuple:
    """slot 取模回绕：L1_macro 只配 1 个模型时两位分析师复用同一 llm 分饰两角。"""
    return models[slot % len(models)]


def _load_session(store: WatchlistStore, market: str) -> dict | None:
    """取该市场的滚动会话（最新一条）。transcript_json/result_json 已被 store 解析。"""
    recs = store.get_meeting_records(meeting_type=MEETING_TYPE, market=market, limit=1)
    return recs[0] if recs else None


def snapshot_is_stale(store: WatchlistStore, market: str, session: dict | None) -> bool:
    """新闻库是否已有比该会话最后一个快照更新的市场新闻（用于决定是否需要重开生成）。"""
    if not session:
        return True
    snaps = [m for m in (session.get("transcript_json") or []) if m.get("type") == "snapshot"]
    if not snaps:
        return True
    def _mx(items):
        return max((n.get("date", "") for n in (items or [])), default="")
    try:
        from bottleneck_hunter.watchlist.news_pipeline import market_sentinel
        db_latest = _mx(store.get_news(market_sentinel(market), limit=15))
        return bool(db_latest) and db_latest > _mx(snaps[-1].get("news"))
    except Exception:  # noqa: BLE001
        return False


def _snapshot_entry(market_ctx: dict, strategy: dict | None) -> dict:
    """把 L1 数据快照 + 当前策略结论组装成一条 snapshot transcript 条目。"""
    rj = (strategy or {}).get("result_json") or {}
    if not isinstance(rj, dict):
        rj = {}
    strat = {}
    if strategy:
        strat = {
            "regime": rj.get("regime", ""),
            "risk_appetite": rj.get("risk_appetite", ""),
            "market_summary": rj.get("market_summary", ""),
            "strategy_text": rj.get("strategy_text", ""),
        }
    return {
        "type": "snapshot", "ts": _now_iso(),
        "market": market_ctx.get("markets", []),   # 供 _snapshot_text 标注主市场/外溢参考
        "indices": market_ctx.get("indices", {}),
        "sentiment": market_ctx.get("sentiment", {}),
        "macro": market_ctx.get("macro", {}),
        "sectors": market_ctx.get("sectors", {}),
        "positioning": market_ctx.get("positioning", {}),  # 期权PCR/机构13F 观察池聚合(美股有/他市空)
        "news": (market_ctx.get("news") or [])[:15],
        "strategy": strat,
    }


def _snapshot_text(snap: dict) -> str:
    """把 snapshot 渲染成喂给 LLM 的紧凑文本。"""
    import json
    parts = []
    note = next((_SNAPSHOT_MARKET_NOTE[m] for m in (snap.get("market") or [])
                 if m in _SNAPSHOT_MARKET_NOTE), "")
    if note:
        parts.append(f"【{note}】")
    # 数据口径与时点(诚实标注)：快照生成时刻 + 广度口径 + 各宏观指标自带 date=其数据时点。
    sent = snap.get("sentiment", {}) or {}
    breadth = ""
    if sent.get("stocks_total"):
        breadth = (f"；广度 stocks_above_sma50={sent.get('stocks_above_sma50')}/"
                   f"{sent.get('stocks_total')} 为**观察池**口径(非全市场广度)")
    parts.append(f"【数据口径：快照生成于 {snap.get('ts', '')[:16]}(北京展示另计)；价格为最近收盘；"
                 f"宏观各指标 date 字段即其数据时点(月频如CPI/PCE会滞后){breadth}】")
    parts += [
        f"大盘指数: {json.dumps(snap.get('indices', {}), ensure_ascii=False)}",
        f"市场情绪(含VIX): {json.dumps(sent, ensure_ascii=False)}",
        f"宏观(利率/汇率/通胀分项/就业等): {json.dumps(snap.get('macro', {}), ensure_ascii=False)}",
        f"板块表现(观察池聚合): {json.dumps(snap.get('sectors', {}), ensure_ascii=False)}",
    ]
    pos = snap.get("positioning") or {}
    if pos:
        parts.append(f"市场结构/持仓定位(观察池聚合·期权PCR+机构13F): {json.dumps(pos, ensure_ascii=False)}")
    parts.append(f"市场近期新闻: {json.dumps(snap.get('news', []), ensure_ascii=False)}")
    st = snap.get("strategy") or {}
    if st:
        parts.append(f"当前L1策略结论: regime={st.get('regime')} / 风险偏好={st.get('risk_appetite')}"
                     f" / 摘要={st.get('market_summary')}")
    wl = snap.get("watchlist") or []
    if wl:
        parts.append(f"用户观察池({len(wl)}只): {json.dumps(wl, ensure_ascii=False)}")
    pos = snap.get("positions")
    if pos:
        parts.append(f"用户当前持仓: {json.dumps(pos, ensure_ascii=False)}")
    elif pos is not None:
        parts.append("用户当前持仓: 空仓")
    return "\n".join(parts)


def _latest_snapshot_text(transcript: list) -> str:
    snaps = [m for m in transcript if m.get("type") == "snapshot"]
    return _snapshot_text(snaps[-1]) if snaps else "（暂无市场数据快照）"


def _context_for_prompt(transcript: list, max_recent: int = MAX_RECENT) -> str:
    """拼上下文：最后一条滚动摘要 + 最近 ≤max_recent 条未折叠对话（单条截断）。

    绝不喂全量 transcript —— 长会话性能与 token 的关键防线。
    """
    parts: list[str] = []
    summaries = [m for m in transcript if m.get("type") == "summary"]
    if summaries:
        parts.append("【两周前历史摘要】" + (summaries[-1].get("content") or ""))
    conv = [m for m in transcript if m.get("type") in ("user", "analyst")]
    for m in conv[-max_recent:]:
        who = "用户" if m.get("type") == "user" else m.get("name", m.get("role", ""))
        parts.append(f"{who}: {(m.get('content') or '')[:CONTENT_CAP]}")
    return "\n".join(parts) if parts else "（暂无历史对话）"


def _analyst_prompt(analyst: dict, snapshot_text: str, ctx_text: str,
                    question: str, peer_answer: str = "", round: int = 0,
                    market_ctx: str = "") -> str:
    tmpl = _load_prompt(analyst["prompt"])
    q = question or "（开场解读：请基于以上市场数据主动给出你的宏观判断）"
    return (tmpl.replace("{snapshot}", snapshot_text)
                .replace("{context}", ctx_text)
                .replace("{question}", q)
                .replace("{peer_answer}", peer_answer or "（本轮无对方观点）")
                .replace("{market_context}", market_ctx or "（未指定市场特性）")
                .replace("{round}", str(round)))


async def _iter_tokens(llm, prompt: str):
    """流式产出 token；模型不支持 astream 时降级 ainvoke + 按块伪流。"""
    try:
        async for chunk in llm.astream(prompt):
            tok = chunk.content if hasattr(chunk, "content") else str(chunk)
            if tok:
                yield tok
    except (NotImplementedError, AttributeError):
        resp = await llm.ainvoke(prompt)
        text = (resp.content if hasattr(resp, "content") else str(resp)).strip()
        for i in range(0, len(text), 120):
            yield text[i:i + 120]


async def _run_analyst(a: dict, models: list, snapshot_text: str, ctx_text: str,
                       question: str, peer_answer: str, round: int, budget,
                       market_ctx: str = ""):
    """跑一位分析师的一轮：流式 yield ('chunk', text)，最后 yield ('done', entry)。"""
    llm, provider, model = _analyst_llm(models, a["slot"])
    prompt = _analyst_prompt(a, snapshot_text, ctx_text, question, peer_answer, round, market_ctx)
    full = ""
    fail_reason = ""
    try:
        async for tok in _iter_tokens(llm, prompt):
            full += tok
            yield "chunk", tok
    except Exception as e:  # noqa: BLE001 单个分析师失败不应中断整条流
        from bottleneck_hunter.llm_clients.fallback import classify_reason
        fail_reason = classify_reason(e)  # 网络/超时/额度/认证… 供前端判断是否给重试
        logger.warning("宏观咨询分析师 %s 生成失败(%s): %s", a["role"], fail_reason, e)
        err = f"（该分析师生成失败：{fail_reason}）"
        full = full + err if full else err
        yield "chunk", err
    if budget:
        budget.record(provider, model, len(prompt) // 3, len(full) // 3, "macro_consult")
    entry = {"type": "analyst", "ts": _now_iso(), "round": round, "role": a["role"],
             "name": a["label"], "provider": provider, "model": model,
             "content": full, "reply_to": None}
    if fail_reason:  # 打标记：前端据此在该气泡显示「🔄 重试」按钮
        entry["failed"] = True
        entry["fail_reason"] = fail_reason
    yield "done", (entry, provider, model)


def _degradation(budget) -> tuple[list, bool, str]:
    """按预算返回 (参与分析师, 是否跑round2, 提示文案)。"""
    mode = budget.get_degradation_mode() if budget else DegradationMode.FULL
    if mode == DegradationMode.MINIMAL:
        return ANALYSTS[:1], False, "预算紧张：本轮单模型、单轮作答"
    if mode == DegradationMode.REDUCED:
        return ANALYSTS, False, "预算偏紧：本轮跳过互评辩论(round2)"
    return ANALYSTS, True, ""


# ── 主流程 ────────────────────────────────────────────

async def _make_snapshot(store: WatchlistStore, market: str, budget, models: list) -> dict:
    """采集 L1 数据快照（含市场新闻）+ 当前策略结论 + 观察池个股 + 当前持仓；采集失败回退空快照。"""
    try:
        market_ctx = await _collect_market_context(store, market)
        await _inject_market_news(store, market, market_ctx, models[0][0], budget)
    except Exception as e:  # noqa: BLE001
        logger.warning("宏观咨询快照采集失败: %s", e)
        market_ctx = {"indices": {}, "sentiment": {}, "macro": {}, "sectors": {}, "news": [], "markets": [market]}
    snap = _snapshot_entry(market_ctx, store.get_latest_macro_strategy())
    wl, pos = _portfolio_context(store, market)
    snap["watchlist"] = wl
    snap["positions"] = pos
    return snap


def _portfolio_context(store: WatchlistStore, market: str) -> tuple[list, list]:
    """观察池个股清单 + 当前持仓，供分析师做 position-aware 解读。持仓采集失败回退空列表。"""
    watchlist: list = []
    for e in store.list_all():
        if normalize_market(e.get("market")) != normalize_market(market):
            continue
        snap = store.get_latest_snapshot(e["ticker"]) or {}
        watchlist.append({
            "ticker": e["ticker"],
            "name": e.get("company_name", ""),
            "sector": e.get("sector", "未分类"),
            "change_pct": snap.get("change_pct"),
            "rsi": snap.get("rsi_14"),
        })
    watchlist = watchlist[:60]   # 上界：防观察池过大撑爆 prompt

    positions: list = []
    try:
        acct = store.get_sim_account()
        for p in store.get_sim_positions(acct.get("id")):
            avg = p.get("avg_cost") or 0
            cur = p.get("current_price") or 0
            positions.append({
                "ticker": p.get("ticker"),
                "shares": p.get("shares"),
                "avg_cost": round(avg, 2),
                "market_value": round(p.get("market_value") or 0, 2),
                "weight_pct": p.get("weight_pct"),
                "pnl_pct": round((cur / avg - 1) * 100, 1) if avg else None,
            })
    except Exception as e:  # noqa: BLE001
        logger.debug("宏观咨询持仓采集失败: %s", e)
    return watchlist, positions


_MAX_REPORT_CHARS = 8000

# 宏观背景研报（全局注入两位分析师）：借 focus_reports 表存一份，跨真实市场(美/A/港)全局、仍按用户隔离。
# 用固定哨兵 ticker + 固定合成市场分区(for_market 纯克隆重绑、不 normalize)读写同一行；真实 ticker/市场不会撞。
MACRO_REPORT_KEY = "__MACRO__"
MACRO_REPORT_MARKET = "__macro__"


def _truncate_report(txt: str, max_chars: int = _MAX_REPORT_CHARS) -> str:
    txt = (txt or "").strip()
    if len(txt) > max_chars:
        txt = txt[:max_chars] + f"\n…(研报较长，已截取前 {max_chars} 字；如需后段请在提问中指明)"
    return txt


def extract_report_text(pdf_source, pages: int = 6) -> str:
    """PDF(bytes 或 path) → 前几页文本并截断，供上传端点/磁盘读取共用。解析失败抛异常由调用方处理。"""
    from bottleneck_hunter.vip.derivatives import _read_pdf_text
    return _truncate_report(_read_pdf_text(pdf_source, pages=pages))


def _external_report_text(ticker: str, max_chars: int = _MAX_REPORT_CHARS) -> str:
    """读取 FOCUS_REPORT_DIR 下 {ticker}.pdf 外部研究报告（如 CFRA/投行研报）文本，注入焦点块。

    复用 vip.derivatives._read_pdf_text 提取前几页。env 未配 / 文件不存在 / 解析失败 → ""，
    静默降级不影响其余资料。ticker 仅取字母数字点划并限定在配置目录内——读盘是信任边界，防路径穿越。
    ponytail: 每次调用现抽（9 页 PDF <50ms）；多轮咨询若吃紧再按 (path, mtime) 加 lru_cache。
    """
    import os
    from pathlib import Path
    base = os.environ.get("FOCUS_REPORT_DIR", "").strip()
    if not base:
        return ""
    tk = "".join(c for c in (ticker or "") if c.isalnum() or c in ".-").upper()
    if not tk:
        return ""
    d = Path(base).resolve()
    fp = (d / f"{tk}.pdf").resolve()
    if fp.parent != d or not fp.is_file():   # 解析后必须直属配置目录，杜绝 ../ 逃逸
        return ""
    try:
        return extract_report_text(fp, pages=4)
    except Exception as e:  # noqa: BLE001
        logger.debug("读取 %s 外部研报失败: %s", tk, e)
        return ""


def _macro_disk_report_text(max_chars: int = _MAX_REPORT_CHARS) -> str:
    """读取 MACRO_REPORT_DIR(未配则回退 FOCUS_REPORT_DIR)下固定文件 _macro.pdf 的全球宏观背景研报文本。

    文件名固定、无用户可控成分（不像个股按 ticker 拼名），仍 .resolve()+直属目录校验做纵深防护。
    env 未配 / 文件不存在 / 解析失败 → ""，静默降级。宏观周报多含跨资产表，取前 8 页。
    """
    import os
    from pathlib import Path
    base = os.environ.get("MACRO_REPORT_DIR", "").strip() or os.environ.get("FOCUS_REPORT_DIR", "").strip()
    if not base:
        return ""
    d = Path(base).resolve()
    fp = (d / "_macro.pdf").resolve()
    if fp.parent != d or not fp.is_file():   # 解析后必须直属配置目录，杜绝 ../ 逃逸
        return ""
    try:
        return extract_report_text(fp, pages=8)
    except Exception as e:  # noqa: BLE001
        logger.debug("读取全球宏观背景研报失败: %s", e)
        return ""


def _macro_report_block(store: WatchlistStore) -> str:
    """全球宏观背景研报块：注入两位分析师每轮上下文（聚焦/不聚焦、各市场都带）。

    优先用户上传（DB，focus_reports 借哨兵键 + 固定 __macro__ 分区 → 跨真实市场全局、按用户隔离）；
    无则回退磁盘 _macro.pdf（运维预置、全员通用）。均无 → ""，静默降级不影响其余快照资料。
    """
    txt = ""
    try:
        rpt = store.for_market(MACRO_REPORT_MARKET).get_focus_report(MACRO_REPORT_KEY)
        if rpt and (rpt.get("report_text") or "").strip():
            txt = rpt["report_text"]
    except Exception:  # noqa: BLE001
        pass
    if not txt:
        txt = _macro_disk_report_text()
    if not txt:
        return ""
    return ("\n【全球宏观背景研报（用户提供,如投行/机构宏观周报;逐字摘录,以报告内注明日期为准;"
            "系全球/跨资产背景,对本土为外部驱动参考,勿与本土基本面主线或系统快照日混淆）】\n" + txt)


def _focus_ticker_block(store: WatchlistStore, ticker: str) -> str:
    """聚焦个股深度资料块：财务/估值 + 机构评级/目标价 + 个股新闻 + 财报惊喜 + 催化剂。

    全部读库、零实时抓取，复用现成读取器（committee 的估值取字段模式 + decision_engine._chip_context
    + store.get_news/get_earnings/get_catalysts_for_entry）。任一子项失败降级为空、绝不带崩。
    全部子项皆空 → 返回诚实占位，让分析师如实说数据不足，绝不臆造。
    """
    import json as _json

    ticker = (ticker or "").strip()
    if not ticker:
        return ""
    parts: list[str] = []

    prof = {}
    snap = {}
    try:
        prof = store.get_company_profile(ticker) or {}
        snap = store.get_latest_snapshot(ticker) or {}
    except Exception:  # noqa: BLE001
        pass
    raw = prof.get("raw") if isinstance(prof.get("raw"), dict) else {}
    name = prof.get("company_name") or prof.get("name") or ""

    def _nz(d: dict) -> dict:  # 仅保留非空项（None/"" 剔除；False/0 保留）
        return {k: v for k, v in d.items() if v not in (None, "")}

    # 三块基本面对齐分析师所需（raw_json 存 yfinance .info 全量，此处只是把已在库的字段配线出来）。
    # ① 估值倍数（PE / EV·EBITDA / EV·Revenue …）
    valuation = _nz({
        "trailing_pe": raw.get("trailingPE"), "forward_pe": raw.get("forwardPE"),
        "price_to_book": raw.get("priceToBook"),
        "price_to_sales": raw.get("priceToSalesTrailing12Months"),
        "ev_to_ebitda": raw.get("enterpriseToEbitda"),
        "ev_to_revenue": raw.get("enterpriseToRevenue"),
        "peg": raw.get("pegRatio") or raw.get("trailingPegRatio"),
        "market_cap": snap.get("market_cap") or raw.get("marketCap"),
        "enterprise_value": raw.get("enterpriseValue"), "beta": raw.get("beta"),
    })
    if valuation:
        parts.append(f"估值倍数: {_json.dumps(valuation, ensure_ascii=False)}")

    # ② 损益（近12月；yfinance 各 margin/growth 为小数，0.42=42%）
    income = _nz({
        "revenue_ttm": raw.get("totalRevenue"), "revenue_growth": raw.get("revenueGrowth"),
        "net_income": raw.get("netIncomeToCommon"),
        "gross_margin": raw.get("grossMargins"), "operating_margin": raw.get("operatingMargins"),
        "ebitda_margin": raw.get("ebitdaMargins"), "profit_margin": raw.get("profitMargins"),
        "roe": raw.get("returnOnEquity"), "roa": raw.get("returnOnAssets"),
        "earnings_growth": raw.get("earningsGrowth"),
    })
    if income:
        parts.append(f"损益(近12月,比率为小数): {_json.dumps(income, ensure_ascii=False)}")

    # ③ 现金流与负债结构（经营/自由现金流 + 债务/流动性）
    balance = _nz({
        "operating_cashflow": raw.get("operatingCashflow"), "free_cashflow": raw.get("freeCashflow"),
        "total_cash": raw.get("totalCash"), "total_debt": raw.get("totalDebt"),
        "debt_to_equity": raw.get("debtToEquity"),
        "current_ratio": raw.get("currentRatio"), "quick_ratio": raw.get("quickRatio"),
    })
    if balance:
        parts.append(f"现金流与负债: {_json.dumps(balance, ensure_ascii=False)}")

    # ③b 深度财务·同比·近5季（FMP 采集，raw_json['financials']；营收/净利=亿美元，各率=百分比）。
    # 国内 .info 必 429 拿不到损益，此块是"净利同比暴跌"类断言的唯一可验证来源。
    fin = raw.get("financials") if isinstance(raw.get("financials"), dict) else {}
    if fin:
        head = _nz({
            "revenue_yi": fin.get("revenue_yi"), "revenue_yoy_pct": fin.get("revenue_yoy_pct"),
            "net_profit_yi": fin.get("net_profit_yi"), "net_profit_yoy_pct": fin.get("net_profit_yoy_pct"),
            "gross_margin_pct": fin.get("gross_margin_pct"), "roe_pct": fin.get("roe_pct"),
            "debt_to_equity_pct": fin.get("debt_to_equity_pct"),
            "operating_cf_per_share": fin.get("operating_cf_per_share"),
            "report_date": fin.get("report_date", ""),
        })
        if head:
            parts.append(f"深度财务(FMP,亿美元/%): {_json.dumps(head, ensure_ascii=False)}")
        qs = [q for q in (fin.get("quarters") or []) if q.get("date")]
        if qs:
            parts.append(f"近5季趋势(营收/净利亿美元,同比%): {_json.dumps(qs[:5], ensure_ascii=False)}")

    # ④ 现价/技术
    tech = _nz({
        "current_price": snap.get("close"), "change_pct": snap.get("change_pct"),
        "rsi_14": snap.get("rsi_14"), "sector": prof.get("sector") or raw.get("sector"),
    })
    if tech:
        parts.append(f"现价/技术: {_json.dumps(tech, ensure_ascii=False)}")

    # 机构/评级/目标价（decision_engine._chip_context，纯读库）
    try:
        from bottleneck_hunter.watchlist.decision_engine import _chip_context
        chip = _chip_context(store, ticker)
        if chip:
            parts.append(f"机构持仓/分析师评级/一致目标价: {_json.dumps(chip, ensure_ascii=False)}")
    except Exception:  # noqa: BLE001
        pass

    # 个股新闻（近 8 条，取 date/title/sentiment）
    try:
        news = store.get_news(ticker, limit=8) or []
        slim = [{"date": n.get("date", ""), "title": n.get("title", ""),
                 "sentiment": n.get("sentiment", "")} for n in news if n.get("title")]
        if slim:
            parts.append(f"个股近期新闻: {_json.dumps(slim, ensure_ascii=False)}")
    except Exception:  # noqa: BLE001
        pass

    # 财报惊喜（近 2 期）
    try:
        earn = store.get_earnings(ticker) or []
        slim_e = [{"report_date": e.get("report_date", ""), "eps_actual": e.get("eps_actual"),
                   "eps_estimate": e.get("eps_estimate"), "eps_surprise_pct": e.get("eps_surprise_pct"),
                   "revenue_actual": e.get("revenue_actual")} for e in earn[:2]]
        slim_e = [e for e in slim_e if any(v not in (None, "") for v in e.values())]
        if slim_e:
            parts.append(f"财报惊喜(近2期): {_json.dumps(slim_e, ensure_ascii=False)}")
    except Exception:  # noqa: BLE001
        pass

    # 个股期权情绪（per-stock PCR + 成交量；隐含波动率 IV 系统未采集，见末尾诚实声明）
    try:
        opts = store.get_options(ticker, limit=1) or []
        if opts:
            o = opts[0]
            slim_o = _nz({
                "date": o.get("date", ""), "put_call_ratio": o.get("put_call_ratio"),
                "call_volume": o.get("total_call_volume"), "put_volume": o.get("total_put_volume"),
                "unusual_volume": bool(o.get("unusual_volume")),
            })
            if slim_o.get("put_call_ratio") is not None:
                parts.append(f"个股期权(PCR/成交量): {_json.dumps(slim_o, ensure_ascii=False)}")
    except Exception:  # noqa: BLE001
        pass

    # 催化剂（需 entry_id：观察池股才有；持仓不在池则无，如实略过）
    try:
        entry_id = next((e.get("id") for e in store.list_all()
                         if str(e.get("ticker") or "").upper() == ticker.upper()), None)
        if entry_id:
            cats = store.get_catalysts_for_entry(entry_id, active_only=True) or []
            slim_c = [{"type": c.get("catalyst_type", ""), "desc": c.get("description", ""),
                       "expected_date": c.get("expected_date", "")} for c in cats[:5]]
            if slim_c:
                parts.append(f"活跃催化剂: {_json.dumps(slim_c, ensure_ascii=False)}")
    except Exception:  # noqa: BLE001
        pass

    # 外部研究报告（如 CFRA/投行研报，逐字注入，非系统采集，明确标注来源与"以报告内日期为准"）：
    # 优先用户上传（DB，按用户+市场私有）；无则回退 FOCUS_REPORT_DIR 磁盘目录（运维预置）。
    ext = ""
    try:
        rpt = store.get_focus_report(ticker)
        if rpt and (rpt.get("report_text") or "").strip():
            ext = rpt["report_text"]
    except Exception:  # noqa: BLE001
        pass
    if not ext:
        ext = _external_report_text(ticker)
    if ext:
        parts.append("外部研究报告(用户提供,如CFRA/投行研报;逐字摘录,以报告内注明日期为准,勿与系统快照日混淆):\n" + ext)

    if not parts:
        return (f"【聚焦个股：{ticker} — 该股暂无系统采集的深度资料，"
                f"建议先加入观察池等待抓取；请如实说明数据不足，勿臆造其财务/估值/目标价】")
    # 结构性缺口：明确告知分析师这些指标系统不采集/不可得，得到确定答复而非反复索要（勿臆造）
    # 注：13F 增减持方向已由本焦点块的 chip JSON(institutional_qoq)提供近两季环比，故不再列入「未采集」缺口。
    gap_note = ("系统当前未采集(如实告知用户,勿臆造数字): 个股隐含波动率(IV)、"
                "联邦基金期货/点阵图隐含降息路径、"
                "逐季现金流/资产负债原始报表(有近5季损益趋势+经营现金流每股,无逐季完整报表)")
    if ext:
        gap_note += "。注:上述部分缺口或已见于本次附带的外部研究报告,请优先引用报告内数据并标注其发布日"
    parts.append(gap_note)
    header = (f"【聚焦个股深度资料：{ticker}"
              f"{('（' + name + '）') if name else ''} — 数据系统定时采集，"
              f"快照日 {snap.get('date', '未知')}，可能滞后；缺项即系统未采集，勿臆造数字】")
    return header + "\n" + "\n".join(parts)


async def stream_opening(store: WatchlistStore, budget: BudgetTracker | None, market: str):
    """打开抽屉：陈列 L1 数据快照 + 两位分析师自动流式开场解读（round0）。

    当日已有 snapshot+开场则只回放历史、不重复调用模型（防重复烧钱）。
    """
    models = get_models_for_role("L1_macro", with_fallback=True)
    if not models:
        yield _sse("error", message="无可用 LLM（请在 AI 配置中为 L1_macro 配置模型）")
        return

    market = normalize_market(market)
    market_ctx = _get_market_context_text([market])   # 市场特性(涨跌停/T+1/宏观驱动)注入提示词
    session = _load_session(store, market)
    transcript = list(session.get("transcript_json") or []) if session else []
    today = _now_iso()[:10]
    last_snap_ts = max((m.get("ts", "") for m in transcript if m.get("type") == "snapshot"), default="")
    snaps = [m for m in transcript if m.get("type") == "snapshot"]

    # 当日已开场 → 回放；但若新闻库已有比上次快照更新的新闻（如全量刷新/定时扫描后），则重生成
    fresher_news = snapshot_is_stale(store, market, session) if snaps else False

    if session and last_snap_ts[:10] == today and not fresher_news:
        if snaps:
            yield _sse("snapshot", **snaps[-1])
        for m in transcript:
            if m.get("type") == "analyst" and m.get("round") == 0 and m.get("ts", "") >= last_snap_ts:
                yield _sse("chunk", role=m["role"], round=0, text=m.get("content", ""))
                yield _sse("msg_done", role=m["role"], round=0,
                           provider=m.get("provider", ""), model=m.get("model", ""))
        yield _sse("done", message_count=len(transcript), replayed=True)
        return

    # 采集新快照
    snap = await _make_snapshot(store, market, budget, models)

    if not session:
        record_id = store.create_meeting_record(
            meeting_type=MEETING_TYPE, title=f"宏观咨询 · {market}", market=market,
            transcript_json=[], result_json={})
        meta = {}
    else:
        record_id = session["id"]
        meta = dict(session.get("result_json") or {})

    transcript.append(snap)
    yield _sse("snapshot", **snap)

    snapshot_text = _snapshot_text(snap)
    snapshot_text += _macro_report_block(store)   # 全球宏观背景研报（不聚焦 round0 也带）
    ctx_text = _context_for_prompt(transcript)
    active, _r2, note = _degradation(budget)
    if note:
        transcript.append({"type": "system", "ts": _now_iso(), "content": note})
        yield _sse("system", content=note)

    yield _sse("start", phase="round0")
    for a in active:
        async for kind, payload in _run_analyst(a, models, snapshot_text, ctx_text, "", "", 0, budget,
                                                 market_ctx=market_ctx):
            if kind == "chunk":
                yield _sse("chunk", role=a["role"], round=0, text=payload)
            else:
                entry, provider, model = payload
                transcript.append(entry)
                yield _sse("msg_done", role=a["role"], round=0, provider=provider, model=model,
                           failed=entry.get("failed", False), fail_reason=entry.get("fail_reason", ""))

    meta["message_count"] = len(transcript)
    store.update_meeting_review(record_id, transcript_json=transcript, result_json=meta)
    yield _sse("done", message_count=len(transcript))


async def stream_consult(store: WatchlistStore, budget: BudgetTracker | None,
                         market: str, question: str, focus_ticker: str = ""):
    """用户提问：round1 两人独立作答 → round2 互评辩论（预算不足时降级）。

    focus_ticker：可选聚焦个股（观察池/持仓内），置定则把该股深度资料块注入当轮 snapshot，
    让分析师结合整体环境给出 position-aware 的守/减/加判断。
    """
    question = (question or "").strip()
    focus_ticker = (focus_ticker or "").strip()
    if not question:
        yield _sse("error", message="问题为空")
        return
    models = get_models_for_role("L1_macro", with_fallback=True)
    if not models:
        yield _sse("error", message="无可用 LLM（请在 AI 配置中为 L1_macro 配置模型）")
        return

    market = normalize_market(market)
    market_ctx = _get_market_context_text([market])
    session = _load_session(store, market)
    if not session:
        record_id = store.create_meeting_record(
            meeting_type=MEETING_TYPE, title=f"宏观咨询 · {market}", market=market,
            transcript_json=[], result_json={})
        transcript: list = []
        meta: dict = {}
    else:
        record_id = session["id"]
        transcript = list(session.get("transcript_json") or [])
        meta = dict(session.get("result_json") or {})

    # 未先 open（直连 API / 竞态）→ 会话里没有快照 → 先采一份，避免分析师拿占位符空数据
    if not any(m.get("type") == "snapshot" for m in transcript):
        snap = await _make_snapshot(store, market, budget, models)
        transcript.append(snap)
        yield _sse("snapshot", **snap)

    # 立即落库用户提问，防止断连丢问题（聚焦个股加 [聚焦 X] 前缀，历史回看能看出这轮聊哪只）
    q_content = f"[聚焦 {focus_ticker}] {question}" if focus_ticker else question
    transcript.append({"type": "user", "ts": _now_iso(), "content": q_content})
    store.update_meeting_review(record_id, transcript_json=transcript)

    snapshot_text = _latest_snapshot_text(transcript)
    snapshot_text += _macro_report_block(store)   # 全球宏观背景研报（聚焦/不聚焦都带）
    # ponytail: 焦点块只注入当轮 prompt、不落 transcript 快照（焦点是每问一次的即时上下文，非会话常驻）；
    #           round1/round2 共用此局部变量故两轮都带；跨轮 stream_retry 会丢焦点深度（仅网络容错，
    #           升级路径＝把 focus_ticker 落进 user 条目、retry 时重建块）。
    if focus_ticker:
        snapshot_text += "\n" + _focus_ticker_block(store, focus_ticker)
    ctx_text = _context_for_prompt(transcript)
    active, do_round2, note = _degradation(budget)
    if note:
        transcript.append({"type": "system", "ts": _now_iso(), "content": note})
        yield _sse("system", content=note)

    # ROUND 1：各自独立作答
    yield _sse("start", phase="round1", question=question)
    round1: dict[str, str] = {}
    for a in active:
        async for kind, payload in _run_analyst(a, models, snapshot_text, ctx_text, question, "", 1, budget,
                                                 market_ctx=market_ctx):
            if kind == "chunk":
                yield _sse("chunk", role=a["role"], round=1, text=payload)
            else:
                entry, provider, model = payload
                transcript.append(entry)
                round1[a["role"]] = entry["content"]
                yield _sse("msg_done", role=a["role"], round=1, provider=provider, model=model,
                           failed=entry.get("failed", False), fail_reason=entry.get("fail_reason", ""))

    # ROUND 2：带入对方 round1 全文，互评辩论
    if do_round2 and len(active) >= 2:
        yield _sse("start", phase="round2")
        for a in active:
            peer = next((x for x in active if x["role"] != a["role"]), None)
            peer_ans = round1.get(peer["role"], "") if peer else ""
            async for kind, payload in _run_analyst(a, models, snapshot_text, ctx_text,
                                                     question, peer_ans, 2, budget, market_ctx=market_ctx):
                if kind == "chunk":
                    yield _sse("chunk", role=a["role"], round=2, text=payload)
                else:
                    entry, provider, model = payload
                    entry["reply_to"] = peer["role"] if peer else None
                    transcript.append(entry)
                    yield _sse("msg_done", role=a["role"], round=2, provider=provider, model=model,
                               failed=entry.get("failed", False), fail_reason=entry.get("fail_reason", ""))

    # ponytail: 整条覆盖写；单用户单抽屉够用（前端 send 期间禁用输入是主防线），
    # 出现真并发再上行级 merge。
    meta["message_count"] = len(transcript)
    store.update_meeting_review(record_id, transcript_json=transcript, result_json=meta)

    await _maybe_compress(store, budget, record_id)
    yield _sse("done", message_count=len(transcript))


async def stream_retry(store: WatchlistStore, budget: BudgetTracker | None,
                       market: str, role: str, provider: str = "", model: str = ""):
    """手动重试某位分析师**最近一条**消息（网络波动/额度不足等中断后）。

    重建该消息当轮的上下文重跑，**原位替换** transcript 里那条失败消息。
    可选 provider/model：留空用该角色该槽的模型（网络类原地重试）；指定则换模型重试（容量/额度类）。
    """
    market = normalize_market(market)
    market_ctx = _get_market_context_text([market])
    session = _load_session(store, market)
    if not session:
        yield _sse("error", message="无历史会话可重试")
        return
    transcript = list(session.get("transcript_json") or [])
    idx = next((i for i in range(len(transcript) - 1, -1, -1)
                if transcript[i].get("type") == "analyst" and transcript[i].get("role") == role), None)
    if idx is None:
        yield _sse("error", message="未找到该分析师的消息")
        return
    a = next((x for x in ANALYSTS if x["role"] == role), None)
    if not a:
        yield _sse("error", message=f"未知分析师: {role}")
        return
    failed_msg = transcript[idx]
    rnd = failed_msg.get("round", 0)

    # 解析模型：指定 provider/model → 换模型重试；否则取该角色该槽模型（原地重试）
    if provider and model:
        from bottleneck_hunter.llm_clients.factory import create_llm
        try:
            model_tuple = (create_llm(provider, model, with_fallback=True), provider, model)
        except Exception as e:  # noqa: BLE001
            yield _sse("error", message=f"模型创建失败: {e}")
            return
    else:
        models = get_models_for_role("L1_macro", with_fallback=True)
        if not models:
            yield _sse("error", message="无可用 LLM（请在 AI 配置中为 L1_macro 配置模型）")
            return
        model_tuple = _analyst_llm(models, a["slot"])

    # 重建上下文：排除失败消息本身（避免"生成失败"文案污染），snapshot/上下文/提问/对方答复按轮复现
    ctx_src = [m for i, m in enumerate(transcript) if i != idx]
    snapshot_text = _latest_snapshot_text(ctx_src)
    snapshot_text += _macro_report_block(store)   # 全球宏观背景研报（重试也带，保持与原轮一致）
    ctx_text = _context_for_prompt(ctx_src)
    question = ""
    if rnd >= 1:
        question = next((m.get("content", "") for m in reversed(transcript[:idx])
                         if m.get("type") == "user"), "")
    peer_ans = ""
    if rnd == 2:
        peer = next((x for x in ANALYSTS if x["role"] != role), None)
        if peer:
            peer_ans = next((m.get("content", "") for m in transcript
                             if m.get("type") == "analyst" and m.get("role") == peer["role"]
                             and m.get("round") == 1), "")

    yield _sse("retry_start", role=role, round=rnd)
    new_entry = None
    async for kind, payload in _run_analyst(a, [model_tuple], snapshot_text, ctx_text, question, peer_ans, rnd, budget,
                                             market_ctx=market_ctx):
        if kind == "chunk":
            yield _sse("chunk", role=role, round=rnd, text=payload)
        else:
            new_entry = payload[0]
    if new_entry is None:
        yield _sse("error", message="重试未产出结果")
        return
    new_entry["reply_to"] = failed_msg.get("reply_to")
    transcript[idx] = new_entry  # 原位替换失败消息
    store.update_meeting_review(session["id"], transcript_json=transcript,
                                result_json=dict(session.get("result_json") or {}))
    yield _sse("msg_done", role=role, round=rnd, provider=new_entry.get("provider", ""),
               model=new_entry.get("model", ""), failed=bool(new_entry.get("failed")),
               fail_reason=new_entry.get("fail_reason", ""))
    yield _sse("retry_done", role=role, round=rnd)


async def _maybe_compress(store: WatchlistStore, budget: BudgetTracker | None, record_id: str) -> None:
    """把两周前、尚未摘要的对话压成一条滚动摘要留在上下文。触发需同时满足数量与时间条件。"""
    if budget and not budget.can_spend():  # MINIMAL 直接跳过压缩
        return
    rec = store.get_meeting_record(record_id)
    if not rec:
        return
    transcript = list(rec.get("transcript_json") or [])
    meta = dict(rec.get("result_json") or {})
    upto = meta.get("unfolded_summarized_upto", "") or ""
    cutoff = _iso_days_ago(FOLD_DAYS)
    pending = [m for m in transcript if m.get("type") in ("user", "analyst")
               and upto < m.get("ts", "") < cutoff]
    if len(pending) < SUMMARY_TRIGGER:
        return

    models = get_models_for_role("L1_macro", with_fallback=True)
    if not models:
        return
    llm, provider, model = models[0]
    prev_summary = next((m.get("content", "") for m in reversed(transcript)
                         if m.get("type") == "summary"), "")
    msgs_text = "\n".join(
        f"{('用户' if m['type'] == 'user' else m.get('name', m.get('role', '')))}: {(m.get('content') or '')[:600]}"
        for m in pending)
    prompt = (_load_prompt("macro_consult_summarize")
              .replace("{prev_summary}", prev_summary or "（无）")
              .replace("{messages}", msgs_text))
    try:
        text = await asyncio.to_thread(lambda: llm.invoke(prompt).content)
        text = (text or "").strip()
    except Exception as e:  # noqa: BLE001
        logger.warning("宏观咨询滚动摘要生成失败: %s", e)
        return
    if not text:
        return
    covers_until = max(m.get("ts", "") for m in pending)
    transcript.append({"type": "summary", "ts": _now_iso(), "covers_until": covers_until,
                       "content": text, "folded_count": len(pending)})
    meta["unfolded_summarized_upto"] = covers_until
    meta["last_summary_ts"] = _now_iso()
    meta["message_count"] = len(transcript)
    if budget:
        budget.record(provider, model, len(prompt) // 3, len(text) // 3, "macro_consult_summary")
    store.update_meeting_review(record_id, transcript_json=transcript, result_json=meta)
