"""F1 · 互动留言系统数据层（_ForumMixin）测试。

覆盖：建表幂等、帖/回帖 CRUD、软删、关评、身份 override + 禁言、每日配额、板设置默认∪覆盖，
以及严格隔离（A 板不可见 B 板）与 fail-closed（未绑定用户读写均抛）。
全部显式传 tmp db_path，绝不依赖 WATCHLIST_DB（见 [[project-watchlist-db-path-not-env]]）。
"""

import pytest

from bottleneck_hunter.watchlist.store import WatchlistStore


@pytest.fixture
def db(tmp_path):
    return tmp_path / "forum.db"


@pytest.fixture
def alice(db):
    return WatchlistStore(db).for_user("alice")


@pytest.fixture
def bob(db):
    return WatchlistStore(db).for_user("bob")


# ── 建表幂等 ──────────────────────────────────────────────
def test_init_db_idempotent(db):
    """同一库重复构造 store 不应报错（_init_db 幂等）。"""
    WatchlistStore(db)
    WatchlistStore(db)  # 第二次不应因表/索引已存在而抛
    WatchlistStore(db).for_user("x").list_forum_posts()  # 表确实可用


# ── 帖子 CRUD + 软删 ──────────────────────────────────────
def test_post_create_list_get_roundtrip(alice):
    pid = alice.create_forum_post("user", "看好这只票", title="观点", ticker="AAPL")
    assert isinstance(pid, int) and pid > 0
    posts = alice.list_forum_posts()
    assert len(posts) == 1
    assert posts[0]["body"] == "看好这只票"
    assert posts[0]["ticker"] == "AAPL"
    got = alice.get_forum_post(pid)
    assert got is not None and got["id"] == pid


def test_post_soft_delete_hides_from_default_list(alice):
    pid = alice.create_forum_post("ai", "AI 观点", author_role_key="committee_value")
    assert alice.soft_delete_forum_post(pid) is True
    assert alice.list_forum_posts() == []
    # 软删保留行：include_deleted 仍可见，行仍在（可审计）
    assert len(alice.list_forum_posts(include_deleted=True)) == 1
    assert alice.get_forum_post(pid)["deleted"] == 1


def test_list_filter_by_role_key(alice):
    alice.create_forum_post("ai", "a", author_role_key="committee_value")
    alice.create_forum_post("ai", "b", author_role_key="committee_growth")
    alice.create_forum_post("user", "c")
    assert len(alice.list_forum_posts(role_key="committee_value")) == 1
    assert len(alice.list_forum_posts()) == 3


# ── 回帖 + 关评 ───────────────────────────────────────────
def test_reply_crud_and_soft_delete(alice):
    pid = alice.create_forum_post("user", "主帖")
    rid = alice.create_forum_reply(pid, "ai", "回一条", author_role_key="watchlist_uzi")
    assert alice.list_forum_replies(pid)[0]["id"] == rid
    assert alice.soft_delete_forum_reply(rid) is True
    assert alice.list_forum_replies(pid) == []
    assert len(alice.list_forum_replies(pid, include_deleted=True)) == 1


def test_comments_closed_toggle(alice):
    pid = alice.create_forum_post("user", "主帖")
    assert alice.get_forum_post(pid)["comments_closed"] == 0
    assert alice.set_forum_comments_closed(pid, True) is True
    assert alice.get_forum_post(pid)["comments_closed"] == 1
    alice.set_forum_comments_closed(pid, False)
    assert alice.get_forum_post(pid)["comments_closed"] == 0


# ── 严格隔离：A 板不可见 B 板 ─────────────────────────────
def test_boards_are_isolated(alice, bob):
    pa = alice.create_forum_post("user", "alice 的帖")
    bob.create_forum_post("user", "bob 的帖")
    assert len(alice.list_forum_posts()) == 1
    assert len(bob.list_forum_posts()) == 1
    assert alice.list_forum_posts()[0]["body"] == "alice 的帖"
    # bob 拿不到 alice 的帖，删不动 alice 的帖
    assert bob.get_forum_post(pa) is None
    assert bob.soft_delete_forum_post(pa) is False
    assert alice.get_forum_post(pa) is not None  # 仍在


# ── fail-closed：未绑定用户读写均抛 ───────────────────────
def test_unbound_write_raises(db):
    store = WatchlistStore(db)  # 未 for_user
    with pytest.raises(ValueError, match="for_user"):
        store.create_forum_post("user", "x")


def test_unbound_read_raises(db):
    store = WatchlistStore(db)  # 未 for_user
    with pytest.raises(ValueError):  # _user_filter 的 forum 护栏拦截
        store.list_forum_posts()


# ── AI 身份 override + 禁言 ───────────────────────────────
def test_identity_override_and_ban(alice, bob):
    assert alice.get_forum_identity_override("committee_value") is None
    alice.set_forum_identity("committee_value", display_name="老陈", age="52")
    row = alice.get_forum_identity_override("committee_value")
    assert row["display_name"] == "老陈" and row["age"] == "52"
    # 局部更新不擦除其它字段
    alice.set_forum_identity("committee_value", personality="沉稳")
    row = alice.get_forum_identity_override("committee_value")
    assert row["display_name"] == "老陈" and row["personality"] == "沉稳"
    # 禁言独立于身份字段，不擦除 display_name
    assert alice.is_forum_role_banned("committee_value") is False
    alice.set_forum_role_banned("committee_value", True)
    assert alice.is_forum_role_banned("committee_value") is True
    assert alice.get_forum_identity_override("committee_value")["display_name"] == "老陈"
    # 隔离：bob 板同角色不受影响
    assert bob.get_forum_identity_override("committee_value") is None
    assert alice.list_forum_identity_overrides().keys() == {"committee_value"}


# ── 每日配额 ──────────────────────────────────────────────
def test_daily_quota_incr_and_board_total(alice):
    day = "2026-09-16"
    assert alice.get_forum_daily_count("L1_macro", day) == 0
    assert alice.incr_forum_daily_count("L1_macro", day) == 1
    assert alice.incr_forum_daily_count("L1_macro", day, n=2) == 3
    assert alice.get_forum_daily_count("L1_macro", day) == 3
    alice.incr_forum_daily_count("vip_advisor", day)
    assert alice.get_forum_board_daily_total(day) == 4  # 全板当日总和
    assert alice.get_forum_board_daily_total("2026-09-17") == 0


def test_daily_quota_isolated(alice, bob):
    day = "2026-09-16"
    alice.incr_forum_daily_count("L1_macro", day, n=5)
    assert bob.get_forum_daily_count("L1_macro", day) == 0
    assert bob.get_forum_board_daily_total(day) == 0


# ── 板设置：默认∪覆盖 ─────────────────────────────────────
def test_settings_default_and_override(alice):
    assert alice.get_forum_settings() == {"ai_enabled": 0, "daily_cap": 20}
    assert alice.is_forum_ai_enabled() is False
    alice.set_forum_settings(ai_enabled=True)
    assert alice.get_forum_settings() == {"ai_enabled": 1, "daily_cap": 20}  # 只改一项，另项保留默认
    assert alice.is_forum_ai_enabled() is True
    alice.set_forum_settings(daily_cap=5)
    assert alice.get_forum_settings() == {"ai_enabled": 1, "daily_cap": 5}


def test_settings_isolated(alice, bob):
    alice.set_forum_settings(ai_enabled=True, daily_cap=3)
    assert bob.get_forum_settings() == {"ai_enabled": 0, "daily_cap": 20}
