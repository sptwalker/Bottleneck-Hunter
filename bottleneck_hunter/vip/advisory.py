"""Phase B · VIP 顾问决策 pass —— 账户级「建议层」，独立于决策中心 L1-L3 模拟盘。

设计（用户拍板）：
- 系统是**周期性顾问**，不下单不执行；本 pass 只出建议（每仓 减/持/加 + 理由 + 风险 + 衍生品提示 + 跨市场覆盖旗标）。
- 单一事实源 = build_account_dossier（真实权益/逐仓成本盈亏/衍生品敞口/新鲜度）+ 本账户投资纲领 + L1 宏观（**只读** get_latest_macro_strategy，不重跑）。
- 复用投委会 4 persona（committee.MEMBERS + _review_single）对草案做独立评审——**不**碰 run_committee_review
  （它强绑 sim_account/execution_plans/gating，会污染模拟盘表）。
- 落 vip_advisory（独立表），不写任何 sim_* 表。

流程：草案生成(1 次 vip_advisor LLM) → 4 persona 并行评审 → 确定性合议(不再多花 1 次 LLM) → number_guard → 落库。
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone

from bottleneck_hunter.vip import portfolio, derivatives, mandate as _mandate
from bottleneck_hunter.vip import compliance, number_guard
from bottleneck_hunter.chain.json_utils import extract_json_object

_ACTIONS = {"减仓", "持有", "加仓"}

# ── C-1 复盘打点隔离常量（recommend.py 导入复用）──
# VIP 建议的 record_prediction 打点必须与决策中心模拟盘校准物理隔离：sim 用 role_context="committee_{role}"
# + prediction_type="vote"，二者聚合口径(get_model_accuracy_stats 按 role_context、get_calibration_weight 按
# role_context+user_id+market)中**唯一干净隔离维度是 role_context**（user_id/market 与 sim 共享）。故 VIP 独占
# role_context="vip_advisor" + 独立 prediction_type，则 sim 的 _consensus 读 committee_* 桶零污染，VIP 自身亦然。
VIP_ROLE_CONTEXT = "vip_advisor"
VIP_PT_ADVICE = "vip_advice"
VIP_PT_RECOMMEND = "vip_recommend"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


_DRAFT_PROMPT = """你是一支资深私人财务顾问团队，为高净值客户的真实证券账户给出**持仓层面的操作建议**。
只依据下面给出的真实数据，不得编造任何价格/收益/占比/股数。系统是周期性顾问，只出建议、不下单。

## 账户档案（结算单事实口径：真实权益=股票+现金，不含衍生品估值；逐仓含成本/未实现盈亏）
{dossier}

{mandate}

## 当前宏观研判（L1，只读）
{macro}

## 衍生品敞口风险（路径依赖/敲出，须单独提示）
{derivatives}

## 价源覆盖（代码判定，非你臆测）
{coverage}

