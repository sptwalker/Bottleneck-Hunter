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

from bottleneck_hunter.vip import compliance, number_guard
from bottleneck_hunter.vip import portfolio, derivatives
from bottleneck_hunter.watchlist.macro_consultation import _iter_tokens


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def create_chat_session(wl_store, title: str = "") -> str:
    sid = uuid.uuid4().hex[:12]
    with wl_store._write_conn() as conn:
        conn.execute(
            f"""INSERT INTO chat_sessions (id, title, created_at, updated_at{wl_store._user_insert_cols()}{wl_store._market_insert_cols()})
               VALUES (?,?,?,?{wl_store._user_insert_vals()}{wl_store._market_insert_vals()})""",
            (sid, title, _now_iso(), _now_iso())
            + wl_store._user_insert_params() + wl_store._market_insert_params(),
        )
    return sid


def append_chat_message(wl_store, session_id: str, role: str, content: str,
                        provider: str = "", model: str = "", fail_reason: str = "") -> str:
    mid = uuid.uuid4().hex[:12]
    with wl_store._write_conn() as conn:
        conn.execute(
            f"""INSERT INTO chat_messages (id, session_id, role, content, provider, model, fail_reason, created_at{wl_store._user_insert_cols()}{wl_store._market_insert_cols()})
               VALUES (?,?,?,?,?,?,?,?{wl_store._user_insert_vals()}{wl_store._market_insert_vals()})""",
            (mid, session_id, role, content, provider, model, fail_reason, _now_iso())
            + wl_store._user_insert_params() + wl_store._market_insert_params(),
        )
        conn.execute(
            f"UPDATE chat_sessions SET updated_at=?, msg_count=msg_count+1 WHERE id=?",
            (_now_iso(), session_id),
        )
    return mid


def list_chat_sessions(wl_store, limit: int = 20) -> list[dict]:
    conn = wl_store._connect()
    try:
        q, p = wl_store._filtered("SELECT * FROM chat_sessions ORDER BY updated_at DESC LIMIT ?", (limit,))
        return [dict(r) for r in conn.execute(q, p).fetchall()]
    finally:
        conn.close()


def get_chat_messages(wl_store, session_id: str, limit: int = 100) -> list[dict]:
    conn = wl_store._connect()
    try:
        q, p = wl_store._filtered("SELECT * FROM chat_messages WHERE session_id=? ORDER BY created_at ASC LIMIT ?", (session_id, limit))
        return [dict(r) for r in conn.execute(q, p).fetchall()]
    finally:
        conn.close()


def _build_facts(wl_store) -> tuple[str, dict]:
    summary = portfolio.build_portfolio_summary(wl_store)
    terms = derivatives.list_derivative_terms(wl_store)
    facts = {
        "portfolio": summary,
        "derivatives": [
            {"family": t.product_family, "underlying": t.underlying_symbol, "ccy": t.currency, "terms": t.terms}
            for t in terms
        ],
    }
    return json.dumps(facts, ensure_ascii=False, default=str), summary


_PROMPT = """你是私人财务AI顾问。请只依据下面的真实 facts 回答，不得编造金额/占比/股数。
若用户问到 facts 里没有的数据，请明确说“当前数据中没有该信息”。
回答要求：简体中文、专业但克制、分点回答，避免空泛。

[facts]\n{facts}\n[/facts]

[question]\n{question}\n[/question]
"""


async def stream_vip_chat(wl_store, *, user_id: str, question: str, session_id: str = "", budget=None):
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

    sid = session_id or create_chat_session(wl_store, title=question[:40])
    append_chat_message(wl_store, sid, "user", question)
    facts_text, summary = _build_facts(wl_store)
    prompt = _PROMPT.format(facts=facts_text, question=question)

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

    # 数字白名单校验（与报告同一公共件）
    unverified = [r["token"] for r in number_guard.verify_numbers(full, facts_text) if r["status"] == "unverified"]
    final_text = number_guard.annotate_unverified(full, facts_text)
    final_text = compliance.with_disclaimer(final_text)
    append_chat_message(wl_store, sid, "assistant", final_text, provider=provider, model=model, fail_reason=fail_reason)
    if budget is not None:
        try:
            budget.record(provider, model, len(prompt) // 3, len(full) // 3, "vip_chat")
        except Exception:
            pass
    yield {"event": "done", "data": json.dumps({"session_id": sid, "provider": provider, "model": model,
                                                      "unverified": unverified, "summary": summary}, ensure_ascii=False)}
