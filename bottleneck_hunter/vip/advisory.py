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
import os
import re
import uuid
from datetime import date, datetime, timezone

import asyncio

from bottleneck_hunter.chain.json_utils import extract_json_object
from bottleneck_hunter.vip import compliance, derivatives, number_guard, portfolio
from bottleneck_hunter.vip import mandate as _mandate

_ACTIONS = {"减仓", "持有", "加仓"}

# ── C-1 复盘打点隔离常量（recommend.py 导入复用）──
# VIP 建议的 record_prediction 打点必须与决策中心模拟盘校准物理隔离：sim 用 role_context="committee_{role}"
# + prediction_type="vote"，二者聚合口径(get_model_accuracy_stats 按 role_context、get_calibration_weight 按
# role_context+user_id+market)中**唯一干净隔离维度是 role_context**（user_id/market 与 sim 共享）。故 VIP 独占
# role_context="vip_advisor" + 独立 prediction_type，则 sim 的 _consensus 读 committee_* 桶零污染，VIP 自身亦然。
VIP_ROLE_CONTEXT = "vip_advisor"
VIP_PT_ADVICE = "vip_advice"
VIP_PT_RECOMMEND = "vip_recommend"


def advisor_calibration(wl_store, provider: str, model: str) -> dict:
    """F1 回接：读回本模型 vip_advisor 桶的历史校准权重(G5 复盘写入)，surfaced 给用户做可信度参考。
    此前该权重算了却无人消费(committee 只读 committee_* 桶)——死信号。这里只读、advice-only、不改选型，
    把'本模型历史 VIP 建议准不准'透明呈现，闭合 记录→校准→回看 的复盘环。
    1.0=中性(无复盘数据或表现如常)；<1 历史偏差大宜更保守；>1 历史表现良好。"""
    try:
        w = wl_store.get_calibration_weight(provider, model, role_context=VIP_ROLE_CONTEXT)
        w = float(w) if w and float(w) > 0 else 1.0
    except Exception:  # noqa: BLE001 - 校准读取失败绝不影响建议主链路
        w = 1.0
    if abs(w - 1.0) < 1e-9:
        note = "历史校准中性（暂无复盘数据或表现如常）"
    elif w < 1.0:
        note = f"本模型历史 VIP 建议校准 {w:.2f}x，偏低——本轮结论宜更保守看待"
    else:
        note = f"本模型历史 VIP 建议校准 {w:.2f}x，历史表现良好"
    return {"calibration_weight": round(w, 2), "note": note}


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

## 历史经验教训（往期策略复盘沉淀，供参考勿盲从）
{experience_cards}

## 外部证据（各持仓近期券商研报 + 知识库片段，作建议的可引依据；"暂无"即无召回，勿臆造）
{evidence}

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


def _render_experience_cards(cards: list[dict]) -> str:
    """把 vip_portfolio 经验卡片压成 prompt 文本（title/置信度/正文）。空 → 明确降级句。"""
    if not cards:
        return "（暂无往期复盘沉淀的经验卡片，按当前数据独立判断。）"
    lines = []
    for c in cards:
        conf = c.get("confidence")
        tag = f"·置信{round(float(conf) * 100)}%" if conf is not None else ""
        lines.append(f"- 【{c.get('category', '')}{tag}】{c.get('title', '')}：{c.get('content', '')}")
    return "\n".join(lines)


async def gather_holdings_evidence(wl_store, holdings: list[dict], *, max_tickers: int = 8) -> str:
    """为逐仓建议召回各持仓的研报 + KB 证据（复用 chain.evidence.gather_evidence，走 hub 凭据/熔断）。

    §9.2 顾问建议增据：VIP 建议此前纯模型判断，此处附各标的真实券商研报摘要 + KB 片段，令建议有据可引。
    全 best-effort：无凭据/未开/异常 → "暂无"，与未接入前逐字节一致，绝不阻断建议主链路。
    max_tickers 上限控 prompt 体量与网络往返（持仓多时取前 N，按 dossier 顺序＝权重降序）。
    """
    from bottleneck_hunter.chain.evidence import gather_evidence
    market = getattr(wl_store, "_market", "") or "us_stock"
    tickers = [h.get("ticker", "").strip() for h in (holdings or []) if h.get("ticker")][:max_tickers]
    if not tickers:
        return "暂无持仓，无需外部证据。"
    async def _one(tk: str) -> str:
        try:
            ev = await gather_evidence(tk, market, f"{tk} 风险 竞争 瓶颈")
        except Exception:  # noqa: BLE001
            ev = ""
        return f"### {tk}\n{ev}" if ev else f"### {tk}\n（暂无研报/知识库召回）"
    blocks = await asyncio.gather(*[_one(t) for t in tickers])
    return "\n\n".join(blocks)


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


# ── P1-3：持仓板块权重 vs L1 板块轮动三桶的确定性对照 —— 把「宏观板块信号 vs 我的持仓」从叙述自由推理变结构化对账 ──
# rotation 兼容 L1 两种输出：英文 {strengthening/weakening/neutral}（_merge_sector_rotation 产出）或中文 {看多/看空}。
_ROTATION_SYNONYMS = {
    "weakening": ("weakening", "看空", "走弱", "减配", "回避", "弱"),
    "strengthening": ("strengthening", "看多", "走强", "增配", "超配", "强"),
}


def _rotation_bucket(rotation: dict, kind: str) -> list[str]:
    """从 L1 sector_rotation 提取某一档（强/弱）的板块名列表；按键名同义词归类，兼容中英键。"""
    out: list[str] = []
    for k, v in (rotation or {}).items():
        ks = str(k).strip()
        if any(s in ks.lower() or s in ks for s in _ROTATION_SYNONYMS[kind]):
            vals = v if isinstance(v, (list, tuple)) else [v]
            out.extend(str(x).strip() for x in vals if str(x).strip())
    return list(dict.fromkeys(out))


