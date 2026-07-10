"""AI 模型运行时健康度 + 候选动态排序（智能调度 Phase 1）。

两件事：
- **ProviderHealth**：进程内熔断记忆。某 provider 硬故障(认证失败/额度耗尽)或失败即进入
  冷却期；rank_providers 在冷却期内把它沉底，避免对已知失效模型反复耗一整轮超时。
- **rank_providers**：按遥测(成功率) + 健康度(熔断) + 主模型加成 给候选 provider 排序。
  **无遥测数据时全部同分 → 稳定排序保持原顺序 → 平滑退化为现状（不劣于静态链）。**

严格按用户隔离：熔断 key=(user_id, provider)，绝无全局共享健康表（见 project_strict_key_isolation）。
ponytail: 进程内 dict + 每次 rank 读一次遥测；多 worker 需跨进程共享 / 高频再上缓存/Redis。
"""
from __future__ import annotations

import logging
import os
import threading
import time

logger = logging.getLogger(__name__)

# 各失败原因的冷却秒数（原因串来自 fallback.classify_reason）
_COOLDOWN_BY_REASON = {
    "认证失败(密钥无效)": 300,   # 硬故障：Key 无效，长冷却
    "频率限制/额度不足": 120,     # 限流/额度：中冷却
    "服务端错误": 60,
    "连接失败": 60,
    "请求超时": 45,
    "调用异常": 30,
}
_DEFAULT_COOLDOWN = 30
_CANCEL_REASON = "调用被取消"  # 用户主动取消，不惩罚


class ProviderHealth:
    """进程内熔断记忆：key=(user_id, provider) → 冷却截止时刻(monotonic)。"""

    def __init__(self) -> None:
        self._until: dict[tuple, float] = {}
        self._lock = threading.Lock()

    def record_failure(self, user_id: str, provider: str, reason: str = "") -> None:
        if reason == _CANCEL_REASON:
            return
        cd = _COOLDOWN_BY_REASON.get(reason, _DEFAULT_COOLDOWN)
        if cd <= 0:
            return
        with self._lock:
            self._until[(user_id or "", (provider or "").lower().strip())] = time.monotonic() + cd

    def record_success(self, user_id: str, provider: str) -> None:
        with self._lock:
            self._until.pop((user_id or "", (provider or "").lower().strip()), None)

    def is_open(self, user_id: str, provider: str) -> bool:
        """True = 该 provider 处于冷却期，应沉底/避免选作主模型。"""
        key = (user_id or "", (provider or "").lower().strip())
        with self._lock:
            t = self._until.get(key)
            if t is None:
                return False
            if time.monotonic() >= t:
                self._until.pop(key, None)
                return False
            return True

    def cooldown_remaining(self, user_id: str, provider: str) -> int:
        key = (user_id or "", (provider or "").lower().strip())
        with self._lock:
            t = self._until.get(key)
            return max(0, int(t - time.monotonic())) if t else 0

    def reset(self) -> None:
        """清空全部熔断状态（测试用）。"""
        with self._lock:
            self._until.clear()


health = ProviderHealth()


# ── 候选排序 ────────────────────────────────────────────
PRIMARY_BONUS = 0.30   # 主模型加成（上限式）：别的模型综合分须超过 主+此值 才顶替它
_MIN_CALLS = 3         # 需≥N次调用样本才信任其成功率，否则中性(1.0)，避免冷启动误判
_STATS_DAYS = 14       # 排序参考最近 N 天遥测


def _ranking_enabled() -> bool:
    """全局 feature flag：BH_SCHEDULER_RANK=0 可一键关回静态顺序。"""
    return os.environ.get("BH_SCHEDULER_RANK", "1") != "0"


def _load_stats(user_id: str) -> dict:
    """按 provider 聚合最近遥测：{provider: {calls, ok_rate}}。读失败→空(全中性)。"""
    try:
        from bottleneck_hunter.watchlist.store import WatchlistStore
        rows = WatchlistStore().get_model_call_stats(days=_STATS_DAYS, user_id=user_id or "")
    except Exception:  # noqa: BLE001
        return {}
    agg: dict[str, dict] = {}
    for r in rows:
        p = (r.get("provider") or "").lower().strip()
        a = agg.setdefault(p, {"calls": 0, "ok": 0})
        a["calls"] += r.get("calls", 0)
        a["ok"] += r.get("ok", 0)
    for a in agg.values():
        a["ok_rate"] = (100.0 * a["ok"] / a["calls"]) if a["calls"] else 100.0
    return agg


def _score(provider: str, user_id: str, primary: str, stats: dict) -> float:
    p = (provider or "").lower().strip()
    s = stats.get(p, {})
    calls = s.get("calls", 0)
    # 可靠性：样本足够才生效(0%→0.3, 100%→1.0)，否则中性 1.0（冷启动不惩罚）
    reliability = (0.3 + 0.7 * s.get("ok_rate", 100.0) / 100.0) if calls >= _MIN_CALLS else 1.0
    # 健康：熔断中重罚沉底
    health_factor = 0.05 if health.is_open(user_id, p) else 1.0
    score = reliability * health_factor
    if p == primary:
        score += PRIMARY_BONUS   # 主模型加成上限
    return score


def rank_providers(providers, user_id: str = "", primary_provider: str = "", stats=None) -> list:
    """按 健康度×可靠性 + 主模型加成 对 provider 列表排序(高→低)。

    **无遥测数据时所有 provider 同分 → 稳定排序保持原顺序 → 平滑退化为现状。**
    feature flag 关闭时直接返回原列表。
    """
    providers = list(providers)
    if not _ranking_enabled() or len(providers) < 2:
        return providers
    if stats is None:
        stats = _load_stats(user_id)
    primary = (primary_provider or "").lower().strip()
    # Python sorted 稳定：同分元素保持原相对顺序（无数据即原顺序）
    return sorted(providers, key=lambda p: _score(p, user_id, primary, stats), reverse=True)


def _selfcheck() -> None:
    """内联自检：熔断 + 排序 + 无数据退化。"""
    h = ProviderHealth()
    h.record_failure("u1", "deepseek", "认证失败(密钥无效)")
    assert h.is_open("u1", "deepseek") and not h.is_open("u1", "qwen")
    assert not h.is_open("u2", "deepseek")  # 按用户隔离
    h.record_success("u1", "deepseek")
    assert not h.is_open("u1", "deepseek")
    h.record_failure("u1", "kimi", _CANCEL_REASON)
    assert not h.is_open("u1", "kimi")  # 取消不惩罚

    # 无数据 → 保持原顺序
    assert rank_providers(["a", "b", "c"], "u", stats={}) == ["a", "b", "c"]
    # 低成功率沉底
    st = {"a": {"calls": 10, "ok_rate": 20.0}, "b": {"calls": 10, "ok_rate": 99.0}}
    assert rank_providers(["a", "b"], "u", stats=st)[0] == "b"
    # 主模型加成：同为无数据时主排第一
    assert rank_providers(["a", "b"], "u", primary_provider="b", stats={})[0] == "b"
    print("health selfcheck OK")


if __name__ == "__main__":
    _selfcheck()
