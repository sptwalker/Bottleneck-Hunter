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

# provider 付费/免费默认档（系统默认，用户策略「免费/付费优先」据此过滤/加权）。
# 国内免费档充足的记 free；无实质免费的记 paid；未知留空(中性)。可后续按 provider 覆盖。
_PROVIDER_TIER = {
    "deepseek": "free", "qwen": "free", "glm": "free", "kimi": "free", "siliconflow": "free",
    "openai": "paid", "anthropic": "paid", "google": "paid",
    "minimax": "paid", "agnes": "paid", "openrouter": "paid",
}


def provider_tier(provider: str) -> str:
    """返回 'free' / 'paid' / ''（未知）。

    自定义 provider id(如 siliconflow_nex_n2_pro / huawei_glm_5_2)不在 _PROVIDER_TIER 表里，
    直接查会得空档，导致付费/免费策略对它们失效。故先精确查表，未命中再按**子串**推断：
    id 里含哪个已知 provider 名就继承其档(按已知名长度降序，最具体优先)。仍无则空(中性)。
    """
    p = (provider or "").lower().strip()
    if p in _PROVIDER_TIER:
        return _PROVIDER_TIER[p]
    for known in sorted(_PROVIDER_TIER, key=len, reverse=True):
        if known in p:
            return _PROVIDER_TIER[known]
    return ""


def load_routing_policy(user_id: str = "", role_key: str = "") -> dict:
    """读用户调度策略：角色覆盖优先，回退全局默认，再回退空(中性 auto/balanced)。"""
    try:
        wl = _get_store()
        pol = wl.get_routing_policy(user_id or "", role_key) if role_key else None
        pol = pol or wl.get_routing_policy(user_id or "", "")
        return pol or {}
    except Exception:  # noqa: BLE001
        return {}


def load_capability_scores(user_id: str = "", role_key: str = "") -> dict:
    """按角色 capability_weights 计算各 provider 的能力综合分(0-10)。无模式测试数据→空(中性)。

    这是调度器**唯一的「质量」信号来源**（模式测试），补上运行时 ops 遥测测不出的
    输出质量维度（JSON 正确性/中文分析/评分区分度）。每个 provider 取其最佳被测模型的分。
    """
    try:
        from bottleneck_hunter.llm_clients.role_registry import get_role
        role = get_role(role_key)
        weights = role.capability_weights if role else None
        if not weights:
            return {}
        total_w = sum(weights.values())
        if total_w <= 0:
            return {}
        rows = _get_store().get_test_results(user_id=user_id or "")
        by_pm: dict[tuple, dict] = {}
        for r in rows:
            by_pm.setdefault((r["provider"], r["model"]), {})[r["test_type"]] = r["score"]
        out: dict[str, float] = {}
        for (prov, _model), scores in by_pm.items():
            comp = sum(scores.get(dim, 0) * w for dim, w in weights.items()) / total_w  # 0-10 加权均值
            p = (prov or "").lower().strip()
            out[p] = max(out.get(p, 0.0), comp)  # 每 provider 取其最佳模型
        return out
    except Exception:  # noqa: BLE001
        return {}


def _ranking_enabled() -> bool:
    """全局 feature flag：BH_SCHEDULER_RANK=0 可一键关回静态顺序。"""
    return os.environ.get("BH_SCHEDULER_RANK", "1") != "0"


# 缓存一份 WatchlistStore（构造含 schema 迁移，约 2.7ms）——排序在 get_models_for_role 热路径每次
# 多路调用，避免每次 new。record 用各自的 _write_conn，读用短连接，共享实例线程安全。
_store = None


def _get_store():
    global _store
    if _store is None:
        from bottleneck_hunter.watchlist.store import WatchlistStore
        _store = WatchlistStore()
    return _store


def _load_stats(user_id: str) -> dict:
    """按 provider 聚合最近遥测：{provider: {calls, ok_rate}}。读失败→空(全中性)。
    空 user_id → 空（严格隔离：无当前用户时绝不借用跨用户聚合来排序）。"""
    if not user_id:
        return {}
    try:
        rows = _get_store().get_model_call_stats(days=_STATS_DAYS, user_id=user_id)
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


