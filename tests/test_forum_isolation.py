"""F4 · 论坛跨板隔离测试（每用户一个私有板）。

同一 db、两个不同 sub 的 client：alice 的帖/身份/禁言/设置对 bob 完全不可见、不可改。
写不到别人板 → rowcount 0 → 404（隔离即「板归属校验」）。
全部显式传 tmp db_path（见 [[project-watchlist-db-path-not-env]]）。
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


def _client(store, sub):
    app = FastAPI()
    app.include_router(router, prefix="/api/forum")
    set_store(store)  # 同一 store；隔离靠每请求 for_user(sub)，非靠不同 store 实例
    app.dependency_overrides[get_current_user] = lambda: {"sub": sub, "username": sub, "role": "user"}
    return TestClient(app)


@pytest.fixture
def alice(store):
    return _client(store, "alice")


@pytest.fixture
def bob(store):
    return _client(store, "bob")


def _ident(client, role_key):
    rows = client.get("/api/forum/identities").json()["identities"]
    return next(r for r in rows if r["role_key"] == role_key)


def test_posts_invisible_across_boards(alice, bob):
    pid = alice.post("/api/forum/posts", json={"body": "alice 的私密看法：满仓干"}).json()["id"]
    assert bob.get("/api/forum/posts").json()["posts"] == []  # bob 看不到 alice 的帖
    assert bob.get(f"/api/forum/posts/{pid}").status_code == 404


def test_cannot_delete_others_post(alice, bob):
    pid = alice.post("/api/forum/posts", json={"body": "alice 的帖，别人删不掉"}).json()["id"]
    assert bob.delete(f"/api/forum/posts/{pid}").status_code == 404  # 删不到别人板
    assert alice.get(f"/api/forum/posts/{pid}").status_code == 200  # alice 的帖仍在


def test_cannot_close_others_comments(alice, bob):
    pid = alice.post("/api/forum/posts", json={"body": "alice 的帖，别人关不了评论"}).json()["id"]
    assert bob.post(f"/api/forum/posts/{pid}/close").status_code == 404


def test_identity_override_isolated(alice, bob):
    alice.put("/api/forum/identities/watchlist_uzi", json={"display_name": "阿泽改名"})
    assert _ident(alice, "watchlist_uzi")["display_name"] == "阿泽改名"
    assert _ident(bob, "watchlist_uzi")["display_name"] == DEFAULT_IDENTITIES["watchlist_uzi"].display_name


def test_ban_isolated(alice, bob):
    alice.post("/api/forum/identities/L1_macro/ban")
    assert _ident(alice, "L1_macro")["banned"] is True
    assert _ident(bob, "L1_macro")["banned"] is False  # bob 不受 alice 禁言影响


def test_settings_isolated(alice, bob):
    alice.put("/api/forum/settings", json={"daily_cap": 1})
    assert alice.get("/api/forum/settings").json()["daily_cap"] == 1
    assert bob.get("/api/forum/settings").json()["daily_cap"] == 20  # bob 仍是默认
