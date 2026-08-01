"""LLM 节点持久熔断（升级处理）——比 health.py 的进程内冷却更硬的一层。

health.py：某调用失败 → 冷却 N 秒后自动恢复（临时避让，无需人工介入）。
provider_gate（本模块）：**认证失败/限流严重** → 在配置中心**持久标注并禁用**该节点，
自动冷却不再放行，须用户**重新配置**（认证）或**重新配置 + 通过流量测试**（限流）才恢复。
根因见 Loki 归因：一个失效 key 每次调度都撞、冷却过期又撞，无限级联拖垮别的 provider。

策略（record_result）：
- 认证失败(密钥无效)      → 1 击即禁（disabled_auth）
- 频率限制/额度不足        → _RL_WINDOW 内累计 _RL_STRIKES 次才禁（disabled_ratelimit），躲开偶发限流
- 成功                    → 清该节点的限流 strike 计数（不动持久禁用，那须人工/测试恢复）

恢复：
- clear_auth_disable —— 仅清认证禁用（save_user_api_key 重存 key 时自动调；限流禁用保留）
- clear             —— 清任意禁用（/recover 端点过流量测试后调）

严格按用户隔离：key=(user_id, provider)，绝无全局共享（见 project_strict_key_isolation）。
ponytail: strike 计数 + is_disabled 判定走进程内 dict（单容器足够）；禁用本身已落库，
          重启只重置未达阈值的计数，安全。多 worker 需把 strike/缓存移 DB/Redis。
ponytail: disabled_info 走 30s TTL 缓存，多 worker 靠 TTL 最终一致。
"""
from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger(__name__)

_AUTH_REASON = "认证失败(密钥无效)"      # 必须与 fallback.classify_reason 完全一致
_RL_REASON = "频率限制/额度不足"
_STATUS_AUTH = "disabled_auth"
_STATUS_RL = "disabled_ratelimit"

_RL_STRIKES = 5        # 限流达此次数才判「严重」→ 禁用
_RL_WINDOW = 600.0     # 秒：strike 计数滑窗（超窗的旧 strike 丢弃）
_CACHE_TTL = 30.0      # 秒：is_disabled/disabled_info 进程内缓存 TTL

_lock = threading.Lock()
# 限流 strike 时间戳滑窗：(uid, provider) → [monotonic, ...]
_strikes: dict[tuple, list] = {}
# 禁用态缓存：(uid, provider) → (expire_monotonic, info|None)
_cache: dict[tuple, tuple] = {}


def _norm(provider: str) -> str:
    return (provider or "").lower().strip()


def _get_store():
    """AuthStore 实例（间接层便于单测注入 tmp 库）。"""
    from bottleneck_hunter.auth.store import AuthStore
    return AuthStore()


def _now() -> float:
    return time.monotonic()


def _invalidate(uid: str, provider: str) -> None:
    _cache.pop((uid or "", _norm(provider)), None)


def _read_info(uid: str, provider: str) -> dict | None:
    """读禁用态（带 TTL 缓存）。返回 store 行 dict 或 None。"""
    k = (uid or "", _norm(provider))
    now = _now()
    with _lock:
        hit = _cache.get(k)
        if hit and hit[0] > now:
            return hit[1]
    try:
        info = _get_store().get_llm_provider_health(uid or "", _norm(provider))
    except Exception:  # noqa: BLE001  读失败按「未禁用」放行，不因熔断表故障阻断全部调用
        info = None
    with _lock:
        _cache[k] = (now + _CACHE_TTL, info)
    return info


def is_disabled(uid: str, provider: str) -> bool:
    return _read_info(uid, provider) is not None


def disabled_info(uid: str, provider: str) -> dict | None:
    """返回 {provider,status,reason,detail,disabled_at} 或 None。供配置中心呈现。"""
    return _read_info(uid, provider)


def _do_disable(uid: str, provider: str, status: str, reason: str, detail: str) -> None:
    """落库禁用；仅在**由启用→禁用**的首次弹一次提示（已禁用不重复弹）。"""
    p = _norm(provider)
    already = is_disabled(uid, provider)
    try:
        _get_store().disable_llm_provider(uid or "", p, status, reason, detail)
    except Exception as e:  # noqa: BLE001
        logger.warning("禁用 LLM 节点落库失败 (%s/%s): %s", uid, p, e)
        return
    _invalidate(uid, provider)
    with _lock:
        _strikes.pop((uid or "", p), None)
    if not already:
        label = "密钥失效" if status == _STATUS_AUTH else "限流严重"
        try:
            from bottleneck_hunter.llm_clients.fallback import push_notice
            push_notice({
                "kind": "provider_disabled",
                "message": f"⛔ {p} 节点因{label}已被禁用，请在 AI 配置中心重新配置"
                           + ("并通过流量测试" if status == _STATUS_RL else ""),
                "provider": p,
                "status": status,
                "reason": reason,
            })
        except Exception:  # noqa: BLE001
            pass
        logger.warning("LLM 节点已禁用：%s/%s status=%s reason=%s", uid, p, status, reason)


