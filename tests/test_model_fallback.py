"""Tests for the AI model auto-fallback + user-notice feature.

覆盖:
- FallbackChatModel: 主成功不提示 / 主失败换备选成功并提示 / 全失败抛错 / async+sync+stream
- classify_reason: 异常 → 中文原因
- ContextVar sink: begin/push/drain 隔离，未 begin 时 push 静默
- build_fallback_candidates: 排除主 provider、仅取有 key 的
- factory.create_llm: with_fallback 包壳/不包壳/无备选降级
- get_models_for_role(默认不包壳) vs get_llm_for_position(包壳)
- web.streaming._notice.with_notices: 穿插 model_fallback 事件
- committee._invoke_with_retry: 切备用模型时 push 提示
"""

import asyncio
import json

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk

import bottleneck_hunter.llm_clients.factory as F
from bottleneck_hunter.llm_clients import fallback as FB
from bottleneck_hunter.llm_clients.fallback import (
    FallbackChatModel, begin_notices, drain_notices, push_notice,
    classify_reason, _build_message,
)


# ── 假 LLM ────────────────────────────────────────────────
class BoomLLM:
    def __init__(self, exc=None):
        self.exc = exc or TimeoutError("simulated timeout")

    async def ainvoke(self, *a, **k):
        raise self.exc

    def invoke(self, *a, **k):
        raise self.exc

    async def astream(self, *a, **k):
        raise self.exc
        yield  # pragma: no cover

    def stream(self, *a, **k):
        raise self.exc
        yield  # pragma: no cover


class OkLLM:
    def __init__(self, content="hello world"):
        self.content = content

    async def ainvoke(self, *a, **k):
        return AIMessage(content=self.content)

    def invoke(self, *a, **k):
        return AIMessage(content=self.content)

    async def astream(self, *a, **k):
        for tok in self.content.split():
            yield AIMessageChunk(content=tok + " ")

    def stream(self, *a, **k):
        for tok in self.content.split():
            yield AIMessageChunk(content=tok + " ")


class PartialLLM:
    """先吐一个 chunk 再报错 —— 用于验证「已开始流式则不重启」。"""
    async def astream(self, *a, **k):
        yield AIMessageChunk(content="partial")
        raise RuntimeError("mid-stream failure")


@pytest.fixture(autouse=True)
def _fresh_notices():
    begin_notices()
    yield
    drain_notices()


def _fb(*cands):
    return FallbackChatModel(candidates=list(cands))


# ── classify_reason ──────────────────────────────────────
class TestClassifyReason:
    def test_timeout(self):
        assert classify_reason(TimeoutError("x")) == "请求超时"
        assert classify_reason(asyncio.TimeoutError()) == "请求超时"
        assert classify_reason(Exception("Request timed out")) == "请求超时"

    def test_auth(self):
        assert classify_reason(Exception("Invalid API key")) == "认证失败(密钥无效)"
        e = Exception("nope"); e.status_code = 401
        assert classify_reason(e) == "认证失败(密钥无效)"

    def test_rate_limit(self):
        assert classify_reason(Exception("Rate limit exceeded")) == "频率限制/额度不足"
        e = Exception("boom"); e.status_code = 429
        assert classify_reason(e) == "频率限制/额度不足"

    def test_connection(self):
        assert classify_reason(ConnectionError("refused")) == "连接失败"
        assert classify_reason(Exception("Failed to establish a new connection")) == "连接失败"

    def test_server_error(self):
        e = Exception("boom"); e.status_code = 503
        assert classify_reason(e) == "服务端错误"

    def test_generic(self):
        assert classify_reason(ValueError("weird")) == "调用异常"


# ── ContextVar sink ──────────────────────────────────────
class TestNoticeSink:
    def test_push_and_drain(self):
        begin_notices()
        push_notice({"kind": "model_fallback", "message": "a"})
        push_notice({"kind": "model_fallback", "message": "b"})
        out = drain_notices()
        assert [n["message"] for n in out] == ["a", "b"]
        # drain 后清空
        assert drain_notices() == []

    def test_push_without_begin_is_silent(self):
        # 模拟后台任务：未 begin_notices()
        FB._notices.set(None)
        push_notice({"kind": "model_fallback", "message": "ignored"})
        assert drain_notices() == []

    def test_build_message_shape(self):
        n = _build_message("deepseek", "deepseek-chat", "请求超时", "qwen", "qwen-plus")
        assert n["kind"] == "model_fallback"
        assert n["failed"] == "deepseek/deepseek-chat"
        assert n["replaced"] == "qwen/qwen-plus"
        assert n["reason"] == "请求超时"
        assert "已自动替换为 qwen/qwen-plus" in n["message"]