def reconcile_sector_rotation(holdings: list[dict], rotation: dict) -> dict:
    """确定性核算持仓板块权重 vs L1 板块轮动，标「重仓于走弱板块」「持有于走强板块」「L1 判强但零持仓」。

    喂 risk_officer（折进 portfolio_risk）+ 前端对照面板。纯函数、零 LLM。板块名双向子串近似匹配
    （持仓 sector 来自观察池 join，rotation 来自 L1；粒度可能不同 → 子串近似而非精确映射）。
    rotation 无强/弱信号 → available False（不硬凑对照）。
    """
    weakening = _rotation_bucket(rotation, "weakening")
    strengthening = _rotation_bucket(rotation, "strengthening")
    if not weakening and not strengthening:
        return {"available": False, "in_weakening": [], "in_strengthening": [],
                "strengthening_unheld": [], "weakening_weight_pct": 0.0,
                "note": "L1 未产出板块轮动强/弱信号，未做对照"}

    by_sector: dict[str, dict] = {}
    for h in holdings or []:
        sec = str(h.get("sector") or "").strip()
        if not sec or sec == "未知":
            continue
        b = by_sector.setdefault(sec, {"weight_pct": 0.0, "tickers": []})
        b["weight_pct"] += float(h.get("weight_pct") or 0)
        if h.get("ticker"):
            b["tickers"].append(str(h["ticker"]))

    def _bucket_of(sec: str) -> str | None:
        """把一个持仓板块归入**唯一**方向：跨两桶取「最贴合」的轮动词（精确匹配 > 子串重叠更长），
        杜绝双向子串双命中（如 '能源'⊂'新能源' 令 新能源 同时进弱/强桶自相矛盾+虚增走弱敞口）。
        平局（含 L1 自身把同板块列强又列弱）→ 先遍历的 weakening 胜出（风险优先，宁可提示走弱）。"""
        best = None  # (exact:0/1, overlap_len, bucket)
        for bucket, kws in (("weak", weakening), ("strong", strengthening)):
            for kw in kws:
                k = str(kw).strip()
                if not k:
                    continue
                if sec == k:
                    cand = (1, len(k), bucket)
                elif k in sec or sec in k:
                    cand = (0, min(len(sec), len(k)), bucket)   # 重叠长度=较短串长，越长越贴合
                else:
                    continue
                if best is None or cand[:2] > best[:2]:
                    best = cand
        return best[2] if best else None

    in_weak, in_strong = [], []
    for sec, b in by_sector.items():
        rec = {"sector": sec, "weight_pct": round(b["weight_pct"], 1), "tickers": b["tickers"]}
        bucket = _bucket_of(sec)
        if bucket == "weak":
            in_weak.append(rec)
        elif bucket == "strong":
            in_strong.append(rec)
    held = list(by_sector)
    unheld = [s for s in strengthening if not any(s in hs or hs in s for hs in held)]
    return {
        "available": True,
        "in_weakening": sorted(in_weak, key=lambda r: -r["weight_pct"]),
        "in_strengthening": sorted(in_strong, key=lambda r: -r["weight_pct"]),
        "strengthening_unheld": unheld,
        "weakening_weight_pct": round(sum(r["weight_pct"] for r in in_weak), 1),
        "note": "板块名子串近似匹配（非精确映射）；对照 L1 轮动作宏观提示，不硬拦。",
    }


# ── P4-3：账户级瓶颈主题暴露 + 机会集缺口图 —— 把差异化的瓶颈引擎首次接到用户自己的账户 ──
# 聚合持仓 bottleneck_node × 权重 vs 观察池候选机会集，diff：over-owned（重仓拥挤主题）/ under-owned
# （观察池有高分候选、账户零/低持仓的高价值瓶颈主题）。止于建议层——只出方向性「腾挪」提示，不下单。
# 天花板（诚实标注）：bottleneck_node 是自由文本非规范化 taxonomy → 只做粗粒度字符串归一(strip/lower)的**定性**图，
# 不量化瓶颈强度暴露（须等 Phase 5 持久化五维评分）。

def bottleneck_theme_exposure(holdings: list[dict], candidates: list[dict]) -> dict:
    """确定性核算账户瓶颈主题暴露 vs 观察池机会集，标 over-owned（拥挤）/ under-owned（缺口）。

    holdings: dossier holdings（带 bottleneck_node/weight_pct，来自 0-5 观察池 join）。
    candidates: 观察池 list_all()（带 bottleneck_node/composite_score/tier/ticker）。
    纯函数、零 LLM。主题名按 strip().lower() 归一后精确聚合（自由文本，不做子串近似——避免误并不同主题）。
    无任一持仓带 bottleneck_node → available False（join 未覆盖，不硬凑）。
    """
    def _norm(x) -> str:
        return str(x or "").strip()

    # 持仓侧：主题 → 累计权重 + 标的
    held: dict[str, dict] = {}
    for h in holdings or []:
        node = _norm(h.get("bottleneck_node"))
        if not node:
            continue
        b = held.setdefault(node.lower(), {"theme": node, "weight_pct": 0.0, "tickers": []})
        b["weight_pct"] += float(h.get("weight_pct") or 0)
        if h.get("ticker"):
            b["tickers"].append(str(h["ticker"]))

    if not held:
        return {"available": False, "over_owned": [], "under_owned": [],
                "note": "持仓未 join 到任何瓶颈主题（观察池覆盖不足），未做主题缺口对照"}

    # 候选侧：主题 → 观察池里该主题的最高分候选（机会集代表）+ 是否已持有
    held_keys = set(held)
    cand_by_theme: dict[str, dict] = {}
    for c in candidates or []:
        node = _norm(c.get("bottleneck_node"))
        if not node:
            continue
        key = node.lower()
        score = float(c.get("composite_score") or 0)
        rep = cand_by_theme.setdefault(key, {"theme": node, "_raw": float("-inf"), "top_score": 0.0,
                                              "top_ticker": "", "tier": "", "n_candidates": 0})
        rep["n_candidates"] += 1
        if score > rep["_raw"]:   # 比未四舍原值，避免已四舍 top_score 令同分带内低分候选顶替真最高分
            rep["_raw"] = score
            rep["top_score"] = round(score, 1)
            rep["top_ticker"] = str(c.get("ticker") or "")
            rep["tier"] = str(c.get("tier") or "")

    # over-owned：已持有主题按权重降序（重仓即拥挤，供「腾挪」参考）
    over = sorted(({"theme": b["theme"], "weight_pct": round(b["weight_pct"], 1),
                    "tickers": b["tickers"],
                    "in_watchlist": (k in cand_by_theme)} for k, b in held.items()),
                  key=lambda r: -r["weight_pct"])
    # under-owned：观察池有候选、账户未持有的主题，按候选最高分降序（高价值被忽视的瓶颈环节）
    under = sorted(({"theme": v["theme"], "top_ticker": v["top_ticker"], "top_score": v["top_score"],
                     "tier": v["tier"], "n_candidates": v["n_candidates"]}
                    for k, v in cand_by_theme.items() if k not in held_keys),
                   key=lambda r: -r["top_score"])
    return {
        "available": True,
        "over_owned": over,
        "under_owned": under,
        "held_theme_count": len(held),
        "note": "瓶颈主题为自由文本、精确归一聚合（定性图，非量化强度暴露）；under-owned=观察池高分候选但账户未持有，"
                "over-owned=账户重仓主题——仅作方向性腾挪提示，止于建议层。",
    }


_BG_CAP = 15  # 逐名背景上限（本地 DB 读，够 committee 判断；超限只标注不静默截断）


