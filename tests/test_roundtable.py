"""Tests for RoundtableMeeting — AI 投研圆桌会议。"""

import json
import pytest
from collections import defaultdict
from unittest.mock import AsyncMock, MagicMock, patch

from bottleneck_hunter.chain.roundtable import RoundtableMeeting, ROLES
from bottleneck_hunter.chain.models import (
    CrossValidationReport,
    MeetingMessage,
    MeetingRanking,
    ModelValidation,
    SupplierInfo,
    SupplierScorecard,
)


def _make_scorecard(name="TestCo", ticker="TEST", score=7.0, node="瓶颈A"):
    return SupplierScorecard(
        supplier=SupplierInfo(name=name, ticker=ticker, market="a_stock", sector="半导体", description="desc"),
        bottleneck_node=node,
        layer=1,
        market_position=7,
        customer_validation=6,
        capacity_status=8,
        financial_health=7,
        valuation=6,
        overall_score=score,
        strengths=["技术领先"],
        weaknesses=["估值偏高"],
    )


def _make_cv_report(name="TestCo", ticker="TEST", score=7.5):
    return CrossValidationReport(
        supplier_name=name,
        ticker=ticker,
        validations=[ModelValidation(model_name="m1", score=score, reasoning="ok", concerns=[])],
        consensus_score=score,
        consensus_reasoning="通过",
        avg_score=score,
    )


def _mock_llm_response(data: dict):
    llm = AsyncMock()
    msg = MagicMock()
    msg.content = json.dumps(data, ensure_ascii=False)
    llm.ainvoke = AsyncMock(return_value=msg)
    return llm


class TestAssignRoles:
    def test_round_robin_with_single_llm(self):
        meeting = RoundtableMeeting(
            validation_models=[{"provider": "openai", "model": "gpt-4o"}],
        )
        mock_llm = MagicMock()
        llms = [("openai/gpt-4o", mock_llm)]
        assignments = meeting._assign_roles(llms)
        assert len(assignments) == 4
        for role in ROLES:
            name, llm = assignments[role["id"]]
            assert name == "openai/gpt-4o"
            assert llm is mock_llm

    def test_round_robin_with_multiple_llms(self):
        meeting = RoundtableMeeting(
            validation_models=[],
        )
        llms = [("a/m1", MagicMock()), ("b/m2", MagicMock()), ("c/m3", MagicMock())]
        assignments = meeting._assign_roles(llms)
        assert assignments["growth"][0] == "a/m1"
        assert assignments["value"][0] == "b/m2"
        assert assignments["risk"][0] == "c/m3"
        assert assignments["chain"][0] == "a/m1"

    @patch("bottleneck_hunter.chain.roundtable.create_llm")
    def test_explicit_role_assignments(self, mock_create):
        mock_llm = MagicMock()
        mock_create.return_value = mock_llm
        meeting = RoundtableMeeting(
            validation_models=[{"provider": "openai", "model": "gpt-4o"}],
            role_assignments={
                "growth": {"provider": "anthropic", "model": "claude"},
                "value": {"provider": "deepseek", "model": "chat"},
            },
        )
        fallback_llm = MagicMock()
        llms = [("openai/gpt-4o", fallback_llm)]
        assignments = meeting._assign_roles(llms)
        assert assignments["growth"][0] == "anthropic/claude"
        assert assignments["value"][0] == "deepseek/chat"
        assert assignments["risk"][0] == "openai/gpt-4o"

    @patch("bottleneck_hunter.chain.roundtable.create_llm", side_effect=Exception("fail"))
    def test_fallback_on_create_failure(self, mock_create):
        meeting = RoundtableMeeting(
            validation_models=[],
            role_assignments={"growth": {"provider": "bad", "model": "bad"}},
        )
        fallback_llm = MagicMock()
        llms = [("fallback/m", fallback_llm)]
        assignments = meeting._assign_roles(llms)
        assert assignments["growth"][0] == "fallback/m"