# ── FallbackChatModel ────────────────────────────────────
class TestFallbackChatModel:
    async def test_primary_success_no_notice(self):
        fb = _fb((OkLLM("primary ok"), "deepseek", "deepseek-chat"),
                 (OkLLM("backup"), "qwen", "qwen-plus"))
        res = await fb.ainvoke("hi")
        assert res.content == "primary ok"
        assert drain_notices() == []

    async def test_fallback_success_pushes_notice(self):
        fb = _fb((BoomLLM(), "deepseek", "deepseek-chat"),
                 (OkLLM("backup ok"), "qwen", "qwen-plus"))
        res = await fb.ainvoke("hi")
        assert res.content == "backup ok"
        notes = drain_notices()
        assert len(notes) == 1
        assert notes[0]["failed"] == "deepseek/deepseek-chat"
        assert notes[0]["replaced"] == "qwen/qwen-plus"
        assert notes[0]["reason"] == "请求超时"

    async def test_all_fail_raises_last(self):
        fb = _fb((BoomLLM(TimeoutError("a")), "deepseek", "m1"),
                 (BoomLLM(ValueError("b")), "qwen", "m2"))
        with pytest.raises(ValueError):
            await fb.ainvoke("hi")
        assert drain_notices() == []  # 没成功就不提示

    def test_sync_invoke_fallback(self):
        fb = _fb((BoomLLM(), "deepseek", "m1"), (OkLLM("sync ok"), "qwen", "m2"))
        res = fb.invoke("hi")
        assert res.content == "sync ok"
        assert len(drain_notices()) == 1

    async def test_astream_fallback_before_first_chunk(self):
        fb = _fb((BoomLLM(), "deepseek", "m1"), (OkLLM("a b c"), "qwen", "m2"))
        chunks = [c.content async for c in fb.astream("hi")]
        assert "".join(chunks).strip() == "a b c"
        assert len(drain_notices()) == 1

    async def test_astream_reraises_after_partial(self):
        # 已吐出 chunk 后失败不应静默重启，而是抛错
        fb = _fb((PartialLLM(), "deepseek", "m1"), (OkLLM("should-not-run"), "qwen", "m2"))
        got = []
        with pytest.raises(RuntimeError):
            async for c in fb.astream("hi"):
                got.append(c.content)
        assert got == ["partial"]
        assert drain_notices() == []


# ── build_fallback_candidates ────────────────────────────
class TestBuildCandidates:
    def _keyed(self, monkeypatch, providers):
        """让指定 provider 视为「当前用户已配置 KEY」，其余无 KEY。"""
        from bottleneck_hunter.auth import current_user
        monkeypatch.setattr(current_user, "current_user_id",
                            __import__("contextvars").ContextVar("t", default="u1"))
        monkeypatch.setattr(F, "_resolve_user_llm_key",
                            lambda provider, uid: ("kkk" if provider in providers else None))
        monkeypatch.setattr(F, "_create_raw_llm", lambda p, m, **k: OkLLM(f"{p}"))

    def test_excludes_primary_only_keyed(self, monkeypatch):
        self._keyed(monkeypatch, {"deepseek", "qwen", "kimi"})
        cands = FB.build_fallback_candidates("deepseek", "deepseek-chat", user_id="u1")
        provs = [c[1] for c in cands]
        assert "deepseek" not in provs          # 排除主
        assert "qwen" in provs and "kimi" in provs

    def test_none_when_no_other_keys(self, monkeypatch):
        self._keyed(monkeypatch, {"deepseek"})  # 只有主有 KEY
        assert FB.build_fallback_candidates("deepseek", "deepseek-chat", user_id="u1") == []


