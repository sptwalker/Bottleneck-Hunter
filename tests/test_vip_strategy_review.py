"""Phase 5c · VIP 顾问·反思式策略复盘（组合中周期）回归：
- 端到端：fake LLM + 造证据 → 复盘落库 vip_strategy_reviews + 经验卡片落库 scope='vip_portfolio'
- VIP 卡片物理隔离：sim 的 get_relevant_cards 取不到 vip_portfolio 卡片
- score_prior_cards：真实 store 命中率 50% → win，applied_count=0 的卡不结
- 规则兜底：无 LLM 不报错，仍落库（provider='rule'）
- 空持仓 → 诚实报错不臆造

自省引擎的纯函数（_pct_change/_validate_review/_rule_based_review/score_prior_cards 边界）已在
strategy_review.__main__ 自检覆盖；此处只测「跨真实 store 的落库/隔离/闭环」这层。
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from bottleneck_hunter.vip import strategy_review
from bottleneck_hunter.vip.advisory import VIP_PT_ADVICE, VIP_ROLE_CONTEXT
from bottleneck_hunter.watchlist.store import WatchlistStore

_FAKE_JSON = (
    '{"critique":"过度集中于 NVDA，区间净值回撤 10%。","market_reality":"命中率 50%，判断部分被证伪。",'
    '"correction":"降低单一标的集中度、提高现金缓冲。",'
    '"cards":[{"title":"控制单一标的集中度","content":"单一标的权重超 30% 时下一轮应减仓。",'
    '"category":"rule","confidence":0.6,"evidence":["NVDA 权重 40%"]}]}'
)

_EV = {
    "dossier": {"holdings": [{"ticker": "NVDA", "shares": 100, "market_value": 10000.0, "weight_pct": 40.0}]},
    "value_series": {"series": [{"total_equity": 100.0, "as_of_date": "2026-01-01"},
                                {"total_equity": 90.0, "as_of_date": "2026-02-01"}],
                     "returns": [{"period": "2026-02", "pct": -10.0}], "basis": "settlement"},
    "advisories": [{"created_at": "2026-01-15",
                    "result": {"portfolio_diagnosis": "过度集中", "holdings": [{"ticker": "NVDA", "action": "减仓"}]}}],
    "ledger": {"kpi": {"settled": 2, "correct": 1, "pending": 0, "hit_rate_pct": 50.0},
               "ledger": [{"date": "2026-01-01", "ticker": "NVDA", "action": "减仓",
                           "correct": True, "chg_pct": -5.0}]},
    "mandate_text": "## 投资纲领\n稳健",
    "macro_text": "## 当前宏观研判\n中性",
}


@pytest.fixture
def wl(tmp_path):
    return WatchlistStore(tmp_path / "wl.db").for_user("u1").for_market("us_stock")


def _fake_llm():
    llm = MagicMock()
    llm.ainvoke = AsyncMock(return_value=MagicMock(content=_FAKE_JSON))
    return llm


async def test_end_to_end_persists_review_and_cards(wl, monkeypatch):
    monkeypatch.setattr(strategy_review, "_assemble_evidence", lambda s, ref: _EV)
    monkeypatch.setattr("bottleneck_hunter.llm_clients.factory.get_models_for_role",
                        lambda *a, **k: [(_fake_llm(), "openai", "gpt-x")])

    out = await strategy_review.run_portfolio_strategy_review(wl, "A", user_id="u1")
    assert out.get("review_id") and not out.get("error")
    assert out["result"]["critique"] and out["result"]["correction"]

    rows = wl.list_vip_strategy_reviews("A", horizon="portfolio")
    assert len(rows) == 1
    r = rows[0]
    assert r["critique"] and r["correction"]
    assert r["result_json"]["market_reality"] and r["result_json"]["hit_rate_pct"] == 50.0

    cards = wl.get_experience_cards(scope="vip_portfolio", scope_key="A")
    assert len(cards) == 1 and cards[0]["category"] == "rule"
    assert cards[0]["source_review_id"] == out["review_id"]

    # 隔离：sim 的 L4 只调 get_relevant_cards（global/ticker/sector 三桶）→ 取不到 vip_portfolio 卡片
    assert wl.get_relevant_cards("NVDA") == []


async def test_rule_fallback_no_llm(wl, monkeypatch):
    monkeypatch.setattr(strategy_review, "_assemble_evidence", lambda s, ref: _EV)
    monkeypatch.setattr("bottleneck_hunter.llm_clients.factory.get_models_for_role",
                        lambda *a, **k: [])  # 无可用模型 → 规则兜底

    out = await strategy_review.run_portfolio_strategy_review(wl, "A", user_id="u1")
    assert out["result"]["provider"] == "rule"
    assert out["result"]["critique"]  # 规则兜底仍有检讨
    assert len(wl.list_vip_strategy_reviews("A")) == 1
    # 命中率 50%≥50 → 走"框架有效"分支，产 1 卡（非"收敛"）
    assert len(wl.get_experience_cards(scope="vip_portfolio", scope_key="A")) == 1


async def test_empty_holdings_errors(wl, monkeypatch):
    monkeypatch.setattr(strategy_review, "_assemble_evidence",
                        lambda s, ref: {**_EV, "dossier": {"holdings": []}})
    out = await strategy_review.run_portfolio_strategy_review(wl, "A", user_id="u1")
    assert "error" in out and "持仓" in out["error"]
    assert wl.list_vip_strategy_reviews("A") == []


def test_score_prior_cards_win(wl):
    # 造已结建议：2 条同桶，1 对(score_delta<2)1 错 → 命中率 50% ≥50 → win
    wl.record_prediction(provider="p", model="m", role_context=VIP_ROLE_CONTEXT,
                         ticker="NVDA", prediction_type=VIP_PT_ADVICE, prediction_value="加仓")
    wl.record_prediction(provider="p", model="m", role_context=VIP_ROLE_CONTEXT,
                         ticker="AMD", prediction_type=VIP_PT_ADVICE, prediction_value="加仓")
    wl.record_outcome("NVDA", VIP_PT_ADVICE, outcome_value="+1%", score_delta=0.0)
    wl.record_outcome("AMD", VIP_PT_ADVICE, outcome_value="-5%", score_delta=5.0)

    cid = wl.create_experience_card("vip_portfolio", "A", "rule", "T", "C")
    wl.increment_card_applied(cid)  # applied_count=1 → 被计分
    wl.create_experience_card("vip_portfolio", "A", "rule", "T2", "C2")  # applied_count=0 → 不计分

    out = strategy_review.score_prior_cards(wl, "A")
    assert out["hit_rate_pct"] == 50.0 and out["is_win"] is True and out["scored"] == 1
    scored = next(c for c in wl.get_experience_cards(scope="vip_portfolio", scope_key="A") if c["id"] == cid)
    assert scored["win_count"] == 1 and scored["loss_count"] == 0


def test_score_prior_cards_no_settled(wl):
    wl.create_experience_card("vip_portfolio", "A", "rule", "T", "C")
    assert strategy_review.score_prior_cards(wl, "A") == {"scored": 0, "reason": "no_settled"}
