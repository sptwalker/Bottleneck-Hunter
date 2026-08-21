"""决策中心数据协商接线测试 —— _run_data_negotiation / _decision_allowed_tickers。

只测 decision_engine 新增的薄接线（协商环核心逻辑已由 test_ai_data_tools.py 用假 hub 覆盖）：
- 允许标的白名单 = 市场观察池 ∪ 层内输入，去重、按市场归一
- 协商成功：模型发 [[DATA_REQ]] → 取数回注 → 返回决策 JSON + fetch_log
- fail-open：协商环内部异常 → 降级为原始 _llm_json_object（决策不中断）
- user_id 经 get_current_user_id() 解析（不靠全局 Key）
"""
import asyncio

import pytest

import bottleneck_hunter.data_provider.ai_tools as ai_tools
import bottleneck_hunter.watchlist.decision_engine as de
from bottleneck_hunter.data_provider.hub import CAP_EARNINGS, CAP_QUOTE
from bottleneck_hunter.watchlist.store import WatchlistStore


class _FakeHub:
    def available_capabilities(self, market, user_id=""):
        return {CAP_QUOTE, CAP_EARNINGS}

    async def fetch(self, cap, ticker, market, user_id=""):
        if ticker == "NVDA" and cap == CAP_EARNINGS:
            return {"report_date": "2026-08-27", "eps_estimate": 1.2}
        return None


class _FakeLLM:
    """按调用序返回预置文本；invoke 同步（decision_engine 用 to_thread 包）。"""

    def __init__(self, replies):
        self._replies = list(replies)
        self.prompts = []

    def invoke(self, prompt, **kw):
        self.prompts.append(prompt)
        text = self._replies.pop(0) if self._replies else '{"done": true}'
        return type("R", (), {"content": text})()


@pytest.fixture(autouse=True)
def _fake_hub(monkeypatch):
    import bottleneck_hunter.data_provider.hub as hubmod
    monkeypatch.setattr(hubmod, "_hub", _FakeHub())
    monkeypatch.setattr(ai_tools, "ENABLED", True)


@pytest.fixture
def store(tmp_path):
    s = WatchlistStore(tmp_path / "dn.db").for_market("us_stock")
    for t in ("NVDA", "AMD"):
        s.add({"ticker": t, "company_name": t, "market": "us_stock", "tier": "focus"})
    return s


def _run(coro):
    return asyncio.run(coro)


def test_allowed_tickers_union_pool_and_extra(store):
    tk = de._decision_allowed_tickers(store, "us_stock", "TSLA", "nvda", "")
    assert set(tk) == {"NVDA", "AMD", "TSLA"}  # 观察池∪层内输入，去重、归一大写、空剔除


def test_negotiation_fetches_and_returns_json(store, monkeypatch):
    monkeypatch.setattr(de, "get_current_user_id", lambda: "u1")
    llm = _FakeLLM([
        '[[DATA_REQ]]{"requests":[{"capability":"earnings","ticker":"NVDA"}]}[[/DATA_REQ]]',
        '{"regime": "bull", "note": "据 2026-08-27 财报日"}',
    ])
    result, log = _run(de._run_data_negotiation(
        llm, "分析大盘。", market="us_stock", layer="1", allowed_tickers=["NVDA"]))
    assert result["regime"] == "bull"
    assert log and log[0]["ok"] and log[0]["ticker"] == "NVDA"
    assert any("2026-08-27" in p for p in llm.prompts)  # 补数据确实回注了后续 prompt


def test_negotiation_fail_open_on_error(store, monkeypatch):
    monkeypatch.setattr(de, "get_current_user_id", lambda: "u1")

    # 让 negotiate 抛异常 → 走 fail-open 分支（降级原始 _llm_json_object）
    async def _boom(*a, **k):
        raise RuntimeError("negotiate exploded")
    monkeypatch.setattr(de.ai_tools, "negotiate", _boom)

    llm = _FakeLLM(['{"regime": "sideways"}'])  # 降级路径只调一次
    result, log = _run(de._run_data_negotiation(
        llm, "分析。", market="us_stock", layer="2", allowed_tickers=["NVDA"]))
    assert result["regime"] == "sideways" and log == []  # 决策照旧、无取数记录


def test_no_request_single_pass(store, monkeypatch):
    monkeypatch.setattr(de, "get_current_user_id", lambda: "u1")
    llm = _FakeLLM(['{"regime": "bear"}'])  # 首轮直接给 JSON、无 DATA_REQ
    result, log = _run(de._run_data_negotiation(
        llm, "分析。", market="us_stock", layer="3", allowed_tickers=["NVDA"]))
    assert result["regime"] == "bear" and log == []


if __name__ == "__main__":  # GBK 控制台可直接 python 跑
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
