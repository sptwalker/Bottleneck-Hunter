"""Phase C · VIP 个性化荐新 pass —— 从观察池里挑「尚未持有」的标的给建仓建议，与 advisory.py 互补。

设计（用户拍板，advisory.py 的近镜像克隆）：
- 系统是**周期性顾问**，只出建议不下单；本 pass 结合账户档案（dossier）+ 投资纲领 + L1 宏观，
  对**观察池中该账户尚未持有**的候选标的给 建仓/关注/规避 + 理由 + 风险 + 与纲领契合 + 软仓位。
- 候选来源 = wl_store.list_all()（已按 tier→综合分排序、已 for_market 隔离）；代码侧预过滤：
  去已持 + 命中纲领排除词 + 保序取前 N（不静默截断，丢弃集回传前端）。
- 复用投委会 4 persona（committee.MEMBERS + _review_single）+ advisory 的确定性合议 _consensus——
  **不**碰 run_committee_review（它强绑 sim_account，会污染模拟盘表）。
- 落 vip_recommendations（独立表），不写任何 sim_* 表。审计复用 create_advice_audit（advice_type=recommendation，
  靠 source_data_ref.kind=new_pick 与周期报告区分）。

流程：候选池(纯逻辑) → 草案(1 次 vip_advisor LLM) → 4 persona 并行评审 → 确定性合议 → number_guard → 落库。
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone

from bottleneck_hunter.vip import portfolio, mandate as _mandate
from bottleneck_hunter.vip import compliance, number_guard
from bottleneck_hunter.vip.advisory import (
    _consensus, format_macro_for_prompt, build_committee_context,
    committee_corpus, annotate_committee, reconcile_draft,
    VIP_ROLE_CONTEXT, VIP_PT_RECOMMEND)  # 复用勿重写
from bottleneck_hunter.chain.json_utils import extract_json_object

_ACTIONS = {"关注", "建仓", "规避"}
_DEFAULT_ACTION = "关注"
_CAP = 30  # 喂 prompt 的候选上限
# tier→定性优先级：只给档位，不把 composite_score 具体分值喂 LLM（避免其引用后被 number_guard 误标 ⚠）
_TIER_PRIORITY = {"focus": "高", "normal": "中", "track": "观察"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


_DRAFT_PROMPT = """你是一支资深私人财务顾问团队，为高净值客户从候选池里挑选**尚未持有的新标的**并给建仓建议。
只依据下面给出的真实数据，不得编造任何价格/PE/目标价/收益率/占比/股数。系统是周期性顾问，只出建议、不下单。

## 账户档案（结算单事实口径：真实权益=股票+现金，不含衍生品估值；含当前逐仓，供判断腾挪空间）
{dossier}

{mandate}

## 当前宏观研判（L1，只读）
{macro}

## 候选标的（本账户可交易市场观察池中「尚未持有」的标的，已按系统综合评分从高到低排列；综合评分仅给优先级档位，无可引用的具体分值）
{candidates}

