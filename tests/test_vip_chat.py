"""P6 实时咨询：流式聊天后端（单模型、facts 注入、会话落库）。"""
import asyncio
import tempfile
from pathlib import Path

from bottleneck_hunter.watchlist.store import WatchlistStore
from bottleneck_hunter.vip import portfolio, chat, derivatives
from bottleneck_hunter.vip.ingest import BrokerStatement, EquityHolding, ReconResult


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
