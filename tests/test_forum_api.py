"""F4 · 论坛 API 契约测试（单用户 alice）。

覆盖：发帖/内容闸(空/攻击)、取帖+回帖、软删、关/开评论、回帖闸、身份读改+禁言、板设置、
AI 手动触发（默认关→空转）。全部显式传 tmp db_path，绝不依赖 WATCHLIST_DB
（见 [[project-watchlist-db-path-not-env]]）。forum 无全局板，故 sub 必须为真实用户名。
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bottleneck_hunter.auth.dependencies import get_current_user
from bottleneck_hunter.watchlist.forum_identity import DEFAULT_IDENTITIES
from bottleneck_hunter.watchlist.store import WatchlistStore
from bottleneck_hunter.web.forum_api import router, set_store


@pytest.fixture
def store(tmp_path):
    return WatchlistStore(tmp_path / "forum.db")


@pytest.fixture
def client(store):
    app = FastAPI()
    app.include_router(router, prefix="/api/forum")
    set_store(store)
    app.dependency_overrides[get_current_user] = lambda: {"sub": "alice", "username": "alice", "role": "user"}
    return TestClient(app)


def _ident(client, role_key):
    rows = client.get("/api/forum/identities").json()["identities"]
    return next(r for r in rows if r["role_key"] == role_key)


# ── 发帖 / 内容闸 ─────────────────────────────────────────
def test_create_and_list_post(client):
    resp = client.post("/api/forum/posts", json={"body": "看好这只票的现金流", "ticker": "AAPL"})
    assert resp.status_code == 200
    post = resp.json()
    assert post["author_type"] == "user" and post["body"] == "看好这只票的现金流"
    listed = client.get("/api/forum/posts").json()["posts"]
    assert len(listed) == 1 and listed[0]["id"] == post["id"]


def test_create_post_rejects_empty(client):
    assert client.post("/api/forum/posts", json={"body": "   "}).status_code == 400


def test_create_post_rejects_attack(client):
    assert client.post("/api/forum/posts", json={"body": "你就是个傻子"}).status_code == 400


def test_get_post_with_replies_and_404(client):
    pid = client.post("/api/forum/posts", json={"body": "标的分析：估值偏低"}).json()["id"]
    resp = client.get(f"/api/forum/posts/{pid}")
    assert resp.status_code == 200
    assert resp.json()["post"]["id"] == pid and resp.json()["replies"] == []
    assert client.get("/api/forum/posts/999999").status_code == 404


# ── 软删 ──────────────────────────────────────────────────
def test_delete_post(client):
    pid = client.post("/api/forum/posts", json={"body": "待删除的帖子内容"}).json()["id"]
    assert client.delete(f"/api/forum/posts/{pid}").status_code == 200
    assert client.get(f"/api/forum/posts/{pid}").status_code == 404
    assert client.delete("/api/forum/posts/999999").status_code == 404


# ── 关 / 开评论 + 回帖闸 ──────────────────────────────────
def test_reply_and_comments_close_open(client):
    pid = client.post("/api/forum/posts", json={"body": "欢迎讨论这只票"}).json()["id"]
    assert client.post(f"/api/forum/posts/{pid}/replies", json={"body": "我也看好，逻辑扎实"}).status_code == 200
    # 关评论后回帖被拒
    assert client.post(f"/api/forum/posts/{pid}/close").status_code == 200
    assert client.post(f"/api/forum/posts/{pid}/replies", json={"body": "还能回吗"}).status_code == 403
    # 开回来后可回
    assert client.post(f"/api/forum/posts/{pid}/open").status_code == 200
    assert client.post(f"/api/forum/posts/{pid}/replies", json={"body": "现在可以回了"}).status_code == 200


def test_reply_to_missing_post_404(client):
    assert client.post("/api/forum/posts/999999/replies", json={"body": "无处安放的回复"}).status_code == 404


def test_reply_rejects_empty(client):
    pid = client.post("/api/forum/posts", json={"body": "一个正常的帖子"}).json()["id"]
    assert client.post(f"/api/forum/posts/{pid}/replies", json={"body": "  "}).status_code == 400


def test_delete_reply(client):
    pid = client.post("/api/forum/posts", json={"body": "帖子正文在此"}).json()["id"]
    rid = client.post(f"/api/forum/posts/{pid}/replies", json={"body": "待删的回复内容"}).json()["reply_id"]
    assert client.delete(f"/api/forum/replies/{rid}").status_code == 200
    assert client.delete("/api/forum/replies/999999").status_code == 404
    assert client.get(f"/api/forum/posts/{pid}").json()["replies"] == []  # 软删后不在列表


# ── 身份 ──────────────────────────────────────────────────
def test_list_identities_has_eight_entered_roles(client):
    rows = client.get("/api/forum/identities").json()["identities"]
    assert len(rows) == len(DEFAULT_IDENTITIES)
    row = _ident(client, "committee_value")
    assert row["display_name"] == "老陈" and row["label"] and row["banned"] is False and row["overridden"] is False


def test_update_identity_override(client):
    resp = client.put("/api/forum/identities/committee_value", json={"display_name": "王五", "age": "60"})
    assert resp.status_code == 200 and resp.json()["display_name"] == "王五" and resp.json()["age"] == "60"
    assert _ident(client, "committee_value")["overridden"] is True


def test_update_identity_rejects_non_entered_role(client):
    assert client.put("/api/forum/identities/pipeline_decompose", json={"display_name": "X"}).status_code == 404


def test_ban_unban_role(client):
    assert client.post("/api/forum/identities/committee_risk/ban").json()["banned"] is True
    assert _ident(client, "committee_risk")["banned"] is True
    assert client.post("/api/forum/identities/committee_risk/unban").json()["banned"] is False
    assert _ident(client, "committee_risk")["banned"] is False


# ── 板设置 ────────────────────────────────────────────────
def test_settings_defaults_and_update(client):
    assert client.get("/api/forum/settings").json() == {"ai_enabled": 0, "daily_cap": 20}
    resp = client.put("/api/forum/settings", json={"daily_cap": 10, "ai_enabled": True})
    assert resp.json() == {"ai_enabled": 1, "daily_cap": 10}


# ── AI 手动触发（默认关）────────────────────────────────
def test_ai_run_disabled_by_default(client):
    resp = client.post("/api/forum/ai/run")
    assert resp.status_code == 200 and resp.json()["posted"] == 0
