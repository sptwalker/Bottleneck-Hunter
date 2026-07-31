"""A股 本土宏观解析（_fetch_cn_macro）：验证 5 个 akshare 接口的 value/change_pct 解析正确。

真实 akshare 不可在 CI 联网调用，故 monkeypatch 成固定 DataFrame，只测「列解析 + 变动口径」这段逻辑：
- CPI/M2：今值-前值 = 百分点变动，且最新未公布(今值 NaN)行需被 dropna 剔除
- LPR/中债10Y：相邻两次报价差 = 百分点变动
- 社融：环比 % 变动
"""

import pandas as pd

from bottleneck_hunter.watchlist import macro_data


class _FakeAk:
    def macro_china_cpi_yearly(self):
        return pd.DataFrame([
            {"日期": "2026-05", "今值": 0.3, "前值": 0.2},
            {"日期": "2026-06", "今值": 0.5, "前值": 0.3},
            {"日期": "2026-07", "今值": None, "前值": 0.5},  # 未公布 → dropna 剔除
        ])

    def macro_china_m2_yearly(self):
        return pd.DataFrame([
            {"日期": "2026-05", "今值": 8.1, "前值": 8.0},
            {"日期": "2026-06", "今值": 8.3, "前值": 8.1},
        ])

    def macro_china_lpr(self):
        return pd.DataFrame([
            {"TRADE_DATE": "2026-05-20", "LPR1Y": 3.1, "LPR5Y": 3.6},
            {"TRADE_DATE": "2026-06-20", "LPR1Y": 3.0, "LPR5Y": 3.5},
        ])

    def bond_zh_us_rate(self):
        return pd.DataFrame([
            {"日期": "2026-07-30", "中国国债收益率10年": 1.83},
            {"日期": "2026-07-31", "中国国债收益率10年": 1.85},
        ])

    def macro_china_shrzgm(self):
        return pd.DataFrame([
            {"月份": "2026-05", "社会融资规模增量": 25000.0},
            {"月份": "2026-06", "社会融资规模增量": 30000.0},
        ])


def test_fetch_cn_macro_parsing(monkeypatch):
    monkeypatch.setattr(macro_data, "ak", _FakeAk())
    out = macro_data._fetch_cn_macro()

    # CPI：最新完整行 2026-06，今值 0.5 / 前值 0.3 → change 0.2；NaN 行被剔除
    assert out["cn_cpi_yoy"]["value"] == 0.5
    assert out["cn_cpi_yoy"]["change_pct"] == 0.2
    assert out["cn_cpi_yoy"]["date"] == "2026-06"

    assert out["cn_m2_yoy"]["value"] == 8.3
    assert out["cn_m2_yoy"]["change_pct"] == 0.2

    # LPR：3.0 vs 前次 3.1 → 降 0.1 个百分点（降息）
    assert out["cn_lpr_1y"]["value"] == 3.0
    assert out["cn_lpr_1y"]["change_pct"] == -0.1

    # 中债10Y：1.85 vs 1.83 → +0.02 个百分点
    assert out["cn_10y_yield"]["value"] == 1.85
    assert out["cn_10y_yield"]["change_pct"] == 0.02

    # 社融：30000 vs 25000 → 环比 +20%
    assert out["cn_social_financing"]["value"] == 30000.0
    assert out["cn_social_financing"]["change_pct"] == 20.0


def test_fetch_cn_macro_partial_failure(monkeypatch):
    """单个接口抛错不影响其余指标（逐项 try/except）。"""
    fake = _FakeAk()
    fake.macro_china_lpr = lambda: (_ for _ in ()).throw(RuntimeError("接口超时"))
    monkeypatch.setattr(macro_data, "ak", fake)
    out = macro_data._fetch_cn_macro()
    assert "cn_lpr_1y" not in out          # 失败项缺席
    assert out["cn_cpi_yoy"]["value"] == 0.5  # 其余照常


def test_fetch_cn_macro_no_akshare(monkeypatch):
    monkeypatch.setattr(macro_data, "ak", None)
    assert macro_data._fetch_cn_macro() == {}


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
