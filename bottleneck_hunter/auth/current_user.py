"""请求级「当前用户」上下文。

用一个 ContextVar 承载当前请求/任务归属的 user_id，让下游（尤其 LLM/数据源
Key 解析）无需在 20+ 处函数签名里穿透 user_id 即可拿到它。

注入点：
- Web：AuthMiddleware 解析出 JWT 用户后 set_current_user(sub)。
- 调度器：per-uid 任务循环里 set_current_user(uid)。
- CLI：显式 BH_CLI_USER_ID。

asyncio.to_thread 在 3.11+ 会 copy_context，故跨线程的 llm.invoke 也读得到。
严格隔离：拿不到 user_id 即「无归属」，Key 解析应当失败而非兜底到全局。
"""

from __future__ import annotations

from contextvars import ContextVar, Token

current_user_id: ContextVar[str] = ContextVar("current_user_id", default="")


def set_current_user(user_id: str) -> Token:
    """设置当前上下文用户，返回 token 供 reset。"""
    return current_user_id.set(user_id or "")


def get_current_user_id() -> str:
    """取当前上下文 user_id（未设置返回空串）。"""
    return current_user_id.get()


def reset_current_user(token: Token) -> None:
    try:
        current_user_id.reset(token)
    except (ValueError, LookupError):
        # 不同上下文（如跨线程）reset 可能失败，忽略即可
        pass
