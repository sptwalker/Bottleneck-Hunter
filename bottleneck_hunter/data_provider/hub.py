"""DataHub — 抽象数据汇总层：能力×市场×多provider×优先级×熔断×记账。

- 全托管 fetch(capability, ...)：quote/daily 委托 FetcherManager（复用其熔断）；
  其余能力遍历 provider 候选，按 priority 逐个尝试，首个成功即返回（去重=取单源），全程记账。
- 半托管 track(source, capability, market)：pipeline 自己取数解析，仅经 hub 记账+熔断状态。

熔断阈值/白名单复用 manager 的常量，不重定义。记账用模块级注入的 store，失败只 debug。
"""

from __future__ import annotations

import contextlib
import logging
import time
from typing import Protocol

from bottleneck_hunter.data_provider.manager import (
    _NON_RETRIABLE,
    CIRCUIT_BREAK_COOLDOWN,
    CIRCUIT_BREAK_THRESHOLD,
)

logger = logging.getLogger(__name__)

# ── 能力常量 ──────────────────────────────────────────────
CAP_QUOTE = "quote"
CAP_DAILY = "daily"
CAP_FINANCIALS = "financials"   # 预留，暂无 provider
CAP_EARNINGS = "earnings"
CAP_NEWS = "news"
CAP_SEC = "sec"
CAP_INSTITUTIONAL = "institutional"
CAP_OPTIONS = "options"
CAP_INSIDER = "insider"
CAP_NOTICE = "notice"
CAP_SMARTMONEY = "smartmoney"

_MANAGER_CAPS = {CAP_QUOTE, CAP_DAILY}  # 这两个委托 FetcherManager，不建 hub 层熔断


class CapabilityProvider(Protocol):
    name: str
    priority: int
    def capabilities(self) -> set[str]: ...
    def markets(self) -> set[str]: ...
    def supports(self, capability: str, market: str) -> bool: ...
    async def fetch(self, capability: str, ticker: str, market: str, user_id: str = "") -> dict | None: ...


class _ProviderState:
    """provider 运行时熔断状态（_FetcherState 泛化，去 BaseFetcher 依赖）。"""

    def __init__(self, provider: CapabilityProvider):
        self.provider = provider
        self.fail_count = 0
        self.last_fail_time = 0.0
        self.total_calls = 0
        self.total_failures = 0

    @property
    def is_circuit_open(self) -> bool:
        if self.fail_count < CIRCUIT_BREAK_THRESHOLD:
            return False
        if time.time() - self.last_fail_time > CIRCUIT_BREAK_COOLDOWN:
            self.fail_count = 0
            return False
        return True

    def record_success(self):
        self.fail_count = 0
        self.total_calls += 1

    def record_failure(self):
        self.fail_count += 1
        self.last_fail_time = time.time()
        self.total_calls += 1
        self.total_failures += 1


# ── 记账 store 注入（hub 单例不持 store） ──────────────────
_stats_store = None


def set_stats_store(store) -> None:
    global _stats_store
    _stats_store = store


def _record(source, capability, market, ok, latency_ms=0.0, rows=0, last_error=""):
    store = _stats_store
    if store is None:
        try:
            from bottleneck_hunter.watchlist.store import WatchlistStore
            store = WatchlistStore()
        except Exception:  # noqa: BLE001
            return
    try:
        store.record_ds_call(source, capability, market, ok,
                             latency_ms=latency_ms, rows=rows, last_error=last_error)
    except Exception as e:  # noqa: BLE001
        logger.debug("DataHub 记账失败: %s", e)


