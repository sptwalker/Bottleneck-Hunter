"""日志上下文：request_id 与进程角色。

两个问题都源于"多进程/多请求共用一个 stderr"：
  1. 排查一次请求要看几十行交织的日志，无法归并 → request_id 贯穿。
  2. scheduler 跑在 FastAPI 进程内，与 API 日志混在一起 → 进程角色入格式串。

request_id 用 ContextVar 传递：中间件写入，formatter 读取，异步任务天然继承。
"""

from __future__ import annotations

import logging
import re
import uuid
from contextvars import ContextVar

_request_id: ContextVar[str] = ContextVar("request_id", default="-")
_role: ContextVar[str] = ContextVar("process_role", default="-")

# 只接受安全字符并限长——入站头不可信，避免日志注入与控制字符
_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")

LOG_FORMAT = "%(asctime)s %(levelname)s [%(process_role)s] [%(request_id)s] %(name)s | %(message)s"


def sanitize_request_id(value: str | None) -> str:
    """校验入站 request id；非法或缺失则重新生成。"""
    if value and _VALID_REQUEST_ID.match(value):
        return value
    return uuid.uuid4().hex[:16]


def get_request_id() -> str:
    return _request_id.get()


def reset_request_id(token: object) -> None:
    _request_id.reset(token)


def set_request_id(value: str) -> object:
    return _request_id.set(value)


def set_process_role(value: str) -> None:
    _role.set(value)


def get_process_role() -> str:
    return _role.get()


class RequestIdFilter(logging.Filter):
    """把 ContextVar 值注入 LogRecord，供 formatter 使用。"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = _request_id.get()
        record.process_role = _role.get()
        return True


def install_log_context(role: str = "api") -> None:
    """给已有 handler 装上 ContextVar 注入器。幂等，可重复调用。"""
    set_process_role(role)
    for handler in logging.getLogger().handlers:
        if not any(isinstance(f, RequestIdFilter) for f in handler.filters):
            handler.addFilter(RequestIdFilter())