def build_committee_context(wl_store, dossier: dict, macro_text: str, bg_items: list[dict], *,
                            market: str = "", mandate_compliance: dict | None = None,
                            sector_rotation: dict | None = None,
                            derivative_barriers: list | None = None) -> dict:
    """组装投委会 4 席评审的真实上下文，全部复用决策中心本地-DB 取数（零新增网络面）。

    - portfolio_risk：decision_engine._portfolio_risk_summary（真实 HHI/VaR/CVaR/beta/相关性），
      positions 直接沿用 dossier holdings 已算好的 weight_pct/market_value。
    - valuation/sentiment/crowding/peer/catalyst：committee.build_ticker_background 逐名聚合 {ticker: 段}。
    bg_items: [{"ticker","entry_id"}]——advisory 传持仓（holdings 无 entry_id→催化剂段自然"暂无"），
              recommend 传候选（观察池 entry 有 id→估值/催化剂全亮）。
    mandate_compliance: P1-1 纲领硬约束结构化对账；None 时按 dossier.account_ref 自动核算（recommend 免改也得注入）。
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

    # P1-1：纲领硬约束结构化对账折进 portfolio_risk（risk_officer 的 prompt 已渲染此字段 → 零 prompt 文件改动即收到结构化违规信号）。
    # None 时按账户自动核算（recommend 路径免改也得注入）；折入 corpus 键 → 委员引用其数字不被误标 ⚠。
    if mandate_compliance is None:
        try:
            mandate_compliance = _mandate.check_mandate_compliance(
                _mandate.load_mandate(wl_store, dossier.get("account_ref", "")), dossier)
        except Exception:  # noqa: BLE001 - 对账失败绝不带崩委员会评审
            mandate_compliance = None
    if mandate_compliance:
        portfolio_risk["mandate_compliance"] = mandate_compliance

    # P1-3：持仓板块权重 vs L1 板块轮动结构化对照，同折进 portfolio_risk（同 corpus 键，同零 prompt 改动路径）。
    # None 时按 wl_store 最新 L1 自动核算（recommend 免改也得注入）；无信号→available False，不注入。
    if sector_rotation is None:
        try:
            _macro = wl_store.get_latest_macro_strategy() if hasattr(wl_store, "get_latest_macro_strategy") else None
            sector_rotation = (_macro or {}).get("sector_rotation") or {}
        except Exception:  # noqa: BLE001 - L1 取数失败绝不带崩委员会评审
            sector_rotation = {}
    rot = reconcile_sector_rotation(holdings, sector_rotation or {})
    if rot.get("available"):
        portfolio_risk["sector_rotation_reconcile"] = rot

    # P1-4：衍生品 KO/KI 状态折进 portfolio_risk（同 corpus 键、同零 prompt 改动路径）——risk_officer 收到
    # 「累购距敲出仅 3%（利润封顶）/FCN 已敲入（本金风险激活）」结构化信号。
    # None 时按账户自动只读扫描（recommend 免改也注入）。
    if derivative_barriers is None:
        try:
            from bottleneck_hunter.vip.projection import derivative_barrier_status
            derivative_barriers = derivative_barrier_status(wl_store, dossier.get("account_ref", ""))
        except Exception:  # noqa: BLE001 - 障碍扫描失败绝不带崩委员会评审
            derivative_barriers = []
    live = [b for b in (derivative_barriers or []) if b.get("available")]
    if live:
        portfolio_risk["derivative_barriers"] = live

    # P4-3：账户级瓶颈主题暴露 + 机会集缺口图折进 portfolio_risk（同 corpus 键、同零 prompt 改动路径）——
    # 委员收到「重仓拥挤主题 X / 观察池高分候选 Y 主题账户零持仓」结构化前瞻信号。持仓已带 bottleneck_node(0-5 join)。
    try:
        _cands = wl_store.list_all() if hasattr(wl_store, "list_all") else []
    except Exception:  # noqa: BLE001 - 观察池取数失败绝不带崩委员会评审
        _cands = []
    theme = bottleneck_theme_exposure(holdings, _cands)
    if theme.get("available"):
        portfolio_risk["bottleneck_theme_exposure"] = theme

    # P4-1/P4-2：组合级压力测试 + 净 Greeks 直接沿用 dossier 已算好的结果（衍生品 payoff 重放 + 股票线性 delta）。
    if dossier.get("stress_test"):
        portfolio_risk["stress_test"] = dossier["stress_test"]
    if dossier.get("net_greeks"):
        portfolio_risk["net_greeks"] = dossier["net_greeks"]

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


def _consensus(reviews: list[dict], *, store=None, market: str = "",
               mandate_compliance: dict | None = None) -> dict:
    """确定性合议：置信×校准加权表决 + H-18 独立性护栏 + 风控软否决 + P1-1 纲领硬约束否决。不再多花 1 次 LLM。

    向后兼容：签名新增 *,store,market,mandate_compliance（全缺省 → 退化旧行为）；返回在旧键基础上**只增**
    diversity_warning / weighted_note / risk_veto / mandate_veto / mandate_violations，vip.js 仅做加性渲染。

    ponytail:
    - 加权 w=max(confidence,1)*calib（calib 复用 committee._member_weights，缺校准/无 store→1.0）；
      confidence 若为 0-1 制则 floor 到 1 → 退化为纯校准加权（安全，不会误放大）。
      同时保留 headcount approve/reject 供展示，加权与人头结论不一致时注 weighted_note。
    - 风控软否决：risk_officer 投 reject 即 risk_veto+caution，verdict 不得为 approve（approve→split）。
    - 纲领硬约束否决（P1-1）：check_mandate_compliance 报 hard 破坏（集中度/排除/回撤突破）时 mandate_veto+caution、
      不得升级为 approve。这是「结构化违规信号」正解——不再依赖 risk_officer 从叙述里自由推理是否破坏硬约束。
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

    # P1-1 纲领硬约束否决：结构化 hard 破坏直接抑制 approve（不靠 LLM 自由推理），surfaced 供主席综述/面板
    mc = mandate_compliance or {}
    mandate_violations = [v.get("label", "") for v in (mc.get("violations") or [])]
    mandate_veto = bool(mandate_violations)
    if mandate_veto:
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
            "weighted_note": weighted_note, "risk_veto": risk_veto,
            "mandate_veto": mandate_veto, "mandate_violations": mandate_violations}


_COMMITTEE_CORPUS_KEYS = ("portfolio_risk", "valuation_data", "sentiment_data",
                          "crowding_data", "peer_comparison", "catalyst_data")


def committee_corpus(context: dict) -> str:
    """把喂进投委会的真实数（组合风险 + 逐名背景）拼成防伪语料段，供 number_guard 校验委员叙述——
    否则委员引用这些合法数字（HHI/VaR/PE…）会被误标 ⚠。两 pass 共用。"""
    return json.dumps({k: context.get(k) for k in _COMMITTEE_CORPUS_KEYS},
                      ensure_ascii=False, default=str)


def annotate_committee(committee: dict, corpus: str, foreign_values: list[float] | None = None) -> list[str]:
    """给委员叙述(assessment/key_concerns)里未在语料中出现的数字加 ⚠，就地写回 committee["members"]，返回未核到 token。

    corpus 须含 committee_corpus(context)（Phase 2 喂进委员会的真实数），否则委员引用合法数字会被误标。
    """
    unverified: list[str] = []

    def ann(text: str) -> str:
        if not text:
            return text
        unverified.extend(r["token"] for r in number_guard.verify_numbers(text, corpus, foreign_values) if r["status"] == "unverified")
        return number_guard.annotate_unverified(text, corpus, foreign_values=foreign_values)

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


