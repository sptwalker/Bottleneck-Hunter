"""产业链缓存复用治理自检 —— 模型不一致重拆 + 退化缓存不遮蔽优质缓存。

根因 #14：用户选 deepseek 却复用了 MiniMax 拆的 79 节点稀疏旧链（表头显示 deepseek，
实际 chain.model_used=MiniMax-Text-01）。修复：get_fresh_chain 加模型匹配门 + low_power 跳过。
"""
from datetime import datetime, timezone, timedelta

import pytest

from bottleneck_hunter.chain.chain_store import ChainStore


def _mk_store(tmp_path):
    return ChainStore(db_path=tmp_path / "chains.db")


def _chain(sector, model, n_nodes, low_power=False):
    nodes = [{"name": "HBM", "layer": 0}]
    for i in range(1, n_nodes):
        nodes.append({"name": f"n{i}", "layer": (i % 4) + 1})
    meta = {"low_power": True} if low_power else {}
    return {"sector": sector, "nodes": nodes, "max_depth": 4, "metadata": meta}


def _save(store, product, model, n, low_power=False, sector="GPU"):
    return store.save_chain(product, _chain(sector, model, n, low_power), model_used=model)


def test_model_mismatch_not_reused(tmp_path):
    """只有 MiniMax 缓存，当前模型是 deepseek → 不复用（返回 None → 触发重拆）。"""
    store = _mk_store(tmp_path)
    _save(store, "HBM", "MiniMax-Text-01", 79)
    got = store.get_fresh_chain("HBM", min_depth=4, sector="GPU", current_model="deepseek-chat")
    assert got is None, "模型不一致必须不复用"


def test_same_model_reused(tmp_path):
    """同模型 deepseek 缓存 → 正常复用。"""
    store = _mk_store(tmp_path)
    _save(store, "HBM", "deepseek-chat", 457)
    got = store.get_fresh_chain("HBM", min_depth=4, sector="GPU", current_model="deepseek-chat")
    assert got is not None
    assert got["model_used"] == "deepseek-chat"


def test_low_power_skipped_prefers_healthy(tmp_path):
    """同模型有健康版和退化版 → 复用健康版，跳过 low_power。"""
    store = _mk_store(tmp_path)
    _save(store, "HBM", "deepseek-chat", 457)                 # v1 健康
    _save(store, "HBM", "deepseek-chat", 40, low_power=True)  # v2 退化(更新但被标)
    got = store.get_fresh_chain("HBM", min_depth=4, sector="GPU", current_model="deepseek-chat")
    assert got is not None
    assert len(got["chain_json"]["nodes"]) == 457, "应跳过退化版，选健康版"


def test_low_power_fallback_when_only_option(tmp_path):
    """只有退化版可选 → 兜底仍复用它（比重拆省），不返回 None。"""
    store = _mk_store(tmp_path)
    _save(store, "HBM", "deepseek-chat", 40, low_power=True)
    got = store.get_fresh_chain("HBM", min_depth=4, sector="GPU", current_model="deepseek-chat")
    assert got is not None, "无其它合格版本时容忍退化版兜底"


def test_empty_current_model_keeps_old_behavior(tmp_path):
    """current_model 为空（兼容旧调用）→ 不校验模型，复用最新合格版。"""
    store = _mk_store(tmp_path)
    _save(store, "HBM", "MiniMax-Text-01", 79)
    got = store.get_fresh_chain("HBM", min_depth=4, sector="GPU")  # 不传 current_model
    assert got is not None


def test_best_recent_node_count(tmp_path):
    """退化检测辅助：取同模型近期最优节点数，排除 partial/low_power。"""
    store = _mk_store(tmp_path)
    _save(store, "HBM", "deepseek-chat", 457)
    _save(store, "HBM", "deepseek-chat", 40, low_power=True)   # 退化版不计
    _save(store, "HBM", "MiniMax-Text-01", 600)               # 别的模型不计
    assert store.best_recent_node_count("HBM", "deepseek-chat", sector="GPU") == 457


def test_stale_cache_not_reused(tmp_path):
    """超 14 天的缓存 → 不复用。"""
    store = _mk_store(tmp_path)
    vid = _save(store, "HBM", "deepseek-chat", 457)
    old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    with store._connect() as conn:
        conn.execute("UPDATE chain_versions SET created_at=? WHERE id=?", (old, vid))
    got = store.get_fresh_chain("HBM", min_depth=4, sector="GPU", current_model="deepseek-chat")
    assert got is None, "过旧缓存不复用"
