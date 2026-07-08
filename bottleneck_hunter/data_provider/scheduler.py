"""数据源智能调度选择器 —— 质量梯队 + 档内均衡 + 免费额度阀。

被 DataHub(providers) 与 FetcherManager(fetchers) 两层共用（纯函数 + 进程内滑窗状态），
**不 import hub/manager** 以免循环依赖。per-day 额度复用现有 datasource_stats 查询，不建新表。

选源顺序（order）：
  1) 丢弃已超免费额度的源（不发请求就跳过 → 真正防超额）；
  2) 按该能力的 priority 分档（小=优先=质量更高）；
  3) 档内按"最近用量最少"升序 → 多源轮换均衡分摊，避免过度使用单一源。
免费源（yfinance/akshare/…）不在额度表 → 永不被额度阀掐断 → 兜底永不断供。
"""
from __future__ import annotations

import logging
import os
import time
from collections import defaultdict, deque

logger = logging.getLogger(__name__)

_LOAD_WINDOW_S = 600  # 档内均衡用的"最近用量"窗口：近 10min 调用次数

# 各源近期调用时刻（均衡负载 + per-min/hour 额度阀共用同一份滑窗）
_recent: dict[str, deque] = defaultdict(deque)

# 免费档保守默认额度（可被 env DS_QUOTA_<SRC> 覆盖）。免费源不入表 = 永不限流。
# 真实上限随各家政策变动（如 AlphaVantage 已 500→25/日），故只做保守默认 + env 旋钮。
_DEFAULT_QUOTA: dict[str, dict[str, int]] = {
    "alphavantage": {"per_day": 20, "per_min": 5},
    "polygon": {"per_min": 5},
    "tiingo": {"per_day": 400, "per_hour": 45},
    "finnhub": {"per_min": 55},
    "fmp": {"per_day": 240},
    "tushare": {"per_min": 400},
}

_WINDOW_SECONDS = {"per_min": 60, "per_hour": 3600, "per_day": 86400}

_store = None
_daily_cache: tuple[float, dict[str, int]] = (0.0, {})
_DAILY_TTL_S = 60  # per-day 计数缓存，避免每次选源都查库


def set_store(store) -> None:
    """app.py 注入 WatchlistStore，供 per-day 额度查 datasource_stats。None 时 per-day 跳过。"""
    global _store
    _store = store


def _quota(source: str) -> dict[str, int]:
    """默认额度 + env 覆盖。env 形如 DS_QUOTA_ALPHAVANTAGE=per_day=20,per_min=5"""
    q = dict(_DEFAULT_QUOTA.get(source, {}))
    raw = os.environ.get(f"DS_QUOTA_{source.upper()}", "")
    for part in raw.split(","):
        k, sep, v = part.partition("=")
        if sep:
            try:
                q[k.strip()] = int(v.strip())
            except ValueError:
                pass
    return q


def note_call(source: str) -> None:
    """每次真实调用后记一次（均衡 + 额度阀共用）。两层记账处各调一次。"""
    dq = _recent[source]
    now = time.time()
    dq.append(now)
    cutoff = now - 86400  # 只保留最长窗口(1天)内的点，防无限增长
    while dq and dq[0] < cutoff:
        dq.popleft()


def _count_within(source: str, window_s: int) -> int:
    dq = _recent.get(source)
    if not dq:
        return 0
    cutoff = time.time() - window_s
    return sum(1 for t in dq if t >= cutoff)


def recent_load(source: str) -> int:
    """近 10min 调用数，作档内均衡负载（越小越优先）。"""
    return _count_within(source, _LOAD_WINDOW_S)


def _daily_calls() -> dict[str, int]:
    """今日各源 DB 累计调用数（60s 缓存）。权威跨进程，但有 <=60s 滞后——短时突发由 per-min 窗口兜住。"""
    global _daily_cache
    ts, data = _daily_cache
    now = time.time()
    if now - ts < _DAILY_TTL_S:
        return data
    fresh: dict[str, int] = {}
    if _store is not None:
        try:
            for row in _store.get_ds_stats_by_source(1):
                fresh[row.get("source", "")] = int(row.get("calls", 0) or 0)
        except Exception as e:  # noqa: BLE001
            logger.debug("scheduler per-day 查询失败: %s", e)
    _daily_cache = (now, fresh)
    return fresh


def is_over_quota(source: str) -> bool:
    """任一窗口(min/hour/day)超限即 True。不在 quota 字典（免费源）恒 False。"""
    q = _quota(source)
    if not q:
        return False
    for key, cap in q.items():
        if key == "per_day":
            if _daily_calls().get(source, 0) >= cap:
                return True
        else:
            win = _WINDOW_SECONDS.get(key)
            if win and _count_within(source, win) >= cap:
                return True
    return False


def cap_prio(provider, cap: str) -> int:
    """该 provider 在某能力下的质量梯队：优先 cap_priority[cap]，回落 provider.priority（向后兼容）。"""
    return getattr(provider, "cap_priority", {}).get(cap, provider.priority)


def order(cands: list[tuple[str, int]]) -> list[str]:
    """入参 [(源名, 该能力priority)] → 排好序的源名列表。

    丢弃超额源 → priority 升序分档 → 档内 recent_load 升序（均衡轮换）。
    """
    live = []
    for name, prio in cands:
        if is_over_quota(name):
            logger.debug("数据源 %s 达免费额度上限，本次跳过换源", name)
            continue
        live.append((name, prio))
    live.sort(key=lambda np: (np[1], recent_load(np[0])))
    return [name for name, _ in live]


def _reset_for_test() -> None:
    """单测隔离用：清空进程内滑窗与缓存。"""
    global _daily_cache
    _recent.clear()
    _daily_cache = (0.0, {})