def enforce_mandate_hard(holdings: list[dict], compliance: dict,
                         sector_by_ticker: dict | None = None) -> list[str]:
    """P1-1 硬拦：对命中纲领硬约束（排除/单仓集中度=按 ticker；板块集中度=按 sector）的持仓，把「加仓」
    确定性下调「持有」并注明原因，返回被拦 ticker。

    这是「硬拦」的落地——结构化违规不止提示，还确定性收敛激进动作。回撤是组合级（无 per-ticker items）→
    由 mandate_veto 抑制整体升级、不逐仓改。排除命中即便原动作是持有也追加退出提示（advice-only，不臆造减仓 sizing）。
    ★ 板块集中度 violation 的 items 是**板块名**（非 ticker，见 mandate.check_mandate_compliance），故按板块匹配：
      板块超限时该板块内所有加仓下调持有。草案持仓 sector 缺失时从 sector_by_ticker（dossier 权威口径）反查。
    ponytail: 只降「加仓→持有」一档；强制减仓需仓位级判断，留给委员会/用户。
    """
    by_ticker: dict[str, list[str]] = {}
    by_sector: dict[str, list[str]] = {}
    for v in (compliance or {}).get("violations", []):
        target = by_sector if v.get("key") == "sector_concentration" else by_ticker
        for it in (v.get("items") or []):
            target.setdefault(str(it), []).append(v.get("label", "硬约束"))
    smap = sector_by_ticker or {}
    blocked: list[str] = []
    for h in holdings:
        tk = str(h.get("ticker", ""))
        sec = str(h.get("sector") or smap.get(tk) or "").strip()      # 草案缺 sector → dossier 权威反查
        labels = by_ticker.get(tk, []) + (by_sector.get(sec, []) if sec else [])
        if not labels:
            continue
        lab = "、".join(dict.fromkeys(labels))
        if str(h.get("action", "")) == "加仓":
            h["action"] = "持有"
            h["reason"] = (str(h.get("reason", "")) + f"（触及纲领硬约束[{lab}]，加仓已下调为持有）").strip()
            blocked.append(tk)
        else:
            h["reason"] = (str(h.get("reason", "")) + f"（触及纲领硬约束[{lab}]，请审慎核对）").strip()
    return blocked


def _parse_weight(s: str) -> float | None:
    """从软仓位串解析百分比中点（money path）：'3%-5%'→4.0；'5%'→5.0；区间取均值；无带%数字→None。
    只认百分号数字，避免把理由里的其它数字误当仓位。"""
    if not s:
        return None
    nums = [float(x) for x in re.findall(r"(\d+(?:\.\d+)?)\s*%", str(s))]
    if not nums:
        return None
    return round(sum(nums) / len(nums), 2)  # 单值即其本身，区间取中点


def _size_one_add(wl_store, ticker: str, hold: dict, total_equity: float) -> dict | None:
    """给一只『加仓』标的估一档波动率缩放的指示性加仓量（INV-2/INV-4：接活 PositionSizer，替代"加仓不说加多少"）。
    ref_price = market_value / shares（结算单 USD 口径）；vol 取近 60 日快照年化。
    数据不足（无价/无快照/波动率算不出/股数为 0）→ None：宁可标未量化，绝不硬凑 0 冒充已量化。"""
    from bottleneck_hunter.watchlist.position_sizing import PositionSizer
    shares_held = float(hold.get("shares") or 0)
    mv = float(hold.get("market_value") or 0)
    price = mv / shares_held if (shares_held > 0 and mv > 0) else 0.0
    if wl_store is None or price <= 0 or total_equity <= 0 or not ticker:
        return None
    try:
        snaps = wl_store.get_snapshots(ticker, days=60)
    except Exception:  # noqa: BLE001 - 取价失败绝不带崩预算对照
        return None
    closes = [float(s["close"]) for s in reversed(snaps or []) if s.get("close") not in (None, "")]
    rets = [closes[i] / closes[i - 1] - 1.0 for i in range(1, len(closes)) if closes[i - 1] > 0]
    vol = PositionSizer.compute_stock_volatility(rets)  # <5 样本→0.0
    if vol <= 0:
        return None
    sized = PositionSizer.volatility_scaled(0.15, vol, total_equity, price)  # 目标年化波动15%，内含单仓≤20%钳制
    if sized.get("shares", 0) <= 0:
        return None
    return {"ticker": ticker, "suggested_shares": sized["shares"],
            "suggested_amount": sized["amount"], "target_weight_pct": sized["weight_pct"],
            "vol_annual_pct": round(vol * 100, 1), "ref_price": round(price, 2)}


def summarize_cash_budget(dossier: dict, advisory_result: dict | None,
                          recommend_result: dict | None, *, wl_store=None) -> dict:
    """指示性现金/仓位预算对照（只提示不约束）：把两个 pass 的建仓候选量化仓位加总，对照可投资现金给 sanity。

    需求侧本不可精确量化（现金是结算单静态口径），故结论仅 indicative：
    - requested_new_buy = Σ(recommend 建仓候选软仓位中点% × total_equity)【有明确仓位%者】
      + Σ(advisory 加仓的波动率缩放估算金额)【传 wl_store 且价/波动率可算者，见 _size_one_add】。
      无仓位%的建仓 / 无法量化的加仓 → 计入 unquantified_adds、不塞进 requested（不拿 0 冒充已量化）。
    - add_suggestions: 逐笔加仓的「建议股数/约 $金额/目标权重%/参考价/年化波动%/现金是否覆盖」（INV-4 可执行性）。
    - available_cash = dossier.cash_balance（结算单口径、不含融资 buying_power）。
    - fits / overcommit_pct / cash_coverage_pct 仅基于「已量化」部分；unquantified_adds>0 时判断偏乐观，note 明示口径。
    """
    total_equity = float(dossier.get("total_equity") or 0)
    available = float(dossier.get("cash_balance") or 0)
    hold_map = {h.get("ticker"): h for h in (dossier.get("holdings") or []) if h.get("ticker")}
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
    add_suggestions: list[dict] = []
    for h in ((advisory_result or {}).get("holdings") or []):
        if str(h.get("action", "")) != "加仓":
            continue
        sized = _size_one_add(wl_store, h.get("ticker", ""), hold_map.get(h.get("ticker", ""), {}), total_equity)
        if sized is None:  # 加仓但价/波动率不可算（或未传 wl_store）→ 仍诚实计未量化
            unquantified += 1
        else:
            requested += sized["suggested_amount"]
            add_suggestions.append(sized)
    requested = round(requested, 2)
    over = requested - available
    fits = over <= 0
    overcommit = round(over, 2) if over > 0 else 0.0
    overcommit_pct = round(over / available * 100, 1) if (over > 0 and available > 0) else 0.0
    run = 0.0  # 逐笔累计现金是否覆盖（按建议顺序）
    for s in add_suggestions:
        run += s["suggested_amount"]
        s["cash_covered"] = run <= available
    cash_coverage_pct = round(available / requested * 100, 1) if requested > 0 else None
    tail = (f"另有 {unquantified} 项加仓/建仓未给出仓位量、未纳入需求合计，容量判断偏乐观。"
            if unquantified else "")
    if add_suggestions:
        tail += f"（{len(add_suggestions)} 项加仓已按波动率缩放估指示性档位：目标年化波动15%、单仓≤20%权益。）"
    return {"available_cash": round(available, 2), "requested_new_buy": requested,
            "fits": fits, "overcommit": overcommit, "overcommit_pct": overcommit_pct,
            "unquantified_adds": unquantified, "add_suggestions": add_suggestions,
            "cash_coverage_pct": cash_coverage_pct,
            "note": "现金口径为结算单静态余额、不含融资(buying_power)，容量判断为指示性。" + tail}


_VERDICT_ZH = {"approve": "通过", "reject": "否决", "split": "分歧"}


