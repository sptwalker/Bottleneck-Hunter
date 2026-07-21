"""yf_gate 接线回归：直连 yfinance 的取数函数必在成功/失败两路都调用 throttle + observe。"""
import types

import pytest

from bottleneck_hunter.watchlist import institutional_pipeline as ip
from bottleneck_hunter.data_provider import yf_gate


@pytest.fixture
def spy(monkeypatch):
    calls = {"throttle": 0, "observe": []}
    monkeypatch.setattr(yf_gate, "throttle", lambda: calls.__setitem__("throttle", calls["throttle"] + 1))
    monkeypatch.setattr(yf_gate, "observe", lambda e=None: calls["observe"].append(e))
    return calls


class _EmptyDF:
    empty = True


def test_institutional_success_path_observes_none(spy, monkeypatch):
    class FakeTicker:
        def __init__(self, t): pass
        institutional_holders = _EmptyDF()
    monkeypatch.setattr(ip, "yf", types.SimpleNamespace(Ticker=FakeTicker))
    ip._fetch_institutional_holders_sync("AAPL")
    assert spy["throttle"] == 1
    assert spy["observe"] == [None]           # 成功路径 observe(None)


def test_institutional_error_path_observes_exception(spy, monkeypatch):
    class FakeTicker:
        def __init__(self, t): pass
        @property
        def institutional_holders(self):
            raise Exception("Too Many Requests. Rate limited.")
    monkeypatch.setattr(ip, "yf", types.SimpleNamespace(Ticker=FakeTicker))
    ip._fetch_institutional_holders_sync("AAPL")
    assert spy["throttle"] == 1
    assert len(spy["observe"]) == 1 and isinstance(spy["observe"][0], Exception)  # 失败路径 observe(err)
