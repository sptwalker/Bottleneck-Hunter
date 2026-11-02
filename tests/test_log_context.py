"""日志上下文：request_id 贯穿与进程角色入格式串。"""

from __future__ import annotations

import logging

from bottleneck_hunter.web.log_context import (
    LOG_FORMAT,
    RequestIdFilter,
    get_process_role,
    get_request_id,
    install_log_context,
    sanitize_request_id,
    set_process_role,
    set_request_id,
)


def test_sanitize_accepts_valid_ids():
    assert sanitize_request_id("abc-123_X.Y:z") == "abc-123_X.Y:z"


def test_sanitize_rejects_and_regenerates():
    """非法字符/超长必须重新生成，否则可注入日志换行。"""
    injected = "bad\nid"
    assert sanitize_request_id(injected) != injected
    assert "\n" not in sanitize_request_id(injected)
    long_id = "a" * 200
    assert sanitize_request_id(long_id) != long_id
    assert sanitize_request_id("") != ""
    assert sanitize_request_id(None)


def test_contextvar_roundtrip():
    token = set_request_id("rid-42")
    try:
        assert get_request_id() == "rid-42"
    finally:
        from bottleneck_hunter.web.log_context import reset_request_id

        reset_request_id(token)


def test_filter_injects_fields():
    set_request_id("rid-7")
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "msg", None, None)
    assert RequestIdFilter().filter(record) is True
    assert record.request_id == "rid-7"
    assert hasattr(record, "process_role")


def test_install_log_context_is_idempotent():
    root = logging.getLogger()
    handler = logging.StreamHandler()
    root.addHandler(handler)
    try:
        install_log_context("test-role")
        install_log_context("test-role")
        filters = [f for f in handler.filters if isinstance(f, RequestIdFilter)]
        assert len(filters) == 1, "重复安装会产生重复 filter"
        assert get_process_role() == "test-role"
    finally:
        root.removeHandler(handler)
        set_process_role("-")


def test_format_renders_role_and_id():
    set_request_id("rid-9")
    set_process_role("scheduler")
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "hello", None, None)
    RequestIdFilter().filter(record)
    rendered = logging.Formatter(LOG_FORMAT).format(record)
    assert "[scheduler]" in rendered
    assert "[rid-9]" in rendered
    assert "hello" in rendered


def test_request_id_middleware_echoes_header():
    """带合法头 → 原样回写；不带 → 自动生成。"""
    from starlette.testclient import TestClient

    from bottleneck_hunter.web.request_id import RequestIdMiddleware

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    client = TestClient(RequestIdMiddleware(app))
    echoed = client.get("/", headers={"X-Request-ID": "given-id-1"})
    assert echoed.headers["x-request-id"] == "given-id-1"

    generated = client.get("/")
    assert generated.headers.get("x-request-id")
    assert generated.headers["x-request-id"] != "given-id-1"