def record_result(uid: str, provider: str, ok: bool, reason: str = "") -> None:
    """在 fallback._record_call 里对每个候选调用旁路调用。fail-silent。"""
    p = _norm(provider)
    if not p:
        return
    k = (uid or "", p)
    if ok:
        with _lock:
            _strikes.pop(k, None)   # 成功即重置限流计数（不动持久禁用）
        return
    if reason == _AUTH_REASON:
        _do_disable(uid, p, _STATUS_AUTH, reason, "")
        return
    if reason == _RL_REASON:
        now = _now()
        with _lock:
            win = [t for t in _strikes.get(k, []) if now - t < _RL_WINDOW]
            win.append(now)
            _strikes[k] = win
            hit = len(win) >= _RL_STRIKES
        if hit:
            _do_disable(uid, p, _STATUS_RL, reason, f"{_RL_WINDOW:.0f}s 内限流 {_RL_STRIKES}+ 次")
    # 其它原因（超时/连接/服务端）不升级为持久禁用——交给 health.py 的临时冷却即可


def clear(uid: str, provider: str) -> bool:
    """解除任意禁用（恢复端点过流量测试后调）。返回是否有记录被清。"""
    p = _norm(provider)
    try:
        cleared = _get_store().clear_llm_provider_health(uid or "", p)
    except Exception as e:  # noqa: BLE001
        logger.warning("解除 LLM 节点禁用失败 (%s/%s): %s", uid, p, e)
        cleared = False
    _invalidate(uid, provider)
    with _lock:
        _strikes.pop((uid or "", p), None)
    return cleared


def clear_auth_disable(uid: str, provider: str) -> bool:
    """仅当当前为「认证禁用」时解除（重存 key 时调）。限流禁用保留，须过流量测试。"""
    info = _read_info(uid, provider)
    if not info or info.get("status") != _STATUS_AUTH:
        return False
    return clear(uid, provider)


def _reset_for_test() -> None:
    """清空进程内 strike/缓存（单测隔离用）。"""
    with _lock:
        _strikes.clear()
        _cache.clear()


def _selfcheck() -> None:
    """内存态自检（注入 fake store，无 DB）：升级/清除/隔离/滑窗。"""
    class _FakeStore:
        def __init__(self):
            self.rows: dict[tuple, dict] = {}

        def disable_llm_provider(self, uid, prov, status, reason="", detail=""):
            self.rows[(uid, prov)] = {"provider": prov, "status": status,
                                      "reason": reason, "detail": detail, "disabled_at": "t"}

        def get_llm_provider_health(self, uid, prov):
            return self.rows.get((uid, prov))

        def clear_llm_provider_health(self, uid, prov):
            return self.rows.pop((uid, prov), None) is not None

    global _get_store
    orig = _get_store
    fake = _FakeStore()
    _get_store = lambda: fake  # noqa: E731
    try:
        _reset_for_test()
        # 认证：1 击即禁
        record_result("u1", "deepseek", False, _AUTH_REASON)
        assert is_disabled("u1", "deepseek")
        assert disabled_info("u1", "deepseek")["status"] == _STATUS_AUTH
        assert not is_disabled("u2", "deepseek")   # 按用户隔离
        # 重存 key（认证恢复）
        assert clear_auth_disable("u1", "deepseek")
        assert not is_disabled("u1", "deepseek")

        # 限流：未达阈值不禁，达阈值才禁
        _reset_for_test()
        for _ in range(_RL_STRIKES - 1):
            record_result("u1", "qwen", False, _RL_REASON)
        assert not is_disabled("u1", "qwen"), "未达阈值不应禁"
        record_result("u1", "qwen", False, _RL_REASON)   # 第 _RL_STRIKES 次
        assert is_disabled("u1", "qwen") and disabled_info("u1", "qwen")["status"] == _STATUS_RL

        # clear_auth_disable 不动限流禁用（须过测试）
        assert not clear_auth_disable("u1", "qwen")
        assert is_disabled("u1", "qwen")
        assert clear("u1", "qwen")               # clear 清任意
        assert not is_disabled("u1", "qwen")

        # 成功重置限流计数：达阈值-1 次后成功一次，再撞不应立即禁
        _reset_for_test()
        for _ in range(_RL_STRIKES - 1):
            record_result("u1", "glm", False, _RL_REASON)
        record_result("u1", "glm", True)         # 成功清零
        record_result("u1", "glm", False, _RL_REASON)
        assert not is_disabled("u1", "glm"), "成功后应重新计数"
        print("provider_gate selfcheck OK")
    finally:
        _get_store = orig
        _reset_for_test()


if __name__ == "__main__":
    _selfcheck()
