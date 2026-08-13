"""研报历史留存（每键最近 6 份）+ AI 演变对比 自检 —— 信任边界优先：

- 迁移无损：旧结构(三列复合主键、无 id)的生产行，实例化 WatchlistStore 触发重建后仍读得到、可与新行共存
- 环形留存：同键 save 8 次 → 恰留最近 6 份、最旧两份被挤掉、get 取最新
- 同秒定序：秒级 uploaded_at 并列时靠自增 id 二级键定序，「最新」确定
- 越权隔离：get/delete_by_id 换 user 或 market 一律读不到/删不到（id 全局自增，必叠 user+market 过滤）
- AI 对比流式：mock 模型/流式原语 → stream_report_compare 产出 chunk→done；不足两份 → error

_iter_tokens/get_models_for_role 均 mock，不打真实 LLM。
"""
import asyncio
import sqlite3

from bottleneck_hunter.watchlist import macro_consultation as mc
from bottleneck_hunter.watchlist.store import WatchlistStore

_OLD_SCHEMA = """CREATE TABLE focus_reports (
    ticker TEXT NOT NULL, filename TEXT DEFAULT '', report_text TEXT DEFAULT '',
    char_len INTEGER DEFAULT 0, uploaded_at TEXT, user_id TEXT DEFAULT '', market TEXT DEFAULT '',
    PRIMARY KEY(ticker, user_id, market))"""


def _store(tmp_path):
    return WatchlistStore(str(tmp_path / "wl.db")).for_user("u1").for_market("us_stock")


async def _drain(agen):
    return [ev async for ev in agen]


def test_migration_lossless(tmp_path):
    """旧结构表(无 id)插一行 → 实例化 Store 触发重建 → id 列已加、旧行仍读得到、同键可与新行共存。"""
    db = tmp_path / "wl.db"
    conn = sqlite3.connect(str(db))
    conn.execute(_OLD_SCHEMA)
    conn.execute(
        "INSERT INTO focus_reports(ticker,filename,report_text,char_len,uploaded_at,user_id,market) "
        "VALUES(?,?,?,?,?,?,?)",
        ("NVDA", "old.pdf", "OLD BODY", 8, "2026-08-01T00:00:00+00:00", "u1", "us_stock"),
    )
    conn.commit()
    conn.close()

    st = WatchlistStore(str(db)).for_user("u1").for_market("us_stock")  # __init__ 触发 _init_db 迁移
    cols = [r[1] for r in sqlite3.connect(str(db)).execute("PRAGMA table_info(focus_reports)").fetchall()]
    assert "id" in cols                                   # 已加自增 id
    r = st.get_focus_report("NVDA")
    assert r and r["report_text"] == "OLD BODY" and r.get("id")   # 旧行无损、获新 id
    st.save_focus_report("NVDA", "new.pdf", "NEW BODY")
    assert len(st.list_focus_reports("NVDA")) == 2         # 新旧共存（不再覆盖）
    assert st.get_focus_report("NVDA")["report_text"] == "NEW BODY"  # get 取最新


def test_ring_retention(tmp_path):
    """同键 save 8 次 → 恰留最近 6 份、最旧两份(f0/f1)被挤掉、get 取最新。"""
    st = _store(tmp_path)
    for i in range(8):
        st.save_focus_report("AAPL", f"f{i}.pdf", f"v{i}")
    items = st.list_focus_reports("AAPL")
    assert len(items) == 6                                 # 防膨胀：只留 6
    assert {it["filename"] for it in items} == {f"f{i}.pdf" for i in range(2, 8)}  # 最旧两份被挤掉
    assert st.get_focus_report("AAPL")["report_text"] == "v7"          # get 取最新
    # 该键实际总行数就是 6（修剪确实删库，非仅 LIMIT 掩盖）
    raw = sqlite3.connect(str(tmp_path / "wl.db")).execute(
        "SELECT COUNT(*) FROM focus_reports WHERE ticker='AAPL'").fetchone()[0]
    assert raw == 6


def test_same_second_ordering(tmp_path):
    """秒级时间戳并列时，自增 id 兜底定序：最后一次 save 稳定胜出、list 严格倒序。"""
    st = _store(tmp_path)
    for c in ("a", "b", "c"):
        st.save_focus_report("MSFT", f"{c}.pdf", c)        # 三次几乎同秒
    assert st.get_focus_report("MSFT")["report_text"] == "c"
    ids = [it["id"] for it in st.list_focus_reports("MSFT")]
    assert ids == sorted(ids, reverse=True)                # id 倒序 = 最新在前，无并列抖动


def test_ownership_isolation(tmp_path):
    """id 全局自增：换 user 或换 market，get/delete_by_id 一律读不到/删不到。"""
    st = _store(tmp_path)
    st.save_focus_report("TSLA", "f.pdf", "SECRET")
    rid = st.get_focus_report("TSLA")["id"]
    assert st.get_focus_report_by_id(rid)["report_text"] == "SECRET"   # 本人本市场可读
    assert st.for_user("u2").get_focus_report_by_id(rid) is None       # 换用户读不到
    assert st.for_market("a_stock").get_focus_report_by_id(rid) is None  # 换市场读不到
    assert st.for_user("u2").delete_focus_report_by_id(rid) is False   # 换用户删不到
    assert st.get_focus_report_by_id(rid) is not None                  # 越权删未生效，原件还在
    assert st.delete_focus_report_by_id(rid) is True                   # 本人可删
    assert st.get_focus_report_by_id(rid) is None


