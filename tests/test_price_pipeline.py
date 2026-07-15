"""price_pipeline 纯函数单元测试。

覆盖技术指标计算（RSI / MACD / SMA）和 A 股代码提取。
所有被测函数均为无副作用纯函数，无需 mock。
"""

import pytest

from bottleneck_hunter.watchlist.price_pipeline import (
    _compute_macd,
    _compute_rsi,
    _compute_sma,
    _extract_astock_code,
    _merge_fill,
)


# ---------------------------------------------------------------------------
# 技术指标
# ---------------------------------------------------------------------------

class TestTechnicalIndicators:
    """RSI / MACD / SMA 计算逻辑。"""

    # ---- RSI ----

    def test_rsi_all_up(self):
        """全涨序列 → RSI 应为 100.0（无亏损）。"""
        closes = list(range(1, 20))  # 1,2,...,19 — 连续上涨
        assert _compute_rsi(closes) == 100.0

    def test_rsi_all_down(self):
        """全跌序列 → RSI 应接近 0。"""
        closes = list(range(20, 0, -1))  # 20,19,...,1 — 连续下跌
        rsi = _compute_rsi(closes)
        assert rsi is not None
        assert rsi < 1.0  # 接近 0，允许浮点误差

    def test_rsi_insufficient_data(self):
        """数据点不足 period+1 时应返回 None。"""
        # 默认 period=14，需要至少 15 个数据点
        assert _compute_rsi([1.0] * 14) is None
        assert _compute_rsi([]) is None

    def test_rsi_normal_range(self):
        """正常波动数据 → RSI 在 0-100 之间。"""
        closes = [44.0, 44.34, 44.09, 43.61, 44.33,
                  44.83, 45.10, 45.42, 45.84, 46.08,
                  45.89, 46.03, 45.61, 46.28, 46.28,
                  46.00, 46.03, 46.41, 46.22, 45.64]
        rsi = _compute_rsi(closes)
        assert rsi is not None
        assert 0 <= rsi <= 100

    # ---- MACD ----

    def test_macd_insufficient_data(self):
        """数据不足 slow+signal (26+9=35) → None。"""
        assert _compute_macd(list(range(34))) is None

    def test_macd_normal(self):
        """足够数据 → 返回 (macd, signal, hist) 三元组。"""
        # 生成 50 个递增收盘价
        closes = [100.0 + i * 0.5 for i in range(50)]
        result = _compute_macd(closes)
        assert result is not None
        macd_val, signal_val, hist_val = result
        assert isinstance(macd_val, float)
        assert isinstance(signal_val, float)
        assert isinstance(hist_val, float)

    # ---- SMA ----

    def test_sma_insufficient_data(self):
        """数据不足 period → None。"""
        assert _compute_sma([1.0, 2.0], 5) is None

    def test_sma_correct_value(self):
        """验证 SMA 计算结果正确：取最后 period 个值的均值。"""
        closes = [10.0, 20.0, 30.0, 40.0, 50.0]
        # period=3 → 均值 = (30+40+50)/3 = 40.0
        assert _compute_sma(closes, 3) == 40.0
        # period=5 → 均值 = (10+20+30+40+50)/5 = 30.0
        assert _compute_sma(closes, 5) == 30.0


# ---------------------------------------------------------------------------
# A 股代码提取
# ---------------------------------------------------------------------------

class TestAStockCodeExtraction:
    """_extract_astock_code 应正确处理各种 A 股 ticker 格式。"""

    @pytest.mark.parametrize("ticker, expected", [
        ("SH600519", "600519"),   # 大写前缀式
        ("sz300750", "300750"),   # 小写前缀式
        ("600519.SH", "600519"), # 后缀式（先 split(".")）
        ("300750", "300750"),    # 纯 6 位数字
    ])
    def test_valid_astock_codes(self, ticker: str, expected: str):
        """各种合法 A 股代码格式 → 提取出 6 位数字。"""
        assert _extract_astock_code(ticker) == expected

    @pytest.mark.parametrize("ticker", [
        "AAPL",    # 美股字母代码
        "12345",   # 5 位数字，非 A 股
    ])
    def test_non_astock_codes(self, ticker: str):
        """非 A 股代码 → None。"""
        assert _extract_astock_code(ticker) is None


# ---------------------------------------------------------------------------
# 免费源智能融合 _merge_fill（方案A）
# ---------------------------------------------------------------------------

class TestMergeFill:
    def test_fill_missing_from_later(self):
        """base 缺的字段从后续源补，base 有的不被覆盖。"""
        assert _merge_fill({"a": 1, "b": None}, {"b": 2, "c": 3}) == {"a": 1, "b": 2, "c": 3}

    def test_empty_placeholders_treated_missing(self):
        """None/''/'-'/'—'/'N/A' 视为空，被后续源补上。"""
        assert _merge_fill({"x": "-", "y": ""}, {"x": "电子", "y": "v"}) == {"x": "电子", "y": "v"}

    def test_prefer_overrides_order(self):
        """prefer 指定源优先(非空)。"""
        extras = {"marketCap": 999}
        assert _merge_fill({"marketCap": 100}, extras, prefer={"marketCap": extras})["marketCap"] == 999

    def test_prefer_empty_falls_back(self):
        """prefer 源该字段为空 → 退回顺序取。"""
        assert _merge_fill({"marketCap": 100}, {"marketCap": None},
                           prefer={"marketCap": {"marketCap": None}})["marketCap"] == 100

    def test_all_empty_returns_empty(self):
        assert _merge_fill({}, {}) == {}
        assert _merge_fill({"a": None}, {"a": ""}) == {}
