"""Phase 5c · VIP 顾问·反思式策略复盘（组合中周期切片）。

区别于「复盘对错」的个股二值台账（那是市场真相的底层证据）：本模块让顾问对**自身历史组合策略**
做阶段性检讨——哪对哪错、与市场实际表现的差距、下一步怎么修正——并把教训沉淀成**经验卡片**，
回填给顾问（advisory 侧注入），让顾问看到自己历史决策结果、逐步提高预测/建议准确率。

设计边界（沿用 VIP 与 sim 物理隔离）：
- 单模型「首席复盘官」自我检讨（一致连贯的声音，比 4-persona 委员会更省更合适）；无模型/预算不足 → 规则兜底。
- 卡片用 scope='vip_portfolio'、scope_key=account_ref：sim 的 L4 只读 get_relevant_cards（仅 global/ticker/sector），
  故 VIP 卡片绝不泄漏进决策中心；VIP 侧精确用 get_experience_cards(scope='vip_portfolio') 取回。
- 一切数字过 number_guard 标注、叙事挂 compliance 免责声明。绝不写 sim_* 表。
"""
from __future__ import annotations

import json
import logging

from bottleneck_hunter.chain.json_utils import extract_json_object
from bottleneck_hunter.vip import compliance, number_guard, portfolio
from bottleneck_hunter.vip import mandate as _mandate
from bottleneck_hunter.vip.advice_review import build_review_ledger
from bottleneck_hunter.vip.advisory import format_macro_for_prompt, list_advisory

logger = logging.getLogger(__name__)

VIP_CARD_SCOPE = "vip_portfolio"
_CARD_CATEGORIES = {"lesson", "pattern", "rule"}

_REVIEW_PROMPT = """你是一位资深私人财务顾问团队的**首席复盘官**，负责对本团队过去一段时间给这个真实账户的
**组合层面策略**做诚实的阶段性检讨。只依据下面的真实数据，绝不编造任何价格/收益/占比/股数。

## 账户当前档案（结算单事实口径）
{dossier}

## 账户净值曲线（区间起止、各期收益率，basis 为口径标签）
{value_series}

## 我们过去给出的历史建议（新→旧，含当时的组合诊断与逐仓动作）
{advisory_history}

## 这些建议后来的市场真相（已结个股建议：动作 vs 实际涨跌 vs 对错，命中率为唯一口径）
{ledger}

{mandate}

## 当前宏观研判（L1，只读）
{macro}

请以「复盘官」的口吻做自我检讨，输出**严格 JSON**（不要 markdown 代码块、不要 JSON 以外任何文字）：
{{
  "critique": "检讨 2-5 句：我们过去的组合策略（集中度/行业暴露/现金/与纲领的匹配）哪里对、哪里错，用净值与命中率佐证",
  "market_reality": "与市场实际表现的差距 1-3 句：净值/命中率说明我们的判断在哪些地方被现实证伪或证实",
  "correction": "策略修正方向 2-4 句：下一阶段应如何调整（可执行、贴合纲领与档案数字，不下单只给方向）",
  "cards": [
    {{"title": "一句话教训标题", "content": "可复用的经验/规律/规则 2-3 句",
      "category": "lesson|pattern|rule", "confidence": 0.5, "evidence": ["支撑该卡的事实要点"]}}
  ]
}}
要求：cards 给 1-3 张，是**可跨期复用**的组合层经验（非个股一次性点评）；简体中文；数字只引用上面出现过的。"""


def _fmt_value_series(vs: dict) -> str:
    series = vs.get("series") or []
    if not series:
        return "暂无净值曲线（未积累多期结算单，无法做区间对比）。"
    first, last = series[0], series[-1]
    chg = _pct_change(vs)
    head = (f"区间 {first.get('as_of_date')} → {last.get('as_of_date')}，"
            f"净值 {first.get('total_equity')} → {last.get('total_equity')}"
            f"（累计 {chg:+.1f}%）" if chg is not None else
            f"区间 {first.get('as_of_date')} → {last.get('as_of_date')}，净值 {last.get('total_equity')}")
    rets = "；".join(f"{r.get('period')}: {r.get('pct')}%" for r in (vs.get("returns") or []))
    basis = vs.get("basis", "")
    return f"{head}。口径 {basis}。逐期收益率：{rets or '无'}"


