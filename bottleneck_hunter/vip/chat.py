"""P6 实时咨询聊天：VIP 顾问单模型流式回答（先确定性预取 facts，不做多轮 tool-loop）。

复用：
- vip_chat 角色（角色矩阵 + fallback + 预算）
- number_guard / compliance
- macro_consultation._iter_tokens（流式/伪流降级）
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from bottleneck_hunter.vip import compliance, number_guard, portfolio
from bottleneck_hunter.vip import mandate as _mandate
from bottleneck_hunter.watchlist.macro_consultation import _iter_tokens


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def create_chat_session(wl_store, title: str = "", account_ref: str = "") -> str:
    account_ref = wl_store.resolve_vip_account_ref(account_ref) if hasattr(wl_store, "resolve_vip_account_ref") else (account_ref or "").strip()
    sid = uuid.uuid4().hex[:12]
    with wl_store._write_conn() as conn:
        conn.execute(
            f"""INSERT INTO chat_sessions (id, title, account_ref, created_at, updated_at{wl_store._user_insert_cols()}{wl_store._market_insert_cols()})
               VALUES (?,?,?,?,?{wl_store._user_insert_vals()}{wl_store._market_insert_vals()})""",
            (sid, title, account_ref, _now_iso(), _now_iso())
            + wl_store._user_insert_params() + wl_store._market_insert_params(),
        )
    return sid


def append_chat_message(wl_store, session_id: str, role: str, content: str,
                        provider: str = "", model: str = "", fail_reason: str = "", account_ref: str = "") -> str:
    account_ref = wl_store.resolve_vip_account_ref(account_ref) if hasattr(wl_store, "resolve_vip_account_ref") else (account_ref or "").strip()
    mid = uuid.uuid4().hex[:12]
    with wl_store._write_conn() as conn:
        conn.execute(
            f"""INSERT INTO chat_messages (id, session_id, role, content, provider, model, fail_reason, account_ref, created_at{wl_store._user_insert_cols()}{wl_store._market_insert_cols()})
               VALUES (?,?,?,?,?,?,?,?,?{wl_store._user_insert_vals()}{wl_store._market_insert_vals()})""",
            (mid, session_id, role, content, provider, model, fail_reason, account_ref, _now_iso())
            + wl_store._user_insert_params() + wl_store._market_insert_params(),
        )
        conn.execute(
            "UPDATE chat_sessions SET updated_at=?, msg_count=msg_count+1 WHERE id=?",
            (_now_iso(), session_id),
        )
    return mid


def list_chat_sessions(wl_store, limit: int = 20, account_ref: str = "") -> list[dict]:
    account_ref = wl_store.resolve_vip_account_ref(account_ref) if hasattr(wl_store, "resolve_vip_account_ref") else (account_ref or "").strip()
    conn = wl_store._connect()
    try:
        q, p = wl_store._filtered("SELECT * FROM chat_sessions WHERE account_ref=? ORDER BY updated_at DESC LIMIT ?", (account_ref, limit))
        return [dict(r) for r in conn.execute(q, p).fetchall()]
    finally:
        conn.close()


def get_chat_session(wl_store, session_id: str, account_ref: str = "") -> dict | None:
    account_ref = wl_store.resolve_vip_account_ref(account_ref) if hasattr(wl_store, "resolve_vip_account_ref") else (account_ref or "").strip()
    conn = wl_store._connect()
    try:
        q, p = wl_store._filtered("SELECT * FROM chat_sessions WHERE id = ? AND account_ref=?", (session_id, account_ref))
        row = conn.execute(q, p).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_chat_messages(wl_store, session_id: str, limit: int = 100, account_ref: str = "") -> list[dict]:
    account_ref = wl_store.resolve_vip_account_ref(account_ref) if hasattr(wl_store, "resolve_vip_account_ref") else (account_ref or "").strip()
    conn = wl_store._connect()
    try:
        q, p = wl_store._filtered("SELECT * FROM chat_messages WHERE session_id=? AND account_ref=? ORDER BY created_at ASC LIMIT ?", (session_id, account_ref, limit))
        return [dict(r) for r in conn.execute(q, p).fetchall()]
    finally:
        conn.close()


def _build_facts(wl_store, account_ref: str = "") -> tuple[str, dict]:
    account_ref = wl_store.resolve_vip_account_ref(account_ref) if hasattr(wl_store, "resolve_vip_account_ref") else (account_ref or "").strip()
    # G4：chat 用完整档案（build_account_dossier）而非单薄的 build_account_summary——
    # 档案含逐仓成本/未实现盈亏、衍生品敞口、流水聚合、价源覆盖、数据新鲜度，与顾问/荐新同一事实源，
    # 用户问"我这仓亏多少/敲出风险/现金够不够"才答得出。dossier 已内含衍生品敞口，不再单独抓一次。
    dossier = portfolio.build_account_dossier(wl_store, account_ref=account_ref)
    return json.dumps(dossier, ensure_ascii=False, default=str), dossier


def _candidate_pool_text(wl_store, dossier: dict, account_ref: str = "") -> str:
    """观察池中「本账户尚未持有」的候选标的（带瓶颈环节/优先级），供顾问「推荐新标的」时有据可依。

    这是本平台的核心信号（产业链瓶颈选股），但此前从不进 chat facts——顾问被问「推荐新投资对象」
    时手里没有任何候选池，只能空谈。复用 recommend 的候选池引擎（已 for_market 隔离、去已持、命中
    纲领排除词剔除），不重造。返回空串＝观察池无合适候选，顾问如实说明即可。
    ponytail: 只给候选宇宙+瓶颈环节+优先级；逐名机构/评级/估值(_chip_context)是更重的扇出，
              待「候选尽调」需求出现再接——顾问可提示需进一步个股尽调。
    """
    from bottleneck_hunter.vip import recommend as _recommend
    try:
        entries = wl_store.list_all()
    except Exception:  # noqa: BLE001
        entries = []
    if not entries:
        return ""
    held = [h.get("ticker") for h in dossier.get("holdings", [])]
    mand = _mandate.load_mandate(wl_store, account_ref=account_ref)
    excl = _recommend._tokenize_exclusions(mand.get("exclusions", ""))
    cands = _recommend.build_candidate_pool(entries, held, excl).get("candidates") or []
    if not cands:
        return ""
    slim = [_recommend._prompt_candidate(c) for c in cands]
    return ("【可选新标的候选池（观察池中本账户尚未持有，按系统综合评分优先级排序）】\n"
            + json.dumps(slim, ensure_ascii=False))


def _latest_recommend_text(wl_store, account_ref: str = "") -> str:
    """带出荐新页面上一份投委会成品建议（精简），供顾问引用「已深思的结论」而非只有原始候选池。

    荐新页(/account/recommend)产出的是 dossier+纲领+L1宏观+候选池经 4-persona 投委会评审后的成品：
    每标的 action/理由/风险/纲领契合/软仓位 + 组合层建议 + 主席综述。信息量远大于原始候选池，此前从不
    进 chat facts。复用 recommend.get_latest_recommendations（读表、零 LLM）。
    诚实标注：这是用户上次点「生成荐新」时的历史快照（可能几天前的宏观/持仓口径），非本次实时研判——
    带 generated_at 让顾问据此提示时效，不把旧结论当当前建议。无则返回空串（用户从未生成过荐新）。
    ponytail: 只抽 candidates 的动作/理由/风险/契合/软仓位 + portfolio_note + chair_summary；
              委员逐条语料(committee)不喂 chat，避免上下文膨胀与 number_guard 误标。
    """
    from bottleneck_hunter.vip import recommend as _recommend
    rec = _recommend.get_latest_recommendations(wl_store, account_ref=account_ref)
    if not rec or not (rec.get("candidates") or rec.get("portfolio_note")):
        return ""
    slim = [{"ticker": c.get("ticker", ""), "action": c.get("action", ""),
             "reason": c.get("reason", ""), "risk": c.get("risk", ""),
             "fit": c.get("fit", ""), "suggested_weight": c.get("suggested_weight", "")}
            for c in (rec.get("candidates") or []) if c.get("ticker")]
    body = {"generated_at": rec.get("generated_at", ""), "chair_summary": rec.get("chair_summary", ""),
            "portfolio_note": rec.get("portfolio_note", ""), "candidates": slim}
    return ("【上一份荐新建议（投委会评审成品，历史快照非实时——引用时须提示其生成时间与时效）】\n"
            + json.dumps(body, ensure_ascii=False))


_PROMPT = """你是私人财务AI顾问。请只依据下面的真实 facts 回答，不得编造金额/占比/股数。
若用户问到 facts 里没有的数据，请明确说“当前数据中没有该信息”。
回答要求：简体中文、专业但克制、分点回答，避免空泛。
下面的「本账户投资纲领」是用户为该账户设定的投资目标与约束，回答涉及建议时须与之一致。
若用户要求推荐新的投资标的，只能从 facts 内「可选新标的候选池」中挑选（结合瓶颈环节与纲领契合度），
不得编造候选池以外的标的；池为空或未出现则如实说明观察池暂无合适候选、可先加入观察池跟踪。
若 facts 含「上一份荐新建议」，可引用其投委会结论作参考，但须说明它是历史快照（点明其 generated_at 生成时间），
提示如需最新研判请到荐新页重新生成；不得把旧建议当作本次实时结论。

