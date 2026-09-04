"""LLM 节点持久熔断（升级处理）——比 health.py 的进程内冷却更硬的一层。

health.py：某调用失败 → 冷却 N 秒后自动恢复（临时避让，无需人工介入）。
provider_gate（本模块）：**认证失败/限流严重** → 在配置中心**持久标注并禁用**该节点，
自动冷却不再放行，须用户**重新配置**（认证）或**重新配置 + 通过流量测试**（限流）才恢复。
根因见 Loki 归因：一个失效 key 每次调度都撞、冷却过期又撞，无限级联拖垮别的 provider。

策略（record_result）：
- 认证失败(密钥无效)      → 1 击即禁（disabled_auth）
- 频率限制/额度不足        → _RL_WINDOW 内累计 _RL_STRIKES 次才禁（disabled_ratelimit），躲开偶发限流
- 请求超时                → _TIMEOUT_WINDOW 内累计 _TIMEOUT_STRIKES(3) 次才禁（disabled_timeout），躲开偶发抖动
- 成功                    → 清该节点的 strike 计数（不动持久禁用，那须人工/测试恢复）

恢复：
- clear_auth_disable —— 仅清认证禁用（save_user_api_key 重存 key 时自动调；限流/超时禁用保留）
- clear             —— 清任意禁用（/recover 端点过流量测试后调）
限流与超时同属「须过流量测试才恢复」——重存 key 不清，须在配置中心跑流量测试通过。

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
_TIMEOUT_REASON = "请求超时"            # 同上：与 classify_reason 完全一致
_ARREARS_REASON = "余额欠费"            # 同上：与 classify_reason 完全一致
_STATUS_AUTH = "disabled_auth"
_STATUS_RL = "disabled_ratelimit"
_STATUS_TIMEOUT = "disabled_timeout"
_STATUS_ARREARS = "disabled_arrears"

_RL_STRIKES = 5        # 限流达此次数才判「严重」→ 禁用
_RL_WINDOW = 600.0     # 秒：strike 计数滑窗（超窗的旧 strike 丢弃）
_TIMEOUT_STRIKES = 3   # 超时达此次数（用户指定：3 次以上）→ 禁用
_TIMEOUT_WINDOW = 600.0
_CACHE_TTL = 30.0      # 秒：is_disabled/disabled_info 进程内缓存 TTL

# 禁用态展示标签 / 须过流量测试才恢复的状态（认证仅重存 key 即恢复；限流+超时+欠费须测试）
_STATUS_LABEL = {_STATUS_AUTH: "密钥失效", _STATUS_RL: "限流严重",
                 _STATUS_TIMEOUT: "超时频发", _STATUS_ARREARS: "余额欠费"}
_TEST_REQUIRED = (_STATUS_RL, _STATUS_TIMEOUT, _STATUS_ARREARS)

_lock = threading.Lock()
# strike 时间戳滑窗，按类别隔离：(uid, provider, cat) → [monotonic, ...]，cat ∈ {"rl","to"}
# 分类隔离确保 2 次超时 + 3 次限流不会相加误触发某一阈值。
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


def _bump_strike(uid: str, provider: str, cat: str, window: float, threshold: int) -> bool:
    """记一次某类(cat)失败 strike，返回是否达阈值。按 (uid,provider,cat) 隔离。"""
    k = (uid or "", _norm(provider), cat)
    now = _now()
    with _lock:
        win = [t for t in _strikes.get(k, []) if now - t < window]
        win.append(now)
        _strikes[k] = win
        return len(win) >= threshold


def _clear_strikes(uid: str, provider: str) -> None:
    """清该节点全部类别的 strike 计数（成功/禁用/解除时调）。"""
    p = _norm(provider)
    with _lock:
        for cat in ("rl", "to"):
            _strikes.pop((uid or "", p, cat), None)


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


# 「硬死」禁用：key 真无效（认证失效）或没钱（欠费）——任何重试必再失败，绝境兜底也不该碰。
# 区别于超时/限流：那多为瞬时抖动被累计升级，绝境时（全队被禁）宁可重试它也别让决策链整体停摆。
_HARD_STATUSES = (_STATUS_AUTH, _STATUS_ARREARS)


def is_hard_disabled(uid: str, provider: str) -> bool:
    """是否「硬死」禁用（认证失效/欠费）。供 factory 绝境兜底 pass 判定哪些节点连重试都免。"""
    info = _read_info(uid, provider)
    return bool(info and info.get("status") in _HARD_STATUSES)


def disabled_info(uid: str, provider: str) -> dict | None:
    """返回 {provider,status,reason,detail,disabled_at} 或 None。供配置中心呈现。"""
    return _read_info(uid, provider)


def _do_disable(uid: str, provider: str, status: str, reason: str, detail: str,
                arrears_flipped: bool = False) -> None:
    """落库禁用；仅在**由启用→禁用**的首次弹一次提示（已禁用不重复弹）。
    arrears_flipped=True 表示该节点因欠费刚被从免费翻成付费档，提示文案随之调整。"""
    p = _norm(provider)
    already = is_disabled(uid, provider)
    try:
        _get_store().disable_llm_provider(uid or "", p, status, reason, detail)
    except Exception as e:  # noqa: BLE001
        logger.warning("禁用 LLM 节点落库失败 (%s/%s): %s", uid, p, e)
        return
    _invalidate(uid, provider)
    _clear_strikes(uid, provider)
    if not already:
        label = _STATUS_LABEL.get(status, "异常")
        if status == _STATUS_ARREARS:
            if arrears_flipped:
                msg = f"⛔ {p} 出现欠费，已判定为**付费模型**并暂停调用，请充值后在 AI 配置中心「测试并恢复」"
            else:
                msg = f"⛔ {p} 节点余额不足/欠费已暂停调用，请充值后在 AI 配置中心「测试并恢复」"
        else:
            msg = (f"⛔ {p} 节点因{label}已被禁用，请在 AI 配置中心重新配置"
                   + ("并通过流量测试" if status in _TEST_REQUIRED else ""))
        try:
            from bottleneck_hunter.llm_clients.fallback import push_notice
            push_notice({
                "kind": "provider_disabled",
                "message": msg,
                "provider": p,
                "status": status,
                "reason": reason,
            })
        except Exception:  # noqa: BLE001
            pass
        _clear_primary_if_matches(uid or "", p, label, status)
        logger.warning("LLM 节点已禁用：%s/%s status=%s reason=%s", uid, p, status, reason)


def _clear_primary_if_matches(uid: str, provider: str, label: str, status: str) -> None:
    """被禁的正是该用户的主模型 → 用户级取消其主模型 + 提示重选（严格用户级，绝不波及他人）。
    惰性 import 断开 provider_gate → factory/store 的循环依赖。fail-silent。"""
    if not uid:
        return
    try:
        from bottleneck_hunter.llm_clients.factory import resolve_primary_for_user
        if _norm(resolve_primary_for_user(uid)) != provider:
            return
        from bottleneck_hunter.watchlist.store import WatchlistStore
        WatchlistStore().set_provider_config_primary("", uid)  # 暂时取消主模型设置
    except Exception as e:  # noqa: BLE001
        logger.warning("主模型失效自动取消失败 (%s/%s): %s", uid, provider, e)
        return
    try:
        from bottleneck_hunter.llm_clients.fallback import push_notice
        push_notice({
            "kind": "primary_failed",
            "message": f"主模型 {provider} 已失效（{label}），已暂时取消主模型设置，请在 AI 配置中心重新指定主节点",
            "provider": provider,
            "status": status,
        })
    except Exception:  # noqa: BLE001
        pass
    logger.warning("用户 %s 主模型 %s 失效，已自动取消主模型设置", uid, provider)


def _flip_free_to_paid_on_arrears(uid: str, provider: str) -> bool:
    """欠费触发时：若该用户对该 provider 的**有效档**当前为免费，翻成付费并返回 True。
    免费模型不该欠费——欠费即证明其实是付费。惰性 import 断循环依赖；fail-silent，
    翻档失败不阻断后续熔断落库。返回是否发生翻档（供提示文案区分）。"""
    if not uid:
        return False
    try:
        from bottleneck_hunter.llm_clients.health import provider_tier
        if provider_tier(provider, uid) != "free":
            return False
        from bottleneck_hunter.watchlist.store import WatchlistStore
        WatchlistStore().set_provider_config_tier(provider, "paid", uid)
        logger.warning("用户 %s 的 %s 出现欠费，付费类型已自动 免费→付费", uid, provider)
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("欠费自动翻档失败 (%s/%s): %s", uid, provider, e)
        return False


def record_result(uid: str, provider: str, ok: bool, reason: str = "") -> None:
    """在 fallback._record_call 里对每个候选调用旁路调用。fail-silent。"""
    p = _norm(provider)
    if not p:
        return
    if ok:
        _clear_strikes(uid, p)   # 成功即重置该节点所有 strike（不动持久禁用）
        return
    if reason == _AUTH_REASON:
        _do_disable(uid, p, _STATUS_AUTH, reason, "")
        return
    if reason == _ARREARS_REASON:
        # 欠费是持久性问题（钱没了不会自愈），1 击即禁，须充值后过流量测试恢复。
        # 免费模型本不该欠费——触发即证明其实是付费，自动翻档 free→paid（用户已定：照常熔断+翻档）。
        flipped = _flip_free_to_paid_on_arrears(uid, p)
        _do_disable(uid, p, _STATUS_ARREARS, reason, "", arrears_flipped=flipped)
        return
    if reason == _RL_REASON:
        if _bump_strike(uid, p, "rl", _RL_WINDOW, _RL_STRIKES):
            _do_disable(uid, p, _STATUS_RL, reason, f"{_RL_WINDOW:.0f}s 内限流 {_RL_STRIKES}+ 次")
        return
    if reason == _TIMEOUT_REASON:
        if _bump_strike(uid, p, "to", _TIMEOUT_WINDOW, _TIMEOUT_STRIKES):
            _do_disable(uid, p, _STATUS_TIMEOUT, reason, f"{_TIMEOUT_WINDOW:.0f}s 内超时 {_TIMEOUT_STRIKES}+ 次")
        return
    # 其它原因（连接/服务端 5xx 等）不升级为持久禁用——交给 health.py 的临时冷却即可


def clear(uid: str, provider: str) -> bool:
    """解除任意禁用（恢复端点过流量测试后调）。返回是否有记录被清。"""
    p = _norm(provider)
    try:
        cleared = _get_store().clear_llm_provider_health(uid or "", p)
    except Exception as e:  # noqa: BLE001
        logger.warning("解除 LLM 节点禁用失败 (%s/%s): %s", uid, p, e)
        cleared = False
    _invalidate(uid, provider)
    _clear_strikes(uid, provider)
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

    global _get_store, _flip_free_to_paid_on_arrears
    orig = _get_store
    orig_flip = _flip_free_to_paid_on_arrears
    fake = _FakeStore()
    _get_store = lambda: fake  # noqa: E731
    flip_calls: list = []
    # fake 翻档钩子：selfcheck 保持纯内存（不连真 WatchlistStore/health）；只验证 arrears 会触发它。
    # 翻档内部逻辑(provider_tier=='free' 才翻)由 pytest 用 fake store 覆盖。
    _flip_free_to_paid_on_arrears = lambda u, pv: (flip_calls.append((u, pv)) or True)  # noqa: E731
    try:
        _reset_for_test()
        # 认证：1 击即禁
        record_result("u1", "deepseek", False, _AUTH_REASON)
        assert is_disabled("u1", "deepseek")
        assert disabled_info("u1", "deepseek")["status"] == _STATUS_AUTH
        assert is_hard_disabled("u1", "deepseek")   # 认证=硬死
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

        # 超时：未达阈值不禁，达 _TIMEOUT_STRIKES(3) 才禁；且须过流量测试（重存 key 不清）
        _reset_for_test()
        for _ in range(_TIMEOUT_STRIKES - 1):
            record_result("u1", "kimi", False, _TIMEOUT_REASON)
        assert not is_disabled("u1", "kimi"), "未达超时阈值不应禁"
        record_result("u1", "kimi", False, _TIMEOUT_REASON)   # 第 _TIMEOUT_STRIKES 次
        assert is_disabled("u1", "kimi") and disabled_info("u1", "kimi")["status"] == _STATUS_TIMEOUT
        assert not is_hard_disabled("u1", "kimi")   # 超时=非硬死（绝境可重试）
        assert not clear_auth_disable("u1", "kimi"), "超时禁用不因重存 key 而清"
        assert is_disabled("u1", "kimi")
        assert clear("u1", "kimi")                # 过流量测试后 clear 清除
        assert not is_disabled("u1", "kimi")

        # 欠费：1 击即禁；但须过流量测试才恢复（重存 key 不清，与认证不同）
        _reset_for_test()
        flip_calls.clear()
        record_result("u1", "openai", False, _ARREARS_REASON)
        assert is_disabled("u1", "openai") and disabled_info("u1", "openai")["status"] == _STATUS_ARREARS
        assert is_hard_disabled("u1", "openai")   # 欠费=硬死
        assert flip_calls == [("u1", "openai")], ("欠费须触发翻档钩子", flip_calls)  # arrears→翻档连接
        assert not clear_auth_disable("u1", "openai"), "欠费禁用不因重存 key 而清"
        assert is_disabled("u1", "openai")
        assert clear("u1", "openai")               # 充值后过流量测试 clear 清除
        assert not is_disabled("u1", "openai")

        # 类别隔离：2 次超时 + 4 次限流（各差 1 达阈值）不应相加误触发
        _reset_for_test()
        for _ in range(_TIMEOUT_STRIKES - 1):
            record_result("u1", "mix", False, _TIMEOUT_REASON)
        for _ in range(_RL_STRIKES - 1):
            record_result("u1", "mix", False, _RL_REASON)
        assert not is_disabled("u1", "mix"), "两类 strike 不应跨类别相加触发禁用"
        print("provider_gate selfcheck OK")
    finally:
        _get_store = orig
        _flip_free_to_paid_on_arrears = orig_flip
        _reset_for_test()


if __name__ == "__main__":
    _selfcheck()
