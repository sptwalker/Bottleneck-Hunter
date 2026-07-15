"""Phase 10B 决策引擎市场分化测试。

覆盖：
- _get_market_context_text 市场上下文生成
- _collect_market_context 市场列表
- 提示词模板 {market_context} 注入
- notice_pipeline A 股公告管道
- scheduler A 股公告管道集成
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

from bottleneck_hunter.watchlist.store import WatchlistStore


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def store(tmp_path):
    """创建临时 store 并添加双市场 ticker。"""
    s = WatchlistStore(tmp_path / "test.db")
    s.add({"ticker": "AAPL", "company_name": "Apple", "market": "us_stock", "tier": "focus"})
    s.add({"ticker": "SH600519", "company_name": "贵州茅台", "market": "a_stock", "tier": "focus"})
    return s


@pytest.fixture
def us_only_store(tmp_path):
    """只有美股的 store。"""
    s = WatchlistStore(tmp_path / "test_us.db")
    s.add({"ticker": "AAPL", "company_name": "Apple", "market": "us_stock", "tier": "focus"})
    return s


@pytest.fixture
def cn_only_store(tmp_path):
    """只有 A 股的 store。"""
    s = WatchlistStore(tmp_path / "test_cn.db")
    s.add({"ticker": "SH600519", "company_name": "贵州茅台", "market": "a_stock", "tier": "focus"})
    return s


# ---------------------------------------------------------------------------
# TestMarketContextText — 市场上下文文本生成
# ---------------------------------------------------------------------------

class TestMarketContextText:

    def test_us_stock_context(self):
        from bottleneck_hunter.watchlist.decision_engine import _get_market_context_text
        text = _get_market_context_text(["us_stock"])
        assert "美股" in text
        assert "T+0" in text
        assert "VIX" in text

    def test_a_stock_context(self):
        from bottleneck_hunter.watchlist.decision_engine import _get_market_context_text
        text = _get_market_context_text(["a_stock"])
        assert "A股" in text
        assert "涨跌停" in text
        assert "北向资金" in text

    def test_dual_market_context(self):
        from bottleneck_hunter.watchlist.decision_engine import _get_market_context_text
        text = _get_market_context_text(["us_stock", "a_stock"])
        assert "A股" in text
        assert "美股" in text

    def test_empty_defaults_to_us(self):
        from bottleneck_hunter.watchlist.decision_engine import _get_market_context_text
        text = _get_market_context_text([])
        assert "美股" in text

    def test_none_defaults_to_us(self):
        from bottleneck_hunter.watchlist.decision_engine import _get_market_context_text
        text = _get_market_context_text(None)
        assert "美股" in text


# ---------------------------------------------------------------------------
# TestCollectMarketContext — 市场数据收集含市场列表
# ---------------------------------------------------------------------------

class TestCollectMarketContext:

    async def test_returns_markets_field(self, store):
        from bottleneck_hunter.watchlist.decision_engine import _collect_market_context
        # 新契约：按 market 参数返回，默认 us_stock；a_stock 需显式传入
        result = await _collect_market_context(store)
        assert "markets" in result
        assert result["markets"] == ["us_stock"]
        result_cn = await _collect_market_context(store, "a_stock")
        assert result_cn["markets"] == ["a_stock"]

    async def test_us_only_store(self, us_only_store):
        from bottleneck_hunter.watchlist.decision_engine import _collect_market_context
        result = await _collect_market_context(us_only_store)
        assert result["markets"] == ["us_stock"]

    async def test_empty_store(self, tmp_path):
        from bottleneck_hunter.watchlist.decision_engine import _collect_market_context
        empty_store = WatchlistStore(tmp_path / "empty.db")
        # 新契约：默认返回 ["us_stock"]，不再自动检测空库
        result = await _collect_market_context(empty_store)
        assert result["markets"] == ["us_stock"]


# ---------------------------------------------------------------------------
# TestPromptMarketContext — 提示词模板包含 {market_context} 占位符
# ---------------------------------------------------------------------------

class TestPromptMarketContext:

    @pytest.mark.parametrize("prompt_name", [
        "decision_macro",
        "decision_macro_check",
        "decision_strategic",
        "decision_tactical",
        "decision_execution",
        "committee_risk",
        "committee_growth",
        "committee_value",
        "committee_contrarian",
    ])
    def test_prompt_has_market_context_var(self, prompt_name):
        from bottleneck_hunter.watchlist.decision_engine import _load_prompt
        content = _load_prompt(prompt_name)
        assert "{market_context}" in content, f"{prompt_name}.md 缺少 {{market_context}}"


# ---------------------------------------------------------------------------
# TestNoticePipeline — A 股公告管道
# ---------------------------------------------------------------------------

class TestNoticePipeline:

    def test_classify_notice(self):
        from bottleneck_hunter.watchlist.notice_pipeline import _classify_notice
        assert _classify_notice("关于业绩预告的公告") == "earnings_preview"
        assert _classify_notice("关于减持计划的公告") == "insider_sell"
        assert _classify_notice("关于回购股份的公告") == "buyback"
        assert _classify_notice("普通公告") == "other"

    def test_extract_code(self):
        from bottleneck_hunter.watchlist.notice_pipeline import _extract_code
        assert _extract_code("SH600519") == "600519"
        assert _extract_code("600519.SH") == "600519"
        assert _extract_code("AAPL") is None

    @patch("bottleneck_hunter.watchlist.notice_pipeline.ak")
    def test_fetch_notices_sync(self, mock_ak):
        from bottleneck_hunter.watchlist.notice_pipeline import _fetch_notices_sync

        # 真实 akshare stock_individual_notice_report 列：代码/名称/公告标题/公告类型/公告日期/网址
        df = pd.DataFrame({
            "代码": ["600519", "600519"],
            "名称": ["贵州茅台", "贵州茅台"],
            "公告标题": ["关于业绩预告的公告", "关于回购股份的公告"],
            "公告类型": ["财报", "股份回购"],
            "公告日期": ["2024-06-15", "2024-06-14"],
            "网址": ["https://example.com/1", "https://example.com/2"],
        })
        mock_ak.stock_individual_notice_report.return_value = df

        results = _fetch_notices_sync("SH600519")
        assert len(results) == 2
        assert results[0]["filing_type"] == "earnings_preview"
        assert results[1]["filing_type"] == "buyback"
        assert results[0]["ticker"] == "SH600519"
        assert results[0]["url"] == "https://example.com/1"

    @patch("bottleneck_hunter.watchlist.notice_pipeline.ak")
    def test_fetch_notices_invalid_ticker(self, mock_ak):
        from bottleneck_hunter.watchlist.notice_pipeline import _fetch_notices_sync
        results = _fetch_notices_sync("INVALID")
        assert results == []
        mock_ak.stock_individual_notice_report.assert_not_called()


# ---------------------------------------------------------------------------
# TestSchedulerNoticeIntegration — scheduler A 股公告管道
# ---------------------------------------------------------------------------

class TestSchedulerNoticeIntegration:

    async def test_daily_scan_astock_calls_notice(self, store):
        """A 股 daily scan 应调用 notice_pipeline。"""
        import bottleneck_hunter.watchlist.scheduler as sched_mod
        sched_mod._wl_store = store
        sched_mod._budget = None

        from bottleneck_hunter.watchlist.scheduler import job_daily_scan

        with patch(
            "bottleneck_hunter.watchlist.news_pipeline.fetch_news_batch",
            new_callable=AsyncMock,
            return_value={"SH600519": 3},
        ), patch(
            "bottleneck_hunter.watchlist.news_pipeline.refresh_market_news",
            new_callable=AsyncMock, return_value=0,
        ), patch(
            "bottleneck_hunter.watchlist.notice_pipeline.fetch_notice_batch",
            new_callable=AsyncMock,
            return_value={"SH600519": {"filings": 2, "trades": 1}},
        ) as mock_notice:
            results = await job_daily_scan(market="a_stock")

        mock_notice.assert_called_once()
        assert "notice" in results

    async def test_daily_scan_us_no_notice(self, store):
        """美股 daily scan 不调用 notice_pipeline。"""
        import bottleneck_hunter.watchlist.scheduler as sched_mod
        sched_mod._wl_store = store
        sched_mod._budget = None

        from bottleneck_hunter.watchlist.scheduler import job_daily_scan

        with patch(
            "bottleneck_hunter.watchlist.news_pipeline.fetch_news_batch",
            new_callable=AsyncMock,
            return_value={"AAPL": 5},
        ), patch(
            "bottleneck_hunter.watchlist.news_pipeline.refresh_market_news",
            new_callable=AsyncMock, return_value=0,
        ), patch(
            "bottleneck_hunter.watchlist.sec_pipeline.fetch_sec_batch",
            new_callable=AsyncMock,
            return_value={"AAPL": {"filings": 2}},
        ), patch(
            "bottleneck_hunter.watchlist.options_pipeline.fetch_options_batch",
            new_callable=AsyncMock,
            return_value={"AAPL": "ok"},
        ), patch(
            "bottleneck_hunter.watchlist.notice_pipeline.fetch_notice_batch",
            new_callable=AsyncMock,
        ) as mock_notice:
            results = await job_daily_scan(market="us_stock")

        mock_notice.assert_not_called()
        assert "notice" not in results


# ---------------------------------------------------------------------------
# decision_api._maybe_json —— JSON 字符串宽松解析
# ---------------------------------------------------------------------------

class TestMaybeJson:
    def test_parses_json_string(self):
        from bottleneck_hunter.web.decision_api import _maybe_json
        assert _maybe_json('{"a": 1}') == {"a": 1}

    def test_bad_json_returns_empty_dict(self):
        from bottleneck_hunter.web.decision_api import _maybe_json
        assert _maybe_json("not json") == {}

    def test_non_string_passthrough(self):
        from bottleneck_hunter.web.decision_api import _maybe_json
        # _maybe_json 契约：保证返回 dict；dict 原样，非 dict（list/None）一律 {}
        assert _maybe_json({"x": 2}) == {"x": 2}
        assert _maybe_json([1, 2]) == {}
        assert _maybe_json(None) == {}
