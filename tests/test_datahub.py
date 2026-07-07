"""DataHub 离线自检 — 路由/降级/熔断/去重/记账/_NON_RETRIABLE。用假 provider，不打外网。"""

import asyncio

import bottleneck_hunter.data_provider.hub as hubmod
from bottleneck_hunter.data_provider.hub import CIRCUIT_BREAK_THRESHOLD, DataHub


class FakeProvider:
    def __init__(self, name, priority, behavior, cap="earnings", market="us_stock"):
        self.name = name
        self.priority = priority
        self._behavior = behavior   # "ok" | "empty" | "raise" | "bad_arg"
        self._cap = cap
        self._market = market
        self.calls = 0

    def capabilities(self): return {self._cap}
    def markets(self): return {self._market}
    def supports(self, c, m): return c == self._cap and m == self._market

    async def fetch(self, capability, ticker, market, user_id=""):
        self.calls += 1
        if self._behavior == "ok":
            return {"ticker": ticker, "src": self.name}
        if self._behavior == "empty":
            return None
        if self._behavior == "bad_arg":
            raise ValueError("bad param")   # _NON_RETRIABLE
        raise RuntimeError("source down")


class _MemStore:
    def __init__(self): self.records = []
    def record_ds_call(self, source, capability, market, ok, latency_ms=0, rows=0, last_error=""):
        self.records.append({"source": source, "ok": ok, "rows": rows, "err": last_error})


def _hub(*providers):
    store = _MemStore()
    hubmod.set_stats_store(store)
    h = DataHub()
    for p in providers:
        h.register(p)
    return h, store


def test_priority_routing_picks_lowest_priority_number():
    p0 = FakeProvider("p0", 0, "ok")
    p1 = FakeProvider("p1", 1, "ok")
    h, store = _hub(p1, p0)   # 注册顺序乱序
    res = asyncio.run(h.fetch("earnings", "AAPL", "us_stock"))
    assert res["src"] == "p0"          # priority=0 优先
    assert p1.calls == 0               # 首个成功即返回，不调 p1（去重取单源）


def test_degradation_falls_through_on_failure():
    p0 = FakeProvider("p0", 0, "raise")
    p1 = FakeProvider("p1", 1, "ok")
    h, store = _hub(p0, p1)
    res = asyncio.run(h.fetch("earnings", "AAPL", "us_stock"))
    assert res["src"] == "p1"          # p0 抛→降级到 p1
    assert p0.calls == 1 and p1.calls == 1


def test_empty_falls_through_not_circuit():
    p0 = FakeProvider("p0", 0, "empty")
    p1 = FakeProvider("p1", 1, "ok")
    h, store = _hub(p0, p1)
    res = asyncio.run(h.fetch("earnings", "AAPL", "us_stock"))
    assert res["src"] == "p1"
    # 空数据不计熔断
    assert h._states["p0"].fail_count == 0


def test_circuit_opens_after_threshold():
    p0 = FakeProvider("p0", 0, "raise")
    h, store = _hub(p0)
    for _ in range(CIRCUIT_BREAK_THRESHOLD):
        asyncio.run(h.fetch("earnings", "X", "us_stock"))
    st = h._states["p0"]
    assert st.is_circuit_open is True
    calls_before = p0.calls
    asyncio.run(h.fetch("earnings", "X", "us_stock"))  # 熔断后应跳过
    assert p0.calls == calls_before                    # 未再调用


def test_non_retriable_not_counted_as_circuit():
    p0 = FakeProvider("p0", 0, "bad_arg")
    h, store = _hub(p0)
    for _ in range(CIRCUIT_BREAK_THRESHOLD + 2):
        asyncio.run(h.fetch("earnings", "X", "us_stock"))
    assert h._states["p0"].fail_count == 0             # ValueError 不计熔断
    assert h._states["p0"].is_circuit_open is False


def test_records_usage():
    p0 = FakeProvider("p0", 0, "ok")
    h, store = _hub(p0)
    asyncio.run(h.fetch("earnings", "AAPL", "us_stock"))
    assert len(store.records) == 1
    assert store.records[0]["source"] == "p0" and store.records[0]["ok"] is True


def test_track_records_and_reraises():
    p = FakeProvider("yf", 0, "ok", cap="news")
    h, store = _hub(p)

    async def ok_run():
        async with h.track("yf", "news", "us_stock") as sink:
            sink["rows"] = 7
    asyncio.run(ok_run())
    assert store.records[-1]["source"] == "yf" and store.records[-1]["rows"] == 7

    async def fail_run():
        async with h.track("yf", "news", "us_stock"):
            raise RuntimeError("boom")
    try:
        asyncio.run(fail_run())
        assert False, "track 应 re-raise"
    except RuntimeError:
        pass
    assert store.records[-1]["ok"] is False            # 失败也记账


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok ", name)
    print("all passed")
