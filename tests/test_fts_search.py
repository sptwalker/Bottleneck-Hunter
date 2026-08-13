"""P0-③ FTS5 全文检索：experience_cards 虚表+触发器同步+回填+隔离+union 自检。

- 新插卡片经触发器即时可 MATCH（外部内容表 ai 触发器）
- 中文关键词(trigram, ≥3 字)命中 content/title；<3 字短路空
- search_cards 不跨 user / 不跨 market（JOIN 回基表 _filtered 隔离）
- update/delete 经 au/ad 触发器同步（旧词消失、新词命中、删后消失）
- 存量卡片（先于 fts 存在）经迁移 'rebuild' 回填
- get_relevant_cards 既含 scope 命中也含正文关键词命中（union 去重、confidence 优先）
"""
from bottleneck_hunter.watchlist.store import WatchlistStore


def _u(tmp_path, name, user="u1", market="us_stock"):
    return WatchlistStore(str(tmp_path / name)).for_user(user).for_market(market)


def test_new_card_immediately_searchable(tmp_path):
    """插卡→触发器同步→即时 MATCH（中文 ≥3 字命中正文与标题）。"""
    s = _u(tmp_path, "a.db")
    s.create_experience_card("global", "", "lesson", "英伟达算力复盘", "数据中心GPU供不应求，涨价逻辑成立")
    assert any("英伟达" in h["title"] for h in s.search_cards("数据中心"))   # 命中 content
    assert any("英伟达" in h["title"] for h in s.search_cards("英伟达算力"))  # 命中 title


def test_trigram_below_3_chars_short_circuits(tmp_path):
    """trigram 固有下限：<3 字符查询短路返回空（2 字中文如「瓶颈」匹配不到，已知限制）。"""
    s = _u(tmp_path, "b.db")
    s.create_experience_card("global", "", "lesson", "瓶颈标题", "供应链瓶颈分析内容测试")
    assert s.search_cards("瓶颈") == []   # 2 字 → 空
    assert s.search_cards("") == []
    assert any(h for h in s.search_cards("供应链瓶颈"))  # 3+ 字 → 命中


def test_search_isolation_across_user_and_market(tmp_path):
    """search_cards 不跨 user / 不跨 market：JOIN 回基表 _filtered 隔离。"""
    db = "iso.db"
    _u(tmp_path, db, "u1", "us_stock").create_experience_card(
        "global", "", "lesson", "光伏逆变器龙头", "组件价格战下逆变器盈利韧性强")
    # 另一用户搜同关键词 → 空
    assert _u(tmp_path, db, "u2", "us_stock").search_cards("逆变器盈利") == []
    # 同用户另一市场 → 空
    assert _u(tmp_path, db, "u1", "a_stock").search_cards("逆变器盈利") == []
    # 同用户同市场 → 命中
    assert _u(tmp_path, db, "u1", "us_stock").search_cards("逆变器盈利")


def test_update_and_delete_trigger_sync(tmp_path):
    """au/ad 触发器：更新后旧词消失新词命中；删除后彻底消失。"""
    s = _u(tmp_path, "ud.db")
    cid = s.create_experience_card("global", "", "lesson", "标题保留", "原始内容储能电池方向")
    assert s.search_cards("储能电池")
    with s._write_conn() as conn:
        conn.execute("UPDATE experience_cards SET content = ? WHERE id = ?", ("改为光伏组件方向", cid))
    assert s.search_cards("储能电池") == []      # 旧词随 au 触发器清除
    assert s.search_cards("光伏组件")            # 新词入索引
    with s._write_conn() as conn:
        conn.execute("DELETE FROM experience_cards WHERE id = ?", (cid,))
    assert s.search_cards("光伏组件") == []      # ad 触发器清除


def test_backfill_indexes_preexisting_cards(tmp_path):
    """存量卡片先于 fts 存在 → 迁移首建时 'rebuild' 回填。"""
    db = str(tmp_path / "bf.db")
    s = WatchlistStore(db)
    s.create_experience_card("global", "", "lesson", "回填测试标题", "数据中心算力紧张导致涨价")
    # 模拟旧库无 fts：删虚表+触发器，重开 store 触发 _migrate_experience_cards_fts 首建+回填
    with s._write_conn() as conn:
        conn.execute("DROP TABLE IF EXISTS experience_cards_fts")
        for t in ("ai", "ad", "au"):
            conn.execute(f"DROP TRIGGER IF EXISTS experience_cards_fts_{t}")
    s2 = WatchlistStore(db)
    assert any("回填测试" in h["title"] for h in s2.search_cards("数据中心"))


def test_get_relevant_cards_union_scope_and_fulltext(tmp_path):
    """get_relevant_cards：scope 命中 + 正文关键词命中并集（scope_key 不匹配也能靠全文补齐）。"""
    s = _u(tmp_path, "rel.db")
    # A：scope=ticker/NVDA → scope 直接命中
    s.create_experience_card("ticker", "NVDA", "lesson", "NVDA 直接归属", "英伟达数据中心逻辑", confidence=0.9)
    # B：scope=ticker/AAPL（scope_key≠NVDA，scope 查不到）但正文提及 NVDA → 靠 FTS union 补齐
    s.create_experience_card("ticker", "AAPL", "lesson", "苹果卡但提及NVDA", "NVDA 芯片占比上升", confidence=0.6)
    # C：无关卡片 → 不应出现
    s.create_experience_card("ticker", "TSLA", "lesson", "特斯拉无关", "电动车产能爬坡", confidence=0.5)
    cards = s.get_relevant_cards("NVDA", sector="", limit=5)
    titles = [c["title"] for c in cards]
    assert "NVDA 直接归属" in titles          # scope 命中
    assert "苹果卡但提及NVDA" in titles        # 全文 union 命中
    assert "特斯拉无关" not in titles          # 无关不进
    assert titles[0] == "NVDA 直接归属"        # confidence 优先排序


def test_get_relevant_cards_no_cross_user_or_market_leak(tmp_path):
    """隔离铁律：get_relevant_cards 的 scope 查询不得跨 user/market 泄露。

    顶层 OR 若不整体括号包裹，_filtered 追加的 AND user_id/market 只绑定最后一个 OR 分支，
    global/ticker 两支会把他人卡片(含全文)泄露进 L4 决策 prompt。此测试在括号缺失时必 FAIL。
    """
    db = "leak.db"
    s1 = _u(tmp_path, db, "u1", "us_stock")
    s1.create_experience_card("global", "", "lesson", "U1全局卡", "组合管理通用经验", confidence=0.9)
    s1.create_experience_card("ticker", "NVDA", "lesson", "U1的NVDA卡", "英伟达持仓经验", confidence=0.8)
    # 另一用户：绝不能看到 u1 的 global / ticker 卡
    leaked_u = _u(tmp_path, db, "u2", "us_stock").get_relevant_cards("NVDA")
    assert leaked_u == [], f"跨用户泄露: {[c['title'] for c in leaked_u]}"
    # 同用户异市场：绝不能看到 us_stock 的卡
    leaked_m = _u(tmp_path, db, "u1", "a_stock").get_relevant_cards("NVDA")
    assert leaked_m == [], f"跨市场泄露: {[c['title'] for c in leaked_m]}"
    # 本人本市场：两张都在
    own = _u(tmp_path, db, "u1", "us_stock").get_relevant_cards("NVDA")
    assert {c["title"] for c in own} == {"U1全局卡", "U1的NVDA卡"}


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
