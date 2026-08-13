"""P0-② 决策证据溯源：prompt_hash 稳定性 + build_provenance 键完整性自检。

- prompt_hash 对真实 prompt 稳定、文件变即变、缺失=missing
- build_provenance 键齐全、models 两种入参归一、tickers 去重排序、extra 合并
"""
from bottleneck_hunter.watchlist import provenance as prov


def test_prompt_hash_stable_and_changes(tmp_path, monkeypatch):
    d = tmp_path / "prompts"
    d.mkdir()
    (d / "demo.md").write_text("hello 提示词", encoding="utf-8")
    monkeypatch.setattr(prov, "_PROMPTS_DIR", d)
    prov.prompt_hash.cache_clear()

    h1 = prov.prompt_hash("demo")
    assert len(h1) == 12 and h1 != "missing"
    assert prov.prompt_hash("demo") == h1  # 稳定

    (d / "demo.md").write_text("hello 提示词 改一字", encoding="utf-8")
    prov.prompt_hash.cache_clear()  # 生产中 prompt 静态；测试显式清缓存验证内容敏感
    assert prov.prompt_hash("demo") != h1  # 文件变即变

    assert prov.prompt_hash("不存在的模板") == "missing"


def test_prompt_hash_real_prompt():
    """真实 chain/prompts/decision_macro.md 存在且可哈希。"""
    prov.prompt_hash.cache_clear()
    h = prov.prompt_hash("decision_macro")
    assert h != "missing" and len(h) == 12


def test_build_provenance_keys_and_normalization():
    p = prov.build_provenance(
        prompts=["decision_macro"],
        models=[("deepseek", "deepseek-chat"), {"provider": "qwen", "model": "qwen-max"}],
        data_as_of="2026-08-13",
        tickers=["NVDA", "AAPL", "NVDA", ""],  # 去重 + 剔空 + 排序
        generated_at="2026-08-13T00:00:00Z",
        extra={"market": "us_stock"},
    )
    assert set(p["prompt_hashes"]) == {"decision_macro"}
    assert p["models_used"] == [
        {"provider": "deepseek", "model": "deepseek-chat"},
        {"provider": "qwen", "model": "qwen-max"},
    ]
    assert p["data_as_of"] == "2026-08-13"
    assert p["tickers"] == ["AAPL", "NVDA"]
    assert p["generated_at"] == "2026-08-13T00:00:00Z"
    assert p["market"] == "us_stock"


def test_build_provenance_defaults():
    p = prov.build_provenance(prompts=[], models=[])
    assert p["prompt_hashes"] == {} and p["models_used"] == []
    assert p["tickers"] == []
    assert p["generated_at"].endswith("Z")  # 缺省填当前 UTC


def test_hash_text_stable_and_changes():
    """内联 prompt 字符串哈希：同串稳定、改一字即变、12 位。"""
    h = prov.hash_text("你是持仓顾问，输出减/持/加建议")
    assert len(h) == 12
    assert prov.hash_text("你是持仓顾问，输出减/持/加建议") == h  # 稳定
    assert prov.hash_text("你是持仓顾问，输出减/持/加建议 v2") != h  # 内容敏感
    assert prov.hash_text("") == prov.hash_text("")  # 空串不崩


def test_build_provenance_extra_prompt_hashes():
    """extra_prompt_hashes 并入 prompt_hashes，与 .md 名共存（VIP 内联模板走此路）。"""
    p = prov.build_provenance(
        prompts=["decision_macro"], models=[("anthropic", "claude")],
        extra_prompt_hashes={"vip_draft": prov.hash_text("草稿模板")},
    )
    assert "decision_macro" in p["prompt_hashes"]           # .md 名照常
    assert p["prompt_hashes"]["vip_draft"] == prov.hash_text("草稿模板")  # 内联并入
    assert p["prompt_hashes"]["decision_macro"] != "missing"


def test_decision_provenance_helper():
    """decision_engine._decision_provenance：真实 prompt 可哈希 + 模型归一 + 市场/层/ticker/快照日齐全。"""
    from bottleneck_hunter.watchlist.decision_engine import _decision_provenance
    p = _decision_provenance(["decision_tactical"], [("deepseek", "deepseek-chat")],
                             "us_stock", "L3", ["nvda", "AAPL", "nvda"])
    assert p["prompt_hashes"]["decision_tactical"] != "missing"  # 真实 .md
    assert p["models_used"] == [{"provider": "deepseek", "model": "deepseek-chat"}]
    assert p["market"] == "us_stock" and p["layer"] == "L3"
    assert p["tickers"] == ["AAPL", "nvda"]  # 去重排序（大小写敏感）
    assert p["data_as_of"] and p["generated_at"].endswith("Z")  # 快照日=_today()


def test_decision_provenance_rule_based_empty():
    """规则决策（硬止损）：prompts/models 空 → prompt_hashes/models_used 空但市场/层仍在（诚实标注非 LLM）。"""
    from bottleneck_hunter.watchlist.decision_engine import _decision_provenance
    p = _decision_provenance([], [], "a_stock", "L4", ["600519"])
    assert p["prompt_hashes"] == {} and p["models_used"] == []
    assert p["market"] == "a_stock" and p["layer"] == "L4"
    assert p["tickers"] == ["600519"]


def test_provenance_survives_execution_plan_roundtrip(tmp_path):
    """验收：_provenance 嵌进 result_json 经真实 SQLite 落库→读回不丢（零表迁移的实证）。

    对应验收标准「任取一条 execution_plan.result_json 能读到 _provenance」——
    走真实 store，而非仅断言注入行存在。
    """
    from bottleneck_hunter.watchlist.decision_engine import _decision_provenance
    from bottleneck_hunter.watchlist.store import WatchlistStore

    s = WatchlistStore(str(tmp_path / "acc.db"))
    entry_id = s.add({"ticker": "NVDA", "company_name": "NVIDIA", "market": "us_stock", "tier": "track"})
    ep = {
        "action": "buy", "shares": 50, "target_price": 188.0, "confidence": 7,
        "_provenance": _decision_provenance(["decision_execution"], [("deepseek", "deepseek-chat")],
                                            "us_stock", "L4", ["NVDA"]),
    }
    plan_id = s.create_execution_plan("tac_x", entry_id, "NVDA", ep)

    got = s.get_execution_plan(plan_id)["result_json"]  # 读回已是解析后的 dict
    prov_back = got["_provenance"]
    assert prov_back["prompt_hashes"]["decision_execution"] != "missing"
    assert prov_back["models_used"] == [{"provider": "deepseek", "model": "deepseek-chat"}]
    assert prov_back["layer"] == "L4" and prov_back["market"] == "us_stock"
    assert prov_back["tickers"] == ["NVDA"] and prov_back["data_as_of"]
    # 亦经 pending 列表读路径（前端/投委会实际取数口径）
    pending = s.get_pending_executions()
    assert pending[0]["result_json"]["_provenance"]["layer"] == "L4"


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