# ── factory.create_llm 包壳逻辑 ──────────────────────────
class TestCreateLLMWrapping:
    def _keyed(self, monkeypatch, providers):
        monkeypatch.setattr(F, "_resolve_user_llm_key",
                            lambda provider, uid: ("kkk" if provider in providers else None))
        monkeypatch.setattr(F, "_create_raw_llm", lambda p, m, **k: OkLLM(f"{p}/{m}"))

    def test_with_fallback_wraps(self, monkeypatch):
        self._keyed(monkeypatch, {"deepseek", "qwen"})
        llm = F.create_llm("deepseek", "deepseek-chat", with_fallback=True, user_id="u1")
        assert isinstance(llm, FallbackChatModel)
        assert len(llm.candidates) >= 2
        assert llm.candidates[0][1] == "deepseek"  # 主在首位

    def test_with_fallback_false_returns_raw(self, monkeypatch):
        self._keyed(monkeypatch, {"deepseek", "qwen"})
        llm = F.create_llm("deepseek", "deepseek-chat", with_fallback=False, user_id="u1")
        assert isinstance(llm, OkLLM)

    def test_no_backups_returns_raw(self, monkeypatch):
        self._keyed(monkeypatch, {"deepseek"})  # 仅主，无备选
        llm = F.create_llm("deepseek", "deepseek-chat", with_fallback=True, user_id="u1")
        assert isinstance(llm, OkLLM)


class TestRoleResolution:
    """get_models_for_role 默认不包壳（保多样性）；get_llm_for_position 包壳。"""

    def _keyed(self, monkeypatch, providers):
        from bottleneck_hunter.auth import current_user
        monkeypatch.setattr(current_user, "current_user_id",
                            __import__("contextvars").ContextVar("t", default="u1"))
        monkeypatch.setattr(F, "_resolve_user_llm_key",
                            lambda provider, uid: ("kkk" if provider in providers else None))
        monkeypatch.setattr(F, "_create_raw_llm", lambda p, m, **k: OkLLM(f"{p}/{m}"))
        monkeypatch.setattr(F, "_load_role_configs_from_db", lambda *a, **k: [])

    def test_get_models_for_role_no_fallback_by_default(self, monkeypatch):
        self._keyed(monkeypatch, {"qwen", "kimi"})
        res = F.get_models_for_role("__fake_role__", user_id="u1")
        assert res and not isinstance(res[0][0], FallbackChatModel)

    def test_get_llm_for_position_wraps(self, monkeypatch):
        self._keyed(monkeypatch, {"qwen", "kimi"})
        llm, provider, model = F.get_llm_for_position("__fake_role__")
        assert isinstance(llm, FallbackChatModel)



# ── with_notices 投递包装器 ──────────────────────────────
class TestWithNotices:
    async def test_emits_fallback_events(self):
        from bottleneck_hunter.web.streaming._common import _sse
        from bottleneck_hunter.web.streaming._notice import with_notices

        async def gen():
            yield _sse("step_progress", message="working")
            push_notice(_build_message("a", "b", "请求超时", "c", "d"))
            yield _sse("done", ok=True)

        events = [e async for e in with_notices(gen(), _sse)]
        names = [e["event"] for e in events]
        assert names == ["step_progress", "done", "model_fallback"]
        payload = json.loads(events[-1]["data"])
        assert payload["kind"] == "model_fallback"
        assert payload["replaced"] == "c/d"

    async def test_final_flush_when_notice_after_last_event(self):
        from bottleneck_hunter.web.streaming._common import _sse
        from bottleneck_hunter.web.streaming._notice import with_notices

        async def gen():
            yield _sse("only")
            push_notice(_build_message("a", "b", "连接失败", "c", "d"))

        events = [e async for e in with_notices(gen(), _sse)]
        assert [e["event"] for e in events] == ["only", "model_fallback"]


# ── committee._invoke_with_retry 提示接线 ────────────────
class TestCommitteeNotice:
    async def test_backup_swap_pushes_notice(self):
        from bottleneck_hunter.watchlist.committee import _invoke_with_retry
        chain = [(BoomLLM(ValueError("bad model")), "deepseek", "m1"),
                 (OkLLM("committee ok"), "qwen", "m2")]
        content, provider, model = await _invoke_with_retry(chain, "prompt", "risk", max_retry=1)
        assert content == "committee ok"
        assert provider == "qwen"
        notes = drain_notices()
        assert len(notes) == 1
        assert notes[0]["failed"] == "deepseek/m1"
        assert notes[0]["replaced"] == "qwen/m2"

    async def test_primary_success_no_notice(self):
        from bottleneck_hunter.watchlist.committee import _invoke_with_retry
        chain = [(OkLLM("ok"), "deepseek", "m1"), (OkLLM("x"), "qwen", "m2")]
        content, provider, _ = await _invoke_with_retry(chain, "p", "risk", max_retry=1)
        assert content == "ok" and provider == "deepseek"
        assert drain_notices() == []
