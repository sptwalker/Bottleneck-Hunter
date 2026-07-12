"""按需翻译：缓存优先，只翻缺失，结果回缓存（新闻中英对照地基）。"""

from __future__ import annotations

from bottleneck_hunter.watchlist.store import WatchlistStore
from bottleneck_hunter.web import translate as T


async def test_translate_caches_and_only_missing(tmp_path, monkeypatch):
    s = WatchlistStore(str(tmp_path / "t.db"))
    T.set_store(s)
    calls = []

    async def fake_llm(texts, target):
        calls.append(list(texts))
        return {t: f"[{target}]{t}" for t in texts}

    monkeypatch.setattr(T, "_llm_translate", fake_llm)

    r1 = await T.translate_texts(["hello", "world"], "zh")
    assert r1 == {"hello": "[zh]hello", "world": "[zh]world"}
    assert calls == [["hello", "world"]]                      # 一次批量翻译

    r2 = await T.translate_texts(["hello", "world"], "zh")     # 全缓存命中
    assert r2 == r1 and len(calls) == 1                        # 未再调 LLM

    await T.translate_texts(["hello", "brand-new"], "zh")      # 部分命中
    assert calls[-1] == ["brand-new"]                          # 只翻缺失的


async def test_translate_empty_and_dedup(tmp_path, monkeypatch):
    s = WatchlistStore(str(tmp_path / "t.db"))
    T.set_store(s)

    async def fake_llm(texts, target):
        return {t: t.upper() for t in texts}

    monkeypatch.setattr(T, "_llm_translate", fake_llm)
    assert await T.translate_texts([], "zh") == {}
    assert await T.translate_texts(["", "  "], "zh") == {}     # 空白过滤
    r = await T.translate_texts(["a", "a", "b"], "en")         # 去重
    assert r == {"a": "A", "b": "B"}
