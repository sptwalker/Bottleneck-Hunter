"""F3 · 论坛管理规则三闸（forum_moderation）测试。

覆盖：normalize/content_hash 去标点大小写、去重（完全同/近似/按角色/用户不判重/隔离）、
内容规则（空/超长/攻击词拒，金融术语放行）、配额（角色硬上限/全板 cap/角色独立/隔离）。
全部显式传 tmp db_path，绝不依赖 WATCHLIST_DB（见 [[project-watchlist-db-path-not-env]]）。
"""

import pytest

from bottleneck_hunter.watchlist.forum_moderation import (
    check_content,
    check_quota,
    content_hash,
    is_duplicate,
    normalize,
)
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


# ── 归一化 / 哈希 ─────────────────────────────────────────
def test_normalize_strips_punct_and_space():
    assert normalize("看好 这只，票！") == "看好这只票"
    assert normalize("Hello, World!") == "helloworld"


def test_content_hash_ignores_punct_and_case():
    assert content_hash("看好这只票") == content_hash("看好 这只，票！")
    assert content_hash("Hello, World!") == content_hash("helloworld")


# ── 去重 ──────────────────────────────────────────────────
def test_is_duplicate_exact_and_near(alice):
    alice.create_forum_post("ai", "老陈认为这只票便宜，值得买入", author_role_key="committee_value")
    assert is_duplicate(alice, "alice", "committee_value", "老陈认为这只票便宜，值得买入") is True
    # 仅标点差异 → 归一化后等价 → 近似重
    assert is_duplicate(alice, "alice", "committee_value", "老陈认为这只票便宜值得买入！！") is True
    # 完全不同话题 → 不重
    assert is_duplicate(alice, "alice", "committee_value", "宏观上我更担心利率和流动性拐点") is False


def test_is_duplicate_per_role(alice):
    alice.create_forum_post("ai", "看好这只票", author_role_key="committee_value")
    # 不同角色说同样的话不算重（各自人设允许各自表达）
    assert is_duplicate(alice, "alice", "committee_growth", "看好这只票") is False
    assert is_duplicate(alice, "alice", "committee_value", "看好这只票") is True


def test_is_duplicate_user_not_deduped(alice):
    alice.create_forum_post("user", "看好这只票")
    assert is_duplicate(alice, "alice", "", "看好这只票") is False  # 用户发帖不判重


def test_is_duplicate_isolated(alice, bob):
    alice.create_forum_post("ai", "看好这只票", author_role_key="committee_value")
    assert is_duplicate(bob, "bob", "committee_value", "看好这只票") is False


# ── 内容规则 ──────────────────────────────────────────────
def test_check_content_rejects_empty():
    ok, reason = check_content("   ")
    assert ok is False and reason


def test_check_content_rejects_too_long():
    ok, reason = check_content("拆" * 8001)
    assert ok is False and "超长" in reason


def test_check_content_rejects_attacks():
    for bad in ["你就是个傻子", "脑子进水了吧", "傻逼东西", "给我滚", "你这个白痴", "just SB"]:
        ok, _ = check_content(bad)
        assert ok is False, bad


def test_check_content_allows_finance_terms():
    # 「垃圾股/垃圾债」是行业术语、指向标的而非人，必须放行
    for good in ["这只就是垃圾股，基本面太差", "垃圾债收益率其实不低", "我看好这只票的现金流"]:
        ok, _ = check_content(good)
        assert ok is True, good


# ── 配额 ──────────────────────────────────────────────────
def test_check_quota_role_hard_cap(alice):
    alice.set_forum_settings(daily_cap=100)  # 抬高板配额，隔离测角色硬上限
    alice.incr_forum_daily_count("L1_macro", n=20)
    ok, reason = check_quota(alice, "alice", "L1_macro")
    assert ok is False and "硬上限" in reason
    # 角色级独立：另一角色今日为 0，仍可发
    ok2, _ = check_quota(alice, "alice", "vip_advisor")
    assert ok2 is True


def test_check_quota_board_cap(alice):
    alice.set_forum_settings(daily_cap=5)
    for rk in ("committee_value", "committee_growth", "committee_risk", "committee_contrarian", "committee_consensus"):
        alice.incr_forum_daily_count(rk, n=1)  # 5 个角色各 1，无人到 20，但全板到 5
    ok, reason = check_quota(alice, "alice", "L1_macro")  # L1_macro 今日 0，但全板已满
    assert ok is False and "配额" in reason


def test_check_quota_ok_when_under(alice):
    ok, reason = check_quota(alice, "alice", "L1_macro")
    assert ok is True and reason == ""


def test_check_quota_isolated(alice, bob):
    alice.set_forum_settings(daily_cap=1)
    alice.incr_forum_daily_count("L1_macro", n=1)
    assert check_quota(alice, "alice", "vip_advisor")[0] is False  # alice 全板已满
    assert check_quota(bob, "bob", "L1_macro")[0] is True  # bob 不受影响