def test_compare_stream(tmp_path, monkeypatch):
    """两份研报 → stream_report_compare 产出 chunk(带文本)→done；prompt 分清早/晚两期。"""
    seen = {}

    async def _fake_iter(llm, prompt):
        seen["prompt"] = prompt
        for t in ("叙事变化：", "从谨慎转积极。"):
            yield t

    monkeypatch.setattr(mc, "get_models_for_role", lambda role, **kw: [(object(), "deepseek", "x")])
    monkeypatch.setattr(mc, "_iter_tokens", _fake_iter)

    st = _store(tmp_path)
    st.save_focus_report("AMD", "wk1.pdf", "上期：通胀顽固、维持谨慎")
    st.save_focus_report("AMD", "wk2.pdf", "本期：通胀回落、转向积极")

    events = asyncio.run(_drain(mc.stream_report_compare(st, None, ticker="AMD", market="us_stock")))
    kinds = [e["event"] for e in events]
    assert "chunk" in kinds and kinds[-1] == "done"
    assert "".join(e["data"].get("text", "") for e in events if e["event"] == "chunk") == "叙事变化：从谨慎转积极。"
    # 早/晚两期都进 prompt，且早在前
    p = seen["prompt"]
    assert "上期：通胀顽固" in p and "本期：通胀回落" in p
    assert p.index("上期：通胀顽固") < p.index("本期：通胀回落")


def test_compare_needs_two(tmp_path, monkeypatch):
    """不足两份 → 直接 error，不打模型。"""
    monkeypatch.setattr(mc, "get_models_for_role", lambda role, **kw: [(object(), "deepseek", "x")])
    st = _store(tmp_path)
    st.save_focus_report("GOOG", "only.pdf", "唯一一份")
    events = asyncio.run(_drain(mc.stream_report_compare(st, None, ticker="GOOG", market="us_stock")))
    assert len(events) == 1 and events[0]["event"] == "error"
    assert "两份" in events[0]["data"]["message"]


def test_compare_macro_partition(tmp_path, monkeypatch):
    """宏观(空 ticker)对比走 __macro__ 分区哨兵键，与个股互不串。"""
    async def _fake_iter(llm, prompt):
        yield "宏观演变"

    monkeypatch.setattr(mc, "get_models_for_role", lambda role, **kw: [(object(), "deepseek", "x")])
    monkeypatch.setattr(mc, "_iter_tokens", _fake_iter)

    st = _store(tmp_path)
    macro = st.for_market(mc.MACRO_REPORT_MARKET)
    macro.save_focus_report(mc.MACRO_REPORT_KEY, "m1.pdf", "宏观上期")
    macro.save_focus_report(mc.MACRO_REPORT_KEY, "m2.pdf", "宏观本期")
    events = asyncio.run(_drain(mc.stream_report_compare(st, None, ticker="", market="us_stock")))
    assert [e["event"] for e in events][-1] == "done"


def test_migration_reentrant_after_crash(tmp_path):
    """崩溃残留：旧结构 focus_reports(无 id) + 上次迁移崩在 DROP/RENAME 前留下的空 focus_reports_new
    孤儿表 → 再实例化 Store 应自愈(先 DROP 孤儿再重建)，旧行无损、无重复、不再永久卡死。"""
    db = tmp_path / "wl.db"
    conn = sqlite3.connect(str(db))
    conn.execute(_OLD_SCHEMA)
    conn.execute(
        "INSERT INTO focus_reports(ticker,filename,report_text,char_len,uploaded_at,user_id,market) "
        "VALUES(?,?,?,?,?,?,?)",
        ("NVDA", "old.pdf", "OLD BODY", 8, "2026-08-01T00:00:00+00:00", "u1", "us_stock"),
    )
    conn.execute("CREATE TABLE focus_reports_new (id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT)")  # 孤儿表
    conn.commit()
    conn.close()

    st = WatchlistStore(str(db)).for_user("u1").for_market("us_stock")  # 不应抛错，静默自愈
    cols = [r[1] for r in sqlite3.connect(str(db)).execute("PRAGMA table_info(focus_reports)").fetchall()]
    assert "id" in cols                                    # 重建成功(孤儿没挡住 CREATE)
    items = st.list_focus_reports("NVDA")
    assert len(items) == 1 and items[0]["filename"] == "old.pdf"   # 旧行无损、无重复
    assert st.get_focus_report("NVDA")["report_text"] == "OLD BODY"


def test_compare_caseless_and_no_self_compare(tmp_path, monkeypatch):
    """小写 ticker 也命中(save/read 均 .upper() 对齐)；old_id==new_id 经去重塌成一份 → error 不自比。"""
    async def _fake_iter(llm, prompt):
        yield "对比"

    monkeypatch.setattr(mc, "get_models_for_role", lambda role, **kw: [(object(), "deepseek", "x")])
    monkeypatch.setattr(mc, "_iter_tokens", _fake_iter)

    st = _store(tmp_path)
    st.save_focus_report("AMD", "wk1.pdf", "上期")
    st.save_focus_report("AMD", "wk2.pdf", "本期")
    rid = st.list_focus_reports("AMD")[0]["id"]

    ev_lower = asyncio.run(_drain(mc.stream_report_compare(st, None, ticker="amd", market="us_stock")))
    assert [e["event"] for e in ev_lower][-1] == "done"    # 小写 ticker 仍命中两份

    ev_self = asyncio.run(_drain(
        mc.stream_report_compare(st, None, ticker="AMD", market="us_stock", old_id=rid, new_id=rid)))
    assert len(ev_self) == 1 and ev_self[0]["event"] == "error"  # 同一份去重后不足两份，拒绝自比


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
