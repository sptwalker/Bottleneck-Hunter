"""AI 模型调用失败自动替换 + 用户提示。

FallbackChatModel 包住一串候选模型：主模型调用失败时自动换用备选重试，
并把「已替换」提示写入一个 ContextVar sink，供各请求渠道 drain 出来提示用户。

设计要点见 docs / 计划：
- 只有「换到备选并成功」才 push 提示；主模型直接成功不提示。
- 全部失败 → 抛最后一个异常，保持既有 try/except 降级行为。
- sink 用 ContextVar，自动穿透 async 调用树（含 asyncio.gather 子任务），
  避免给几十处调用点加参数。后台任务未 begin_notices() 时提示只落日志。
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextvars import ContextVar

from langchain_core.language_models import BaseChatModel
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from pydantic import ConfigDict

logger = logging.getLogger(__name__)

# ── 提示 sink（请求级）──────────────────────────────────
_notices: ContextVar[list | None] = ContextVar("llm_fallback_notices", default=None)
# ── 使用清单 sink（请求级）：记录本次实际用到的 (provider, model) ──
_usage: ContextVar[list | None] = ContextVar("llm_usage", default=None)


def begin_notices() -> None:
    """请求入口调用：开启一次收集（后续 push 才会被记录）。"""
    _notices.set([])
    _usage.set([])


def push_notice(notice: dict) -> None:
    lst = _notices.get()
    if lst is not None:
        lst.append(notice)


def _push_usage(provider: str, model: str) -> None:
    lst = _usage.get()
    if lst is not None:
        lst.append((provider, model))


def drain_usage() -> dict | None:
    """汇总本次各模型使用次数：{"summary": "deepseek×3, qwen×1", "items": [...]}。无则 None。"""
    lst = _usage.get()
    if not lst:
        return None
    counts: dict[tuple, int] = {}
    for pm in lst:
        counts[pm] = counts.get(pm, 0) + 1
    items = [{"provider": p, "model": m, "count": c} for (p, m), c in counts.items()]
    items.sort(key=lambda x: -x["count"])
    summary = "、".join(f"{it['provider']}×{it['count']}" if it["count"] > 1 else it["provider"] for it in items)
    lst.clear()
    return {"summary": summary, "items": items}


def drain_notices() -> list[dict]:
    """取出并清空当前收集到的提示。"""
    lst = _notices.get()
    if not lst:
        return []
    out = list(lst)
    lst.clear()
    return out


# ── 调用遥测（智能调度 Phase 0 地基）─────────────────────
# 每个候选调用在结局处旁路记一条（成败/延迟/原因），喂养 Phase 1 的健康度/速度排序。
# 严格 fail-silent：遥测异常绝不影响 LLM 主链路。
_metric_store = None


def _get_metric_store():
    global _metric_store
    if _metric_store is None:
        from bottleneck_hunter.watchlist.store import WatchlistStore
        _metric_store = WatchlistStore()  # 缓存一份（构造含 schema 迁移），record 用各自的 _write_conn
    return _metric_store


def _validate(msg) -> tuple[bool, str]:
    """输出格式校验（Phase 2）。fail-silent：校验层异常绝不影响主链路（放行）。"""
    try:
        from bottleneck_hunter.llm_clients.validate import validate_output
        return validate_output(msg)
    except Exception:  # noqa: BLE001
        return True, ""


def _record_call(provider: str, model: str, ok: bool, t0: float, reason: str = "") -> None:
    """旁路记一次候选调用遥测 + 更新运行时健康度（fail-silent）。
    ponytail: 同步旁路写；LLM 调用秒级、频率低，撞 _write_lock 概率极小。
    高并发场景再改内存滑窗聚合 / asyncio.to_thread 异步落盘。"""
    import sys
    try:
        from bottleneck_hunter.auth.current_user import get_current_user_id
        uid = get_current_user_id()
    except Exception:  # noqa: BLE001
        uid = ""
    # 健康度：内存态，始终更新（含测试），供 rank_providers 熔断沉底
    try:
        from bottleneck_hunter.llm_clients.health import health
        if ok:
            health.record_success(uid, provider)
        else:
            health.record_failure(uid, provider, reason)
    except Exception:  # noqa: BLE001
        pass
    # 遥测落库：测试运行中跳过，避免假模型数据污染 Phase 1 排序
    if "pytest" in sys.modules:
        return
    try:
        latency_ms = (time.monotonic() - t0) * 1000
        _get_metric_store().record_model_call(
            provider, model, ok, latency_ms=latency_ms, reason=reason, user_id=uid,
        )
    except Exception:  # noqa: BLE001
        pass


def classify_reason(exc: Exception) -> str:
    """把异常映射成面向用户的中文短语。"""
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return "请求超时"
    msg = str(exc).lower()
    status = getattr(exc, "status_code", None) or getattr(exc, "http_status", None)
    if status in (401, 403) or any(k in msg for k in ("api key", "api_key", "unauthorized", "authentication", "invalid key", "permission")):
        return "认证失败(密钥无效)"
    if status == 429 or any(k in msg for k in ("rate limit", "rate_limit", "too many requests", "quota", "余额", "insufficient")):
        return "频率限制/额度不足"
    if isinstance(exc, (ConnectionError, OSError)) or any(k in msg for k in ("connection", "connect", "getaddrinfo", "network", "remotedisconnected")):
        return "连接失败"
    if isinstance(exc, asyncio.CancelledError):
        return "调用被取消"
    if isinstance(status, int) and 500 <= status < 600 or "internal server" in msg or "bad gateway" in msg or "service unavailable" in msg:
        return "服务端错误"
    if "timeout" in msg or "timed out" in msg:
        return "请求超时"
    return "调用异常"


def _build_message(fp: str, fm: str, reason: str, np: str, nm: str) -> dict:
    return {
        "kind": "model_fallback",
        "message": f"⚠️ {fp}/{fm} 模型因{reason}调用失败，本次已自动替换为 {np}/{nm} 模型",
        "failed": f"{fp}/{fm}",
        "replaced": f"{np}/{nm}",
        "reason": reason,
    }


class FallbackChatModel(BaseChatModel):
    """按顺序尝试候选 `[(llm, provider, model), ...]`，失败即换下一个并提示。"""

    model_config = ConfigDict(arbitrary_types_allowed=True)
    candidates: list

    @property
    def _llm_type(self) -> str:
        return "fallback"

    def _notify(self, first_reason: str, win_provider: str, win_model: str) -> None:
        fp, fm = self.candidates[0][1], self.candidates[0][2]
        notice = _build_message(fp, fm, first_reason, win_provider, win_model)
        push_notice(notice)
        logger.warning("模型自动替换：%s", notice["message"])

    # ── async（主用路径）──────────────────────────────
    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        last_exc = None
        first_reason = None
        for i, (llm, provider, model) in enumerate(self.candidates):
            t0 = time.monotonic()
            try:
                msg = await llm.ainvoke(messages, stop=stop, **kwargs)
                vok, vreason = _validate(msg)
                is_last = i == len(self.candidates) - 1
                accept = vok or is_last  # 末候选即便格式不佳也接受，不为格式问题整体失败
                # 接受即视为该次可用 → 记成功、不开熔断（末候选被返回给用户，是"用了它"不是"它挂了"）
                _record_call(provider, model, accept, t0, "" if accept else vreason)
                if accept:
                    _push_usage(provider, model)
                    if i > 0:
                        self._notify(first_reason or "调用异常", provider, model)
                    return ChatResult(generations=[ChatGeneration(message=msg)])
                # 输出格式不合格且有下一候选 → 视同失败，换模型
                last_exc = last_exc or ValueError(vreason)
                if i == 0:
                    first_reason = vreason
                logger.warning("候选模型 %s/%s 输出校验不合格(%s)，尝试下一候选", provider, model, vreason)
            except Exception as e:  # noqa: BLE001 - 逐候选降级
                last_exc = e
                reason = classify_reason(e)
                _record_call(provider, model, False, t0, reason)
                if i == 0:
                    first_reason = reason
                logger.warning("候选模型 %s/%s 调用失败(%s): %s", provider, model, reason, e)
        raise last_exc

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        last_exc = None
        first_reason = None
        for i, (llm, provider, model) in enumerate(self.candidates):
            emitted = False
            t0 = time.monotonic()
            try:
                async for chunk in llm.astream(messages, stop=stop, **kwargs):
                    emitted = True
                    yield ChatGenerationChunk(message=chunk)
                _record_call(provider, model, True, t0)
                _push_usage(provider, model)
                if i > 0:
                    self._notify(first_reason or "调用异常", provider, model)
                return
            except Exception as e:  # noqa: BLE001
                reason = classify_reason(e)
                _record_call(provider, model, False, t0, reason)
                if emitted:
                    raise  # 已吐出部分 token，无法安全重启，交由上层处理
                last_exc = e
                if i == 0:
                    first_reason = reason
                logger.warning("候选模型 %s/%s 流式失败(%s): %s", provider, model, reason, e)
        if last_exc:
            raise last_exc

    # ── sync（少数同步调用路径，如 asyncio.to_thread(lambda: llm.invoke(...))）──
    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        last_exc = None
        first_reason = None
        for i, (llm, provider, model) in enumerate(self.candidates):
            t0 = time.monotonic()
            try:
                msg = llm.invoke(messages, stop=stop, **kwargs)
                vok, vreason = _validate(msg)
                is_last = i == len(self.candidates) - 1
                accept = vok or is_last
                _record_call(provider, model, accept, t0, "" if accept else vreason)
                if accept:
                    _push_usage(provider, model)
                    if i > 0:
                        self._notify(first_reason or "调用异常", provider, model)
                    return ChatResult(generations=[ChatGeneration(message=msg)])
                last_exc = last_exc or ValueError(vreason)
                if i == 0:
                    first_reason = vreason
                logger.warning("候选模型 %s/%s 输出校验不合格(%s)，尝试下一候选", provider, model, vreason)
            except Exception as e:  # noqa: BLE001
                last_exc = e
                reason = classify_reason(e)
                _record_call(provider, model, False, t0, reason)
                if i == 0:
                    first_reason = reason
                logger.warning("候选模型 %s/%s 调用失败(%s): %s", provider, model, reason, e)
        raise last_exc

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):
        last_exc = None
        first_reason = None
        for i, (llm, provider, model) in enumerate(self.candidates):
            emitted = False
            t0 = time.monotonic()
            try:
                for chunk in llm.stream(messages, stop=stop, **kwargs):
                    emitted = True
                    yield ChatGenerationChunk(message=chunk)
                _record_call(provider, model, True, t0)
                _push_usage(provider, model)
                if i > 0:
                    self._notify(first_reason or "调用异常", provider, model)
                return
            except Exception as e:  # noqa: BLE001
                reason = classify_reason(e)
                _record_call(provider, model, False, t0, reason)
                if emitted:
                    raise
                last_exc = e
                if i == 0:
                    first_reason = reason
                logger.warning("候选模型 %s/%s 流式失败(%s): %s", provider, model, reason, e)
        if last_exc:
            raise last_exc


def build_fallback_candidates(primary_provider: str, primary_model: str,
                              user_id: str = "", temperature: float = 0.3) -> list:
    """构造备选候选列表（不含主模型）：全局「主要」provider 前置，其后接用户全部已注册
    provider + 应急链；仅取当前用户已配 KEY、启用中、且不同于主模型的 provider
    （严格隔离 + 跳过被禁用）。不再只提供硬编码 4 家应急链——否则主模型失效时，
    用户配的其它 provider 无法被自动替换。"""
    # 延迟导入避免与 factory 循环依赖
    from bottleneck_hunter.llm_clients.factory import (
        _FALLBACK_CHAIN, _user_has_llm_key, create_llm, resolve_provider_model,
        is_provider_active, get_primary_provider, list_custom_provider_ids,
    )
    from bottleneck_hunter.auth.current_user import get_current_user_id

    uid = user_id or get_current_user_id()
    out = []
    primary = (primary_provider or "").lower().strip()
    try:
        universe = list_custom_provider_ids()
    except Exception:
        universe = []
    # 备选链 = 全局主要 provider 前置 + 用户全部已注册 provider + 应急链兜底
    chain = ([get_primary_provider()] if get_primary_provider() else []) + list(universe) + [p for p, _ in _FALLBACK_CHAIN]
    seen: set[str] = set()
    for provider in chain:
        provider = (provider or "").lower().strip()
        if not provider or provider == primary or provider in seen:
            continue
        seen.add(provider)
        if not is_provider_active(provider):  # 跳过已被管理员禁用的 provider
            continue
        if not _user_has_llm_key(provider, uid):  # 严格：只用当前用户自己配了 KEY 的备选
            continue
        model = resolve_provider_model(provider, uid)
        if not model:
            continue
        try:
            llm = create_llm(provider, model, temperature=temperature, with_fallback=False, user_id=uid)
            out.append((llm, provider, model))
        except Exception:  # noqa: BLE001
            continue
    # 按运行时健康度/遥测/用户策略对备选重排：熔断中/低成功率的沉底，主模型加成保持粘性。
    # build_fallback_candidates 不知具体角色，用全局策略(role_key='')。
    # 无数据/无策略 → rank_providers 稳定排序保持原顺序（平滑退化为现状）。
    try:
        from bottleneck_hunter.llm_clients.health import rank_providers, load_routing_policy
        policy = load_routing_policy(uid, "")
        ranked = rank_providers([c[1] for c in out], uid, get_primary_provider(), policy=policy)
        pos = {p: i for i, p in enumerate(ranked)}
        out.sort(key=lambda c: pos.get(c[1], 999))
    except Exception:  # noqa: BLE001
        pass
    return out


def _selfcheck() -> None:
    """assert 自检：主模型必失败 → 应换备选成功且产生一条提示。"""
    class _Boom:
        async def ainvoke(self, *a, **k):
            raise TimeoutError("simulated timeout")

    class _OK:
        async def ainvoke(self, *a, **k):
            from langchain_core.messages import AIMessage
            return AIMessage(content="ok")

    begin_notices()
    fb = FallbackChatModel(candidates=[(_Boom(), "deepseek", "deepseek-chat"),
                                       (_OK(), "qwen", "qwen-plus")])
    res = asyncio.run(fb.ainvoke("hi"))
    assert res.content == "ok", res
    notes = drain_notices()
    assert len(notes) == 1 and notes[0]["kind"] == "model_fallback", notes
    assert "已自动替换为 qwen/qwen-plus" in notes[0]["message"], notes[0]["message"]
    assert notes[0]["reason"] == "请求超时", notes[0]
    print("fallback selfcheck OK; reason=", notes[0]["reason"], "replaced=", notes[0]["replaced"])


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    _selfcheck()
