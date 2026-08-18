"""P6 实时咨询聊天：VIP 顾问单模型流式回答（先确定性预取 facts，不做多轮 tool-loop）。

复用：
- vip_chat 角色（角色矩阵 + fallback + 预算）
- number_guard / compliance
- macro_consultation._iter_tokens（流式/伪流降级）
"""
from __future__ import annotations

import json
import re
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


def _macro_text(wl_store) -> str:
    """当前 L1 宏观研判并进 facts——用户问「该不该加仓/降现金/换行业」时有宏观锚点。

    复用 advisory.format_macro_for_prompt（缺失/异常已自降级为中性稳健句，绝不带崩），
    此前 chat 完全不带宏观，advisory/recommend/strategy_review 却都喂。返回值必非空（降级句兜底），
    故不做空串分支，直接包标题块。
    """
    from bottleneck_hunter.vip.advisory import format_macro_for_prompt
    return ("【当前 L1 宏观研判（系统周度生成，供宏观/加减仓/换行业问题参考）】\n"
            + format_macro_for_prompt(wl_store))


def _experience_text(wl_store, account_ref: str = "") -> str:
    """往期复盘沉淀的经验卡片（scope='vip_portfolio'）并进 facts——顾问吸取自身历史教训、自我校准。

    复用 advisory 的取数与渲染（get_experience_cards + _render_experience_cards）。无卡/异常→空串
    （不塞占位，上层不拼）。account_ref 用于 scope_key，与 advisory 回填侧口径一致。
    """
    from bottleneck_hunter.vip.advisory import _render_experience_cards
    try:
        cards = wl_store.get_experience_cards(scope="vip_portfolio", scope_key=account_ref, limit=8)
    except Exception:  # noqa: BLE001
        cards = []
    if not cards:
        return ""
    return ("【往期复盘沉淀的经验卡片（历史教训，避免重蹈）】\n"
            + _render_experience_cards(cards))


def _advisory_ledger_text(wl_store, account_ref: str = "") -> str:
    """上一份账户诊断建议（精简）+ 顾问历史命中率台账并进 facts。

    用户问「最新账户策略/持仓该减该加」→ 引上一份诊断成品（投委会评审结论，历史快照带 generated_at）；
    问「你上次让我加的仓对了吗」→ 引命中率台账。两段独立降级，皆无→空串。
    复用 get_latest_advisory / build_review_ledger（后者只读零 LLM、口径唯一）。
    ponytail: 诊断只抽逐仓 action/理由 + 现金预算 + 主席综述（照 _latest_recommend_text 抽轻字段，
              不塞委员逐条语料）；台账只取末 10 行防上下文膨胀，全量在复盘页看。
    """
    from bottleneck_hunter.vip.advice_review import build_review_ledger
    from bottleneck_hunter.vip.advisory import get_latest_advisory
    parts: list[str] = []

    try:
        adv = get_latest_advisory(wl_store, account_ref=account_ref)
    except Exception:  # noqa: BLE001
        adv = None
    if adv:
        holds = [{"ticker": h.get("ticker", ""), "action": h.get("action", ""),
                  "reason": h.get("reason", ""), "risk": h.get("risk", "")}
                 for h in (adv.get("holdings") or []) if h.get("ticker")]
        body = {"generated_at": adv.get("generated_at", ""), "chair_summary": adv.get("chair_summary", ""),
                "portfolio_diagnosis": adv.get("portfolio_diagnosis", ""), "holdings": holds}
        parts.append("【上一份账户诊断建议（投委会评审成品·历史快照，引用须提示 generated_at 时效）】\n"
                     + json.dumps(body, ensure_ascii=False, default=str))

    try:
        led = build_review_ledger(wl_store)
    except Exception:  # noqa: BLE001
        led = None
    if led and (led.get("kpi") or {}).get("settled"):
        kpi = led.get("kpi") or {}
        tail = (led.get("ledger") or [])[:10]
        body = {"kpi": kpi, "recent": tail}
        parts.append("【顾问历史命中率与对错台账（回答「上次建议对不对」用）】\n"
                     + json.dumps(body, ensure_ascii=False, default=str))

    return "\n".join(parts)


# 从问题里抽疑似美股代码（1-6 位大写字母，可含点）；用于「用户点名个股 → 查该票研报」。
# 复用 live_quote 同款正则，但**不与持仓取交集**：研报可能属未持有的观察/候选票；库里无该票研报则自然返回空。
_TICKER_RE = re.compile(r"\b[A-Z]{1,6}(?:\.[A-Z]{1,2})?\b")
_STOCK_REPORT_MAX = 3          # 单次问答最多带几只个股研报，防上下文膨胀
_STOCK_REPORT_CHARS = 4000     # 每只研报截断字符（对齐决策中心个股研报前 4 页量级）