class DataHub:
    def __init__(self):
        self._states: dict[str, _ProviderState] = {}

    def register(self, provider: CapabilityProvider) -> None:
        self._states[provider.name] = _ProviderState(provider)

    def _candidates(self, capability: str, market: str, user_id: str = "") -> list[_ProviderState]:
        """健康(支持+未熔断+有key)候选 → 调度器：丢超额、按能力质量梯队分档、档内均衡轮换。

        无 key 的 keyed 源在此就排除，避免其被 note_call/记账污染额度阀与健康统计。
        """
        from bottleneck_hunter.data_provider import scheduler
        from bottleneck_hunter.data_provider.data_source_catalog import (
            _CATALOG_BY_ID,
            resolve_data_source_key,
        )
        healthy = {}
        for st in self._states.values():
            p = st.provider
            if not p.supports(capability, market) or st.is_circuit_open:
                continue
            # keyed 源（在数据源目录里）无 key → 不进候选（零网络、零记账）；免费源(akshare/yfinance)恒可用
            if p.name in _CATALOG_BY_ID and not resolve_data_source_key(p.name, user_id):
                continue
            healthy[p.name] = st
        pairs = [(name, scheduler.cap_prio(st.provider, capability)) for name, st in healthy.items()]
        return [healthy[name] for name in scheduler.order(pairs)]

    async def fetch(self, capability: str, ticker: str, market: str, user_id: str = "") -> dict | None:
        """全托管取数：首个成功的 provider 即返回（去重取单源），全程记账。"""
        if capability in _MANAGER_CAPS:
            return await self._fetch_via_manager(capability, ticker, market)

        for st in self._candidates(capability, market, user_id):
            src = st.provider.name
            from bottleneck_hunter.data_provider import scheduler
            scheduler.note_call(src)  # 记一次调用（均衡负载 + 额度阀共用；空返回也算消耗）
            t0 = time.time()
            try:
                data = await st.provider.fetch(capability, ticker, market, user_id)
                dt = (time.time() - t0) * 1000
                if data:
                    st.record_success()
                    n = len(data) if isinstance(data, (list, dict)) else 1
                    _record(src, capability, market, True, dt, n)
                    return data
                # 返回空视为无数据，不计熔断，试下一个
                _record(src, capability, market, True, dt, 0)
            except _NON_RETRIABLE as e:
                _record(src, capability, market, False, (time.time() - t0) * 1000, 0, str(e))
            except Exception as e:  # noqa: BLE001
                st.record_failure()
                _record(src, capability, market, False, (time.time() - t0) * 1000, 0, str(e))
        return None

    async def _fetch_via_manager(self, capability: str, ticker: str, market: str) -> dict | None:
        from bottleneck_hunter.data_provider import get_fetcher_manager
        mgr = get_fetcher_manager()
        t0 = time.time()
        try:
            if capability == CAP_QUOTE:
                q = await mgr.fetch_realtime(ticker, market)
                dt = (time.time() - t0) * 1000
                if q is not None:
                    src = getattr(q, "source", None) or f"manager:{market}"
                    _record(src, capability, market, True, dt, 1)
                    return q.model_dump() if hasattr(q, "model_dump") else dict(q)
                _record(f"manager:{market}", capability, market, True, dt, 0)
                return None
            df = await mgr.fetch_daily(ticker, market)
            dt = (time.time() - t0) * 1000
            n = len(df) if df is not None else 0
            _record(f"manager:{market}", capability, market, n > 0, dt, n)
            return {"ticker": ticker, "rows": n, "_df": df} if n > 0 else None
        except Exception as e:  # noqa: BLE001
            _record(f"manager:{market}", capability, market, False, (time.time() - t0) * 1000, 0, str(e))
            return None

    @contextlib.asynccontextmanager
    async def track(self, source: str, capability: str, market: str):
        """半托管：包住 pipeline 自己的取数调用，仅记账；异常 re-raise（不改原行为）。

        **不触碰 CapabilityProvider 的熔断计数** —— 半托管直连管线与全托管 provider 若同名
        （如 "yfinance" 既是 track 源又是期权 provider），共享 _ProviderState 会互相复位/误开熔断。
        故 track 只写 datasource_stats，熔断由各能力的全托管路径独立管理。

        用法：
            async with get_hub().track("yfinance", CAP_NEWS, market) as sink:
                data = 真实取数(...)
                sink["rows"] = len(data)
        """
        sink: dict = {"rows": 0}
        t0 = time.time()
        try:
            yield sink
            dt = (time.time() - t0) * 1000
            _record(source, capability, market, True, dt, int(sink.get("rows", 0)))
        except _NON_RETRIABLE as e:
            _record(source, capability, market, False, (time.time() - t0) * 1000, 0, str(e))
            raise
        except Exception as e:  # noqa: BLE001
            _record(source, capability, market, False, (time.time() - t0) * 1000, 0, str(e))
            raise

    def get_status(self) -> list[dict]:
        out = []
        for st in sorted(self._states.values(), key=lambda s: s.provider.priority):
            p = st.provider
            out.append({
                "name": p.name, "priority": p.priority,
                "capabilities": sorted(p.capabilities()), "markets": sorted(p.markets()),
                "circuit_open": st.is_circuit_open, "fail_count": st.fail_count,
                "total_calls": st.total_calls, "total_failures": st.total_failures,
            })
        return out


_hub: DataHub | None = None


def get_hub() -> DataHub:
    global _hub
    if _hub is None:
        _hub = _create_hub()
    return _hub


def _create_hub() -> DataHub:
    hub = DataHub()
    try:
        from bottleneck_hunter.data_provider.providers import build_providers
        for p in build_providers():
            hub.register(p)
    except Exception as e:  # noqa: BLE001
        logger.warning("DataHub provider 注册失败: %s", e)
    return hub
