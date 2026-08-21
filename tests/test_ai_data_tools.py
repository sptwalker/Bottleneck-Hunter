"""AI 分析师数据调用协商环测试 —— manifest 口径 / 取数回注 / 校验拒绝 / 失败降级 / 一键关。

用假 DataHub 注入（沿 hub 单例注入法），不触真实 provider/网络。覆盖需求 1-4 的核心不变量：
清单即承诺（只列可用能力）、有请求即取数回注、越界/未收录能力拒绝、取数失败带错继续、事故一键关。
"""
import asyncio

import pytest

import bottleneck_hunter.data_provider.ai_tools as ai_tools
from bottleneck_hunter.data_provider.hub import CAP_EARNINGS, CAP_QUOTE


class _FakeHub:
    def available_capabilities(self, market, user_id=""):
        return {CAP_QUOTE, CAP_EARNINGS}

    async def fetch(self, cap, ticker, market, user_id=""):
        if ticker == "NVDA" and cap == CAP_EARNINGS:
            return {"report_date": "2026-08-27", "eps_estimate": 1.2}
        if ticker == "FAIL":
            raise RuntimeError("boom")
        return None


@pytest.fixture(autouse=True)
def _fake_hub(monkeypatch):
    import bottleneck_hunter.data_provider.hub as hubmod
    monkeypatch.setattr(hubmod, "_hub", _FakeHub())
    monkeypatch.setattr(ai_tools, "ENABLED", True)  # 默认开（不受环境变量影响）


def _run(coro):
    return asyncio.run(coro)


def test_manifest_lists_only_available_labeled_caps():
    caps = {m["capability"] for m in ai_tools.build_manifest("us_stock", "u1")}
    assert caps == {CAP_QUOTE, CAP_EARNINGS}  # 只列可用+已收录标签的能力


def test_negotiate_fetches_and_reinjects():
    async def ask(p):
        if "第1轮补充数据" in p:
            assert "2026-08-27" in p, "补数据未回注 prompt"
            return "据财报日期，建议持有。"
        return '[[DATA_REQ]]{"requests":[{"capability":"earnings","ticker":"NVDA"}]}[[/DATA_REQ]]'

    final, log, data_text = _run(ai_tools.negotiate(
        ask, "分析 NVDA。", market="us_stock", user_id="u1", allowed_tickers=["NVDA"]))
    assert "建议持有" in final and "DATA_REQ" not in final
    assert log and log[0]["ok"] and log[0]["ticker"] == "NVDA"
    assert "2026-08-27" in data_text  # 补数据文本回传供 guard_corpus


def test_out_of_scope_and_unknown_cap_rejected():
    async def ask(p):
        if "第1轮补充数据" in p:
            return "据现有信息维持中性。"
        return ('[[DATA_REQ]]{"requests":[{"capability":"insider","ticker":"NVDA"},'
                '{"capability":"earnings","ticker":"TSLA"}]}[[/DATA_REQ]]')

    final, log, _ = _run(ai_tools.negotiate(
        ask, "分析。", market="us_stock", user_id="u1", allowed_tickers=["NVDA"]))
    assert all(not x["ok"] for x in log)  # insider 未收录、TSLA 越界，全拒
    assert "维持中性" in final


def test_fetch_failure_continues_with_reason():
    async def ask(p):
        if "第1轮补充数据" in p:
            return "该数据缺失，据现有信息维持观望。"
        return '[[DATA_REQ]]{"requests":[{"capability":"earnings","ticker":"FAIL"}]}[[/DATA_REQ]]'

    final, log, _ = _run(ai_tools.negotiate(
        ask, "分析。", market="us_stock", user_id="u1", allowed_tickers=["FAIL"]))
    assert log and not log[0]["ok"] and "boom" in (log[0].get("error") or "")
    assert "观望" in final  # 失败不崩、缺数据继续


def test_no_block_single_pass():
    calls = {"n": 0}

    async def ask(p):
        calls["n"] += 1
        return "无需补数据，直接回答。"

    final, log, data_text = _run(ai_tools.negotiate(
        ask, "分析。", market="us_stock", user_id="u1", allowed_tickers=["NVDA"]))
    assert calls["n"] == 1 and log == [] and data_text == ""
    assert "直接回答" in final


def test_disabled_flag_short_circuits(monkeypatch):
    monkeypatch.setattr(ai_tools, "ENABLED", False)
    seen = {"p": ""}

    async def ask(p):
        seen["p"] = p
        return '[[DATA_REQ]]{"requests":[{"capability":"earnings","ticker":"NVDA"}]}[[/DATA_REQ]]留字'

    final, log, data_text = _run(ai_tools.negotiate(
        ask, "分析。", market="us_stock", user_id="u1", allowed_tickers=["NVDA"]))
    assert log == [] and data_text == ""                     # 关闭时零取数
    assert "可申请的实时数据能力" not in seen["p"]            # 未注入清单
    assert "留字" in final and "DATA_REQ" not in final       # 仍剥离残块
