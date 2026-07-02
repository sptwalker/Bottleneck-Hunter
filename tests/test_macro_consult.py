"""L1 宏观咨询互动自检 — 守住 4 个关键行为：单模型回绕 / 落库回读 / 两周摘要触发 / 上下文有界。"""

import asyncio
from datetime import datetime, timedelta, timezone

from bottleneck_hunter.watchlist import macro_consultation as mc
from bottleneck_hunter.watchlist.store import WatchlistStore


def _iso_days_ago(days):
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")


def test_analyst_llm_single_model():
    """L1_macro 只配 1 个模型时，两位分析师都能拿到 llm（slot 取模回绕），不越界。"""
    models = [("LLM_A", "deepseek", "deepseek-chat")]  # 长度 1
    assert mc._analyst_llm(models, 0) == models[0]
    assert mc._analyst_llm(models, 1) == models[0]     # slot1 回绕到 slot0
    two = [("LLM_A", "p", "m"), ("LLM_B", "q", "n")]
    assert mc._analyst_llm(two, 0) == two[0]
    assert mc._analyst_llm(two, 1) == two[1]


def test_transcript_roundtrip(tmp_path):
    """user + analyst(round1×2, round2×2) 落库后可原样读回。"""
    store = WatchlistStore(str(tmp_path / "t.db"))
    transcript = [{"type": "user", "ts": mc._now_iso(), "content": "该防守还是进攻？"}]
    for rnd in (1, 2):
        for a in mc.ANALYSTS:
            transcript.append({"type": "analyst", "ts": mc._now_iso(), "round": rnd,
                               "role": a["role"], "name": a["label"], "content": f"{a['role']} r{rnd}"})
    rid = store.create_meeting_record(meeting_type=mc.MEETING_TYPE, title="宏观咨询 · us_stock",
                                      market="us_stock", transcript_json=transcript, result_json={})
    assert rid
    recs = store.get_meeting_records(meeting_type=mc.MEETING_TYPE, market="us_stock", limit=1)
    assert len(recs) == 1
    tr = recs[0]["transcript_json"]
    assert len(tr) == 5
    analysts = [m for m in tr if m["type"] == "analyst"]
    assert {m["round"] for m in analysts} == {1, 2}
    assert {m["role"] for m in analysts} == {a["role"] for a in mc.ANALYSTS}


def test_compress_trigger(tmp_path, monkeypatch):
    """≥SUMMARY_TRIGGER 条两周前消息 → 生成 1 条摘要并推进 upto；不足则不触发。"""
    class _Resp:
        content = "这是滚动摘要正文。"
    class _LLM:
        def invoke(self, prompt):
            return _Resp()
    monkeypatch.setattr(mc, "get_models_for_role", lambda role: [(_LLM(), "deepseek", "x")])

    store = WatchlistStore(str(tmp_path / "t.db"))
    old_ts = _iso_days_ago(30)  # 远早于 14 天
    many = [{"type": "user" if i % 2 else "analyst", "ts": old_ts, "round": 1,
             "role": "macro_market", "content": f"m{i}"} for i in range(mc.SUMMARY_TRIGGER + 1)]
    rid = store.create_meeting_record(meeting_type=mc.MEETING_TYPE, market="us_stock",
                                      title="t", transcript_json=many, result_json={})
    asyncio.run(mc._maybe_compress(store, None, rid))
    rec = store.get_meeting_record(rid)
    summaries = [m for m in rec["transcript_json"] if m.get("type") == "summary"]
    assert len(summaries) == 1
    assert summaries[0]["folded_count"] == mc.SUMMARY_TRIGGER + 1
    assert rec["result_json"].get("unfolded_summarized_upto")

    # 不足阈值：不触发
    store2 = WatchlistStore(str(tmp_path / "t2.db"))
    few = [{"type": "analyst", "ts": old_ts, "round": 1, "role": "macro_market", "content": f"m{i}"}
           for i in range(10)]
    rid2 = store2.create_meeting_record(meeting_type=mc.MEETING_TYPE, market="us_stock",
                                        title="t", transcript_json=few, result_json={})
    asyncio.run(mc._maybe_compress(store2, None, rid2))
    rec2 = store2.get_meeting_record(rid2)
    assert not [m for m in rec2["transcript_json"] if m.get("type") == "summary"]


def test_context_bounded():
    """500 条对话 → 上下文只取最近 MAX_RECENT 条 + 摘要，不喂全量。"""
    transcript = [{"type": "summary", "ts": mc._now_iso(), "content": "旧摘要"}]
    transcript += [{"type": "analyst", "ts": mc._now_iso(), "round": 1, "role": "macro_market",
                    "name": "🌐 宏观市场分析师", "content": "x" * 2000} for _ in range(500)]
    ctx = mc._context_for_prompt(transcript)
    # 上界：摘要 + MAX_RECENT 条（每条截 CONTENT_CAP）
    assert len(ctx) <= (mc.MAX_RECENT + 1) * (mc.CONTENT_CAP + 120)
    # 只保留最近 MAX_RECENT 条对话（+1 摘要行）
    assert ctx.count("宏观市场分析师") <= mc.MAX_RECENT


def test_portfolio_context_watchlist(tmp_path):
    """观察池个股进入快照上下文；持仓采集恒返回 list（空仓不报错）。"""
    store = WatchlistStore(str(tmp_path / "t.db"))
    store.add({"ticker": "NVDA", "company_name": "NVIDIA", "tier": "focus",
               "market": "us_stock", "sector": "半导体"})
    store.add({"ticker": "0700.HK", "company_name": "腾讯", "tier": "focus",
               "market": "hk_stock", "sector": "互联网"})
    wl, pos = mc._portfolio_context(store, "us_stock")
    tickers = {w["ticker"] for w in wl}
    assert "NVDA" in tickers          # 本市场观察池个股入清单
    assert "0700.HK" not in tickers   # 他市场不串
    assert wl[0]["name"] == "NVIDIA" and wl[0]["sector"] == "半导体"
    assert isinstance(pos, list)      # 空仓返回空 list，不抛异常
    # 快照文本包含观察池段
    snap = mc._snapshot_entry({"indices": {}, "sentiment": {}, "macro": {}, "sectors": {}, "news": []}, None)
    snap["watchlist"], snap["positions"] = wl, pos
    text = mc._snapshot_text(snap)
    assert "用户观察池" in text and "NVDA" in text
    assert "空仓" in text             # positions=[] → 显式"空仓"