def _fmt_advisory_history(rows: list[dict], limit: int = 4) -> str:
    if not rows:
        return "暂无历史顾问建议（尚未生成过 advisory）。"
    out = []
    for r in rows[:limit]:
        res = r.get("result") or {}
        diag = str(res.get("portfolio_diagnosis", "")).strip()
        acts = "、".join(f"{h.get('ticker')} {h.get('action')}" for h in (res.get("holdings") or [])
                         if h.get("ticker"))
        out.append(f"[{r.get('created_at', '')[:10]}] 诊断：{diag[:120]} | 动作：{acts[:200]}")
    return "\n".join(out)


def _fmt_ledger(led: dict) -> str:
    kpi = led.get("kpi") or {}
    rows = led.get("ledger") or []
    hr = kpi.get("hit_rate_pct")
    head = (f"已结 {kpi.get('settled', 0)} 条、对 {kpi.get('correct', 0)} 条、"
            f"命中率 {hr}%（未结 {kpi.get('pending', 0)} 条）"
            if hr is not None else "暂无已结建议（数据待积累）")
    lines = [f"{r.get('date')} {r.get('ticker')} {r.get('action')} → "
             f"{'对' if r.get('correct') else ('错' if r.get('correct') is False else '未结')}"
             f"（{r.get('chg_pct')}%）" for r in rows[:15]]
    return head + ("\n" + "\n".join(lines) if lines else "")


def _pct_change(vs: dict) -> float | None:
    """区间累计涨跌 %：首末净值端点。缺任一端或首值为 0 → None（诚实留空，不臆造）。"""
    series = vs.get("series") or []
    if len(series) < 2:
        return None
    first = series[0].get("total_equity")
    last = series[-1].get("total_equity")
    if not first:
        return None
    return round((last / first - 1) * 100, 2)


def _assemble_evidence(wl_store, account_ref: str) -> dict:
    """聚合复盘只读证据（全复用既有引擎）。绝不带崩：任一子项缺失走降级文本。"""
    dossier = portfolio.build_account_dossier(wl_store, account_ref=account_ref)
    vs = portfolio.value_series(wl_store, account_ref=account_ref)
    try:
        advisories = list_advisory(wl_store, account_ref=account_ref, limit=6)
    except Exception:  # noqa: BLE001
        advisories = []
    ledger = build_review_ledger(wl_store)
    mandate_text = _mandate.format_mandate_for_prompt(wl_store, account_ref=account_ref)
    macro_text = format_macro_for_prompt(wl_store)
    return {"dossier": dossier, "value_series": vs, "advisories": advisories,
            "ledger": ledger, "mandate_text": mandate_text, "macro_text": macro_text}


def _validate_review(raw: str) -> dict:
    """解析复盘 JSON + 规范化卡片（非法 category→lesson、confidence 夹到 [0,1]）。"""
    data = extract_json_object(raw) or {}
    cards = []
    for c in (data.get("cards") or []):
        if not isinstance(c, dict):
            continue
        title = str(c.get("title", "")).strip()
        content = str(c.get("content", "")).strip()
        if not title or not content:
            continue
        cat = str(c.get("category", "")).strip()
        if cat not in _CARD_CATEGORIES:
            cat = "lesson"
        try:
            conf = float(c.get("confidence", 0.5))
        except (TypeError, ValueError):
            conf = 0.5
        conf = min(1.0, max(0.0, conf))
        ev = [str(x).strip() for x in (c.get("evidence") or []) if str(x).strip()]
        cards.append({"title": title, "content": content, "category": cat,
                      "confidence": round(conf, 2), "evidence": ev})
    return {
        "critique": str(data.get("critique", "")).strip(),
        "market_reality": str(data.get("market_reality", "")).strip(),
        "correction": str(data.get("correction", "")).strip(),
        "cards": cards,
    }


def _rule_based_review(ev: dict) -> dict:
    """诚实降级：无 LLM/预算时用净值涨跌 + 命中率产出最小检讨，不臆造。"""
    chg = _pct_change(ev["value_series"])
    hr = (ev["ledger"].get("kpi") or {}).get("hit_rate_pct")
    facts = []
    if chg is not None:
        facts.append(f"区间净值 {chg:+.1f}%")
    if hr is not None:
        facts.append(f"已结建议命中率 {hr}%")
    if not facts:
        return {"critique": "数据不足（无多期净值、无已结建议），待积累后再复盘。",
                "market_reality": "", "correction": "继续积累结算单与建议结算样本。", "cards": []}
    critique = "（规则兜底·无 LLM 反思）阶段事实：" + "、".join(facts) + "。"
    if hr is not None and hr < 50:
        correction = "命中率偏低，下一区间宜收敛激进动作、提高现金缓冲、复核集中度是否超纲。"
        card = {"title": "命中率偏低时收敛激进度",
                "content": "区间命中率低于 50% 时，下一轮建议应降低加仓强度、优先控回撤。",
                "category": "rule", "confidence": 0.5, "evidence": facts}
    else:
        correction = "延续当前框架，持续监控集中度与纲领匹配度。"
        card = {"title": "框架有效时保持纪律", "content": "命中率达标时不因短期波动漂移策略，维持既定纲领约束。",
                "category": "rule", "confidence": 0.5, "evidence": facts}
    return {"critique": critique, "market_reality": "", "correction": correction, "cards": [card]}