[facts]\n{facts}\n[/facts]

[mandate]\n{mandate}\n[/mandate]

[question]\n{question}\n[/question]
"""


async def stream_vip_chat(wl_store, *, user_id: str, question: str, session_id: str = "", budget=None, account_ref: str = ""):
    if not question.strip():
        yield {"event": "error", "data": json.dumps({"message": "问题为空"}, ensure_ascii=False)}
        return

    from bottleneck_hunter.llm_clients.factory import get_models_for_role
    if budget is not None and not budget.can_spend():
        yield {"event": "error", "data": json.dumps({"message": "预算不足，暂不生成咨询回答"}, ensure_ascii=False)}
        return
    models = get_models_for_role("vip_chat", user_id=user_id, with_fallback=True)
    if not models:
        yield {"event": "error", "data": json.dumps({"message": "无可用 LLM（请在 AI 配置中为 vip_chat 配置模型）"}, ensure_ascii=False)}
        return
    llm, provider, model = models[0]

    if session_id:
        sess = get_chat_session(wl_store, session_id, account_ref=account_ref)
        if not sess:
            yield {"event": "error", "data": json.dumps({"message": "会话不存在或不属于当前账户/市场"}, ensure_ascii=False)}
            return
        sid = session_id
    else:
        sid = create_chat_session(wl_store, title=question[:40], account_ref=account_ref)
    append_chat_message(wl_store, sid, "user", question, account_ref=account_ref)
    facts_text, dossier = _build_facts(wl_store, account_ref=account_ref)
    # 荐新信号：观察池「未持有」候选池 + 上一份投委会荐新成品并进 facts，顾问被问「推荐新标的」时有据可依。
    # 二者互补：候选池＝实时全集兜底；荐新成品＝已深思的历史结论（带 generated_at 供时效提示）。复用 recommend 引擎。
    pool_text = _candidate_pool_text(wl_store, dossier, account_ref=account_ref)
    if pool_text:
        facts_text = facts_text + "\n" + pool_text
    rec_text = _latest_recommend_text(wl_store, account_ref=account_ref)
    if rec_text:
        facts_text = facts_text + "\n" + rec_text
    # 特性二 P1：对话内实时行情立查——现价并进 facts（LLM 可见）+ guard 语料（防误标"未核到"）。
    # 只读、单趟、不写库；失败/无映射票留 skipped，不塞 0。市场从 store 取（现查只做 us_stock）。
    from bottleneck_hunter.vip import live_quote
    live = await live_quote.fetch_live_quotes(
        question, dossier.get("holdings") or [],
        market=getattr(wl_store, "_market", "") or "us_stock", user_id=user_id)
    if live.get("usd_text"):
        facts_text = facts_text + "\n" + live["usd_text"]
    mandate_text = _mandate.format_mandate_for_prompt(wl_store, account_ref=account_ref)
    prompt = _PROMPT.format(facts=facts_text, mandate=mandate_text, question=question)
    # number_guard 白名单语料并入纲领文本：纲领里的收益目标/回撤%是用户设定的合法数字，避免误标"未核到"
    guard_corpus = facts_text + "\n" + mandate_text

    yield {"event": "session", "data": json.dumps({"session_id": sid}, ensure_ascii=False)}
    yield {"event": "disclaimer", "data": json.dumps({"content": compliance.DISCLAIMER_ZH}, ensure_ascii=False)}

    full = ""
    fail_reason = ""
    try:
        async for tok in _iter_tokens(llm, prompt):
            full += tok
            yield {"event": "chunk", "data": json.dumps({"text": tok}, ensure_ascii=False)}
    except Exception as e:  # noqa: BLE001
        fail_reason = str(e)[:160]
        msg = f"（该回答生成失败：{fail_reason}）"
        full = msg
        yield {"event": "chunk", "data": json.dumps({"text": msg}, ensure_ascii=False)}

    # 数字白名单校验（与报告同一公共件）；非美元衍生条款价/已实现盈亏不得核验叙述里的美元断言（跨币防误核）
    fv = number_guard.foreign_account_values(dossier)
    fv = fv + list(live.get("foreign_prices") or [])   # 外币现价并入外币池，同理不核美元断言
    unverified = [r["token"] for r in number_guard.verify_numbers(full, guard_corpus, fv) if r["status"] == "unverified"]
    final_text = number_guard.annotate_unverified(full, guard_corpus, foreign_values=fv)
    final_text = compliance.with_disclaimer(final_text)
    append_chat_message(wl_store, sid, "assistant", final_text, provider=provider, model=model, fail_reason=fail_reason, account_ref=account_ref)
    if budget is not None:
        try:
            budget.record(provider, model, len(prompt) // 3, len(full) // 3, "vip_chat")
        except Exception:
            pass
    yield {"event": "done", "data": json.dumps({"session_id": sid, "provider": provider, "model": model,
                                                      "unverified": unverified, "dossier": dossier,
                                                      "live_quotes": live.get("quotes", []),
                                                      "live_skipped": live.get("skipped", [])}, ensure_ascii=False)}
