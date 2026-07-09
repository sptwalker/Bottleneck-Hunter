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
        "indices": market_ctx.get("indices", {}),
        "sentiment": market_ctx.get("sentiment", {}),
        "macro": market_ctx.get("macro", {}),
        "sectors": market_ctx.get("sectors", {}),
        "news": (market_ctx.get("news") or [])[:15],
        "strategy": strat,
    }


def _snapshot_text(snap: dict) -> str:
    """把 snapshot 渲染成喂给 LLM 的紧凑文本。"""
    import json
    parts = [
        f"大盘指数: {json.dumps(snap.get('indices', {}), ensure_ascii=False)}",
        f"市场情绪(含VIX): {json.dumps(snap.get('sentiment', {}), ensure_ascii=False)}",
        f"宏观(利率/汇率等): {json.dumps(snap.get('macro', {}), ensure_ascii=False)}",
        f"板块表现(观察池聚合): {json.dumps(snap.get('sectors', {}), ensure_ascii=False)}",
        f"市场近期新闻: {json.dumps(snap.get('news', []), ensure_ascii=False)}",
    ]
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
                    question: str, peer_answer: str = "", round: int = 0) -> str:
    tmpl = _load_prompt(analyst["prompt"])
    q = question or "（开场解读：请基于以上市场数据主动给出你的宏观判断）"
    return (tmpl.replace("{snapshot}", snapshot_text)
                .replace("{context}", ctx_text)
                .replace("{question}", q)
                .replace("{peer_answer}", peer_answer or "（本轮无对方观点）")
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
                       question: str, peer_answer: str, round: int, budget):
    """跑一位分析师的一轮：流式 yield ('chunk', text)，最后 yield ('done', entry)。"""
    llm, provider, model = _analyst_llm(models, a["slot"])
    prompt = _analyst_prompt(a, snapshot_text, ctx_text, question, peer_answer, round)
    full = ""
    try:
        async for tok in _iter_tokens(llm, prompt):
            full += tok
            yield "chunk", tok
    except Exception as e:  # noqa: BLE001 单个分析师失败不应中断整条流
        logger.warning("宏观咨询分析师 %s 生成失败: %s", a["role"], e)
        err = f"（该分析师生成失败：{e}）"
        full = full + err if full else err
        yield "chunk", err
    if budget:
        budget.record(provider, model, len(prompt) // 3, len(full) // 3, "macro_consult")
    entry = {"type": "analyst", "ts": _now_iso(), "round": round, "role": a["role"],
             "name": a["label"], "provider": provider, "model": model,
             "content": full, "reply_to": None}
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


async def stream_opening(store: WatchlistStore, budget: BudgetTracker | None, market: str):
    """打开抽屉：陈列 L1 数据快照 + 两位分析师自动流式开场解读（round0）。

    当日已有 snapshot+开场则只回放历史、不重复调用模型（防重复烧钱）。
    """
    models = get_models_for_role("L1_macro")
    if not models:
        yield _sse("error", message="无可用 LLM（请在 AI 配置中为 L1_macro 配置模型）")
        return

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
    ctx_text = _context_for_prompt(transcript)
    active, _r2, note = _degradation(budget)
    if note:
        transcript.append({"type": "system", "ts": _now_iso(), "content": note})
        yield _sse("system", content=note)

    yield _sse("start", phase="round0")
    for a in active:
        async for kind, payload in _run_analyst(a, models, snapshot_text, ctx_text, "", "", 0, budget):
            if kind == "chunk":
                yield _sse("chunk", role=a["role"], round=0, text=payload)
            else:
                entry, provider, model = payload
                transcript.append(entry)
                yield _sse("msg_done", role=a["role"], round=0, provider=provider, model=model)

    meta["message_count"] = len(transcript)
    store.update_meeting_review(record_id, transcript_json=transcript, result_json=meta)
    yield _sse("done", message_count=len(transcript))


async def stream_consult(store: WatchlistStore, budget: BudgetTracker | None,
                         market: str, question: str):
    """用户提问：round1 两人独立作答 → round2 互评辩论（预算不足时降级）。"""
    question = (question or "").strip()
    if not question:
        yield _sse("error", message="问题为空")
        return
    models = get_models_for_role("L1_macro")
    if not models:
        yield _sse("error", message="无可用 LLM（请在 AI 配置中为 L1_macro 配置模型）")
        return

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

    # 立即落库用户提问，防止断连丢问题
    transcript.append({"type": "user", "ts": _now_iso(), "content": question})
    store.update_meeting_review(record_id, transcript_json=transcript)

    snapshot_text = _latest_snapshot_text(transcript)
    ctx_text = _context_for_prompt(transcript)
    active, do_round2, note = _degradation(budget)
    if note:
        transcript.append({"type": "system", "ts": _now_iso(), "content": note})
        yield _sse("system", content=note)

    # ROUND 1：各自独立作答
    yield _sse("start", phase="round1", question=question)
    round1: dict[str, str] = {}
    for a in active:
        async for kind, payload in _run_analyst(a, models, snapshot_text, ctx_text, question, "", 1, budget):
            if kind == "chunk":
                yield _sse("chunk", role=a["role"], round=1, text=payload)
            else:
                entry, provider, model = payload
                transcript.append(entry)
                round1[a["role"]] = entry["content"]
                yield _sse("msg_done", role=a["role"], round=1, provider=provider, model=model)

    # ROUND 2：带入对方 round1 全文，互评辩论
    if do_round2 and len(active) >= 2:
        yield _sse("start", phase="round2")
        for a in active:
            peer = next((x for x in active if x["role"] != a["role"]), None)
            peer_ans = round1.get(peer["role"], "") if peer else ""
            async for kind, payload in _run_analyst(a, models, snapshot_text, ctx_text,
                                                     question, peer_ans, 2, budget):
                if kind == "chunk":
                    yield _sse("chunk", role=a["role"], round=2, text=payload)
                else:
                    entry, provider, model = payload
                    entry["reply_to"] = peer["role"] if peer else None
                    transcript.append(entry)
                    yield _sse("msg_done", role=a["role"], round=2, provider=provider, model=model)

    # ponytail: 整条覆盖写；单用户单抽屉够用（前端 send 期间禁用输入是主防线），
    # 出现真并发再上行级 merge。
    meta["message_count"] = len(transcript)
    store.update_meeting_review(record_id, transcript_json=transcript, result_json=meta)

    await _maybe_compress(store, budget, record_id)
    yield _sse("done", message_count=len(transcript))


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

    models = get_models_for_role("L1_macro")
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