def _period_label(ev: dict) -> str:
    series = ev["value_series"].get("series") or []
    if series:
        return f"{series[0].get('as_of_date')}~{series[-1].get('as_of_date')}"
    return ""


async def run_portfolio_strategy_review(wl_store, account_ref: str, *,
                                        user_id: str = "", budget=None) -> dict:
    """生成并落库组合中周期策略复盘 + 沉淀经验卡片。返回 {review_id, result} 或 {error}。"""
    account_ref = (account_ref or "").strip()
    if not account_ref:
        return {"error": "请先选择具体子账户再生成策略复盘"}  # 见 memory:dc_sim_account_decoupled
    ev = _assemble_evidence(wl_store, account_ref)
    if not ev["dossier"].get("holdings"):
        return {"error": "该账户暂无持仓，请先上传月结单"}

    provider = model = ""
    review = None
    budget_ok = budget is None or budget.can_spend()
    if budget_ok:
        try:
            from bottleneck_hunter.llm_clients.factory import get_models_for_role
            models = get_models_for_role("vip_advisor", user_id=user_id, with_fallback=True)
        except Exception:  # noqa: BLE001
            models = []
        if models:
            llm, provider, model = models[0]
            prompt = _REVIEW_PROMPT.format(
                dossier=json.dumps(ev["dossier"], ensure_ascii=False, default=str),
                value_series=_fmt_value_series(ev["value_series"]),
                advisory_history=_fmt_advisory_history(ev["advisories"]),
                ledger=_fmt_ledger(ev["ledger"]),
                mandate=ev["mandate_text"], macro=ev["macro_text"])
            try:
                resp = await llm.ainvoke(prompt)
                review = _validate_review(getattr(resp, "content", resp) if not isinstance(resp, str) else resp)
            except Exception:  # noqa: BLE001
                logger.debug("VIP 策略复盘 LLM 调用失败，走规则兜底", exc_info=True)
                review = None
    if not review or not review.get("critique"):
        review = _rule_based_review(ev)
        provider = provider or "rule"
        model = model or "fallback"

    # number_guard：叙事里未核到的金额/百分比就地标注（facts = 档案+净值+台账真实数）
    facts = (json.dumps(ev["dossier"], ensure_ascii=False, default=str)
             + "\n" + _fmt_value_series(ev["value_series"]) + "\n" + _fmt_ledger(ev["ledger"]))
    fv = number_guard.foreign_account_values(ev["dossier"])
    for k in ("critique", "market_reality", "correction"):
        if review.get(k):
            review[k] = number_guard.annotate_unverified(review[k], facts, foreign_values=fv)

    period = _period_label(ev)
    result = {
        "account_ref": account_ref, "horizon": "portfolio", "period": period,
        "critique": review["critique"], "market_reality": review["market_reality"],
        "correction": review["correction"], "cards": review["cards"],
        "hit_rate_pct": (ev["ledger"].get("kpi") or {}).get("hit_rate_pct"),
        "value_change_pct": _pct_change(ev["value_series"]),
        "provider": provider, "model": model,
        "disclaimer": compliance.DISCLAIMER_ZH,
    }
    review_id = wl_store.create_vip_strategy_review(
        account_ref, horizon="portfolio", period=period,
        critique=review["critique"], correction=review["correction"],
        result_json=result, provider=provider, model=model)
    result["review_id"] = review_id

    # 沉淀经验卡片（scope='vip_portfolio' → 决策中心 get_relevant_cards 取不到，物理隔离）
    card_ids = []
    for c in review["cards"]:
        cid = wl_store.create_experience_card(
            scope=VIP_CARD_SCOPE, scope_key=account_ref, category=c["category"],
            title=c["title"], content=c["content"], evidence=c.get("evidence"),
            confidence=c.get("confidence", 0.5), source_review_id=review_id)
        card_ids.append(cid)
    result["card_ids"] = card_ids
    return {"review_id": review_id, "result": result}