class TestBuildAgenda:
    def test_with_config(self):
        meeting = RoundtableMeeting(validation_models=[])
        agenda = meeting._build_agenda({"sector": "GPU", "end_product": "显卡"}, 5)
        assert "GPU" in agenda
        assert "显卡" in agenda
        assert "5 家" in agenda

    def test_without_config(self):
        meeting = RoundtableMeeting(validation_models=[])
        agenda = meeting._build_agenda(None, 3)
        assert "3 家" in agenda

    def test_empty_sector(self):
        meeting = RoundtableMeeting(validation_models=[])
        agenda = meeting._build_agenda({"sector": "", "end_product": ""}, 2)
        assert "2 家" in agenda


class TestBuildChainOverview:
    def test_with_chain_data(self):
        meeting = RoundtableMeeting(validation_models=[])
        chain_data = {
            "layers": [
                {"layer_name": "L1", "nodes": [{"name": "光模块"}, {"name": "芯片"}]},
            ]
        }
        bottlenecks = [
            {"node_name": "光模块", "overall_score": 8.5, "key_insights": ["供需紧张"]},
        ]
        overview = meeting._build_chain_overview(chain_data, bottlenecks)
        assert "光模块" in overview
        assert "8.5" in overview
        assert "供需紧张" in overview

    def test_no_chain_data(self):
        meeting = RoundtableMeeting(validation_models=[])
        assert meeting._build_chain_overview(None, None) == ""

    def test_empty_layers(self):
        meeting = RoundtableMeeting(validation_models=[])
        overview = meeting._build_chain_overview({"layers": []}, [])
        assert "产业链" in overview


class TestBuildCompanyBriefing:
    def test_basic_briefing(self):
        meeting = RoundtableMeeting(validation_models=[])
        scorecards = [_make_scorecard()]
        cv_reports = [_make_cv_report()]
        briefing = meeting._build_company_briefing(scorecards, cv_reports)
        assert "TestCo" in briefing
        assert "TEST" in briefing
        assert "瓶颈A" in briefing

    def test_briefing_without_cv(self):
        meeting = RoundtableMeeting(validation_models=[])
        scorecards = [_make_scorecard()]
        briefing = meeting._build_company_briefing(scorecards, [])
        assert "TestCo" in briefing
        assert "N/A" in briefing


class TestComputeBorda:
    def test_basic_ranking(self):
        meeting = RoundtableMeeting(validation_models=[])
        msgs = [
            MeetingMessage(
                round_num=2, role="growth", participant_name="成长",
                content="ok",
                structured_data={
                    "revised_ranking": [
                        {"rank": 1, "ticker": "AAA", "name": "A公司"},
                        {"rank": 2, "ticker": "BBB", "name": "B公司"},
                        {"rank": 3, "ticker": "CCC", "name": "C公司"},
                    ]
                },
            ),
            MeetingMessage(
                round_num=2, role="value", participant_name="价值",
                content="ok",
                structured_data={
                    "revised_ranking": [
                        {"rank": 1, "ticker": "AAA", "name": "A公司"},
                        {"rank": 2, "ticker": "CCC", "name": "C公司"},
                        {"rank": 3, "ticker": "BBB", "name": "B公司"},
                    ]
                },
            ),
        ]
        rankings = meeting.compute_borda(msgs)
        assert rankings[0].ticker == "AAA"
        assert rankings[0].borda_points == 6  # 3+3
        assert rankings[0].supporter_count == 2
        assert len(rankings) == 3

    def test_empty_messages(self):
        meeting = RoundtableMeeting(validation_models=[])
        rankings = meeting.compute_borda([])
        assert rankings == []

    def test_no_structured_data(self):
        meeting = RoundtableMeeting(validation_models=[])
        msgs = [
            MeetingMessage(round_num=2, role="growth", participant_name="成长",
                           content="ok", structured_data=None),
        ]
        rankings = meeting.compute_borda(msgs)
        assert rankings == []

    def test_partial_ranking(self):
        meeting = RoundtableMeeting(validation_models=[])
        msgs = [
            MeetingMessage(
                round_num=2, role="growth", participant_name="成长",
                content="ok",
                structured_data={
                    "revised_ranking": [
                        {"rank": 1, "ticker": "AAA", "name": "A"},
                    ]
                },
            ),
        ]
        rankings = meeting.compute_borda(msgs)
        assert len(rankings) == 1
        assert rankings[0].borda_points == 3

    def test_supporters_and_opposers(self):
        meeting = RoundtableMeeting(validation_models=[])
        msgs = [
            MeetingMessage(
                round_num=2, role="growth", participant_name="成长",
                content="ok",
                structured_data={
                    "revised_ranking": [
                        {"rank": 1, "ticker": "AAA", "name": "A"},
                    ]
                },
            ),
            MeetingMessage(
                round_num=2, role="risk", participant_name="风险",
                content="ok",
                structured_data={
                    "revised_ranking": [
                        {"rank": 1, "ticker": "BBB", "name": "B"},
                    ]
                },
            ),
        ]
        rankings = meeting.compute_borda(msgs)
        aaa = next(r for r in rankings if r.ticker == "AAA")
        assert "growth" in aaa.supporters
        assert "risk" in aaa.opposers


