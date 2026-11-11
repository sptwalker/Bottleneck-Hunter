"""统一费率模型（2026-11）：按品种判印花税，实盘模拟与回测共用一份。

病史：费率此前三处各写各的——实盘模拟 0.1% 双边且无印花税、回测 2.5bp+5bp 但无生产调用方、
权益曲线又按 0.1% 反推现金。ETF 与可转债法定免印花税，用股票默认值套上去会系统性高估成本。
"""
import pytest

from bottleneck_hunter.watchlist.fees import (
    InstrumentKind,
    detect_instrument_kind,
    resolve_fee_model,
)


def test_stamp_duty_only_for_stocks():
    stock = resolve_fee_model("600519.SS", "a_stock")
    etf = resolve_fee_model("510880.SS", "a_stock")
    bond = resolve_fee_model("113050.SS", "a_stock")
    assert stock.stamp_tax(100_000, "sell") == pytest.approx(50.0)
    assert stock.stamp_tax(100_000, "buy") == 0.0
    assert etf.stamp_tax(100_000, "sell") == 0.0
    assert bond.stamp_tax(100_000, "sell") == 0.0


def test_us_has_no_cost():
    assert resolve_fee_model("AAPL", "us_stock").total(10_000, "sell") == 0.0


def test_min_commission_applies_to_small_orders():
    stock = resolve_fee_model("600519.SS", "a_stock")
    assert stock.commission(1_000, "buy") == 5.0          # 万3=0.3 元 → 最低 5 元
    assert stock.commission(100_000, "buy") == 30.0


def test_instrument_detection():
    assert detect_instrument_kind("000001.SZ") is InstrumentKind.STOCK
    assert detect_instrument_kind("300750.SZ") is InstrumentKind.STOCK
    assert detect_instrument_kind("688981.SS") is InstrumentKind.STOCK
    assert detect_instrument_kind("510880.SS") is InstrumentKind.ETF
    assert detect_instrument_kind("159915.SZ") is InstrumentKind.ETF
    assert detect_instrument_kind("113050.SS") is InstrumentKind.CONVERTIBLE
    assert detect_instrument_kind("128001.SZ") is InstrumentKind.CONVERTIBLE
    assert detect_instrument_kind("900901.SS") is InstrumentKind.B_SHARE
    assert detect_instrument_kind("AAPL", "us_stock") is InstrumentKind.STOCK