def chair_summary(committee: dict, cash_budget: dict | None = None) -> str:
    """0-9：投委会主席综述行——确定性拼装 verdict+票数+关键护栏信号+现金容量，**不再花第 2 次 LLM**。

    一句话把「4 席怎么投、有没有被风控/独立性护栏拦、拟新增买入现金够不够」讲清，作为逐条建议之上的总纲。
    纯拼装：只读 _consensus 已算好的结构化结论 + summarize_cash_budget 的容量口径，零模型调用。"""
    verdict = committee.get("verdict", "")
    vtxt = _VERDICT_ZH.get(verdict, verdict or "—")
    parts = [f"投委会加权表决：{vtxt}（赞成 {committee.get('approve', 0)} / 否决 {committee.get('reject', 0)}）"]
    if committee.get("mandate_veto"):
        vio = "、".join(committee.get("mandate_violations") or []) or "硬约束"
        parts.append(f"触及纲领硬约束（{vio}），已抑制升级为通过")
    if committee.get("risk_veto"):
        parts.append("风控委员否决，已抑制升级为通过")
    elif committee.get("caution"):
        parts.append("多数持保留，建议审慎核对")
    if committee.get("diversity_warning"):
        parts.append("委员独立性降级，结论仅供参考")
    if cash_budget:
        req = cash_budget.get("requested_new_buy", 0) or 0
        avail = cash_budget.get("available_cash", 0) or 0
        if not cash_budget.get("fits", True):
            if avail > 0:
                parts.append(f"拟新增买入约 ${req:,.0f} 超可投资现金 ${avail:,.0f}"
                             f"（超 {cash_budget.get('overcommit_pct', 0) or 0:.0f}%）")
            else:  # 现金恰为 0/负 → overcommit_pct 恒 0，不印自相矛盾的"超 0%"
                parts.append(f"拟新增买入约 ${req:,.0f}，但无可投资现金")
        elif req > 0:
            parts.append(f"拟新增买入约 ${req:,.0f}，现金可覆盖")
        if cash_budget.get("unquantified_adds"):
            parts.append(f"另有 {cash_budget['unquantified_adds']} 项未量化，容量判断偏乐观")
    return "；".join(parts) + "。"


# P1-2：本轮账户统一行动清单——把 advisory(减/持/加) 与 recommend(建仓/关注/规避) 并成一张按可执行性排序的清单。
_ACTION_RANK = {"减仓": 0, "建仓": 1, "加仓": 2, "关注": 3, "持有": 4, "规避": 5}
_ACTIONABLE = {"减仓", "加仓", "建仓"}  # 本轮需下手的动作（持有/关注/规避＝不动/观察/回避，非行动）


def _merge_actions(adv: dict | None, rec: dict | None, add_sized: dict) -> list[dict]:
    """纯合并（决策路径）：advisory holdings + recommend candidates → 按可执行性排序的统一行动行。
    加仓项就地附 cash_budget 已算好的指示性 sizing（同一算法，口径不漂移）；同档稳定排序保 持仓→荐新 顺序。"""
    actions: list[dict] = []
    for h in ((adv or {}).get("holdings") or []):
        act = str(h.get("action", ""))
        item = {"ticker": h.get("ticker", ""), "action": act, "source": "持仓",
                "reason": h.get("reason", ""), "risk": h.get("risk", ""),
                "derivative_note": h.get("derivative_note", ""), "actionable": act in _ACTIONABLE}
        if act == "加仓" and item["ticker"] in add_sized:
            item["sizing"] = add_sized[item["ticker"]]
        actions.append(item)
    for c in ((rec or {}).get("candidates") or []):
        act = str(c.get("action", ""))
        actions.append({"ticker": c.get("ticker", ""), "action": act, "source": "荐新",
                        "reason": c.get("reason", ""), "risk": c.get("risk", ""),
                        "fit": c.get("fit", ""), "suggested_weight": c.get("suggested_weight", ""),
                        "actionable": act in _ACTIONABLE})
    actions.sort(key=lambda a: _ACTION_RANK.get(a["action"], 9))
    return actions


def build_action_plan(wl_store, account_ref: str = "") -> dict:
    """P1-2：读最新 advisory + 最新 recommend，合并成「本轮账户行动清单」并对两 pass 一起做现金配平。

    纯确定性拼装：只读两份已落库结果 + 复用 summarize_cash_budget 同一现金原语，零 LLM、不下单。
    任一 pass 缺失只并另一侧（sources 诚实标注偏乐观）；两侧皆无 → available False。
    """
    account_ref = (account_ref or "").strip()
    adv = get_latest_advisory(wl_store, account_ref)
    from bottleneck_hunter.vip import recommend as _recommend  # 惰性：recommend 顶层已 import advisory，避免循环
    rec = _recommend.get_latest_recommendations(wl_store, account_ref)
    if not adv and not rec:
        return {"available": False, "actions": [], "note": "尚无顾问建议或荐新，请先在对应标签页生成。"}
    dossier = portfolio.build_account_dossier(wl_store, account_ref=account_ref)
    cash_budget = summarize_cash_budget(dossier, adv, rec, wl_store=wl_store)  # 跨 pass 配平（加仓+建仓一起对照现金）
    add_sized = {s["ticker"]: s for s in cash_budget.get("add_suggestions", [])}
    actions = _merge_actions(adv, rec, add_sized)
    return {"available": True, "account_ref": account_ref,
            "data_as_of": (dossier.get("as_of_hint") or {}).get("data_as_of", ""),
            "actions": actions, "n_actionable": sum(1 for a in actions if a["actionable"]),
            "cash_budget": cash_budget,
            "sources": {"advisory_available": bool(adv), "advisory_at": (adv or {}).get("generated_at", ""),
                        "recommend_available": bool(rec), "recommend_at": (rec or {}).get("generated_at", "")}}


# 结算单月频，>此天数视为过期（需重新上传结算单）；env 可调
_STALE_DAYS = int(os.getenv("BH_VIP_STALE_DAYS", "45"))


def _days_between(iso_a: str, iso_b: str) -> int | None:
    """|a - b| 的自然日数（各取前 10 位日期段）；任一不可解析→None（诚实未知，不硬凑 0）。"""
    try:
        return abs((date.fromisoformat((iso_a or "")[:10]) - date.fromisoformat((iso_b or "")[:10])).days)
    except (ValueError, TypeError):
        return None


def verification_receipt(result: dict, *, stale_days: int = _STALE_DAYS) -> dict:
    """0-10：good-path 绿色核验回执——数字全核(number_guard 零未核)＋持仓可溯源＋数据新鲜 三项皆过时的正向信号。

    是 ⚠未核到 警示的正面对偶：把"无警示=沉默"翻成显式回执，让用户看见"本次数字全部溯源、快照日已知、在时效内"。
    任一不满足→green=False 并逐项列出未过原因（不掩盖）。纯确定性拼装，零 LLM。"""
    unverified = result.get("unverified") or []
    data_as_of = result.get("data_as_of") or ""
    gap = _days_between(result.get("generated_at", ""), data_as_of) if data_as_of else None
    fresh_ok = gap is not None and gap <= stale_days
    checks = [
        {"key": "numbers", "label": "数字全部溯源", "ok": not unverified,
         "detail": "建议中的数字均可在账户数据中核到" if not unverified else f"{len(unverified)} 个数字未核到"},
        {"key": "sourced", "label": "持仓可溯源", "ok": bool(data_as_of),
         "detail": f"快照截至 {data_as_of}" if data_as_of else "缺持仓快照日期"},
        {"key": "fresh", "label": "数据新鲜", "ok": fresh_ok,
         "detail": (f"快照距生成 {gap} 天，在 {stale_days} 天时效内" if fresh_ok else
                    (f"快照距生成 {gap} 天，已超 {stale_days} 天时效，建议重新上传结算单"
                     if gap is not None else "无法判定时效"))},
    ]
    green = all(c["ok"] for c in checks)
    return {"green": green, "checks": checks,
            "note": "本次建议数字全核、持仓可溯源且在时效内。" if green else "部分核验未通过，请留意下列提示。"}