def score_prior_cards(wl_store, account_ref: str) -> dict:
    """卡片自进化：取本账户已结建议命中率，对被注入过（applied_count>0）的 vip_portfolio 卡片结胜负。

    命中率 ≥50% → win，否则 loss（复用 update_card_outcome 的贝叶斯后验）。
    # ponytail: 账户级命中率作组合卡胜负信号（区间聚合，命中率取 vip_advisor 桶 user+market 级），
    #   非逐卡因果归因；若日后要精确到「哪条卡改了哪次建议」，再在 vip_advisory.result_json 串 applied→prediction 链。
    """
    account_ref = (account_ref or "").strip()
    hr = (build_review_ledger(wl_store).get("kpi") or {}).get("hit_rate_pct")
    if hr is None:
        return {"scored": 0, "reason": "no_settled"}
    is_win = hr >= 50.0
    cards = wl_store.get_experience_cards(scope=VIP_CARD_SCOPE, scope_key=account_ref, limit=100)
    scored = 0
    for c in cards:
        if (c.get("applied_count") or 0) > 0:
            wl_store.update_card_outcome(c["id"], is_win)
            scored += 1
    return {"scored": scored, "hit_rate_pct": hr, "is_win": is_win}


if __name__ == "__main__":
    # 隔离守卫：VIP 卡片 scope 绝不撞 sim 的 global/ticker/sector（get_relevant_cards 三桶）
    assert VIP_CARD_SCOPE not in ("global", "ticker", "sector")

    # _pct_change：首末端点
    assert _pct_change({"series": [{"total_equity": 100}, {"total_equity": 110}]}) == 10.0
    assert _pct_change({"series": [{"total_equity": 100}]}) is None
    assert _pct_change({"series": [{"total_equity": 0}, {"total_equity": 5}]}) is None

    # _validate_review：非法 category→lesson、confidence 夹紧、缺 title/content 丢弃
    v = _validate_review('{"critique":"c","correction":"x","cards":['
                         '{"title":"T","content":"C","category":"weird","confidence":9},'
                         '{"title":"","content":"skip"}]}')
    assert v["critique"] == "c" and len(v["cards"]) == 1
    assert v["cards"][0]["category"] == "lesson" and v["cards"][0]["confidence"] == 1.0

    # _rule_based_review：有净值+命中率 → 产 ≥1 卡；命中率<50 出 rule 卡
    rb = _rule_based_review({"value_series": {"series": [{"total_equity": 100, "as_of_date": "2026-01-01"},
                                                          {"total_equity": 90, "as_of_date": "2026-02-01"}]},
                             "ledger": {"kpi": {"hit_rate_pct": 30.0}}})
    assert rb["cards"] and rb["cards"][0]["category"] == "rule" and "收敛" in rb["correction"]
    # 无数据 → 诚实降级 0 卡
    rb0 = _rule_based_review({"value_series": {"series": []}, "ledger": {"kpi": {}}})
    assert rb0["cards"] == [] and "数据不足" in rb0["critique"]

    # score_prior_cards 命中率映射：0.5 边界 → win
    class _FakeStore:
        def __init__(self, hr, cards):
            self._hr, self._cards, self.calls = hr, cards, []
        # build_review_ledger 直读这两个方法
        def list_settled_predictions(self, **_):
            return []
        def get_model_accuracy_stats(self, **_):
            from bottleneck_hunter.vip.advisory import VIP_ROLE_CONTEXT
            if self._hr is None:
                return []
            # 造 total/correct/pending 令 hit_rate = _hr：settled=100、correct=_hr、pending=0
            return [{"role_context": VIP_ROLE_CONTEXT, "total": 100, "correct": int(self._hr), "pending": 0}]
        _market = "us_stock"
        def get_experience_cards(self, **_):
            return self._cards
        def update_card_outcome(self, cid, is_win):
            self.calls.append((cid, is_win))
    fs = _FakeStore(50.0, [{"id": "c1", "applied_count": 2}, {"id": "c2", "applied_count": 0}])
    out = score_prior_cards(fs, "A")
    assert out["is_win"] is True and out["scored"] == 1 and fs.calls == [("c1", True)]
    fs2 = _FakeStore(30.0, [{"id": "c1", "applied_count": 1}])
    assert score_prior_cards(fs2, "A")["is_win"] is False and fs2.calls == [("c1", False)]
    assert score_prior_cards(_FakeStore(None, []), "A") == {"scored": 0, "reason": "no_settled"}

    print("strategy_review self-check OK")
