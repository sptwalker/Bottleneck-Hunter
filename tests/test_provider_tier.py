"""用户自定义模型付费类型（免费/付费）：per-user 存储 + provider_tier 优先级 + 欠费自动翻档。"""

from __future__ import annotations

import bottleneck_hunter.llm_clients.health as H
from bottleneck_hunter.watchlist.store import WatchlistStore


# ── 1) 存储层：setter/getter/批量/隔离 ──────────────────────────
def test_provider_tier_store_roundtrip(tmp_path):
    s = WatchlistStore(str(tmp_path / "t.db"))
    s.set_provider_config_tier("foo", "paid", "u1")
    assert s.get_provider_tier("foo", "u1") == "paid"
    # 未设过的 provider → 空
    assert s.get_provider_tier("bar", "u1") == ""
    # 换 user 读不到（严格用户级隔离）
    assert s.get_provider_tier("foo", "u2") == ""
    # 改档：free 覆盖 paid
    s.set_provider_config_tier("foo", "free", "u1")
    assert s.get_provider_tier("foo", "u1") == "free"
    # 非法值归一为空
    s.set_provider_config_tier("foo", "gold", "u1")
    assert s.get_provider_tier("foo", "u1") == ""
    # 批量：只回已设的
    s.set_provider_config_tier("a", "free", "u1")
    s.set_provider_config_tier("b", "paid", "u1")
    tiers = s.get_provider_tiers("u1")
    assert tiers.get("a") == "free" and tiers.get("b") == "paid"
    assert "foo" not in tiers        # 已归空的不出现
    assert s.get_provider_tiers("u2") == {}   # 隔离


# ── 2) provider_tier 优先级：用户档 > 静态表 > 空 ────────────────
def test_provider_tier_priority(tmp_path, monkeypatch):
    s = WatchlistStore(str(tmp_path / "t.db"))
    monkeypatch.setattr(H, "_get_store", lambda: s)
    # 用户显式设定压过静态表子串继承（siliconflow_x 本会子串继承 siliconflow=free）
    s.set_provider_config_tier("siliconflow_x", "paid", "u1")
    assert H.provider_tier("siliconflow_x", "u1") == "paid"
    # 用户没设 → 回退静态表（子串继承不破）
    assert H.provider_tier("siliconflow_x", "u2") == "free"
    # user_id 为空 → 纯静态表（旧行为不破）
    assert H.provider_tier("deepseek", "") == "free"
    assert H.provider_tier("openai", "") == "paid"
    assert H.provider_tier("unknown_zzz", "") == ""


# ── 3) 欠费自动翻档：免费档欠费→翻付费；paid/空档不翻 ──────────────
def test_arrears_flip_free_to_paid(tmp_path, monkeypatch):
    import bottleneck_hunter.llm_clients.provider_gate as G

    s = WatchlistStore(str(tmp_path / "t.db"))
    monkeypatch.setattr(H, "_get_store", lambda: s)
    # provider_gate 内部惰性 import WatchlistStore()——指向同一 tmp 库
    monkeypatch.setattr("bottleneck_hunter.watchlist.store.WatchlistStore", lambda: s)

    # 用户档=free → 欠费触发翻档
    s.set_provider_config_tier("myfree", "free", "u1")
    assert G._flip_free_to_paid_on_arrears("u1", "myfree") is True
    assert s.get_provider_tier("myfree", "u1") == "paid"

    # 用户档=paid → 不翻
    s.set_provider_config_tier("mypaid", "paid", "u1")
    assert G._flip_free_to_paid_on_arrears("u1", "mypaid") is False
    assert s.get_provider_tier("mypaid", "u1") == "paid"

    # 空档 → 不翻（provider_tier 回退静态表，未知 provider 得空，不判定为 free）
    assert G._flip_free_to_paid_on_arrears("u1", "unknown_zzz") is False
    assert s.get_provider_tier("unknown_zzz", "u1") == ""