请输出**严格 JSON**（不要 markdown 代码块、不要 JSON 以外任何文字）：
{{
  "portfolio_note": "组合层面再平衡建议 2-4 句：结合当前持仓集中度/行业暴露与纲领，说明新增标的应如何配置、腾挪空间从何而来",
  "candidates": [
    {{"ticker": "标的代码", "action": "关注|建仓|规避", "reason": "如何融入现有组合（贴合纲领与档案，不引用具体价格数字）", "risk": "该标的主要风险", "fit": "与本账户投资纲领的契合度", "suggested_weight": "软仓位区间如 3%-5%，或留空"}}
  ]
}}
要求：action 只能是 关注/建仓/规避 三选一；**命中纲领【排除/禁投清单】的标的必须 action=规避 并说明（硬约束）**；综合评分用定性表述（高/中/低优先级）不得引用具体分值；不得编造价格/PE/目标价/收益率；不必覆盖全部候选，聚焦最值得出手的若干只；简体中文。"""


def _tokenize_exclusions(text: str) -> list[str]:
    """把纲领排除清单切成小写去重词表。ponytail: 只保留 len>=2 的词——
    单字 token（如"股"）会子串命中几乎所有标的、把整个候选池清空（money-path bug）。"""
    seen, out = set(), []
    for p in re.split(r"[、,，;；/\s]+", text or ""):
        t = p.strip().lower()
        if len(t) >= 2 and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _excluded(entry: dict, tokens: list[str]) -> bool:
    """排除词子串命中 ticker/公司名(中英)/行业 任一即视为命中（大小写不敏感、高精度）。"""
    if not tokens:
        return False
    hay = " ".join(str(entry.get(k) or "") for k in
                   ("ticker", "company_name", "company_name_cn", "sector")).lower()
    return any(tok in hay for tok in tokens)


def build_candidate_pool(entries: list[dict], held, excl_tokens: list[str], *, cap: int = _CAP) -> dict:
    """纯逻辑（money path）：从观察池 entries 剔除已持 + 命中排除词，保序取前 cap。

    entries 已按 tier→综合分排好序、已 for_market 隔离。返回 candidates + stats（丢弃集回传，不静默截断）。
    """
    held_set = {str(h).strip().upper() for h in (held or []) if str(h).strip()}
    dropped_held, dropped_excluded, kept = [], [], []
    for e in (entries or []):
        tk = str(e.get("ticker") or "").strip()
        if not tk:
            continue
        if tk.upper() in held_set:
            dropped_held.append(tk)
            continue
        if _excluded(e, excl_tokens):
            dropped_excluded.append(tk)
            continue
        kept.append(e)
    shown = kept[:cap]
    candidates = [{
        "id": str(e.get("id") or "").strip(),  # 观察池 entry_id，供逐名背景取催化剂（不喂 LLM，_prompt_candidate 不含它）
        "ticker": str(e.get("ticker") or "").strip(),
        "company_name_cn": str(e.get("company_name_cn") or e.get("company_name") or "").strip(),
        "sector": str(e.get("sector") or "").strip(),
        "tier": str(e.get("tier") or "").strip(),
        "composite_score": e.get("composite_score"),
        "bottleneck_node": str(e.get("bottleneck_node") or "").strip(),
    } for e in shown]
    return {"candidates": candidates,
            "stats": {"n_total": len(entries or []), "n_after_excl": len(kept), "n_shown": len(shown),
                      "capped": len(kept) > cap,
                      "dropped_held": dropped_held, "dropped_excluded": dropped_excluded}}


def _prompt_candidate(c: dict) -> dict:
    """喂 LLM 的精简候选：tier 折成定性优先级，不含可引用的具体综合分。"""
    return {"ticker": c["ticker"], "name": c["company_name_cn"], "sector": c["sector"],
            "bottleneck_node": c["bottleneck_node"],
            "priority": _TIER_PRIORITY.get(c.get("tier", ""), "中")}


def _build_inputs(wl_store, account_ref: str) -> dict:
    """聚合本 pass 的只读输入。绝不带崩：任一子项缺失走降级。"""
    dossier = portfolio.build_account_dossier(wl_store, account_ref=account_ref)
    mandate_text = _mandate.format_mandate_for_prompt(wl_store, account_ref=account_ref)
    mandate_obj = _mandate.load_mandate(wl_store, account_ref=account_ref)
    macro_text = format_macro_for_prompt(wl_store)
    # ponytail: 候选＝该市场观察池全量（list_all 已 for_market 隔离）。"本账户可交易市场"用 store 的 market 桶近似
    #           （有持仓＝已在该市场活动）；升级路径＝账户级可交易市场白名单，单用户工具暂不需要。
    try:
        entries = wl_store.list_all()
    except Exception:  # noqa: BLE001
        entries = []
    held = [h.get("ticker") for h in dossier.get("holdings", [])]
    excl = _tokenize_exclusions(mandate_obj.get("exclusions", ""))
    pool = build_candidate_pool(entries, held, excl)
    return {"dossier": dossier, "mandate_text": mandate_text, "macro_text": macro_text, "pool": pool}


def _validate_draft(raw: str) -> dict:
    """解析草案 JSON + 规范化 action（非法→关注），保证 candidates 结构可渲染。"""
    data = extract_json_object(raw) or {}
    cands = []
    for c in (data.get("candidates") or []):
        if not isinstance(c, dict):
            continue
        action = str(c.get("action", "")).strip()
        if action not in _ACTIONS:
            action = _DEFAULT_ACTION
        cands.append({
            "ticker": str(c.get("ticker", "")).strip(),
            "action": action,
            "reason": str(c.get("reason", "")).strip(),
            "risk": str(c.get("risk", "")).strip(),
            "fit": str(c.get("fit", "")).strip(),
            "suggested_weight": str(c.get("suggested_weight", "")).strip(),
        })
    return {"portfolio_note": str(data.get("portfolio_note", "")).strip(), "candidates": cands}


def _annotate(draft: dict, corpus: str) -> list[str]:
    """给草案文本里未在档案/纲领语料中出现的数字加⚠，返回未核到 token。
    只扫 reason/risk/fit + portfolio_note——suggested_weight 是软仓位建议（非事实断言），标⚠会误导，不扫。"""
    unverified: list[str] = []

    def ann(text: str) -> str:
        if not text:
            return text
        unverified.extend(r["token"] for r in number_guard.verify_numbers(text, corpus) if r["status"] == "unverified")
        return number_guard.annotate_unverified(text, corpus)

    draft["portfolio_note"] = ann(draft["portfolio_note"])
    for c in draft["candidates"]:
        c["reason"] = ann(c["reason"])
        c["risk"] = ann(c["risk"])
        c["fit"] = ann(c["fit"])
    seen, out = set(), []
    for t in unverified:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


async def generate_account_recommendations(wl_store, *, account_ref: str = "", user_id: str = "", budget=None) -> dict:
    """生成并落库账户荐新建议。返回 {recommendation_id, result}。空 ref/无持仓/空池/无模型/预算不足 → 明确 error。"""
    from bottleneck_hunter.llm_clients.factory import get_models_for_role
    from bottleneck_hunter.watchlist.committee import MEMBERS, _review_single
    import asyncio

    account_ref = (account_ref or "").strip()
    if not account_ref:
        # 硬守卫：空 ref 会经 build_account_dossier→get_sim_account("") 落到决策中心 sim_account('')（读并惰性建行）。
        # 前端 requireConcreteAccount 已挡，此处补后端防护。见 memory:dc_sim_account_decoupled。
        return {"error": "请先选择具体子账户再生成荐新建议"}
    inputs = _build_inputs(wl_store, account_ref)
    dossier = inputs["dossier"]
    if not dossier.get("holdings"):
        # 无持仓＝无法确认本账户可交易市场（市场代理失败），先要求上传月结单
        return {"error": "该账户暂无持仓，无法确认本账户可交易市场，请先上传月结单"}
    pool = inputs["pool"]
    if not pool["candidates"]:
        return {"error": "当前市场观察池暂无可荐标的（可能已全部持有或被纲领排除）"}
    if budget is not None and not budget.can_spend():
        return {"error": "预算不足，暂不生成荐新建议"}
    models = get_models_for_role("vip_advisor", user_id=user_id, with_fallback=True)
    if not models:
        return {"error": "无可用 LLM（请在 AI 配置中为 vip_advisor 配置模型）"}
    llm, provider, model = models[0]

    # ── 1) 草案生成 ──
    prompt = _DRAFT_PROMPT.format(
        dossier=json.dumps(dossier, ensure_ascii=False, default=str),
        mandate=inputs["mandate_text"], macro=inputs["macro_text"],
        candidates=json.dumps([_prompt_candidate(c) for c in pool["candidates"]], ensure_ascii=False))
    resp = await llm.ainvoke(prompt)
    draft = _validate_draft(getattr(resp, "content", resp) if not isinstance(resp, str) else resp)
    if not draft["candidates"]:
        return {"error": "草案生成失败或未返回荐新标的，请重试"}

    # ── 2) 投委会 4 persona 并行评审（复用 committee._review_single；组合风险取自持仓、逐名背景取自候选，不碰 sim 表）──
    context = build_committee_context(
        wl_store, dossier, inputs["macro_text"],
        [{"ticker": c["ticker"], "entry_id": c.get("id")} for c in pool["candidates"]])
    # ponytail: 委员会对整批荐股出一个总裁决（镜像 advisory 的整体 draft 评审），非逐标的 N×4 次评审
    exec_plan = {"account_ref": account_ref, "mandate": inputs["mandate_text"], "draft": draft,
                 "current_holdings": [h.get("ticker") for h in dossier.get("holdings", [])]}
    reviews = await asyncio.gather(*[_review_single(m, exec_plan, context) for m in MEMBERS],
                                   return_exceptions=True)
    reviews = [r for r in reviews if isinstance(r, dict)]

    # ── 3) 确定性合议 + number_guard（草案 + 委员叙述都过防伪；corpus 含喂进委员会的真实数）──
    committee = _consensus(reviews, store=wl_store, market=getattr(wl_store, "_market", "") or "")
    # corpus = dossier + mandate + macro + 喂委员会的真实数（候选价财事实不在其中——草案编造候选价格本应被 ⚠，这是正确的安全行为）
    corpus = (json.dumps(dossier, ensure_ascii=False, default=str)
              + "\n" + inputs["mandate_text"] + "\n" + inputs["macro_text"]
              + "\n" + committee_corpus(context))
    unverified = _annotate(draft, corpus)
    unverified = list(dict.fromkeys(unverified + annotate_committee(committee, corpus)))

    # ── 3b) 草案↔投委会对账：reject→建仓降关注、caution/split→加警示注（memory:vip_advisory_pass 用户已确认强度）──
    reconciled = reconcile_draft(draft["candidates"], "action", {"建仓": "关注"}, committee)

    result = {
        "account_ref": account_ref,
        "generated_at": _now_iso(),
        "portfolio_note": draft["portfolio_note"],
        "candidates": draft["candidates"],
        "pool_stats": pool["stats"],
        "committee": committee,
        "unverified": unverified,
        "reconciled": reconciled,
        "provider": provider, "model": model,
        "disclaimer": compliance.DISCLAIMER_ZH,
    }

    # ── C-1 复盘打点（record_prediction，只写不评）：为 5b 复盘启动数据时钟。role_context=vip_advisor +
    #    prediction_type=vip_recommend 独占桶，与 sim 的 committee_*/vote 及 advisory 的 vip_advice 都区分开；
    #    旁路容错——打点失败只 debug、绝不影响荐新主链路。记录 reconcile 后的最终动作。
    try:
        mkt = getattr(wl_store, "_market", "") or ""
        for c in draft["candidates"]:
            if c.get("ticker"):
                wl_store.record_prediction(
                    provider=provider, model=model, role_context=VIP_ROLE_CONTEXT,
                    ticker=c["ticker"], prediction_type=VIP_PT_RECOMMEND,
                    prediction_value=c["action"], market=mkt)
    except Exception:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).debug("VIP recommend 复盘打点失败（不影响荐新）", exc_info=True)

    # ── 4) 落库（独立表，绝不写 sim_*）──
    rid = uuid.uuid4().hex[:12]
    with wl_store._write_conn() as conn:
        conn.execute(
            f"""INSERT INTO vip_recommendations (id, account_ref, result_json, provider, model, created_at{wl_store._user_insert_cols()}{wl_store._market_insert_cols()})
               VALUES (?,?,?,?,?,?{wl_store._user_insert_vals()}{wl_store._market_insert_vals()})""",
            (rid, account_ref, json.dumps(result, ensure_ascii=False, default=str), provider, model, _now_iso())
            + wl_store._user_insert_params() + wl_store._market_insert_params(),
        )

    # ── 5) 审计留痕（auth.db，无 PII 金额；advice_type 复用 recommendation，靠 kind=new_pick 与周期报告区分）──
    try:
        import hashlib
        from bottleneck_hunter.auth.store import AuthStore
        uid = getattr(wl_store, "_user_id", "") or user_id or ""
        if uid:
            AuthStore().create_advice_audit(
                uid, advice_type="recommendation", advice_ref=rid,
                source_data_ref={"account_ref": account_ref, "verdict": committee.get("verdict", ""),
                                 "kind": "new_pick", "tickers": [c["ticker"] for c in draft["candidates"]]},
                model_provider=provider, model_name=model,
                disclaimer_version=compliance.DISCLAIMER_VERSION,
                content_hash=hashlib.sha256(
                    json.dumps(result, ensure_ascii=False, default=str, sort_keys=True).encode()).hexdigest(),
                market=getattr(wl_store, "_market", "") or "us_stock")
    except Exception:  # noqa: BLE001
        pass

    if budget is not None:
        try:
            budget.record(provider, model, len(prompt) // 3, 500, "vip_advisor")
        except Exception:  # noqa: BLE001
            pass
    return {"recommendation_id": rid, "result": result}


def get_latest_recommendations(wl_store, account_ref: str = "") -> dict | None:
    """读该账户最近一份荐新建议（供前端进标签页回显）。"""
    account_ref = (account_ref or "").strip()
    conn = wl_store._connect()
    try:
        q, p = wl_store._filtered(
            "SELECT result_json, created_at FROM vip_recommendations WHERE account_ref = ? ORDER BY created_at DESC LIMIT 1",
            (account_ref,))
        row = conn.execute(q, p).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    try:
        return json.loads(row["result_json"])
    except (json.JSONDecodeError, TypeError):
        return None


def list_recommendations(wl_store, account_ref: str = "", limit: int = 20) -> list[dict]:
    """读该账户历史荐新建议（新→旧）。每条带完整 result，前端点选即回看。"""
    account_ref = (account_ref or "").strip()
    limit = max(1, min(int(limit or 20), 100))  # 已 clamp 为 int，直插 SQL 无注入风险
    conn = wl_store._connect()
    try:
        q, p = wl_store._filtered(
            f"SELECT id, result_json, provider, model, created_at FROM vip_recommendations "
            f"WHERE account_ref = ? ORDER BY created_at DESC LIMIT {limit}",
            (account_ref,))
        rows = conn.execute(q, p).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        try:
            result = json.loads(r["result_json"])
        except (json.JSONDecodeError, TypeError):
            continue
        out.append({"id": r["id"], "created_at": r["created_at"],
                    "provider": r["provider"], "model": r["model"],
                    "verdict": (result.get("committee") or {}).get("verdict", ""),
                    "n_candidates": len(result.get("candidates") or []), "result": result})
    return out


if __name__ == "__main__":
    # ponytail 自检：纯逻辑（候选池 / 草案规范化 / number_guard 字段范围 / 空 ref 硬守卫）——LLM 路径需真实模型，不入自检
    POOL = [
        {"ticker": "NVDA", "company_name_cn": "英伟达", "sector": "半导体", "tier": "focus", "composite_score": 9.1, "bottleneck_node": "GPU"},
        {"ticker": "MU", "company_name_cn": "美光", "sector": "半导体存储", "tier": "focus", "composite_score": 8.4, "bottleneck_node": "HBM"},
        {"ticker": "KWEB", "company_name_cn": "中概互联ETF", "sector": "杠杆ETF", "tier": "normal", "composite_score": 6.0, "bottleneck_node": ""},
        {"ticker": "BE", "company_name_cn": "博隆能源", "sector": "燃料电池", "tier": "track", "composite_score": 5.2, "bottleneck_node": "SOFC"},
    ]

    # 1) 候选池：已持 NVDA 剔除；排除词"杠杆ETF"命中 KWEB(sector)、"博隆"命中 BE(公司名)；保序
    pool = build_candidate_pool(POOL, held=["nvda"], excl_tokens=_tokenize_exclusions("杠杆ETF、博隆"))
    assert [c["ticker"] for c in pool["candidates"]] == ["MU"], pool
    assert pool["stats"]["dropped_held"] == ["NVDA"], pool
    assert set(pool["stats"]["dropped_excluded"]) == {"KWEB", "BE"}, pool
    assert pool["stats"]["n_total"] == 4 and pool["stats"]["n_after_excl"] == 1, pool

    # 2) 单字 token 守卫：排除串产生的孤立"股"不得清空全池（len<2 被丢弃）
    toks = _tokenize_exclusions("股，白酒")
    assert "股" not in toks and "白酒" in toks, toks
    pool2 = build_candidate_pool(POOL, held=[], excl_tokens=toks)
    assert pool2["stats"]["n_after_excl"] == 4, pool2  # 无一被"股"误杀

    # 3) cap：40 进 30 出，capped=True，保序（首项综合分最高）
    big = [{"ticker": f"T{i}", "composite_score": 100 - i, "tier": "normal"} for i in range(40)]
    pc = build_candidate_pool(big, held=[], excl_tokens=[], cap=30)
    assert pc["stats"]["n_shown"] == 30 and pc["stats"]["capped"] is True, pc["stats"]
    assert pc["candidates"][0]["ticker"] == "T0", pc["candidates"][0]  # 保序：入序首项
    assert build_candidate_pool([], [], [])["candidates"] == []       # 空输入

    # 4) _excluded 大小写不敏感
    assert _excluded({"ticker": "TSLA"}, ["tsla"]) is True
    assert _excluded({"sector": "半导体"}, ["半导体"]) is True
    assert _excluded({"ticker": "AAPL"}, ["tsla"]) is False

    # 5) 草案：剥 fence；非法 action→关注；缺字段 coerce；suggested_weight 自由串保留
    d = _validate_draft('```json\n{"portfolio_note":"现金充裕可加半导体",'
                        '"candidates":[{"ticker":"MU","action":"买入","reason":"补存储","risk":"周期","fit":"契合科技聚焦","suggested_weight":"3%-5%"},'
                        '{"ticker":"BE","action":"规避"}]}\n```')
    assert d["candidates"][0]["action"] == "关注", d          # 非法"买入"→关注
    assert d["candidates"][0]["suggested_weight"] == "3%-5%"
    assert d["candidates"][1]["action"] == "规避" and d["candidates"][1]["reason"] == ""
    assert d["portfolio_note"] == "现金充裕可加半导体"

    # 6) _annotate 字段范围：reason 里 $999 被标 ⚠；语料含的 72% 不误标；suggested_weight 的 12% 不扫（不进 unverified）
    draft = {"portfolio_note": "集中度 72%", "candidates": [
        {"reason": "占比 72%，另有 $999 臆造", "risk": "回撤", "fit": "契合", "suggested_weight": "8%-12%"}]}
    unv = _annotate(draft, '{"top5":72}')
    assert "$999" in unv and "72%" not in unv, unv
    assert "12%" not in unv and "8%" not in unv, unv                  # suggested_weight 未扫
    assert "$999 ⚠未核到" in draft["candidates"][0]["reason"], draft   # 就地标注生效

    # 7) 空 account_ref 硬守卫：碰 wl_store 之前就返回 error（否则会读/惰性写决策中心 sim_account('')）
    import asyncio as _aio

    class _Boom:
        def __getattr__(self, _):
            raise AssertionError("空 ref 不应触碰 wl_store")
    for ref in ("", "  "):
        r = _aio.run(generate_account_recommendations(_Boom(), account_ref=ref))
        assert r.get("error"), r

    print("recommend self-check OK")
