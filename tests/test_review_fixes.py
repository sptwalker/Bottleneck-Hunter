"""审查修复回归：投委会改单钳制 + 符号校验 + 反向 finnhub 兜底。"""
import pytest

from bottleneck_hunter.watchlist.store import WatchlistStore
from bottleneck_hunter.watchlist.store_base import validate_ticker, normalize_ticker


@pytest.fixture
def store(tmp_path):
    return WatchlistStore(tmp_path / "t.db")


def _plan(store, shares, action="buy", price=100.0):
    return store.create_execution_plan(
        tactical_plan_id="tp", entry_id="e", ticker="AAPL",
        result_json={"action": action, "shares": shares, "target_price": price},
    )


# ── 投委会改单只准缩量（F6）────────────────────────────
def test_committee_cannot_increase_shares(store):
    pid = _plan(store, 50)
    store.apply_committee_modifications(pid, {"shares": 100})   # 放大→应钳回 50
    assert store.get_execution_plan(pid)["shares"] == 50


def test_committee_reduction_allowed(store):
    pid = _plan(store, 50)
    store.apply_committee_modifications(pid, {"shares": 30})    # 缩量→放行
    assert store.get_execution_plan(pid)["shares"] == 30


def test_committee_increase_blocked_when_original_zero(store):
    pid = _plan(store, 0)
    store.apply_committee_modifications(pid, {"shares": 200})   # 原 0 不得被放大（F6 核心）
    assert store.get_execution_plan(pid)["shares"] == 0


def test_committee_nonnumeric_shares_no_crash(store):
    pid = _plan(store, 50)
    store.apply_committee_modifications(pid, {"shares": "all"})  # 非数值不得崩溃
    assert store.get_execution_plan(pid)["shares"] == 50        # 钳回原值


# ── 符号校验 ──────────────────────────────────────────
def test_validate_ticker_us():
    for ok in ["AAPL", "GOOGL", "TSM", "SPCX", "BRK.B", "BRK-B"]:
        validate_ticker(normalize_ticker(ok, "us_stock"), "us_stock")   # 不抛
    for bad in ["SPACEX", "SPACE X", "TOOLONG", ""]:
        with pytest.raises(ValueError):
            validate_ticker(normalize_ticker(bad, "us_stock"), "us_stock")


def test_validate_ticker_astock_lenient():
    validate_ticker(normalize_ticker("600519.SH", "a_stock"), "a_stock")  # 不强校验，不抛


# ── 反向 finnhub 兜底（无 key 优雅返回）────────────────
def test_finnhub_fallback_no_key():
    from bottleneck_hunter.web.streaming.reverse import _finnhub_company_profile
    assert _finnhub_company_profile("SPCX") == {}
