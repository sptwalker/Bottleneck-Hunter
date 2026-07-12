"""宏观咨询容错：失败标记 + 手动重试原位替换。"""

from __future__ import annotations

from bottleneck_hunter.watchlist import macro_consultation as mc


class _BoomLLM:
    async def astream(self, prompt, **k):
        raise ConnectionError("network down")
        yield  # 使其成为 async 生成器（不可达）

    async def ainvoke(self, prompt, **k):
        raise ConnectionError("network down")


async def _collect(agen):
    out = []
    async for item in agen:
        out.append(item)
    return out


async def test_run_analyst_flags_failure():
    a = {"slot": 0, "role": "macro_market", "label": "x", "prompt": "macro_consult_market"}
    events = await _collect(
        mc._run_analyst(a, [(_BoomLLM(), "kimi", "moonshot-v1-8k")], "snap", "ctx", "", "", 0, None))
    done = [p for kind, p in events if kind == "done"]
    entry = done[0][0]
    assert entry["failed"] is True
    assert entry["fail_reason"] == "连接失败"      # classify_reason 分类
    assert "生成失败" in entry["content"]


class _FakeStore:
    def __init__(self, transcript):
        self._t = transcript
        self.saved = None

    def get_meeting_records(self, **k):
        return [{"id": "r1", "transcript_json": self._t, "result_json": {}}]

    def update_meeting_review(self, rid, **k):
        self.saved = k.get("transcript_json")


async def test_stream_retry_replaces_failed_message(monkeypatch):
    async def _fake_run(a, models, snap, ctx, q, peer, rnd, budget):
        yield "chunk", "新答复"
        yield "done", ({"type": "analyst", "role": a["role"], "round": rnd, "content": "新答复",
                        "provider": "deepseek", "model": "deepseek-chat", "reply_to": None},
                       "deepseek", "deepseek-chat")

    monkeypatch.setattr(mc, "_run_analyst", _fake_run)
    monkeypatch.setattr(mc, "get_models_for_role",
                        lambda *a, **k: [("LLM", "deepseek", "deepseek-chat")])
    monkeypatch.setattr(mc, "_latest_snapshot_text", lambda t: "snap")
    monkeypatch.setattr(mc, "_context_for_prompt", lambda t: "ctx")

    transcript = [
        {"type": "snapshot", "ts": "2026-07-12T00:00:00"},
        {"type": "analyst", "role": "macro_market", "round": 0, "ts": "2026-07-12T00:01:00",
         "content": "（该分析师生成失败：连接失败）", "failed": True, "fail_reason": "连接失败"},
    ]
    store = _FakeStore(transcript)

    events = await _collect(mc.stream_retry(store, None, "us_stock", "macro_market"))
    # 原位替换：失败消息(idx1)内容更新、failed 标记消失
    assert store.saved is not None
    replaced = store.saved[1]
    assert replaced["content"] == "新答复"
    assert not replaced.get("failed")
    # 事件流含 retry_start / msg_done(failed=False) / retry_done
    kinds = [e.get("event") for e in events if isinstance(e, dict)]
    assert "retry_start" in kinds and "retry_done" in kinds