请输出**严格 JSON**（不要 markdown 代码块、不要 JSON 以外任何文字）：
{{
  "portfolio_diagnosis": "组合层面诊断 2-4 句：集中度/行业暴露/与纲领风险偏好和回撤上限的匹配度",
  "cross_market_coverage": "针对上面【价源覆盖】里代码判定为无活跃价源的标的，解释其市值为结算单结转、判断须谨慎的含义；不要自行臆测哪些标的未覆盖",
  "holdings": [
    {{"ticker": "标的代码", "action": "减仓|持有|加仓", "reason": "建议理由（贴合纲领与档案数字）", "risk": "该仓主要风险", "derivative_note": "若该标的有衍生品敞口则提示，否则留空"}}
  ]
}}
要求：holdings 覆盖档案里每一只持仓；action 只能是 减仓/持有/加仓 三选一；**须遵守纲领的回撤上限与排除清单（硬约束）**；简体中文。"""


def _render_coverage(dossier: dict) -> str:
    """把 dossier 里代码判定的价源覆盖压成事实文本，供 prompt 注入（LLM 只解释不判定）。"""
    pc = dossier.get("price_coverage") or {}
    unc = pc.get("uncovered") or []
    if not unc:
        return f"全部 {pc.get('n_total', 0)} 只持仓/衍生品标的均有活跃价源，市值为最新行情。"
    return (f"以下 {len(unc)} 只标的【无活跃价源】（港股/ISIN 无 yfinance 映射，或美股未回填快照），"
            f"其市值为结算单结转价、非最新行情，对其的任何加减仓判断须明确标注此不确定性："
            f"{'、'.join(unc)}。其余 {pc.get('n_covered', 0)} 只有最新价源。")


_MACRO_FALLBACK = "暂无最新宏观研判，请按中性稳健处理。"


def format_macro_for_prompt(wl_store) -> str:
    """把 L1 宏观研判压成结构化文本供 prompt/投委会注入。缺失/异常降级为中性稳健，绝不带崩。

    ponytail: 只读 get_latest_macro_strategy 全字段（regime/风险偏好/建议现金/综述/板块轮动/风险因子/关键信号）；
              任一段缺则跳过该段，全缺走降级句。以前只取 market_summary 一段，L1 的仓位/风险信号丢失。
    """
    try:
        macro = wl_store.get_latest_macro_strategy() if hasattr(wl_store, "get_latest_macro_strategy") else None
    except Exception:  # noqa: BLE001
        macro = None
    if not macro:
        return _MACRO_FALLBACK

    def _flat(v) -> str:
        return "/".join(str(x) for x in v) if isinstance(v, (list, tuple)) else str(v)

    parts: list[str] = []
    head = []
    if str(macro.get("regime", "") or "").strip():
        head.append(f"市场状态 {str(macro['regime']).strip()}")
    if str(macro.get("risk_appetite", "") or "").strip():
        head.append(f"风险偏好 {str(macro['risk_appetite']).strip()}")
    if macro.get("recommended_cash_pct") is not None:
        head.append(f"建议现金比例 {macro['recommended_cash_pct']}%")
    if head:
        parts.append("L1 研判：" + " · ".join(head))
    if str(macro.get("market_summary", "") or "").strip():
        parts.append(f"市场综述：{str(macro['market_summary']).strip()}")
    rotation = macro.get("sector_rotation") or {}
    if isinstance(rotation, dict) and rotation:
        items = "；".join(f"{k}: {_flat(v)}" for k, v in rotation.items() if str(v).strip())
        if items:
            parts.append(f"板块轮动：{items}")
    risks = [str(x).strip() for x in (macro.get("risk_factors") or []) if str(x).strip()]
    if risks:
        parts.append("风险因子：" + "、".join(risks))
    signals = [str(x).strip() for x in (macro.get("key_signals") or []) if str(x).strip()]
    if signals:
        parts.append("关键信号：" + "、".join(signals))
    return "\n".join(parts).strip() or _MACRO_FALLBACK


_BG_CAP = 15  # 逐名背景上限（本地 DB 读，够 committee 判断；超限只标注不静默截断）


def build_committee_context(wl_store, dossier: dict, macro_text: str, bg_items: list[dict], *, market: str = "") -> dict:
    """组装投委会 4 席评审的真实上下文，全部复用决策中心本地-DB 取数（零新增网络面）。

    - portfolio_risk：decision_engine._portfolio_risk_summary（真实 HHI/VaR/CVaR/beta/相关性），
      positions 直接沿用 dossier holdings 已算好的 weight_pct/market_value。
    - valuation/sentiment/crowding/peer/catalyst：committee.build_ticker_background 逐名聚合 {ticker: 段}。
    bg_items: [{"ticker","entry_id"}]——advisory 传持仓（holdings 无 entry_id→催化剂段自然"暂无"），
              recommend 传候选（观察池 entry 有 id→估值/催化剂全亮）。
    ponytail: 逐名背景上限 _BG_CAP=15，超限 coverage_note 标"仅前 N 只"不静默截断；
              非观察池标的→估值/情绪段回退"暂无"（诚实降级不编造）；
              全 sector 未知→去掉 max_sector_weight（单桶算 100% 会误报>40%告警）。
    """
    from bottleneck_hunter.watchlist.committee import build_ticker_background
    from bottleneck_hunter.watchlist.decision_engine import _portfolio_risk_summary

    market = market or getattr(wl_store, "_market", "") or ""
    holdings = dossier.get("holdings", []) or []
    items = [it for it in (bg_items or []) if it.get("ticker")]

    # ── 逐名真实背景（本地 DB）→ 按段聚合 {ticker: data}；各段取数失败自然降级"暂无" ──
    backgrounds: dict[str, dict] = {}
    for it in items[:_BG_CAP]:
        try:
            backgrounds[it["ticker"]] = build_ticker_background(
                wl_store, it["ticker"], it.get("entry_id") or "", market)
        except Exception:  # noqa: BLE001
            continue

    def _seg(key: str):
        m = {tk: bg.get(key) for tk, bg in backgrounds.items() if bg.get(key) not in (None, "", [], {})}
        return m or "暂无"

    def _sector(tk: str) -> str:  # sector 顺手从背景估值段取（同批 DB 读，不额外查）
        v = backgrounds.get(tk, {}).get("valuation_data")
        return (str(v.get("sector")).strip() if isinstance(v, dict) and v.get("sector") else "") or "未知"

    # ── 真实组合风险（holdings 已带 weight_pct/market_value，直接映射 positions）──
    positions = [{"ticker": h.get("ticker", ""), "market_value": h.get("market_value") or 0,
                  "weight_pct": h.get("weight_pct") or 0, "sector": _sector(h.get("ticker", ""))}
                 for h in holdings if h.get("ticker")]
    try:
        portfolio_risk = _portfolio_risk_summary(wl_store, positions, dossier.get("total_equity") or 0) or {}
    except Exception:  # noqa: BLE001
        portfolio_risk = {}
    if portfolio_risk and positions and all(p["sector"] == "未知" for p in positions):
        portfolio_risk.pop("max_sector_weight_pct", None)
        portfolio_risk["sector_note"] = "板块数据不全，未计算板块集中度"
    if not portfolio_risk:  # 空持仓/异常兜底：退回 dossier 薄口径，至少给委员集中度
        portfolio_risk = {"top5_concentration_pct": dossier.get("top5_concentration_pct"),
                          "n_holdings": dossier.get("n_holdings")}
    portfolio_risk.setdefault("derivative_exposure", dossier.get("derivative_exposure", []))

    context = {
        "macro_summary": macro_text,
        "account_status": {
            "total_equity": dossier.get("total_equity"), "cash_balance": dossier.get("cash_balance"),
            "positions": [{"ticker": h.get("ticker"), "shares": h.get("shares"),
                           "market_value": h.get("market_value"), "avg_cost": h.get("avg_cost"),
                           "unrealized_pnl": h.get("unrealized_pnl")} for h in holdings],
        },
        "portfolio_risk": portfolio_risk,
        "valuation_data": _seg("valuation_data"),
        "sentiment_data": _seg("sentiment_data"),
        "crowding_data": _seg("crowding_data"),
        "peer_comparison": _seg("peer_comparison"),
        "catalyst_data": _seg("catalyst_data"),
    }
    if len(items) > _BG_CAP:
        context["coverage_note"] = f"仅前 {_BG_CAP} 只标的有逐名详细背景（共 {len(items)} 只）"
    return context


def _build_inputs(wl_store, account_ref: str) -> dict:
    """聚合本 pass 的全部只读输入。绝不带崩：任一子项缺失走降级文本。"""
    dossier = portfolio.build_account_dossier(wl_store, account_ref=account_ref)
    mandate_text = _mandate.format_mandate_for_prompt(wl_store, account_ref=account_ref)
    macro_text = format_macro_for_prompt(wl_store)
    try:
        deriv_terms = derivatives.list_derivative_terms(wl_store, account_ref=account_ref)
    except Exception:  # noqa: BLE001
        deriv_terms = []
    deriv_text = portfolio.render_derivative_summary(deriv_terms).strip() or "无衍生品敞口。"
    return {"dossier": dossier, "mandate_text": mandate_text, "macro_text": macro_text,
            "deriv_text": deriv_text, "coverage_text": _render_coverage(dossier)}


def _validate_draft(raw: str) -> dict:
    """解析草案 JSON + 规范化 action（非法→持有），保证 holdings 结构可渲染。"""
    data = extract_json_object(raw) or {}
    holdings = []
    for h in (data.get("holdings") or []):
        if not isinstance(h, dict):
            continue
        action = str(h.get("action", "")).strip()
        if action not in _ACTIONS:
            action = "持有"
        holdings.append({
            "ticker": str(h.get("ticker", "")).strip(),
            "action": action,
            "reason": str(h.get("reason", "")).strip(),
            "risk": str(h.get("risk", "")).strip(),
            "derivative_note": str(h.get("derivative_note", "")).strip(),
        })
    return {
        "portfolio_diagnosis": str(data.get("portfolio_diagnosis", "")).strip(),
        "cross_market_coverage": str(data.get("cross_market_coverage", "")).strip(),
        "holdings": holdings,
    }


def _consensus(reviews: list[dict], *, store=None, market: str = "") -> dict:
    """确定性合议：置信×校准加权表决 + H-18 独立性护栏 + 风控软否决。不再多花 1 次 LLM。

    向后兼容：签名新增 *,store,market（缺省 store=None → 校准权重全 1.0，退化为纯置信加权；
    无 confidence → 退化为等权）；返回在旧键(verdict/caution/approve/reject/members)基础上**只增**
    diversity_warning / weighted_note / risk_veto，vip.js 仅做加性渲染。

    ponytail:
    - 加权 w=max(confidence,1)*calib（calib 复用 committee._member_weights，缺校准/无 store→1.0）；
      confidence 若为 0-1 制则 floor 到 1 → 退化为纯校准加权（安全，不会误放大）。
      同时保留 headcount approve/reject 供展示，加权与人头结论不一致时注 weighted_note。
    - 风控软否决：risk_officer 投 reject 即 risk_veto+caution，verdict 不得为 approve（approve→split）——
      用"风控否决=软否决"作硬约束破坏的确定性代理（关键词扫叙述判"回撤/排除破坏"太脆），升级路径＝结构化违规信号。
    """
    from bottleneck_hunter.watchlist.committee import MEMBERS, _member_weights
    label_of = {m["role"]: m["label"] for m in MEMBERS}
    valid = [r for r in reviews if isinstance(r, dict) and not r.get("error")]
    calib = _member_weights(store, {r.get("role", ""): r for r in valid}, market) if valid else {}

    members, approve, reject = [], 0, 0
    w_approve = w_reject = 0.0
    risk_veto = False
    for r in valid:
        role = r.get("role", "")
        vote = str(r.get("vote", "abstain"))
        try:
            conf = float(r.get("confidence", 0) or 0)
        except (TypeError, ValueError):
            conf = 0.0
        w = max(conf, 1.0) * float(calib.get(role, 1.0) or 1.0)
        if vote.startswith("approve"):
            approve += 1
            w_approve += w
        elif vote == "reject":
            reject += 1
            w_reject += w
            if role == "risk_officer":
                risk_veto = True
        members.append({
            "role": role, "label": label_of.get(role, role),
            "vote": vote, "confidence": r.get("confidence", 0),
            "key_concerns": r.get("key_concerns", []) or [],
            "assessment": r.get("overall_assessment", "") or "",
            "provider": r.get("provider", ""), "model": r.get("model", ""),
        })

    n = approve + reject
    verdict = "approve" if w_approve > w_reject else ("reject" if w_reject > w_approve else "split")
    caution = bool(n and reject >= approve)
    head_verdict = "approve" if approve > reject else ("reject" if reject > approve else "split")
    weighted_note = ""
    if verdict != head_verdict:
        weighted_note = (f"置信×校准加权后结论为「{verdict}」，与人头票「{head_verdict}」"
                         f"（{approve}赞成/{reject}反对）不一致，以加权为准。")

    if risk_veto:  # 风控软否决：抬 caution，且不得升级为 approve
        caution = True
        if verdict == "approve":
            verdict = "split"

    providers_used = [r.get("provider", "") for r in valid]
    distinct = {p for p in providers_used if p}
    diversity_warning = ""
    if len(providers_used) >= 2 and len(distinct) <= 1:
        diversity_warning = (f"投委会独立性降级：{len(providers_used)} 位委员实际由同一 provider"
                             f"（{next(iter(distinct), '?')}）提供，交叉验证退化为 1 模型算 N 次，结论仅供参考。")

    return {"verdict": verdict, "caution": caution, "approve": approve, "reject": reject,
            "members": members, "diversity_warning": diversity_warning,
            "weighted_note": weighted_note, "risk_veto": risk_veto}


_COMMITTEE_CORPUS_KEYS = ("portfolio_risk", "valuation_data", "sentiment_data",
                          "crowding_data", "peer_comparison", "catalyst_data")


def committee_corpus(context: dict) -> str:
    """把喂进投委会的真实数（组合风险 + 逐名背景）拼成防伪语料段，供 number_guard 校验委员叙述——
    否则委员引用这些合法数字（HHI/VaR/PE…）会被误标 ⚠。两 pass 共用。"""
    return json.dumps({k: context.get(k) for k in _COMMITTEE_CORPUS_KEYS},
                      ensure_ascii=False, default=str)


def annotate_committee(committee: dict, corpus: str) -> list[str]:
    """给委员叙述(assessment/key_concerns)里未在语料中出现的数字加 ⚠，就地写回 committee["members"]，返回未核到 token。

    corpus 须含 committee_corpus(context)（Phase 2 喂进委员会的真实数），否则委员引用合法数字会被误标。
    """
    unverified: list[str] = []

    def ann(text: str) -> str:
        if not text:
            return text
        unverified.extend(r["token"] for r in number_guard.verify_numbers(text, corpus) if r["status"] == "unverified")
        return number_guard.annotate_unverified(text, corpus)

    for m in committee.get("members", []):
        m["assessment"] = ann(m.get("assessment", ""))
        kc = m.get("key_concerns", [])
        if isinstance(kc, list):
            m["key_concerns"] = [ann(str(c)) for c in kc]
    return list(dict.fromkeys(unverified))


def reconcile_draft(items: list[dict], action_key: str, downgrade_map: dict, committee: dict) -> bool:
    """按投委会结论就地对齐草案动作，返回是否发生对账。两 pass 共用。

    - verdict=="reject"：命中 downgrade_map 的激进动作降一档 + 注下调理由；其余动作仅注否决提示。
    - caution 或 verdict=="split"：每条仅加警示注，**不改动作**（用户已确认的对账强度）。
    - verdict=="approve" 且 not caution：原样不动，返回 False。

    ponytail: 降级只降一档（不做多档链式）；caution 只加注不改动作。升级路径＝多档降级表 + 逐仓风险分级。
    """
    verdict = committee.get("verdict", "")
    caution = bool(committee.get("caution", False))
    if verdict == "reject":
        for it in items:
            act = str(it.get(action_key, ""))
            if act in downgrade_map:
                new = downgrade_map[act]
                it[action_key] = new
                it["reason"] = (str(it.get("reason", "")) + f"（投委会否决，动作已由「{act}」下调至「{new}」）").strip()
            else:
                it["reason"] = (str(it.get("reason", "")) + "（投委会否决，建议维持保守）").strip()
        return True
    if caution or verdict == "split":
        for it in items:
            it["reason"] = (str(it.get("reason", "")) + "（投委会提示警示，请谨慎核对）").strip()
        return True
    return False


def _parse_weight(s: str) -> float | None:
    """从软仓位串解析百分比中点（money path）：'3%-5%'→4.0；'5%'→5.0；区间取均值；无带%数字→None。
    只认百分号数字，避免把理由里的其它数字误当仓位。"""
    if not s:
        return None
    nums = [float(x) for x in re.findall(r"(\d+(?:\.\d+)?)\s*%", str(s))]
    if not nums:
        return None
    return round(sum(nums) / len(nums), 2)  # 单值即其本身，区间取中点


def summarize_cash_budget(dossier: dict, advisory_result: dict | None,
                          recommend_result: dict | None) -> dict:
    """指示性现金/仓位预算对照（只提示不约束）：把两个 pass 的建仓候选量化仓位加总，对照可投资现金给 sanity。

    需求侧本不可精确量化（advisory 加仓无仓位量、现金是结算单静态口径），故结论仅 indicative：
    - requested_new_buy = Σ(recommend 建仓候选软仓位中点% × total_equity)——仅统计「有明确仓位%」者；
      无仓位%的建仓/所有 advisory 加仓 → 计入 unquantified_adds、不塞进 requested（不拿 0 冒充已量化）。
    - available_cash = dossier.cash_balance（结算单口径、不含融资 buying_power）。
    - fits / overcommit_pct 仅基于「已量化」部分；unquantified_adds>0 时判断偏乐观，note 明示口径。
    """
    total_equity = float(dossier.get("total_equity") or 0)
    available = float(dossier.get("cash_balance") or 0)
    requested = 0.0
    unquantified = 0
    for c in ((recommend_result or {}).get("candidates") or []):
        if str(c.get("action", "")) != "建仓":
            continue
        w = _parse_weight(c.get("suggested_weight", ""))
        if w is None:
            unquantified += 1
        else:
            requested += w / 100.0 * total_equity
    for h in ((advisory_result or {}).get("holdings") or []):
        if str(h.get("action", "")) == "加仓":  # 加仓无仓位量 → 一律未量化
            unquantified += 1
    requested = round(requested, 2)
    over = requested - available
    fits = over <= 0
    overcommit = round(over, 2) if over > 0 else 0.0
    overcommit_pct = round(over / available * 100, 1) if (over > 0 and available > 0) else 0.0
    tail = (f"另有 {unquantified} 项加仓/建仓未给出仓位量、未纳入需求合计，容量判断偏乐观。"
            if unquantified else "")
    return {"available_cash": round(available, 2), "requested_new_buy": requested,
            "fits": fits, "overcommit": overcommit, "overcommit_pct": overcommit_pct,
            "unquantified_adds": unquantified,
            "note": "现金口径为结算单静态余额、不含融资(buying_power)，容量判断为指示性。" + tail}


def _annotate(draft: dict, corpus: str) -> list[str]:
    """就地给草案文本里未在档案/纲领语料中出现的数字加⚠标注，返回未核到 token 列表。"""
    unverified: list[str] = []
    def ann(text: str) -> str:
        if not text:
            return text
        unverified.extend(r["token"] for r in number_guard.verify_numbers(text, corpus) if r["status"] == "unverified")
        return number_guard.annotate_unverified(text, corpus)
    draft["portfolio_diagnosis"] = ann(draft["portfolio_diagnosis"])
    draft["cross_market_coverage"] = ann(draft["cross_market_coverage"])
    for h in draft["holdings"]:
        h["reason"] = ann(h["reason"])
        h["risk"] = ann(h["risk"])
    # 去重保序
    seen, out = set(), []
    for t in unverified:
        if t not in seen:
            seen.add(t); out.append(t)
    return out


async def generate_account_advisory(wl_store, *, account_ref: str = "", user_id: str = "", budget=None) -> dict:
    """生成并落库账户顾问建议。返回 {advisory_id, result}。空 ref/无持仓/无模型/预算不足 → 明确 error。"""
    from bottleneck_hunter.llm_clients.factory import get_models_for_role
    from bottleneck_hunter.watchlist.committee import MEMBERS, _review_single
    import asyncio

    account_ref = (account_ref or "").strip()
    if not account_ref:
        # 硬守卫：空 ref 会经 build_account_dossier→build_portfolio_summary→get_sim_account("")
        # 落到决策中心自有 sim_account('')（读、并惰性建 DC L4 模拟盘行）。前端 requireConcreteAccount 已挡，
        # 此处补后端防护，杜绝直连 API 越界读写决策中心模拟盘。见 memory:dc_sim_account_decoupled。
        return {"error": "请先选择具体子账户再生成顾问建议"}
    inputs = _build_inputs(wl_store, account_ref)
    dossier = inputs["dossier"]
    if not dossier.get("holdings"):
        return {"error": "该账户暂无持仓，请先上传月结单"}
    if budget is not None and not budget.can_spend():
        return {"error": "预算不足，暂不生成顾问建议"}
    models = get_models_for_role("vip_advisor", user_id=user_id, with_fallback=True)
    if not models:
        return {"error": "无可用 LLM（请在 AI 配置中为 vip_advisor 配置模型）"}
    llm, provider, model = models[0]

    # ── 1) 草案生成 ──
    prompt = _DRAFT_PROMPT.format(
        dossier=json.dumps(dossier, ensure_ascii=False, default=str),
        mandate=inputs["mandate_text"], macro=inputs["macro_text"],
        derivatives=inputs["deriv_text"], coverage=inputs["coverage_text"])
    resp = await llm.ainvoke(prompt)
    draft = _validate_draft(getattr(resp, "content", resp) if not isinstance(resp, str) else resp)
    if not draft["holdings"]:
        return {"error": "草案生成失败或未返回持仓建议，请重试"}

    # ── 2) 投委会 4 persona 并行评审（复用 committee._review_single；喂真实组合风险+逐名背景，不碰 sim 表）──
    context = build_committee_context(
        wl_store, dossier, inputs["macro_text"],
        [{"ticker": h.get("ticker"), "entry_id": h.get("entry_id")} for h in dossier.get("holdings", [])])
    exec_plan = {"account_ref": account_ref, "mandate": inputs["mandate_text"], "draft": draft}
    reviews = await asyncio.gather(*[_review_single(m, exec_plan, context) for m in MEMBERS],
                                   return_exceptions=True)
    reviews = [r for r in reviews if isinstance(r, dict)]

    # ── 3) 确定性合议 + number_guard（草案 + 委员叙述都过防伪；corpus 含喂进委员会的真实数）──
    committee = _consensus(reviews, store=wl_store, market=getattr(wl_store, "_market", "") or "")
    corpus = (json.dumps(dossier, ensure_ascii=False, default=str)
              + "\n" + inputs["mandate_text"] + "\n" + inputs["macro_text"]
              + "\n" + committee_corpus(context))
    unverified = _annotate(draft, corpus)
    unverified = list(dict.fromkeys(unverified + annotate_committee(committee, corpus)))

    # ── 3b) 草案↔投委会对账：reject→加仓降持有、caution/split→加警示注（memory:vip_advisory_pass 用户已确认强度）──
    reconciled = reconcile_draft(draft["holdings"], "action", {"加仓": "持有"}, committee)

    result = {
        "account_ref": account_ref,
        "generated_at": _now_iso(),
        "portfolio_diagnosis": draft["portfolio_diagnosis"],
        "cross_market_coverage": draft["cross_market_coverage"],
        "holdings": draft["holdings"],
        "committee": committee,
        "unverified": unverified,
        "reconciled": reconciled,
        "provider": provider, "model": model,
        "disclaimer": compliance.DISCLAIMER_ZH,
    }

    # ── C-1 复盘打点（record_prediction，只写不评）：为 5b 复盘启动数据时钟，并给 VIP 自己的准确率信号。
    #    role_context=vip_advisor 独占桶，与 sim 的 committee_*/vote 物理隔离；旁路容错——打点失败只 debug、
    #    绝不影响建议主链路（仿 record_model_call 哲学）。记录的是 reconcile 后的最终动作。
    try:
        mkt = getattr(wl_store, "_market", "") or ""
        for h in draft["holdings"]:
            if h.get("ticker"):
                wl_store.record_prediction(
                    provider=provider, model=model, role_context=VIP_ROLE_CONTEXT,
                    ticker=h["ticker"], prediction_type=VIP_PT_ADVICE,
                    prediction_value=h["action"], market=mkt)
    except Exception:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).debug("VIP advisory 复盘打点失败（不影响建议）", exc_info=True)

    # ── 4) 落库（独立表，绝不写 sim_*）──
    aid = uuid.uuid4().hex[:12]
    with wl_store._write_conn() as conn:
        conn.execute(
            f"""INSERT INTO vip_advisory (id, account_ref, result_json, provider, model, created_at{wl_store._user_insert_cols()}{wl_store._market_insert_cols()})
               VALUES (?,?,?,?,?,?{wl_store._user_insert_vals()}{wl_store._market_insert_vals()})""",
            (aid, account_ref, json.dumps(result, ensure_ascii=False, default=str), provider, model, _now_iso())
            + wl_store._user_insert_params() + wl_store._market_insert_params(),
        )

    # ── 5) 审计留痕（auth.db，无 PII 金额；与周期报告同口径 create_advice_audit）──
    try:
        import hashlib
        from bottleneck_hunter.auth.store import AuthStore
        uid = getattr(wl_store, "_user_id", "") or user_id or ""
        if uid:
            AuthStore().create_advice_audit(
                uid, advice_type="recommendation", advice_ref=aid,
                source_data_ref={"account_ref": account_ref, "verdict": committee.get("verdict", ""),
                                 "tickers": [h["ticker"] for h in draft["holdings"]]},
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
    return {"advisory_id": aid, "result": result}


def get_latest_advisory(wl_store, account_ref: str = "") -> dict | None:
    """读该账户最近一份顾问建议（供前端进标签页回显）。"""
    account_ref = (account_ref or "").strip()
    conn = wl_store._connect()
    try:
        q, p = wl_store._filtered(
            "SELECT result_json, created_at FROM vip_advisory WHERE account_ref = ? ORDER BY created_at DESC LIMIT 1",
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


def list_advisory(wl_store, account_ref: str = "", limit: int = 20) -> list[dict]:
    """读该账户历史顾问建议（新→旧）。每条带完整 result，前端点选即可回看，无需按 id 再取一次。"""
    account_ref = (account_ref or "").strip()
    limit = max(1, min(int(limit or 20), 100))  # 已 clamp 为 int，直插 SQL 无注入风险
    conn = wl_store._connect()
    try:
        q, p = wl_store._filtered(
            f"SELECT id, result_json, provider, model, created_at FROM vip_advisory "
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
                    "n_holdings": len(result.get("holdings") or []), "result": result})
    return out


if __name__ == "__main__":
    # ponytail 自检：纯逻辑（草案规范化 / 合议 / number_guard 语料）——LLM 路径需真实模型，不入自检
    # 1) 草案：非法 action 回退持有，字段完整
    d = _validate_draft('```json\n{"portfolio_diagnosis":"集中度偏高","cross_market_coverage":"港股未覆盖",'
                        '"holdings":[{"ticker":"NVDA","action":"清仓","reason":"占比过高","risk":"回撤"},'
                        '{"ticker":"BE","action":"加仓","reason":"低估","risk":"波动","derivative_note":"有累购敞口"}]}\n```')
    assert d["holdings"][0]["action"] == "持有", d          # 非法"清仓"→持有
    assert d["holdings"][1]["action"] == "加仓"
    assert d["cross_market_coverage"] == "港股未覆盖"

    # 2) 合议：3 approve + 1 reject → approve 且不 caution；2 reject vs 1 approve → caution
    c1 = _consensus([{"role": "risk_officer", "vote": "approve"}, {"role": "growth_investor", "vote": "approve_with_modification"},
                     {"role": "value_investor", "vote": "approve"}, {"role": "contrarian", "vote": "reject"}])
    assert c1["verdict"] == "approve" and c1["caution"] is False, c1
    c2 = _consensus([{"role": "risk_officer", "vote": "reject"}, {"role": "contrarian", "vote": "reject"},
                     {"role": "growth_investor", "vote": "approve"}, {"role": "value_investor", "error": "no llm"}])
    assert c2["verdict"] == "reject" and c2["caution"] is True and len(c2["members"]) == 3, c2

    # 3) number_guard：档案语料含 72% → 不误标；凭空 40% → 标未核到
    draft = {"portfolio_diagnosis": "集中度 72%，超过 40% 目标", "cross_market_coverage": "",
             "holdings": [{"reason": "占比 72%", "risk": "回撤 25%"}]}
    unv = _annotate(draft, '{"top5":72}\n最大回撤 25%')
    assert "40%" in unv and "72%" not in unv and "25%" not in unv, unv

    # 4) 空 account_ref 硬守卫：必须在碰 wl_store 之前就返回 error（否则会读/惰性写决策中心 sim_account('')）
    import asyncio as _aio
    class _Boom:
        def __getattr__(self, _):
            raise AssertionError("空 ref 不应触碰 wl_store")
    for ref in ("", "  "):
        r = _aio.run(generate_account_advisory(_Boom(), account_ref=ref))
        assert r.get("error"), r

    # 5) 价源覆盖渲染：代码判定的未覆盖标的进文本；全覆盖走"均有活跃价源"
    t_unc = _render_coverage({"price_coverage": {"uncovered": ["0700.HK"], "n_covered": 2, "n_total": 3}})
    assert "0700.HK" in t_unc and "无活跃价源" in t_unc, t_unc
    assert "均有活跃价源" in _render_coverage({"price_coverage": {"uncovered": [], "n_covered": 3, "n_total": 3}})

    # 6) 价源覆盖判定（portfolio._price_coverage）：有 close→covered；无快照/close 为 None→uncovered（去重保序）
    class _Snap:
        def __init__(self, m): self._m = m
        def get_latest_snapshot(self, t): return self._m.get(t)
    pc = portfolio._price_coverage(
        _Snap({"NVDA": {"close": 100, "date": "2026-07-24"}, "BE": {"close": None}}),
        [{"ticker": "NVDA"}, {"ticker": "0700.HK"}], [{"underlying": "BE"}])
    assert pc["uncovered"] == ["0700.HK", "BE"] and pc["n_covered"] == 1 and pc["n_total"] == 3, pc

    # 7) format_macro_for_prompt：全字段结构化入文本；None/异常/仅综述均安全降级
    class _Macro:
        def __init__(self, m): self._m = m
        def get_latest_macro_strategy(self): return self._m
    full = _Macro({"regime": "risk_off", "risk_appetite": "defensive", "recommended_cash_pct": 30.0,
                   "market_summary": "美债利率高企", "sector_rotation": {"看多": ["公用事业"], "看空": ["半导体"]},
                   "risk_factors": ["流动性收紧", "地缘冲突"], "key_signals": ["VIX>25"]})
    t = format_macro_for_prompt(full)
    for seg in ("risk_off", "defensive", "30.0%", "美债利率高企", "公用事业", "流动性收紧", "VIX>25"):
        assert seg in t, (seg, t)                                   # 六段结构化字段全部入链，非只取 market_summary
    assert format_macro_for_prompt(_Macro(None)) == _MACRO_FALLBACK  # 无研判→降级句
    class _Boom2:
        def get_latest_macro_strategy(self): raise RuntimeError("db down")
    assert format_macro_for_prompt(_Boom2()) == _MACRO_FALLBACK      # 异常→降级句不带崩
    assert format_macro_for_prompt(object()) == _MACRO_FALLBACK      # 无该方法→降级句
    only = format_macro_for_prompt(_Macro({"market_summary": "震荡"}))
    assert only == "市场综述：震荡", only                            # 仅综述：其余段跳过、不崩、无 L1 行

    # 8) build_committee_context：真实组合风险(HHI) + 逐名背景聚合 + sector 降级 + CAP（复用决策中心本地取数）
    class _RiskStore:
        _market = "us_stock"
        def get_snapshots(self, tk, days=60): return [{"close": 100 + i} for i in range(25)]   # 25 日价→风险可算
        def get_company_profile(self, tk):
            return {"raw": {"trailingPE": 50}, "sector": "半导体"} if tk == "NVDA" else {}
        def get_latest_snapshot(self, tk):
            return {"close": 400, "market_cap": 1e12} if tk == "NVDA" else {}
        # 其余取数方法缺失→build_ticker_background 各段 except 降级"暂无"
    dossier8 = {"total_equity": 100000, "cash_balance": 30000, "n_holdings": 2, "top5_concentration_pct": 70.0,
                "derivative_exposure": [], "holdings": [
                    {"ticker": "NVDA", "shares": 100, "market_value": 40000, "weight_pct": 40, "avg_cost": 300, "unrealized_pnl": 10000},
                    {"ticker": "MU", "shares": 200, "market_value": 30000, "weight_pct": 30, "avg_cost": 100, "unrealized_pnl": 5000}]}
    ctx = build_committee_context(_RiskStore(), dossier8, "L1 研判：市场状态 risk_off",
                                  [{"ticker": "NVDA", "entry_id": "e1"}, {"ticker": "MU", "entry_id": ""}])
    assert ctx["portfolio_risk"].get("concentration_hhi") is not None, ctx["portfolio_risk"]        # 真实 HHI 已算
    assert isinstance(ctx["valuation_data"], dict) and "NVDA" in ctx["valuation_data"], ctx["valuation_data"]  # 逐名估值聚合
    assert ctx["catalyst_data"] == "暂无", ctx["catalyst_data"]        # 无 get_catalysts_for_entry→各段[]→"暂无"
    assert ctx["macro_summary"] == "L1 研判：市场状态 risk_off"        # 宏观直传
    assert "max_sector_weight_pct" in ctx["portfolio_risk"], ctx["portfolio_risk"]  # NVDA=半导体/MU=未知→非全未知，保留

    # 8b) 全 sector 未知 → 去 max_sector_weight + 标 sector_note（不误报>40%告警）
    class _NoSector(_RiskStore):
        def get_company_profile(self, tk): return {}
        def get_latest_snapshot(self, tk): return {}
    ctx2 = build_committee_context(_NoSector(), dossier8, "宏观",
                                   [{"ticker": "NVDA", "entry_id": ""}, {"ticker": "MU", "entry_id": ""}])
    assert "max_sector_weight_pct" not in ctx2["portfolio_risk"] and ctx2["portfolio_risk"].get("sector_note"), ctx2["portfolio_risk"]

    # 8c) 空持仓 → portfolio_risk 退回 dossier 薄口径（top5/n_holdings 兜底）
    ctx3 = build_committee_context(_RiskStore(), {"holdings": [], "top5_concentration_pct": 55.0, "n_holdings": 3}, "宏观", [])
    assert ctx3["portfolio_risk"]["top5_concentration_pct"] == 55.0, ctx3["portfolio_risk"]

    # 8d) 逐名背景 CAP：16 只 → 仅前 15 只有背景 + coverage_note 不静默截断
    ctx4 = build_committee_context(_RiskStore(), dossier8, "宏观", [{"ticker": f"T{i}", "entry_id": ""} for i in range(16)])
    assert "coverage_note" in ctx4 and "共 16" in ctx4["coverage_note"], ctx4.get("coverage_note")

    # 9) 合议加固：加权翻转 / H-18 独立性护栏 / 风控软否决 / 委员叙述防伪
    # 9a) 1 席高置信 approve vs 2 席低置信 reject（无风控反对）→ 加权翻成 approve、注 weighted_note（store=None→校准全 1.0）
    c9 = _consensus([{"role": "growth_investor", "vote": "approve", "confidence": 9, "provider": "openai"},
                     {"role": "value_investor", "vote": "reject", "confidence": 1, "provider": "glm"},
                     {"role": "contrarian", "vote": "reject", "confidence": 1, "provider": "qwen"}])
    assert c9["verdict"] == "approve" and c9["weighted_note"] and c9["risk_veto"] is False, c9  # 人头 reject、加权 approve
    # 9b) 同 provider 4 席 → diversity_warning；provider 各异 → 无
    same = _consensus([{"role": r, "vote": "approve", "provider": "glm"} for r in
                       ("risk_officer", "growth_investor", "value_investor", "contrarian")])
    assert same["diversity_warning"] and "glm" in same["diversity_warning"], same
    mixed = _consensus([{"role": "risk_officer", "vote": "approve", "provider": "openai"},
                        {"role": "growth_investor", "vote": "approve", "provider": "glm"}])
    assert mixed["diversity_warning"] == "", mixed
    # 9c) 风控 reject 而人头多数 approve → risk_veto、verdict 降 split、caution=True
    veto = _consensus([{"role": "risk_officer", "vote": "reject", "confidence": 5, "provider": "openai"},
                       {"role": "growth_investor", "vote": "approve", "confidence": 5, "provider": "glm"},
                       {"role": "value_investor", "vote": "approve", "confidence": 5, "provider": "qwen"},
                       {"role": "contrarian", "vote": "approve", "confidence": 5, "provider": "deepseek"}])
    assert veto["risk_veto"] is True and veto["verdict"] == "split" and veto["caution"] is True, veto
    # 9d) 委员叙述纳入 number_guard：assessment 造 $999(不在 corpus)→⚠+进 unverified；corpus 内 72%/25% 不误标
    comm = {"members": [{"assessment": "占比 72%，目标价 $999", "key_concerns": ["回撤 25%", "估值 $888 偏高"]}]}
    unv9 = annotate_committee(comm, '{"top5":72}\n最大回撤 25%')
    assert "$999" in unv9 and "$888" in unv9 and "72%" not in unv9 and "25%" not in unv9, unv9
    assert "$999 ⚠未核到" in comm["members"][0]["assessment"], comm["members"][0]["assessment"]  # 就地写回

    # 10) reconcile_draft：reject 降一档+注 / caution 仅加注不改动作 / approve 原样
    hs = [{"ticker": "NVDA", "action": "加仓", "reason": "低估"}, {"ticker": "MU", "action": "持有", "reason": "观望"}]
    r10 = reconcile_draft(hs, "action", {"加仓": "持有"}, {"verdict": "reject", "caution": True})
    assert r10 is True and hs[0]["action"] == "持有" and "下调至「持有」" in hs[0]["reason"], hs
    assert hs[1]["action"] == "持有" and "否决" in hs[1]["reason"], hs        # 非激进动作仅注不改
    cs = [{"ticker": "BE", "action": "建仓", "reason": "看好"}]
    r10b = reconcile_draft(cs, "action", {"建仓": "关注"}, {"verdict": "approve", "caution": True})
    assert r10b is True and cs[0]["action"] == "建仓" and "警示" in cs[0]["reason"], cs  # caution 不降级、仅加注
    ks = [{"ticker": "AAPL", "action": "加仓", "reason": "强"}]
    assert reconcile_draft(ks, "action", {"加仓": "持有"}, {"verdict": "approve", "caution": False}) is False, ks
    assert ks[0]["action"] == "加仓" and ks[0]["reason"] == "强", ks       # approve 且不 caution→原样

    # 11) B: _parse_weight —— 区间取中点 / 单值本身 / 无带%数字→None（不把非仓位数字误当仓位）
    assert _parse_weight("3%-5%") == 4.0, _parse_weight("3%-5%")
    assert _parse_weight("5%") == 5.0
    assert _parse_weight("") is None and _parse_weight("适量") is None and _parse_weight(None) is None
    assert _parse_weight("持有 3 只") is None

    # 12) B: summarize_cash_budget —— 已量化建仓 vs 现金；加仓/无%建仓只计未量化不入需求
    dossier_b = {"total_equity": 10000.0, "cash_balance": 1000.0}
    one = summarize_cash_budget(dossier_b, None, {"candidates": [{"action": "建仓", "suggested_weight": "5%"}]})
    assert one["requested_new_buy"] == 500.0 and one["fits"] is True, one            # 5%*10000=500 ≤ 1000
    two = summarize_cash_budget(dossier_b, None, {"candidates": [
        {"action": "建仓", "suggested_weight": "40%"}, {"action": "建仓", "suggested_weight": "40%"}]})
    assert two["fits"] is False and two["overcommit_pct"] > 0, two                   # 8000 > 1000
    adds = summarize_cash_budget(dossier_b, {"holdings": [
        {"action": "加仓"}, {"action": "加仓"}, {"action": "持有"}]}, None)
    assert adds["unquantified_adds"] == 2 and adds["requested_new_buy"] == 0.0, adds  # 加仓无仓位量→只计数
    novol = summarize_cash_budget(dossier_b, None, {"candidates": [{"action": "建仓", "suggested_weight": ""}]})
    assert novol["unquantified_adds"] == 1 and novol["requested_new_buy"] == 0.0, novol  # 建仓无%→不塞 0 进需求

    # 13) C-1 隔离回归守卫：VIP 打点常量绝不撞 sim 的 role_context/prediction_type（防手滑致校准交叉污染）
    assert VIP_PT_ADVICE not in ("vote",) and VIP_PT_RECOMMEND not in ("vote",)
    assert not VIP_ROLE_CONTEXT.startswith("committee_")
    assert VIP_PT_ADVICE != VIP_PT_RECOMMEND

    print("advisory self-check OK")
