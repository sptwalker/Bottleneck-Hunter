"""P0-① 数据完整性：OHLC 净化 + A股量能单位归一 + 跨源一致性自检。

- 脏 bar（high<low / 非正价 / NaN close）被丢弃
- baostock A股量能（股）÷100 → 手，与 akshare（手）同量级
- US 源量能不动（本就以股计）
- 全非法 → None（让 manager 切下一个源）
- close_divergence：同数据 0、2% 偏差被检出（跨源 1% 回归的可运行 primitive）
"""
import numpy as np
import pandas as pd

from bottleneck_hunter.data_provider.cleaning import clean_ohlc, close_divergence


def _bar(date, o, h, lo, c, v):
    return {"date": date, "open": o, "high": h, "low": lo, "close": c, "volume": v}


def _bar_amt(date, o, h, lo, c, v, amt):
    d = _bar(date, o, h, lo, c, v)
    d["amount"] = amt
    return d


def test_drops_dirty_bars():
    df = pd.DataFrame([
        _bar("2026-08-01", 10, 11, 9, 10.5, 3000),   # ok
        _bar("2026-08-02", 10, 8, 9, 9.5, 3000),     # high<low → 丢
        _bar("2026-08-03", -1, 11, 9, 10.0, 3000),   # 负价 → 丢
        _bar("2026-08-04", 10, 11, 9, np.nan, 3000), # close NaN → 丢
        _bar("2026-08-05", 10, 12, 9, 11.0, 4000),   # ok
    ])
    out = clean_ohlc(df, "akshare", "a_stock")
    assert list(out["date"]) == ["2026-08-01", "2026-08-05"]
    assert list(out.index) == [0, 1]  # 重置索引


def test_baostock_volume_shares_to_hands():
    """baostock 报「股」→ ÷100 归一为「手」，消除切源 100× 漂移。"""
    df = pd.DataFrame([_bar("2026-08-01", 10, 11, 9, 10.5, 300000)])  # 300000股 = 3000手
    out = clean_ohlc(df, "baostock", "a_stock")
    assert int(out.iloc[0]["volume"]) == 3000


def test_akshare_ashare_volume_untouched():
    """akshare 本就以「手」计，不动。"""
    df = pd.DataFrame([_bar("2026-08-01", 10, 11, 9, 10.5, 3000)])
    out = clean_ohlc(df, "akshare", "a_stock")
    assert int(out.iloc[0]["volume"]) == 3000


def test_amount_derivation_overrides_source_name_hands():
    """有 amount → 用 amount/(close×vol) 反推单位，压过源名假设。
    r≈100(手) → 不缩，即便源名(baostock)在「股」兜底集合里。"""
    # close=10, vol=3000手, amount=10×3000×100=3_000_000 → r=100 → 手
    df = pd.DataFrame([_bar_amt("2026-08-01", 10, 11, 9, 10.0, 3000, 3_000_000)])
    out = clean_ohlc(df, "baostock", "a_stock")  # 源名说「股」，数据说「手」
    assert int(out.iloc[0]["volume"]) == 3000  # 数据裁决：不缩


def test_amount_derivation_catches_shares_source_agnostic():
    """r≈1(股) → ÷100，无论源名。直接覆盖审查关切：pytdx 若实为「股」也被数据当场抓到。"""
    # close=10, vol=300000股, amount=10×300000=3_000_000 → r=1 → 股
    df = pd.DataFrame([_bar_amt("2026-08-01", 10, 11, 9, 10.0, 300000, 3_000_000)])
    out = clean_ohlc(df, "pytdx", "a_stock")
    assert int(out.iloc[0]["volume"]) == 3000  # ÷100 归一到「手」


def test_amount_missing_falls_back_to_source_name():
    """amount 缺失 → 退回源名假设：baostock=股(÷100)、akshare=手(不动)。"""
    df_b = pd.DataFrame([_bar("2026-08-01", 10, 11, 9, 10.0, 300000)])
    assert int(clean_ohlc(df_b, "baostock", "a_stock").iloc[0]["volume"]) == 3000
    df_a = pd.DataFrame([_bar("2026-08-01", 10, 11, 9, 10.0, 3000)])
    assert int(clean_ohlc(df_a, "akshare", "a_stock").iloc[0]["volume"]) == 3000


def test_amount_zero_falls_back_to_source_name():
    """amount 全零(停牌/旧数据)无法反推 → 退回源名(baostock=股→÷100)。"""
    df = pd.DataFrame([_bar_amt("2026-08-01", 10, 11, 9, 10.0, 300000, 0)])
    out = clean_ohlc(df, "baostock", "a_stock")
    assert int(out.iloc[0]["volume"]) == 3000


def test_us_volume_untouched_even_for_shares_source_name():
    """美股市场：即便源名在集合里也不归一（分支仅 a_stock）。US 量能本就以股计。"""
    df = pd.DataFrame([_bar("2026-08-01", 100, 101, 99, 100.5, 5_000_000)])
    out = clean_ohlc(df, "yfinance", "us_stock")
    assert int(out.iloc[0]["volume"]) == 5_000_000
    out2 = clean_ohlc(df, "baostock", "us_stock")  # 名字在集合但市场非A股 → 不动
    assert int(out2.iloc[0]["volume"]) == 5_000_000


def test_all_invalid_returns_none():
    df = pd.DataFrame([
        _bar("2026-08-01", 0, 0, 0, 0, 0),
        _bar("2026-08-02", 10, 5, 9, 9.5, 3000),  # high<low
    ])
    assert clean_ohlc(df, "pytdx", "a_stock") is None


def test_empty_and_none_passthrough():
    assert clean_ohlc(None, "akshare", "a_stock") is None
    empty = pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])
    out = clean_ohlc(empty, "akshare", "a_stock")
    assert out is not None and out.empty


def test_cross_source_divergence():
    """跨源一致性 primitive：同数据 0；某日 2% 偏差被检出（>1% 报警口径）。"""
    base = pd.DataFrame([
        _bar("2026-08-01", 10, 11, 9, 10.00, 3000),
        _bar("2026-08-02", 10, 11, 9, 20.00, 3000),
    ])
    assert close_divergence(base, base) == 0.0
    other = base.copy()
    other.loc[1, "close"] = 20.40  # +2%
    div = close_divergence(base, other)
    assert 0.019 < div < 0.021
    assert div > 0.01  # 触发跨源报警


def test_divergence_no_common_dates():
    a = pd.DataFrame([_bar("2026-08-01", 10, 11, 9, 10.0, 3000)])
    b = pd.DataFrame([_bar("2026-09-01", 10, 11, 9, 10.0, 3000)])
    assert close_divergence(a, b) == 0.0


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
