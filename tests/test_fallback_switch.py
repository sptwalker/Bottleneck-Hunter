"""调用层统一：内部按候选硬超时切换（★ wait_for 自毁修复）+ record-only 记账 + 用户级主模型隔离。

覆盖计划验证项 1、3：
- 内部超时切换：候选-0 ainvoke 睡眠 > _CAND_TIMEOUT → 内部 asyncio.TimeoutError（是 Exception，
  非 CancelledError）被逐候选 except 捕获 → 记账(请求超时) + 前进候选-1 成功 → 返回备节点 + 1 条切换通知。
  **证 ★ 修复**：超时在候选中途可达，无需 chain/* 外层 wait_for（后者抛 CancelledError 穿透 except 自毁）。
- record-only 单候选：wrap_record_only 只产 1 候选（保扇出多样性），失败仍走 _record_call 记账
  （ok=False + 分类 reason）→ 喂 provider_gate 熔断层（record_result→is_disabled 由 test_provider_gate 证）。
- 用户级主模型：set_provider_config_primary(p, uidA) → get(uidA)==p 且 get(uidB) is None；空 pid 取消。
"""
import asyncio

import pytest
from langchain_core.messages import AIMessage

from bottleneck_hunter.llm_clients import fallback as FB
from bottleneck_hunter.llm_clients.fallback import (
    FallbackChatModel,
    begin_notices,
    drain_notices,
    wrap_record_only,
)


class SlowLLM:
    """ainvoke 睡眠 delay 秒——用于触发内部 wait_for 硬超时。"""
    def __init__(self, delay: float, content: str = "slow"):
        self.delay = delay
        self.content = content

    async def ainvoke(self, *a, **k):
        await asyncio.sleep(self.delay)
        return AIMessage(content=self.content)


class OkLLM:
    def __init__(self, content="ok"):
        self.content = content

    async def ainvoke(self, *a, **k):
        return AIMessage(content=self.content)


class AuthBoomLLM:
    async def ainvoke(self, *a, **k):
        raise Exception("Invalid API key")  # → classify_reason 认证失败


# ── 内部超时切换（★ 修复）───────────────────────────────
async def test_internal_timeout_switches_to_backup(monkeypatch):
    monkeypatch.setattr(FB, "_CAND_TIMEOUT", 0.1)  # 主候选 2s 睡眠必超时
    begin_notices()
    fb = FallbackChatModel(candidates=[
        (SlowLLM(2.0), "deepseek", "deepseek-chat"),
        (OkLLM("backup ok"), "qwen", "qwen-plus"),
    ])
    res = await fb.ainvoke("hi")
    assert res.content == "backup ok"                     # 无感切到备节点
    notes = drain_notices()
    assert len(notes) == 1
    assert notes[0]["kind"] == "model_fallback"
    assert notes[0]["reason"] == "请求超时"                # 超时被记为原因（喂 provider_gate）
    assert notes[0]["replaced"] == "qwen/qwen-plus"


async def test_all_candidates_timeout_raises_timeout(monkeypatch):
    # 全部候选超时 → 抛 TimeoutError（是 Exception，仍由上层 except 捕获，不自毁）
    monkeypatch.setattr(FB, "_CAND_TIMEOUT", 0.1)
    begin_notices()
    fb = FallbackChatModel(candidates=[
        (SlowLLM(2.0), "deepseek", "m1"),
        (SlowLLM(2.0), "qwen", "m2"),
    ])
    with pytest.raises((asyncio.TimeoutError, TimeoutError)):
        await fb.ainvoke("hi")
    assert drain_notices() == []                          # 没成功就不提示


# ── record-only 记账路径（扇出）─────────────────────────
async def test_wrap_record_only_single_candidate_records_failure(monkeypatch):
    calls = []
    monkeypatch.setattr(FB, "_record_call",
                        lambda provider, model, ok, t0, reason="": calls.append((provider, ok, reason)))
    fb = wrap_record_only(AuthBoomLLM(), "DeepSeek", "deepseek-chat")
    assert isinstance(fb, FallbackChatModel)
    assert len(fb.candidates) == 1                        # 单候选：只记账不切换，保扇出多样性
    assert fb.candidates[0][1] == "deepseek"              # provider 归一化小写
    with pytest.raises(Exception):
        await fb.ainvoke("hi")
    assert calls == [("deepseek", False, "认证失败(密钥无效)")]  # 失败被记账（喂 provider_gate）


# ── 用户级主模型隔离 ───────────────────────────────────
def test_user_level_primary_isolation(tmp_path):
    from bottleneck_hunter.watchlist.store import WatchlistStore
    store = WatchlistStore(tmp_path / "wl.db")
    store.set_provider_config_primary("deepseek", "uidA")
    assert store.get_primary_provider_config("uidA") == "deepseek"
    assert store.get_primary_provider_config("uidB") is None   # 严格隔离，不波及他人
    # 空 pid = 取消主模型（主模型失效自动清除即用此）
    store.set_provider_config_primary("", "uidA")
    assert store.get_primary_provider_config("uidA") is None
    assert store.get_primary_provider_config("uidB") is None
