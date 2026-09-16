"""F2 · 论坛 AI 人性化身份（forum_identity）测试。

覆盖：默认人设键 == 8 入驻角色且是 ROLE_REGISTRY 子集、键与 Identity.role_key 一致、
默认∪override 合并（空字段回退）、按板主隔离、非入驻角色拒绝、system_preamble、禁言排除。
全部显式传 tmp db_path，绝不依赖 WATCHLIST_DB（见 [[project-watchlist-db-path-not-env]]）。
"""

import pytest

from bottleneck_hunter.llm_clients.role_registry import ROLE_REGISTRY
from bottleneck_hunter.watchlist.forum_identity import (
    DEFAULT_IDENTITIES,
    FORUM_ROLE_KEYS,
    get_identity,
    selectable_role_keys,
)
from bottleneck_hunter.watchlist.store import WatchlistStore

_EXPECTED = {
    "committee_value", "committee_growth", "committee_risk", "committee_contrarian",
    "committee_consensus", "L1_macro", "vip_advisor", "watchlist_uzi",
}


@pytest.fixture
def db(tmp_path):
    return tmp_path / "forum.db"


@pytest.fixture
def alice(db):
    return WatchlistStore(db).for_user("alice")


@pytest.fixture
def bob(db):
    return WatchlistStore(db).for_user("bob")


# ── 默认人设表 & 注册表一致性 ─────────────────────────────
def test_default_identities_keys_match_entered_roles():
    assert set(DEFAULT_IDENTITIES) == _EXPECTED
    assert set(FORUM_ROLE_KEYS) == _EXPECTED


def test_default_identities_subset_of_role_registry():
    assert set(DEFAULT_IDENTITIES).issubset(ROLE_REGISTRY)


def test_identity_role_key_and_fields_consistent():
    for k, v in DEFAULT_IDENTITIES.items():
        assert k == v.role_key
        assert v.display_name and v.persona_identity and v.personality  # 无空人设


# ── get_identity 合并 ─────────────────────────────────────
def test_get_identity_returns_default_when_no_override(alice):
    ident = get_identity(alice, "alice", "committee_value")
    assert ident.display_name == "老陈" and ident.age == "52"


def test_get_identity_merges_override_over_default(alice):
    alice.set_forum_identity("committee_value", display_name="王五", age="60")
    ident = get_identity(alice, "alice", "committee_value")
    assert ident.display_name == "王五" and ident.age == "60"  # override 生效
    # 未覆盖字段回退默认
    assert ident.personality == DEFAULT_IDENTITIES["committee_value"].personality


def test_get_identity_empty_override_field_falls_back(alice):
    # 只改 display_name，其它 override 列为空串 → 空串回退默认（不是被清空）
    alice.set_forum_identity("committee_growth", display_name="小美")
    ident = get_identity(alice, "alice", "committee_growth")
    assert ident.display_name == "小美"
    assert ident.gender == DEFAULT_IDENTITIES["committee_growth"].gender
    assert ident.persona_identity == DEFAULT_IDENTITIES["committee_growth"].persona_identity


def test_get_identity_isolated_per_user(alice, bob):
    alice.set_forum_identity("watchlist_uzi", display_name="阿泽改名")
    assert get_identity(alice, "alice", "watchlist_uzi").display_name == "阿泽改名"
    assert get_identity(bob, "bob", "watchlist_uzi").display_name == \
        DEFAULT_IDENTITIES["watchlist_uzi"].display_name


def test_get_identity_rejects_non_entered_role(alice):
    with pytest.raises(KeyError):
        get_identity(alice, "alice", "pipeline_decompose")  # 纯机器角色，未入驻


# ── system_preamble ──────────────────────────────────────
def test_system_preamble_contains_identity_and_discipline():
    p = DEFAULT_IDENTITIES["committee_contrarian"].system_preamble()
    assert "老康" in p and "45" in p
    assert "对事不对人" in p and "不攻击他人" in p


# ── 禁言排除 ──────────────────────────────────────────────
def test_selectable_role_keys_excludes_banned(alice):
    assert set(selectable_role_keys(alice, "alice")) == _EXPECTED
    alice.set_forum_role_banned("committee_risk", True)
    keys = selectable_role_keys(alice, "alice")
    assert "committee_risk" not in keys and len(keys) == 7


def test_selectable_role_keys_isolated(alice, bob):
    alice.set_forum_role_banned("L1_macro", True)
    assert "L1_macro" in selectable_role_keys(bob, "bob")  # bob 不受 alice 禁言影响