def _report_text(wl_store, question: str) -> str:
    """决策中心上传的研报原文并进 facts：宏观研报全局带 + 问题点名且库里确有研报的个股按需带。

    宏观：复用 macro_consultation._macro_report_block（内部自 .for_market(__macro__)、按用户隔离、
          含日期声明与缺失降级）。个股：抽问题里点名的 ticker，逐只 get_focus_report（已按 user+market
          隔离，A股票读不到美股研报），仅命中库里确有研报的票；最多 _STOCK_REPORT_MAX 只、每只截断。
    注意与 _macro_text 的区别：那是 L1 策略结论(系统产物)，这是研报 PDF 原文(输入原料)，二者互补。
    ponytail: 只带研报文本本身，不带 _focus_ticker_block 的财务/估值/新闻全套——dossier 已有，避免重复+膨胀。
    """
    from bottleneck_hunter.watchlist.macro_consultation import _macro_report_block
    parts: list[str] = []
    try:
        macro = _macro_report_block(wl_store)
    except Exception:  # noqa: BLE001
        macro = ""
    if macro:
        parts.append(macro)

    # 个股研报：去重、剔除已被宏观占用的语义无关短词由 get_focus_report 命中与否天然过滤（库无该键→None）
    seen: list[str] = []
    n_stock = 0
    for tk in _TICKER_RE.findall((question or "").upper()):
        if n_stock >= _STOCK_REPORT_MAX:
            break
        if tk in seen:
            continue
        seen.append(tk)
        try:
            rpt = wl_store.get_focus_report(tk)
        except Exception:  # noqa: BLE001
            rpt = None
        body = (rpt or {}).get("report_text") if rpt else ""
        if body and body.strip():
            parts.append(f"【{tk} 个股研报（用户上传·逐字摘录，以报告内注明日期为准）】\n"
                         + body.strip()[:_STOCK_REPORT_CHARS])
            n_stock += 1
    return "\n".join(parts)


