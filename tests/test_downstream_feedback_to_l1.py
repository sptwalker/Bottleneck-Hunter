"""P2-D（N-15）投委会裁决回流 L1：get_committee_rejection_summary + _format_downstream_feedback。

N-15 的病：投委会的裁决到不了 L1。同一标的反复被否、而 L1 仍写着"超配"，两边持续相互抵消，
每轮烧一次全链算力，用户只看到"一直建议买、一直不执行"两套说辞。

本轮只做**留痕 + 进 prompt**，不自动改 regime。所以这里守的是三件事：
  ① 聚合口径必须只算"委员会说了不"（rejected / needs_review），
     不能把 needs_discussion（僵持未决）算成"已被否决"；
  ② 多用户 / 多市场隔离 —— 别人的否决记录不得出现在我的 L1 prompt 里；
  ③ L1 日检是每日必跑链路：附属读库失败必须吞成"无"，绝不能让整轮日检挂掉。

第 ③ 条是本文件存在的主要理由：它守的是"宁可没反馈，也不能因反馈而断链"这个取舍。
"""
from __future__ import annotations

import pytest

from bottleneck_hunter.watchlist.decision_engine import _format_downstream_feedback
from bottleneck_hunter.watchlist.store import WatchlistStore


@pytest.fixture
def db(tmp_path, monkeypatch):
    from bottleneck_hunter.auth import store as auth_store_mod
    monkeypatch.setattr(auth_store_mod, "_DEFAULT_DB", tmp_path / "auth.db")
    return tmp_path / "wl.db"


def _seed(base, table, cols, rows):
    """裸 SQL 批量插入（显式带 user_id/market，绕过 _filtered，用于构造样本）。"""
    ph = ",".join("?" * len(cols))
    with base._write_conn() as conn:
        for r in rows:
            conn.execute(f"INSERT INTO {table} ({','.join(cols)}) VALUES ({ph})", r)


def _plan(base, pid, ticker, action="buy", user="u1", market="us_stock", entry="e1"):
    _seed(base, "execution_plans",
          ["id", "ticker", "action", "entry_id", "created_at", "user_id", "market"],
          [(pid, ticker, action, entry, "2026-09-01T00:00:00", user, market)])


def _verdict(base, cid, pid, verdict, user="u1", market="us_stock", at="2026-09-01T00:00:00"):
    _seed(base, "committee_consensus",
          ["id", "execution_plan_id", "final_verdict", "created_at", "user_id", "market"],
          [(cid, pid, verdict, at, user, market)])


# ── ① 聚合口径 ──────────────────────────────────────────────────────────────

def test_only_rejected_and_needs_review_count(db):
    """rejected / needs_review 计入；approved 系列与 needs_discussion 一律不计。"""
    base = WatchlistStore(db)
    _plan(base, "p1", "ORCL")
    for i, v in enumerate(["rejected", "rejected", "needs_review"]):
        _verdict(base, f"c{i}", "p1", v)
    # 这些是"委员会没说不行"，不能算成被否
    for i, v in enumerate(["approved", "approved_with_modifications", "needs_discussion"]):
        _verdict(base, f"ok{i}", "p1", v)

    rows = base.for_user("u1").for_market("us_stock").get_committee_rejection_summary(min_count=2)

    assert len(rows) == 1
    assert rows[0]["ticker"] == "ORCL"
    assert rows[0]["rejects"] == 3  # 2 rejected + 1 needs_review；approved×2 与讨论中不计


def test_needs_discussion_alone_is_not_a_rejection(db):
    """只被"僵持未决"过、从未被否的标的，不得出现在反馈里（把"还在吵"记成"已否掉"是失真）。"""
    base = WatchlistStore(db)
    _plan(base, "p1", "TSLA")
    for i in range(5):
        _verdict(base, f"c{i}", "p1", "needs_discussion")

    rows = base.for_user("u1").for_market("us_stock").get_committee_rejection_summary(min_count=2)
    assert rows == []