def _score(provider: str, user_id: str, primary: str, stats: dict, policy: dict, caps: dict) -> float:
    p = (provider or "").lower().strip()
    s = stats.get(p, {})
    calls = s.get("calls", 0)
    # 可靠性：样本足够才生效(0%→0.3, 100%→1.0)，否则中性 1.0（冷启动不惩罚）
    reliability = (0.3 + 0.7 * s.get("ok_rate", 100.0) / 100.0) if calls >= _MIN_CALLS else 1.0
    score = reliability
    # 能力先验（模式测试的质量分 0-10）：乘子。质量优先策略放宽差异区间(0.5~1.0)，否则温和(0.7~1.0)。无数据中性
    if caps:
        comp = caps.get(p)
        if comp is not None:
            lo = 0.5 if (policy and policy.get("optimize_for") == "quality") else 0.7
            score *= lo + (1.0 - lo) * (max(0.0, min(10.0, comp)) / 10.0)
    if p == primary:
        score += PRIMARY_BONUS   # 主模型加成上限
    # 用户策略：免费/付费偏好 + 质量/价格优化
    if policy:
        tier = provider_tier(p)
        pref = policy.get("prefer_tier", "auto")
        opt = policy.get("optimize_for", "balanced")
        if pref == "free" and tier == "free":
            score += 0.5   # 免费优先（健康的免费模型排前；免费全熔断→付费自然升上来）
        elif pref == "paid" and tier == "paid":
            score += 0.2
        if opt == "price" and tier == "free":
            score += 0.3   # 价格优先＝偏向免费
        # optimize_for=='quality' 上面已放宽能力乘子区间
    # 熔断：**最终乘子**，压过一切加成（坏了就别硬顶）。放最后，确保已熔断者恒沉到健康 provider 之下，
    # 同时保留多个全熔断时的相对次序（不至无候选）。这是熔断唯一可靠的沉底点——若在加成前乘，
    # 后续加法(主模型/免费/价格 最高+1.1)会把死 provider 抬回顶部。
    if health.is_open(user_id, p):
        score *= 0.05
    return score


def rank_providers(providers, user_id: str = "", primary_provider: str = "",
                   policy: dict | None = None, role_key: str = "", stats=None, caps=None) -> list:
    """按 健康度×可靠性×能力先验 + 主模型加成 + 用户策略 对 provider 列表排序(高→低)。

    **无遥测/无能力数据/无策略时所有 provider 同分 → 稳定排序保持原顺序 → 平滑退化为现状。**
    feature flag 关闭时直接返回原列表。role_key 提供时加载该角色的能力分先验。
    """
    providers = list(providers)
    if not _ranking_enabled() or len(providers) < 2:
        return providers
    if stats is None:
        stats = _load_stats(user_id)
    if caps is None:
        caps = load_capability_scores(user_id, role_key) if role_key else {}
    primary = (primary_provider or "").lower().strip()
    policy = policy or {}
    # Python sorted 稳定：同分元素保持原相对顺序（无数据/无策略即原顺序）
    return sorted(providers, key=lambda p: _score(p, user_id, primary, stats, policy, caps), reverse=True)


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
    # 熔断必须压过一切加成（W1 回归）：已熔断的免费主模型，在 free+price 策略 + 满能力分下，
    # 仍须排在健康付费 provider 之后（否则冷却期每请求先打死模型白耗一轮）。
    health.reset()
    health.record_failure("u", "deepseek", "认证失败(密钥无效)")
    caps = {"deepseek": 10.0, "openai": 5.0}
    r = rank_providers(["deepseek", "openai"], "u", primary_provider="deepseek",
                       policy={"prefer_tier": "free", "optimize_for": "price"},
                       caps=caps, stats={})
    assert r == ["openai", "deepseek"], ("熔断被加成击穿！", r)
    health.reset()
    print("health selfcheck OK")


if __name__ == "__main__":
    _selfcheck()
