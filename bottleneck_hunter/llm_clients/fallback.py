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
from contextvars import ContextVar

from langchain_core.language_models import BaseChatModel
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from pydantic import ConfigDict

logger = logging.getLogger(__name__)

# ── 提示 sink（请求级）──────────────────────────────────
_notices: ContextVar[list | None] = ContextVar("llm_fallback_notices", default=None)


def begin_notices() -> None:
    """请求入口调用：开启一次收集（后续 push 才会被记录）。"""
    _notices.set([])


def push_notice(notice: dict) -> None:
    lst = _notices.get()
    if lst is not None:
        lst.append(notice)


def drain_notices() -> list[dict]:
    """取出并清空当前收集到的提示。"""
    lst = _notices.get()
    if not lst:
        return []
    out = list(lst)
    lst.clear()
    return out


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
            try:
                msg = await llm.ainvoke(messages, stop=stop, **kwargs)
                if i > 0:
                    self._notify(first_reason or "调用异常", provider, model)
                return ChatResult(generations=[ChatGeneration(message=msg)])
            except Exception as e:  # noqa: BLE001 - 逐候选降级
                last_exc = e
                reason = classify_reason(e)
                if i == 0:
                    first_reason = reason
                logger.warning("候选模型 %s/%s 调用失败(%s): %s", provider, model, reason, e)
        raise last_exc

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        last_exc = None
        first_reason = None
        for i, (llm, provider, model) in enumerate(self.candidates):
            emitted = False
            try:
                async for chunk in llm.astream(messages, stop=stop, **kwargs):
                    emitted = True
                    yield ChatGenerationChunk(message=chunk)
                if i > 0:
                    self._notify(first_reason or "调用异常", provider, model)
                return
            except Exception as e:  # noqa: BLE001
                if emitted:
                    raise  # 已吐出部分 token，无法安全重启，交由上层处理
                last_exc = e
                reason = classify_reason(e)
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
            try:
                msg = llm.invoke(messages, stop=stop, **kwargs)
                if i > 0:
                    self._notify(first_reason or "调用异常", provider, model)
                return ChatResult(generations=[ChatGeneration(message=msg)])
            except Exception as e:  # noqa: BLE001
                last_exc = e
                reason = classify_reason(e)
                if i == 0:
                    first_reason = reason
                logger.warning("候选模型 %s/%s 调用失败(%s): %s", provider, model, reason, e)
        raise last_exc

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):
        last_exc = None
        first_reason = None
        for i, (llm, provider, model) in enumerate(self.candidates):
            emitted = False
            try:
                for chunk in llm.stream(messages, stop=stop, **kwargs):
                    emitted = True
                    yield ChatGenerationChunk(message=chunk)
                if i > 0:
                    self._notify(first_reason or "调用异常", provider, model)
                return
            except Exception as e:  # noqa: BLE001
                if emitted:
                    raise
                last_exc = e
                reason = classify_reason(e)
                if i == 0:
                    first_reason = reason
                logger.warning("候选模型 %s/%s 流式失败(%s): %s", provider, model, reason, e)
        if last_exc:
            raise last_exc


def build_fallback_candidates(primary_provider: str, primary_model: str,
                              user_id: str = "", temperature: float = 0.3) -> list:
    """构造备选候选列表（不含主模型）：全局「主要」provider 前置，其后接 _FALLBACK_CHAIN；
    仅取当前用户已配 KEY、启用中、且不同于主模型的 provider（严格隔离 + 跳过被禁用）。"""
    # 延迟导入避免与 factory 循环依赖
    from bottleneck_hunter.llm_clients.factory import (
        _FALLBACK_CHAIN, _user_has_llm_key, create_llm, resolve_provider_model,
        is_provider_active, get_primary_provider,
    )
    from bottleneck_hunter.auth.current_user import get_current_user_id

    uid = user_id or get_current_user_id()
    out = []
    primary = (primary_provider or "").lower().strip()
    # 主要 provider 前置到备选链首，实现「其它模型失效自动替换为主要」
    chain = ([get_primary_provider()] if get_primary_provider() else []) + [p for p, _ in _FALLBACK_CHAIN]
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