def _annotate(draft: dict, corpus: str, foreign_values: list[float] | None = None) -> list[str]:
    """就地给草案文本里未在档案/纲领语料中出现的数字加⚠标注，返回未核到 token 列表。"""
    unverified: list[str] = []
    def ann(text: str) -> str:
        if not text:
            return text
        unverified.extend(r["token"] for r in number_guard.verify_numbers(text, corpus, foreign_values) if r["status"] == "unverified")
        return number_guard.annotate_unverified(text, corpus, foreign_values=foreign_values)
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
    import asyncio

    from bottleneck_hunter.llm_clients.factory import get_models_for_role
    from bottleneck_hunter.watchlist.committee import MEMBERS, _review_single

    account_ref = (account_ref or "").strip()
    if not account_ref:
        # 硬守卫：空 ref 会经 build_account_dossier→build_account_summary→get_sim_account("")
        # 落到决策中心自有 sim_account('')（读、并惰性建 DC L4 模拟盘行）。前端 requireConcreteAccount 已挡，
        # 此处补后端防护，杜绝直连 API 越界读写决策中心模拟盘。见 memory:dc_sim_account_decoupled。
        return {"error": "请先选择具体子账户再生成顾问建议"}
    inputs = _build_inputs(wl_store, account_ref)
    dossier = inputs["dossier"]
    if not dossier.get("holdings"):
        return {"error": "该账户暂无持仓，请先上传月结单"}
    # P1-1：纲领硬约束结构化对账（一次算好，复用于委员会注入 / 合议否决 / 硬拦 / 结果面板）。绝不带崩。
    try:
        mandate_comp = _mandate.check_mandate_compliance(
            _mandate.load_mandate(wl_store, account_ref), dossier)
    except Exception:  # noqa: BLE001
        mandate_comp = {}
    # P1-4：衍生品 KO/KI 状态只读扫描（全量，含 available False 项供面板诚实标注；委员会仅收 available 项）。
    try:
        from bottleneck_hunter.vip.projection import derivative_barrier_status
        deriv_barriers = derivative_barrier_status(wl_store, account_ref)
    except Exception:  # noqa: BLE001
        deriv_barriers = []
    if budget is not None and not budget.can_spend():
        return {"error": "预算不足，暂不生成顾问建议"}
    models = get_models_for_role("vip_advisor", user_id=user_id, with_fallback=True)
    if not models:
        return {"error": "无可用 LLM（请在 AI 配置中为 vip_advisor 配置模型）"}
    llm, provider, model = models[0]

    # ── 1) 草案生成 ──
    # 回填往期策略复盘沉淀的经验卡片（scope='vip_portfolio'）：闭环的「喂回」侧。逐卡 increment_card_applied，
    # 记 applied_card_ids 供 strategy_review.score_prior_cards 认定「被注入过」。缺卡/异常降级空文本，绝不带崩。
    try:
        prior_cards = wl_store.get_experience_cards(
            scope="vip_portfolio", scope_key=account_ref, limit=8)
    except Exception:  # noqa: BLE001
        prior_cards = []
    # §9.2 顾问建议增据：逐仓研报 + KB 证据（best-effort，无凭据/未开 → "暂无"，不阻断）。
    evidence_text = await gather_holdings_evidence(wl_store, dossier.get("holdings", []))
    prompt = _DRAFT_PROMPT.format(
        dossier=json.dumps(dossier, ensure_ascii=False, default=str),
        mandate=inputs["mandate_text"], macro=inputs["macro_text"],
        derivatives=inputs["deriv_text"], coverage=inputs["coverage_text"],
        experience_cards=_render_experience_cards(prior_cards),
        evidence=evidence_text)
    # §9.3 推理期主动补数据：草案模型可发 [[DATA_REQ]] 经 DataHub 实时取数回注（研报/估值分位/财务→Gangtise 优先），
    # 至多 2 轮 8 次；可查范围仅限本账户持仓。数据链异常 → 降级为无补数据单趟（建议照常产出，与本文件其它缺省降级同风格）。
    holdings_tickers = [h.get("ticker") for h in dossier.get("holdings", []) if h.get("ticker")]
    adv_market = getattr(wl_store, "_market", "") or ""

    async def _ask(p):
        r = await llm.ainvoke(p)
        return getattr(r, "content", r) if not isinstance(r, str) else r

    try:
        from bottleneck_hunter.data_provider import ai_tools
        draft_text, _fetch_log, _ = await ai_tools.negotiate(
            _ask, prompt, market=adv_market, user_id=user_id, allowed_tickers=holdings_tickers)
    except Exception:  # noqa: BLE001  协商环/取数异常绝不阻断建议生成
        draft_text = await _ask(prompt)
    draft = _validate_draft(draft_text)
    if not draft["holdings"]:
        return {"error": "草案生成失败或未返回持仓建议，请重试"}

    # ── 2) 投委会 4 persona 并行评审（复用 committee._review_single；喂真实组合风险+逐名背景，不碰 sim 表）──
    context = build_committee_context(
        wl_store, dossier, inputs["macro_text"],
        [{"ticker": h.get("ticker"), "entry_id": h.get("entry_id")} for h in dossier.get("holdings", [])],
        mandate_compliance=mandate_comp, derivative_barriers=deriv_barriers)
    exec_plan = {"account_ref": account_ref, "mandate": inputs["mandate_text"], "draft": draft}
    reviews = await asyncio.gather(*[_review_single(m, exec_plan, context) for m in MEMBERS],
                                   return_exceptions=True)
    reviews = [r for r in reviews if isinstance(r, dict)]

    # ── 3) 确定性合议 + number_guard（草案 + 委员叙述都过防伪；corpus 含喂进委员会的真实数）──
    committee = _consensus(reviews, store=wl_store, market=getattr(wl_store, "_market", "") or "",
                           mandate_compliance=mandate_comp)
    corpus = (json.dumps(dossier, ensure_ascii=False, default=str)
              + "\n" + inputs["mandate_text"] + "\n" + inputs["macro_text"]
              + "\n" + committee_corpus(context))
    fv = number_guard.foreign_account_values(dossier)  # 非美元衍生价/已实现盈亏：$令牌不据此核实（跨币防误核）
    unverified = _annotate(draft, corpus, fv)
    unverified = list(dict.fromkeys(unverified + annotate_committee(committee, corpus, fv)))

    # ── 3b) 草案↔投委会对账：reject→加仓降持有、caution/split→加警示注（memory:vip_advisory_pass 用户已确认强度）──
    reconciled = reconcile_draft(draft["holdings"], "action", {"加仓": "持有"}, committee)
    # ── 3b') P1-1 纲领硬拦：命中排除/集中度的持仓「加仓」确定性下调「持有」（先于容量对照，被拦项不计入拟新增买入）──
    #    板块集中度按 sector 匹配——草案持仓未必回带 sector，故传 dossier 权威 ticker→sector 映射兜底。
    sector_by_ticker = {h.get("ticker"): (h.get("sector") or "")
                        for h in dossier.get("holdings", []) if h.get("ticker")}
    mandate_blocked = enforce_mandate_hard(draft["holdings"], mandate_comp, sector_by_ticker=sector_by_ticker)

    # ── 3c) 0-9 主席综述行：本 pass 只含加仓量化（recommend 侧建仓未知→None），容量口径与 budget 端点一致 ──
    cash_budget = summarize_cash_budget(dossier, {"holdings": draft["holdings"]}, None, wl_store=wl_store)

    result = {
        "account_ref": account_ref,
        "generated_at": _now_iso(),
        "data_as_of": (dossier.get("as_of_hint") or {}).get("data_as_of", ""),  # 0-1：持仓数据截至日
        "chair_summary": chair_summary(committee, cash_budget),  # 0-9：确定性主席综述行（无第 2 次 LLM）
        "portfolio_diagnosis": draft["portfolio_diagnosis"],
        "cross_market_coverage": draft["cross_market_coverage"],
        "holdings": draft["holdings"],
        "committee": committee,
        "mandate_compliance": mandate_comp,        # P1-1：合规对账面板（集中度/排除/回撤/聚焦，确定性）
        "mandate_blocked": mandate_blocked,        # 被硬拦下调为持有的标的
        "sector_rotation_reconcile": context["portfolio_risk"].get("sector_rotation_reconcile"),  # P1-3：板块轮动对照
        "derivative_barriers": deriv_barriers,     # P1-4：衍生品 KO/KI 状态面板（距障碍%/触发/剩余名义）
        "unverified": unverified,
        "reconciled": reconciled,
        "provider": provider, "model": model,
        "advisor_calibration": advisor_calibration(wl_store, provider, model),  # F1：surfaced 可信度
        "applied_card_ids": [c["id"] for c in prior_cards if c.get("id")],  # 5c：本轮注入的经验卡片（供 score_prior_cards）
        "disclaimer": compliance.DISCLAIMER_ZH,
    }
    # 逐卡记一次「被引用」（applied_count+1 / last_applied_at），旁路容错——不影响建议主链路
    for c in prior_cards:
        if c.get("id"):
            try:
                wl_store.increment_card_applied(c["id"])
            except Exception:  # noqa: BLE001
                import logging
                logging.getLogger(__name__).debug("VIP 经验卡片 applied 计数失败（不影响建议）", exc_info=True)
    result["verification_receipt"] = verification_receipt(result)  # 0-10：读 unverified/data_as_of/generated_at
    # P0-②：证据溯源——draft prompt 哈希 + 实际模型 + 数据截至日 + 标的，嵌进 result_json 与审计（复盘可辩护，分辨模型幻觉 vs 数据错）
    from bottleneck_hunter.watchlist.provenance import build_provenance, hash_text
    _draft_hash = hash_text(_DRAFT_PROMPT)  # VIP draft 是内联模板(非 .md)，直接哈希字符串
    result["_provenance"] = build_provenance(
        prompts=[], models=[(provider, model)],
        data_as_of=result.get("data_as_of", ""),
        tickers=[h.get("ticker", "") for h in draft["holdings"]],
        generated_at=result.get("generated_at", ""),
        extra={"layer": "vip_advisor", "market": getattr(wl_store, "_market", "") or ""},
        extra_prompt_hashes={"vip_draft": _draft_hash},
    )
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
                                 "tickers": [h["ticker"] for h in draft["holdings"]],
                                 "prompt_hash": _draft_hash},  # P0-②：审计溯源补 draft prompt 哈希
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

    # 12b) B: 加仓量化（INV-2/INV-4 接活 PositionSizer）——传 wl_store 且价/波动率可算→给建议股数/金额/覆盖率
    class _SnapStore:
        def get_snapshots(self, tk, days=60):  # 25 日温和上行→年化波动率可算(>0)
            return [{"close": 100 + i * 0.5} for i in range(25)]
    dossier_q = {"total_equity": 100000.0, "cash_balance": 50000.0,
                 "holdings": [{"ticker": "NVDA", "shares": 100, "market_value": 40000.0}]}  # 参考价 400
    q = summarize_cash_budget(dossier_q, {"holdings": [{"ticker": "NVDA", "action": "加仓"}]},
                              None, wl_store=_SnapStore())
    assert len(q["add_suggestions"]) == 1 and q["unquantified_adds"] == 0, q      # 已量化→不再计未量化
    sg = q["add_suggestions"][0]
    assert sg["suggested_shares"] > 0 and sg["suggested_amount"] > 0, sg          # 有具体股数/金额
    assert sg["target_weight_pct"] <= 20.0 + 1e-6, sg                            # 单仓≤20% 权益钳制生效
    assert q["cash_coverage_pct"] is not None and sg["cash_covered"] is True, q   # 5万现金覆盖率有值且够
    # 未传 wl_store（旧行为）→ 加仓仍诚实计未量化、不硬凑
    q0 = summarize_cash_budget(dossier_q, {"holdings": [{"ticker": "NVDA", "action": "加仓"}]}, None)
    assert q0["add_suggestions"] == [] and q0["unquantified_adds"] == 1, q0

    # 13) C-1 隔离回归守卫：VIP 打点常量绝不撞 sim 的 role_context/prediction_type（防手滑致校准交叉污染）
    assert VIP_PT_ADVICE not in ("vote",) and VIP_PT_RECOMMEND not in ("vote",)
    assert not VIP_ROLE_CONTEXT.startswith("committee_")
    assert VIP_PT_ADVICE != VIP_PT_RECOMMEND

    # 14) F1 回接自检：advisor_calibration 读回 vip_advisor 桶权重并 surfaced（中性/偏低/良好/异常兜底）
    class _CalStub:
        def __init__(self, w): self._w = w
        def get_calibration_weight(self, p, m, role_context="", market=""):
            if self._w is None:
                raise RuntimeError("boom")
            assert role_context == VIP_ROLE_CONTEXT  # 必须读 vip_advisor 桶，不得串 committee_*
            return self._w
    assert advisor_calibration(_CalStub(1.0), "x", "y")["calibration_weight"] == 1.0
    assert "中性" in advisor_calibration(_CalStub(1.0), "x", "y")["note"]
    assert "保守" in advisor_calibration(_CalStub(0.7), "x", "y")["note"]        # <1 提醒更保守
    assert advisor_calibration(_CalStub(1.3), "x", "y")["calibration_weight"] == 1.3
    assert advisor_calibration(_CalStub(None), "x", "y")["calibration_weight"] == 1.0  # 异常兜底中性
    assert advisor_calibration(_CalStub(0.0), "x", "y")["calibration_weight"] == 1.0   # 非正权重视作中性

    # 15) 0-9 chair_summary：确定性拼装 verdict+票数+护栏+现金容量，零 LLM
    s_ok = chair_summary({"verdict": "approve", "approve": 3, "reject": 1})
    assert s_ok.startswith("投委会加权表决：通过（赞成 3 / 否决 1）") and s_ok.endswith("。"), s_ok
    s_veto = chair_summary({"verdict": "split", "approve": 3, "reject": 1, "risk_veto": True,
                            "diversity_warning": "全 glm"},
                           {"requested_new_buy": 60000.0, "available_cash": 50000.0, "fits": False,
                            "overcommit_pct": 20.0, "unquantified_adds": 2})
    assert "风控委员否决" in s_veto and "独立性降级" in s_veto, s_veto
    assert "超可投资现金 $50,000" in s_veto and "另有 2 项未量化" in s_veto, s_veto
    s_fit = chair_summary({"verdict": "approve", "approve": 4, "reject": 0, "caution": False},
                          {"requested_new_buy": 10000.0, "available_cash": 50000.0, "fits": True})
    assert "现金可覆盖" in s_fit and "风控" not in s_fit, s_fit    # fits 且无护栏→只报可覆盖

    # 16) 0-10 verification_receipt：三项皆过→green；缺一→green=False 且列出未过项
    green = verification_receipt({"unverified": [], "data_as_of": "2026-07-20",
                                  "generated_at": "2026-08-02T00:00:00+00:00"}, stale_days=45)
    assert green["green"] is True and all(c["ok"] for c in green["checks"]), green   # 13 天 < 45
    bad_num = verification_receipt({"unverified": ["$999"], "data_as_of": "2026-07-20",
                                    "generated_at": "2026-08-02T00:00:00+00:00"}, stale_days=45)
    assert bad_num["green"] is False and bad_num["checks"][0]["ok"] is False, bad_num  # 有未核数→不绿
    stale = verification_receipt({"unverified": [], "data_as_of": "2026-01-01",
                                  "generated_at": "2026-08-02T00:00:00+00:00"}, stale_days=45)
    assert stale["green"] is False and stale["checks"][2]["ok"] is False, stale        # 超时效→不绿
    blank = verification_receipt({"unverified": [], "data_as_of": "",
                                  "generated_at": "2026-08-02T00:00:00+00:00"})
    assert blank["green"] is False and blank["checks"][1]["ok"] is False, blank         # 无快照日→不可溯源+时效未知
    assert _days_between("2026-08-02T00:00:00+00:00", "2026-07-20") == 13
    assert _days_between("bad", "2026-07-20") is None                                   # 不可解析→None，不硬凑

    # 17) P1-1 纲领硬约束接线：_consensus 收结构化违规→mandate_veto 抑制升级 / enforce 硬拦 / chair 综述
    mc_bad = {"compliant": False, "violations": [{"key": "single_concentration", "label": "单仓集中度",
                                                  "detail": "NVDA 40% > 15%", "items": ["NVDA"]}]}
    # 全票 approve 但触硬约束 → verdict 由 approve 压到 split、mandate_veto、caution
    cmv = _consensus([{"role": r, "vote": "approve", "confidence": 5, "provider": p} for r, p in
                      (("risk_officer", "openai"), ("growth_investor", "glm"),
                       ("value_investor", "qwen"), ("contrarian", "deepseek"))],
                     mandate_compliance=mc_bad)
    assert cmv["mandate_veto"] is True and cmv["verdict"] == "split" and cmv["caution"] is True, cmv
    assert cmv["mandate_violations"] == ["单仓集中度"], cmv
    # 无违规（compliant）→ 不触发 mandate_veto，不干扰原表决
    cmv_ok = _consensus([{"role": "growth_investor", "vote": "approve", "confidence": 5, "provider": "glm"}],
                        mandate_compliance={"compliant": True, "violations": []})
    assert cmv_ok["mandate_veto"] is False and cmv_ok["verdict"] == "approve", cmv_ok
    # 缺省不传 → 向后兼容，无 mandate_veto
    assert _consensus([{"role": "growth_investor", "vote": "approve"}])["mandate_veto"] is False

    # enforce_mandate_hard：命中标的「加仓」降「持有」+注；命中但非加仓→仅注；未命中→不动
    hs2 = [{"ticker": "NVDA", "action": "加仓", "reason": "看好"},
           {"ticker": "MU", "action": "持有", "reason": "观望"},
           {"ticker": "AAPL", "action": "加仓", "reason": "稳"}]
    mc2 = {"violations": [{"key": "exclusions", "label": "排除清单", "items": ["NVDA", "MU"]}]}
    blocked = enforce_mandate_hard(hs2, mc2)
    assert blocked == ["NVDA"], blocked                                    # 只 NVDA 是加仓被下调
    assert hs2[0]["action"] == "持有" and "硬约束" in hs2[0]["reason"], hs2  # 加仓→持有
    assert hs2[1]["action"] == "持有" and "审慎" in hs2[1]["reason"], hs2    # 命中但持有→仅注
    assert hs2[2]["action"] == "加仓" and "硬约束" not in hs2[2]["reason"], hs2  # 未命中→不动

    # chair_summary 含纲领硬约束行
    s_mv = chair_summary({"verdict": "split", "approve": 4, "reject": 0,
                          "mandate_veto": True, "mandate_violations": ["单仓集中度", "排除清单"]})
    assert "触及纲领硬约束" in s_mv and "单仓集中度" in s_mv, s_mv

    # 18) P1-3 板块轮动对照：重仓走弱板块入 in_weakening、持有走强入 in_strengthening、判强零持仓入 unheld
    rot = reconcile_sector_rotation(
        [{"ticker": "NVDA", "weight_pct": 40.0, "sector": "半导体"},
         {"ticker": "XOM", "weight_pct": 30.0, "sector": "能源"},
         {"ticker": "KO", "weight_pct": 10.0, "sector": "未知"}],   # 未知板块跳过
        {"strengthening": ["半导体板块"], "weakening": ["能源"], "neutral": ["医药"]})
    assert rot["available"] is True, rot
    assert [r["sector"] for r in rot["in_weakening"]] == ["能源"], rot            # 能源∈弱
    assert rot["weakening_weight_pct"] == 30.0, rot
    assert [r["sector"] for r in rot["in_strengthening"]] == ["半导体"], rot       # '半导体'⊂'半导体板块'
    assert "半导体板块" not in rot["strengthening_unheld"], rot                    # 已持有→不列 unheld
    # 中文键 兼容（看多/看空）+ 判强零持仓
    rot2 = reconcile_sector_rotation(
        [{"ticker": "AAPL", "weight_pct": 20.0, "sector": "科技"}],
        {"看多": ["医药", "科技"], "看空": ["地产"]})
    assert "医药" in rot2["strengthening_unheld"] and "科技" not in rot2["strengthening_unheld"], rot2
    assert [r["sector"] for r in rot2["in_strengthening"]] == ["科技"], rot2
    # 无强/弱信号 → available False，不硬凑
    rot3 = reconcile_sector_rotation([{"ticker": "AAPL", "weight_pct": 20.0, "sector": "科技"}],
                                     {"neutral": ["医药"]})
    assert rot3["available"] is False and rot3["in_weakening"] == [], rot3

    print("advisory self-check OK")