def test_min_count_filters_single_rejection(db):
    """被否 1 次是噪声（个股时点/数据质量问题），min_count=2 起才算"反复"。"""
    base = WatchlistStore(db)
    _plan(base, "p1", "AAA")
    _plan(base, "p2", "BBB")
    _verdict(base, "c1", "p1", "rejected")
    _verdict(base, "c2", "p2", "rejected")
    _verdict(base, "c3", "p2", "rejected")

    rows = base.for_user("u1").for_market("us_stock").get_committee_rejection_summary(min_count=2)
    assert [r["ticker"] for r in rows] == ["BBB"]

    # min_count=1 → 两条都出；按被否次数降序
    rows = base.for_user("u1").for_market("us_stock").get_committee_rejection_summary(min_count=1)
    assert [r["ticker"] for r in rows] == ["BBB", "AAA"]


def test_days_window_excludes_old_rejections(db):
    """窗口外的旧否决不计入（否则"半年前被否过"会永久挂在 L1 prompt 上）。"""
    base = WatchlistStore(db)
    _plan(base, "p1", "AAA")
    _plan(base, "p2", "BBB")
    _verdict(base, "c1", "p1", "rejected", at="2026-01-01T00:00:00")  # 远超 30 天
    _verdict(base, "c2", "p1", "rejected", at="2026-01-01T00:00:00")
    _verdict(base, "c3", "p2", "rejected")                            # date('now')
    _verdict(base, "c4", "p2", "rejected")

    rows = base.for_user("u1").for_market("us_stock").get_committee_rejection_summary(
        days=30, min_count=2)
    assert [r["ticker"] for r in rows] == ["BBB"]


def test_ticker_action_grouping_split(db):
    """同一标的的买/卖意图分开聚合 —— "买被否 3 次"与"卖被否 3 次"是两种不同的分歧。"""
    base = WatchlistStore(db)
    _plan(base, "p1", "ORCL", action="buy")
    _plan(base, "p2", "ORCL", action="sell")
    for i in range(3):
        _verdict(base, f"b{i}", "p1", "rejected")
        _verdict(base, f"s{i}", "p2", "rejected")

    rows = base.for_user("u1").for_market("us_stock").get_committee_rejection_summary(min_count=2)
    assert {(r["ticker"], r["action"], r["rejects"]) for r in rows} == {
        ("ORCL", "buy", 3), ("ORCL", "sell", 3)}


def test_limit_caps_rows(db):
    """limit 生效，且取的是被否次数最多的那几个。"""
    base = WatchlistStore(db)
    for n, tk in enumerate(["A", "B", "C"]):
        pid = f"p{n}"
        _plan(base, pid, tk)
        for i in range(n + 2):  # A:2 B:3 C:4
            _verdict(base, f"{tk}{i}", pid, "rejected")

    rows = base.for_user("u1").for_market("us_stock").get_committee_rejection_summary(
        min_count=2, limit=2)
    assert [r["ticker"] for r in rows] == ["C", "B"]


def test_sector_attached_when_present_and_none_when_absent(db):
    """sector 只是附带展示字段：有值带出，无值不得让整条记录消失（生产上 A 股 sector 全空）。"""
    base = WatchlistStore(db)
    _plan(base, "p1", "ORCL")
    _plan(base, "p2", "600519.SS", market="a_stock")
    _seed(base, "watchlist", ["id", "ticker", "company_name", "tier", "sector",
                              "added_at", "user_id", "market"],
          [("w1", "ORCL", "Oracle", "focus", "Technology", "2026-09-01", "u1", "us_stock")])
    # 600519.SS 故意不入 watchlist → LEFT JOIN 后 sector 为 NULL
    for i in range(2):
        _verdict(base, f"u{i}", "p1", "rejected")
        _verdict(base, f"a{i}", "p2", "rejected", market="a_stock")

    us = base.for_user("u1").for_market("us_stock").get_committee_rejection_summary(min_count=2)
    assert us[0]["sector"] == "Technology"

    cn = base.for_user("u1").for_market("a_stock").get_committee_rejection_summary(min_count=2)
    assert len(cn) == 1 and cn[0]["ticker"] == "600519.SS"
    assert not (cn[0]["sector"] or "")  # 没有板块照样出记录


# ── ② 隔离 ─────────────────────────────────────────────────────────────────

