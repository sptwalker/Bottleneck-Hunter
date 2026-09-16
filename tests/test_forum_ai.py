"""F5 · 论坛 AI 发帖引擎测试（stub model，不真调 provider）。

覆盖：ai_enabled=0 空转 / 开启后落库+计数 / 全板配额上限 / 内容闸不计配额 /
禁言角色排除 / 回复既有帖。全部显式 tmp db_path（见 [[project-watchlist-db-path-not-env]]），
sub 用真实用户名 alice（forum 无全局板）。stub 通过 monkeypatch factory.get_models_for_role 注入，
并固定 random 以去除 50% 回帖/洗牌的非确定性。
"""

import asyncio

import pytest
from langchain_core.messages import AIMessage

from bottleneck_hunter.llm_clients import factory
from bottleneck_hunter.watchlist import forum_ai
from bottleneck_hunter.watchlist.forum_identity import FORUM_ROLE_KEYS
from bottleneck_hunter.watchlist.store import WatchlistStore

_OK_BODY = "我觉得这只票的现金流被低估了，估值有修复空间，值得再看一眼。"


class _StubLLM:
    def __init__(self, text):
        self._text = text

    async def ainvoke(self, messages, **kwargs):  # 与 FallbackChatModel.ainvoke 同签名（消息列表）
        return AIMessage(content=self._text)


def _patch_model(monkeypatch, text=_OK_BODY):
    monkeypatch.setattr(factory, "get_models_for_role",
                        lambda role_key, **kw: [(_StubLLM(text), "stub", "stub-model")])


def _force_post(monkeypatch):
    monkeypatch.setattr(forum_ai.random, "random", lambda: 1.0)   # ≥0.5 → 永不回帖，只发帖
    monkeypatch.setattr(forum_ai.random, "shuffle", lambda seq: None)  # 固定角色顺序，确定性


@pytest.fixture
def bound(tmp_path):
    return WatchlistStore(tmp_path / "forum.db").for_user("alice")


def test_disabled_by_default_returns_zero(bound, monkeypatch):
    _patch_model(monkeypatch)
    _force_post(monkeypatch)
    r = asyncio.run(forum_ai.run_forum_ai_round(bound, "alice"))
    assert r == {"posts": 0, "replies": 0}
    assert bound.list_forum_posts() == []  # 默认 ai_enabled=0：空转不落库


def test_posts_and_counts_when_enabled(bound, monkeypatch):
    _patch_model(monkeypatch)
    _force_post(monkeypatch)
    bound.set_forum_settings(ai_enabled=True)
    r = asyncio.run(forum_ai.run_forum_ai_round(bound, "alice", max_posts=2))
    assert r == {"posts": 2, "replies": 0}  # _force_post → 只发帖
    posts = bound.list_forum_posts()
    assert len(posts) == 2
    assert all(p["author_type"] == "ai" and p["author_role_key"] in FORUM_ROLE_KEYS for p in posts)
    assert sum(bound.get_forum_daily_count(rk) for rk in FORUM_ROLE_KEYS) == 2
    assert bound.get_forum_board_daily_total() == 2


def test_board_daily_cap_limits_round(bound, monkeypatch):
    _patch_model(monkeypatch)
    _force_post(monkeypatch)
    bound.set_forum_settings(ai_enabled=True, daily_cap=1)
    r = asyncio.run(forum_ai.run_forum_ai_round(bound, "alice", max_posts=5))
    assert r == {"posts": 1, "replies": 0}  # budget=min(cap-已发, 上限)=1
    assert bound.get_forum_board_daily_total() == 1


def test_content_gate_skips_without_counting(bound, monkeypatch):
    _patch_model(monkeypatch, text="你就是个傻子")  # 命中人身攻击正则
    _force_post(monkeypatch)
    bound.set_forum_settings(ai_enabled=True)
    r = asyncio.run(forum_ai.run_forum_ai_round(bound, "alice", max_posts=3))
    assert r == {"posts": 0, "replies": 0}
    assert bound.list_forum_posts() == []
    assert bound.get_forum_board_daily_total() == 0  # 违规不计配额


def test_banned_roles_excluded(bound, monkeypatch):
    _patch_model(monkeypatch)
    _force_post(monkeypatch)
    bound.set_forum_settings(ai_enabled=True)
    for rk in FORUM_ROLE_KEYS:
        bound.set_forum_role_banned(rk, True)
    r = asyncio.run(forum_ai.run_forum_ai_round(bound, "alice", max_posts=3))
    assert r == {"posts": 0, "replies": 0}  # 全禁言 → 无候选


def test_replies_to_existing_post(bound, monkeypatch):
    _patch_model(monkeypatch, text="补充一点，这块业务的护城河其实还在，别急着看空。")
    monkeypatch.setattr(forum_ai.random, "random", lambda: 0.0)  # <0.5 → 永远回帖
    monkeypatch.setattr(forum_ai.random, "shuffle", lambda seq: None)
    monkeypatch.setattr(forum_ai.random, "choice", lambda seq: seq[0])
    bound.set_forum_settings(ai_enabled=True)
    pid = bound.create_forum_post("user", "我看多这只票，逻辑是产能瓶颈缓解。")
    r = asyncio.run(forum_ai.run_forum_ai_round(bound, "alice", max_posts=1))
    assert r == {"posts": 0, "replies": 1}  # _force reply → 只回帖
    replies = bound.list_forum_replies(pid)
    assert len(replies) == 1 and replies[0]["author_type"] == "ai"
    posts = bound.list_forum_posts()
    assert len(posts) == 1 and posts[0]["reply_count"] == 1  # 未新增帖，且该帖 reply_count 计到 1