class TestBuildParticipantsInfo:
    def test_basic(self):
        meeting = RoundtableMeeting(validation_models=[])
        assignments = {
            role["id"]: (f"model/{role['id']}", MagicMock())
            for role in ROLES
        }
        info = meeting._build_participants_info(assignments)
        assert len(info) == 4
        assert info[0]["role"] == "growth"
        assert info[0]["avatar_letter"] == "成"


class TestRunRound1:
    @pytest.mark.asyncio
    async def test_parallel_execution(self):
        meeting = RoundtableMeeting(validation_models=[])
        mock_llm = AsyncMock()
        msg = MagicMock()
        msg.content = json.dumps({"speech": "我认为A公司最好", "ranking": [{"rank": 1, "ticker": "A"}]})
        mock_llm.ainvoke = AsyncMock(return_value=msg)

        assignments = {role["id"]: ("test/model", mock_llm) for role in ROLES}
        messages = await meeting.run_round1(assignments, "briefing text")
        assert len(messages) == 4
        assert all(m.round_num == 1 for m in messages)
        assert {m.role for m in messages} == {"growth", "value", "risk", "chain"}

    @pytest.mark.asyncio
    async def test_callback_called(self):
        meeting = RoundtableMeeting(validation_models=[])
        mock_llm = AsyncMock()
        msg = MagicMock()
        msg.content = json.dumps({"speech": "test"})
        mock_llm.ainvoke = AsyncMock(return_value=msg)

        assignments = {role["id"]: ("test/model", mock_llm) for role in ROLES}
        callback = AsyncMock()
        await meeting.run_round1(assignments, "briefing", callback)
        assert callback.call_count == 4

    @pytest.mark.asyncio
    async def test_llm_failure_handled(self):
        meeting = RoundtableMeeting(validation_models=[])
        mock_llm = AsyncMock()
        mock_llm.ainvoke = AsyncMock(side_effect=Exception("LLM down"))

        assignments = {role["id"]: ("test/model", mock_llm) for role in ROLES}
        messages = await meeting.run_round1(assignments, "briefing")
        assert len(messages) == 4
        assert all("失败" in m.content for m in messages)


class TestRunRound2:
    @pytest.mark.asyncio
    async def test_sequential_with_context(self):
        meeting = RoundtableMeeting(validation_models=[])
        call_count = 0

        async def mock_invoke(msgs, **kwargs):
            nonlocal call_count
            call_count += 1
            m = MagicMock()
            m.content = json.dumps({"speech": f"辩论{call_count}", "revised_ranking": []})
            return m

        mock_llm = AsyncMock()
        mock_llm.ainvoke = mock_invoke

        assignments = {role["id"]: ("test/model", mock_llm) for role in ROLES}
        round1 = [
            MeetingMessage(round_num=1, role=r["id"], participant_name=r["name"], content="初始发言")
            for r in ROLES
        ]
        messages = await meeting.run_round2(assignments, "briefing", round1)
        assert len(messages) == 4
        assert call_count == 4
        assert all(m.round_num == 2 for m in messages)