_PROMPT = """你是私人财务AI顾问。请只依据下面的真实 facts 回答，不得编造金额/占比/股数。
若用户问到 facts 里没有的数据，请明确说“当前数据中没有该信息”。
回答要求：简体中文、专业但克制、分点回答，避免空泛。
下面的「本账户投资纲领」是用户为该账户设定的投资目标与约束，回答涉及建议时须与之一致。
你可围绕用户咨询就以下维度提供参考：
① 宏观研判——据 facts 内「L1 宏观研判」（市场状态/风险偏好/建议现金比例/板块轮动/风险因子），
   若 facts 含「全球宏观背景研报」或「个股研报」（用户上传的投行/机构研报原文），可据其逐字观点作答，
   但须以研报内注明日期为准、勿与系统快照日混淆；用户点名的个股若 facts 无其研报，说明系统暂无该票研报；
② 账户与持仓策略——据 dossier 逐仓事实 + 「上一份账户诊断建议」（历史快照，引用须点明其 generated_at 时效、非本次实时）；
③ 主要持仓与观察池候选的战术观点——据 dossier 持仓 + 「可选新标的候选池」；
④ 衍生品——据 dossier 的 derivative_exposure / derivative_summary / net_greeks / stress_test，
   可就敲入敲出障碍、杠杆、尾部风险给提示；facts 无相关字段则说明当前无衍生品数据；
⑤ 银行/券商推介产品分析——facts 内无此类数据；仅当用户在问题中贴出或上传了产品条款时，
   才按其条款 + 本账户投资纲领 + 现有持仓即席分析（收益结构/隐含风险/与纲领契合/是否与现有持仓重复暴露），
   并明确声明「以上仅据你所提供的条款、非系统留存数据」；用户未提供条款则说明无法凭空分析。
若用户要求推荐新的投资标的，只能从 facts 内「可选新标的候选池」中挑选（结合瓶颈环节与纲领契合度），
不得编造候选池以外的标的；池为空或未出现则如实说明观察池暂无合适候选、可先加入观察池跟踪。
若 facts 含「上一份荐新建议」，可引用其投委会结论作参考，但须说明它是历史快照（点明其 generated_at 生成时间），
提示如需最新研判请到荐新页重新生成；不得把旧建议当作本次实时结论。
引用「命中率台账」时如实呈现胜率，不夸大顾问过往表现。

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
    # 补齐 chat 此前独缺、advisory/recommend/strategy_review 都喂的三块背景：宏观锚点 / 账户诊断+命中率台账 / 经验卡片。
    # 顺序：宏观（背景）→ 账户诊断+台账（结论）→ 经验卡片（教训收尾）。各自已内建缺失降级，空段不拼。
    report_text = _report_text(wl_store, question)
    if report_text:
        facts_text = facts_text + "\n" + report_text
    macro_text = _macro_text(wl_store)
    if macro_text:
        facts_text = facts_text + "\n" + macro_text
    adv_text = _advisory_ledger_text(wl_store, account_ref=account_ref)
    if adv_text:
        facts_text = facts_text + "\n" + adv_text
    exp_text = _experience_text(wl_store, account_ref=account_ref)
    if exp_text:
        facts_text = facts_text + "\n" + exp_text
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


if __name__ == "__main__":
    # ponytail: 自检 —— 三个背景 helper：有数据→含标题块与关键字段；缺数据/抛异常→空串（不带崩、不塞占位）
    class _Cur:
        def __init__(self, row):
            self._row = row
        def execute(self, *a, **k):
            return self
        def fetchone(self):
            return self._row
        def close(self):
            pass

    class _FakeStore:
        _market = "us_stock"
        def __init__(self, *, macro=None, cards=None, adv_row=None, settled=None, stats=None, reports=None):
            self._macro = macro
            self._cards = cards
            self._adv_row = adv_row
            self._settled = settled or []
            self._stats = stats or []
            self._reports = reports or {}
        def get_latest_macro_strategy(self):
            if self._macro is None:
                raise RuntimeError("no macro")
            return self._macro
        def get_experience_cards(self, **k):
            if self._cards == "boom":
                raise RuntimeError("boom")
            return self._cards or []
        def _connect(self):
            return _Cur(self._adv_row)
        def _filtered(self, sql, params):
            return sql, params
        def list_settled_predictions(self, **k):
            return self._settled
        def get_model_accuracy_stats(self, **k):
            return self._stats
        def for_market(self, m):
            return self
        def get_focus_report(self, tk):
            body = self._reports.get(tk)
            return {"report_text": body} if body else None

    # 1) 宏观：有 regime → 标题+regime；缺失(raise)→仍非空(降级句)且含标题（永不空段）
    t = _macro_text(_FakeStore(macro={"regime": "bull", "risk_appetite": "balanced"}))
    assert "L1 宏观研判" in t and "bull" in t, t
    assert "L1 宏观研判" in _macro_text(_FakeStore(macro=None))

    # 2) 经验卡片：有卡→标题+正文；空→""；异常→""
    t = _experience_text(_FakeStore(cards=[{"title": "别追高", "content": "回撤后再入", "category": "纪律"}]))
    assert "经验卡片" in t and "别追高" in t, t
    assert _experience_text(_FakeStore(cards=[])) == ""
    assert _experience_text(_FakeStore(cards="boom")) == ""

    # 3) 诊断+台账：有诊断+已结台账→两段齐；全缺→""；store 缺方法(异常)→""
    _adv = {"generated_at": "2026-08-01T00:00:00+00:00", "chair_summary": "整体持有",
            "portfolio_diagnosis": "集中度偏高",
            "holdings": [{"ticker": "MU", "action": "减仓", "reason": "估值高", "risk": "周期"}]}
    _row = {"result_json": json.dumps(_adv, ensure_ascii=False), "created_at": "2026-08-01"}
    _settled = [{"prediction_date": "2026-07-01", "ticker": "MU", "prediction_value": "加仓",
                 "prediction_type": "vip_advice", "outcome_value": "chg=+5%", "is_correct": 1}]
    _stats = [{"role_context": "vip_advisor", "total": 10, "correct": 6, "pending": 2}]
    t = _advisory_ledger_text(_FakeStore(adv_row=_row, settled=_settled, stats=_stats))
    assert "账户诊断建议" in t and "MU" in t and "命中率" in t, t
    assert _advisory_ledger_text(_FakeStore(adv_row=None, settled=[], stats=[])) == ""
    assert _advisory_ledger_text(object()) == ""  # 缺方法→双段异常降级→空串

    # 4) 研报：宏观全局带 + 问题点名且库有→带；点名但库无→不带；未点名→不带；超上限截断
    from bottleneck_hunter.watchlist.macro_consultation import MACRO_REPORT_KEY
    _rs = _FakeStore(reports={MACRO_REPORT_KEY: "宏观周报：软着陆", "NVDA": "英伟达研报正文" * 500, "MU": "美光研报"})
    t = _report_text(_rs, "NVDA 和 MU 现在能追吗")
    assert "全球宏观背景研报" in t and "宏观周报" in t, t
    assert "NVDA 个股研报" in t and "MU 个股研报" in t, t
    assert len(t) < 200 + _STOCK_REPORT_CHARS * 2 + 2000, "个股研报应按 _STOCK_REPORT_CHARS 截断"
    # 点名但库无该票研报→不带该段（诚实无中生有）
    assert "TSLA 个股研报" not in _report_text(_rs, "TSLA 怎么看")
    # 未点名个股→只有宏观段、无任何个股段
    assert "个股研报" not in _report_text(_rs, "现在大盘怎么看")

    print("chat 背景 helper 自检通过：宏观兜底 / 经验卡片降级 / 诊断+台账拼装与异常降级 / 研报全局+按需")
