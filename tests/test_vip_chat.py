"""P6 实时咨询：流式聊天后端（单模型、facts 注入、会话落库）。"""
import asyncio
import tempfile
from pathlib import Path

from bottleneck_hunter.vip import chat, derivatives, portfolio
from bottleneck_hunter.vip.ingest import BrokerStatement, EquityHolding, ReconResult
from bottleneck_hunter.watchlist.store import WatchlistStore


def _stmt():
    holds = [
        EquityHolding(ticker="GOOGL", company="Alphabet Inc", quantity=100, market_value_usd=200000.0),
        EquityHolding(ticker="US4642875235", company="iShares Semiconductor ETF", quantity=1500, market_value_usd=961140.0),
    ]
    total = sum(h.market_value_usd for h in holds)
    return BrokerStatement(content_hash="h1", period_end="2026-06-30", holdings=holds,
                           cash_balances=[], total_cash_usd=50000.0,
                           recon=ReconResult(holdings_count=2, holdings_total_usd=total,
                                             statement_equities_total_usd=total, delta_usd=0.0, status="ok"))


class _FakeLLM:
    async def astream(self, prompt):
        for x in ["组合总权益 $1,211,140.00。", "前五大集中度较高。"]:
            yield type("C", (), {"content": x})()


def _collect(agen):
    async def run():
        out=[]
        async for e in agen:
            out.append(e)
        return out
    return asyncio.run(run())


def test_stream_vip_chat_and_persist(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        wl = WatchlistStore(Path(d)/"wl.db").for_user("u1").for_market("us_stock")
        stmt = _stmt()
        portfolio.normalize_statement(wl, stmt, account_ref="A1")
        portfolio.materialize_portfolio(wl, account_ref="A1", cash_total_usd=stmt.total_cash_usd)
        derivatives.save_derivative_term(
            wl,
            derivatives.DerivativeTerm("equity_accumulator", "MU", "USD", 365,
                                       {"afp": 625.5, "knock_out_price": 910.7, "daily_shares": 3, "step_up_daily_shares": 6}),
            source_file_name="x.pdf", source_file_hash="h", broker="nomura")
        monkeypatch.setattr("bottleneck_hunter.llm_clients.factory.get_models_for_role",
                            lambda *a, **k: [(_FakeLLM(), "deepseek", "deepseek-chat")])
        events = _collect(chat.stream_vip_chat(wl, user_id="u1", question="我的组合风险在哪？"))
        kinds = [e["event"] for e in events]
        assert kinds[0] == "session" and "disclaimer" in kinds and "done" in kinds
        msgs = chat.get_chat_messages(wl, events[0]["data"] and __import__('json').loads(events[0]["data"])["session_id"])
        assert len(msgs) == 2 and msgs[0]["role"] == "user" and msgs[1]["role"] == "assistant"
        assert "重要声明" in msgs[1]["content"]


def test_candidate_pool_text_excludes_held(monkeypatch):
    """荐新候选池：观察池「未持有」标的入 facts、瓶颈环节带出；已持有标的不入池；空观察池返回空串。"""
    with tempfile.TemporaryDirectory() as d:
        wl = WatchlistStore(Path(d)/"wl.db").for_user("u1").for_market("us_stock")
        stmt = _stmt()
        portfolio.normalize_statement(wl, stmt, account_ref="A1")
        portfolio.materialize_portfolio(wl, account_ref="A1", cash_total_usd=stmt.total_cash_usd)
        dossier = portfolio.build_account_dossier(wl, account_ref="A1")
        assert chat._candidate_pool_text(wl, dossier, account_ref="A1") == ""  # 观察池空→空串

        wl.add({"ticker": "MU", "company_name": "Micron", "company_name_cn": "美光",
                "tier": "focus", "market": "us_stock", "sector": "半导体存储", "bottleneck_node": "HBM"})
        wl.add({"ticker": "GOOGL", "company_name": "Alphabet", "company_name_cn": "谷歌",
                "tier": "normal", "market": "us_stock", "sector": "互联网"})  # 已持有→应剔除
        text = chat._candidate_pool_text(wl, dossier, account_ref="A1")
        assert "MU" in text and "HBM" in text        # 未持有候选 + 瓶颈环节入池
        assert "GOOGL" not in text                    # 已持有标的不进候选池
        assert "composite_score" not in text          # 只喂定性优先级，不喂可引用分值


def test_latest_recommend_text(monkeypatch):
    """荐新成品接入：读上一份投委会荐新，抽 action/理由/风险/契合/软仓位+主席综述；无则空串；委员语料不喂。"""
    import json as _json

    with tempfile.TemporaryDirectory() as d:
        wl = WatchlistStore(Path(d)/"wl.db").for_user("u1").for_market("us_stock")
        assert chat._latest_recommend_text(wl, account_ref="A1") == ""  # 从未生成过→空串

        result = {"generated_at": "2026-08-01T00:00:00+00:00", "chair_summary": "综述：可小幅建仓存储。",
                  "portfolio_note": "科技集中度偏高，新增须控单一权重。",
                  "candidates": [{"ticker": "MU", "action": "建仓", "reason": "补 HBM 敞口",
                                  "risk": "存储周期", "fit": "契合科技聚焦", "suggested_weight": "3%-5%"}],
                  "committee": {"reviews": [{"member": "risk", "text": "内部语料不应外泄到 chat"}]}}
        with wl._write_conn() as conn:
            conn.execute(
                f"""INSERT INTO vip_recommendations (id, account_ref, result_json, provider, model, created_at{wl._user_insert_cols()}{wl._market_insert_cols()})
                   VALUES (?,?,?,?,?,?{wl._user_insert_vals()}{wl._market_insert_vals()})""",
                ("r1", "A1", _json.dumps(result, ensure_ascii=False), "deepseek", "deepseek-chat", "2026-08-01T00:00:00+00:00")
                + wl._user_insert_params() + wl._market_insert_params())
        text = chat._latest_recommend_text(wl, account_ref="A1")
        assert "MU" in text and "建仓" in text and "3%-5%" in text  # 动作+软仓位带出
        assert "2026-08-01" in text                                  # 生成时间供时效提示
        assert "内部语料不应外泄" not in text                        # 委员逐条语料不喂 chat


def test_stream_vip_chat_rejects_unknown_session(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        wl = WatchlistStore(Path(d)/"wl.db").for_user("u1").for_market("us_stock")
        stmt = _stmt()
        portfolio.normalize_statement(wl, stmt, account_ref="A1")
        portfolio.materialize_portfolio(wl, account_ref="A1", cash_total_usd=stmt.total_cash_usd)
        monkeypatch.setattr("bottleneck_hunter.llm_clients.factory.get_models_for_role",
                            lambda *a, **k: [(_FakeLLM(), "deepseek", "deepseek-chat")])
        events = _collect(chat.stream_vip_chat(wl, user_id="u1", question="hi", session_id="missing"))
        assert events and events[0]["event"] == "error"
        assert chat.list_chat_sessions(wl) == []
