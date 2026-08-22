"""事实核对护栏 fact_check —— 承重不变量 + 多处改写 span 正确性 + 市场隔离。

用假 store（内存快照）+ monkeypatch 补拉，不触网络。覆盖 P1/P2 共用原语的关键行为：
标签锚定提取防火墙、紧容差纠正/认证、库内优先→可疑补拉、他市 key 不核。
"""
import asyncio

import pytest

from bottleneck_hunter.vip import fact_check, number_guard
from bottleneck_hunter.watchlist import macro_data
from bottleneck_hunter.watchlist.store_base import _today as _bj_today


class _Store:
    def __init__(self, snaps=None):
        self._snaps = snaps or []
        self.saved = []

    def get_latest_macro_snapshots(self):
        return self._snaps

    def save_macro_snapshot(self, indicator, date, value, fetched_at=None, change_pct=0.0):
        self.saved.append((indicator, value))


@pytest.fixture
def no_live(monkeypatch):
    monkeypatch.setattr(macro_data, "_fetch_yf_quote", lambda code: None)


def _run(coro):
    return asyncio.run(coro)


def test_tolerance_invariant():
    # 承重：0.43% 抄写漂移必须被指数容差抓、被 number_guard 放（否则本方案失效）
    err = abs(7674.37 - 7641.16) / 7641.16
    assert fact_check._INDEX_TOL < err < number_guard._REL_TOL


def test_extraction_firewall():
    ms = fact_check.extract_index_mentions("标普500收于7674.37，成交日30JUN26页3", {"sp500", "nasdaq"})
    assert [m[2] for m in ms] == ["7674.37"]  # 日期/页码不入选


def test_correct_over_tolerance(no_live):
    st = _Store([{"indicator": "sp500", "value": 7641.16, "date": _bj_today()}])
    txt, cert = _run(fact_check.reconcile("标普500收于7674.37。", st, market="us_stock"))
    assert "7641.16 ⚠系统核实" in txt and "7674.37" not in txt
    assert cert["corrected"] == 1


def test_certify_within_tolerance(no_live):
    st = _Store([{"indicator": "sp500", "value": 7641.16, "date": _bj_today()}])
    txt, cert = _run(fact_check.reconcile("标普500约 7641.5。", st, market="us_stock"))
    assert "7641.5 ✓" in txt and cert["certified"] == 1


def test_multi_span_rewrite_back_to_front(no_live):
    # 两处纠正：从后往前改写，两个 span 都正确落位
    st = _Store([{"indicator": "sp500", "value": 7641.16, "date": _bj_today()},
                 {"indicator": "nasdaq", "value": 26067.17, "date": _bj_today()}])
    txt, cert = _run(fact_check.reconcile("标普500 7674.37，纳指 26999.99。", st, market="us_stock"))
    assert "7641.16 ⚠系统核实" in txt and "26067.17 ⚠系统核实" in txt
    assert cert["corrected"] == 2


def test_live_topup_on_stale(monkeypatch):
    # 库内过期 → 实时补拉一次成为权威 + 落库
    monkeypatch.setattr(macro_data, "_fetch_yf_quote", lambda code: {"value": 7641.16, "change_pct": 0.4})
    st = _Store([{"indicator": "sp500", "value": 7000.0, "date": "2020-01-01"}])
    txt, cert = _run(fact_check.reconcile("标普500 7000。", st, market="us_stock"))
    assert "7641.16 ⚠系统核实" in txt
    assert ("sp500", 7641.16) in st.saved
    assert cert["items"][0]["source"] == "yfinance实时"


def test_market_isolation():
    # a_stock 下不核 sp500 提及（守市场隔离）
    txt, cert = _run(fact_check.reconcile("标普500 7674.37。", _Store([]), market="a_stock"))
    assert txt == "标普500 7674.37。" and cert["items"] == []


def test_price_high_confidence():
    q = [{"ticker": "NVDA", "price": 175.2, "currency": "USD"}]
    txt, its = fact_check.reconcile_prices("NVDA 现价 $180.5。", q)
    assert "$175.2 ⚠系统核实" in txt and its[0]["verdict"] == "⚠纠正"


if __name__ == "__main__":  # GBK 控制台可直接 python 跑
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
