"""Tests for hot sector detector."""

import pytest
import pandas as pd
from unittest.mock import patch, MagicMock

from bottleneck_hunter.chain.hot_sector import (
    HotSector,
    HotSectorDetector,
    HotSectorResult,
)


def _make_concept_df() -> pd.DataFrame:
    """Mock concept board price ranking DataFrame."""
    return pd.DataFrame({
        "板块名称": ["光模块", "CPO", "人形机器人", "算力租赁", "磷化铟"],
        "涨跌幅": [5.2, 4.8, 3.9, 3.5, 3.1],
        "换手率": [8.5, 7.2, 6.1, 5.5, 4.3],
        "成交额": [120.5, 98.3, 85.7, 72.1, 45.6],
        "上涨家数": [18, 15, 12, 10, 8],
        "下跌家数": [2, 3, 5, 4, 2],
        "领涨股票": ["中际旭创", "天孚通信", "绿的谐波", "首都在线", "云南锗业"],
    })


def _make_flow_df() -> pd.DataFrame:
    """Mock capital flow DataFrame."""
    return pd.DataFrame({
        "板块名称": ["光模块", "CPO", "算力租赁", "人形机器人", "AI芯片"],
        "主力净流入": [15.3, 12.1, 8.5, 6.2, 5.1],
    })


class TestHotSectorDetector:
    @patch("bottleneck_hunter.chain.hot_sector.ak.stock_sector_fund_flow_rank")
    @patch("bottleneck_hunter.chain.hot_sector.ak.stock_board_concept_name_em")
    @patch("bottleneck_hunter.chain.hot_sector.ak.stock_board_industry_name_em")
    def test_detect_basic(
        self, mock_industry, mock_concept, mock_flow,
    ):
        mock_concept.return_value = _make_concept_df()
        mock_industry.return_value = pd.DataFrame()
        mock_flow.return_value = _make_flow_df()

        detector = HotSectorDetector(top_n=10)
        result = detector.detect()

        assert isinstance(result, HotSectorResult)
        assert len(result.concept_sectors) > 0
        assert len(result.all_ranked) > 0
        # 光模块 should be top ranked (high price change + high capital flow)
        assert result.all_ranked[0].name == "光模块"
        assert result.all_ranked[0].composite_score > 0

    @patch("bottleneck_hunter.chain.hot_sector.ak.stock_sector_fund_flow_rank")
    @patch("bottleneck_hunter.chain.hot_sector.ak.stock_board_concept_name_em")
    @patch("bottleneck_hunter.chain.hot_sector.ak.stock_board_industry_name_em")
    def test_detect_with_rotation(
        self, mock_industry, mock_concept, mock_flow,
    ):
        mock_concept.return_value = _make_concept_df()
        mock_industry.return_value = pd.DataFrame()
        mock_flow.return_value = _make_flow_df()

        detector = HotSectorDetector(top_n=10)
        result = detector.detect()

        # All our mock data is concept type, so emerging_themes should be populated
        assert len(result.emerging_themes) > 0
        for theme in result.emerging_themes:
            assert theme.sector_type == "concept"
            assert theme.signal_count >= 2

    def test_scoring_logic(self):
        detector = HotSectorDetector()
        sector = HotSector(
            name="测试",
            sector_type="concept",
            price_change_pct=5.0,
            turnover_rate=8.0,
            main_net_inflow=10.0,
            up_count=15,
            down_count=3,
        )
        detector._compute_score(sector)
        assert sector.composite_score > 0
        assert sector.signal_count >= 3

    def test_format_result(self):
        result = HotSectorResult(
            all_ranked=[
                HotSector(name="光模块", sector_type="concept", composite_score=8.5, signal_count=4,
                          price_change_pct=5.2, main_net_inflow=15.3),
            ],
            emerging_themes=[
                HotSector(name="光模块", sector_type="concept", composite_score=8.5,
                          price_change_pct=5.2, main_net_inflow=15.3),
            ],
        )
        detector = HotSectorDetector()
        text = detector.format_result(result)
        assert "热点板块检测" in text
        assert "光模块" in text
        assert "8.5" in text