def test_user_and_market_isolation(db):
    """u2 的否决、或别的市场的否决，绝不出现在 u1 的 L1 prompt 里。"""
    base = WatchlistStore(db)
    _plan(base, "p1", "MINE")                                            # u1/us
    _plan(base, "p2", "OTHERUSER", user="u2")                            # u2/us
    _plan(base, "p3", "CN", market="a_stock")                            # u1/a_stock
    for i in range(3):
        _verdict(base, f"m{i}", "p1", "rejected")
        _verdict(base, f"o{i}", "p2", "rejected", user="u2")
        _verdict(base, f"n{i}", "p3", "rejected", market="a_stock")

    us = base.for_user("u1").for_market("us_stock").get_committee_rejection_summary(min_count=2)
    assert [r["ticker"] for r in us] == ["MINE"]

    assert base.for_user("u2").for_market("us_stock") \
        .get_committee_rejection_summary(min_count=2)[0]["ticker"] == "OTHERUSER"


# ── ③ 渲染 + 吞异常（本文件的核心理由） ──────────────────────────────────────

def test_format_empty_returns_none_marker(db):
    """无反馈 → 字面"无"（prompt 里写的是"显示无时不要凭空评论执行层"）。"""
    assert _format_downstream_feedback(WatchlistStore(db).for_user("u1").for_market("us_stock")) == "无"


def test_format_renders_ticker_action_count_date(db):
    """渲染出标的/意图/次数/日期；sector 有值才追加，无值不留空标签。"""
    base = WatchlistStore(db)
    _plan(base, "p1", "ORCL")
    _plan(base, "p2", "600519.SS")
    _seed(base, "watchlist", ["id", "ticker", "company_name", "tier", "sector",
                              "added_at", "user_id", "market"],
          [("w1", "ORCL", "Oracle", "focus", "Technology", "2026-09-01", "u1", "us_stock")])
    for i in range(3):
        _verdict(base, f"c{i}", "p1", "rejected", at="2026-09-20T00:00:00")
    for i in range(2):
        _verdict(base, f"d{i}", "p2", "rejected", at="2026-09-21T00:00:00")

    text = _format_downstream_feedback(base.for_user("u1").for_market("us_stock"))

    assert "ORCL" in text and "buy" in text and "3" in text
    assert "2026-09-20" in text
    assert "Technology" in text
    assert "600519.SS" in text
    assert "板块：" not in text.split("600519.SS")[1]  # 无 sector 的条目不追加空标签


def test_format_swallows_store_exception(db):
    """P2-D 的关键取舍：读库失败 → 退化成"无"，不抛。

    L1 日检是每日必跑的全链入口，一个**参考信息**的读库失败让它整轮挂掉，代价远大于收益。
    这条测试故意让 store 抛异常：若把 try/except 去掉，L1 日检会在 prompt 组装阶段直接崩。
    """
    class Boom:
        def get_committee_rejection_summary(self, **_kw):
            raise RuntimeError("db down")

    assert _format_downstream_feedback(Boom()) == "无"


def test_format_limit_is_five(db):
    """上限 5 条：prompt 是每日必带的，不能让它无限膨胀。"""
    base = WatchlistStore(db)
    for n in range(7):
        pid = f"p{n}"
        _plan(base, pid, f"T{n}")
        for i in range(2):
            _verdict(base, f"{n}-{i}", pid, "rejected")

    text = _format_downstream_feedback(base.for_user("u1").for_market("us_stock"))
    assert len(text.splitlines()) == 5


if __name__ == "__main__":
    import io
    import sys
    import tempfile
    from pathlib import Path

    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "wl.db"
        b = WatchlistStore(p)
        _plan(b, "p1", "ORCL")
        for i in range(3):
            _verdict(b, f"c{i}", "p1", "rejected", at="2026-09-20T00:00:00")
        _plan(b, "p2", "TSLA")
        for i in range(4):
            _verdict(b, f"t{i}", "p2", "needs_discussion")

        st = b.for_user("u1").for_market("us_stock")
        rows = st.get_committee_rejection_summary(min_count=2)
        assert [r["ticker"] for r in rows] == ["ORCL"], rows
        print("self-check 聚合口径 OK:", rows)

        txt = _format_downstream_feedback(st)
        assert "ORCL" in txt and "TSLA" not in txt, txt
        print("self-check 渲染 OK:\n" + txt)

        class Boom:
            def get_committee_rejection_summary(self, **_kw):
                raise RuntimeError("down")

        assert _format_downstream_feedback(Boom()) == "无"
        print("self-check 吞异常 OK")
