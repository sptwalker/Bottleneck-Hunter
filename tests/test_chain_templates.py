"""特性四 · 产业链共享模板库：用户隔离 + 公开可见 + owner-only 改删 + 快照重建。"""
from __future__ import annotations

from bottleneck_hunter.chain.models import ChainGraph
from bottleneck_hunter.dataflows.store import AnalysisStore

_CHAIN = {
    "sector": "GPU/AI算力", "end_product": "GPU", "max_depth": 3,
    "nodes": [{"name": "HBM", "description": "高带宽显存", "layer": 1,
               "layer_type": "material", "function": "存储"}],
    "links": [], "version": 1,
}


def _store(tmp_path, uid):
    return AnalysisStore(db_path=tmp_path / "a.db").for_user(uid)


def test_owner_isolation_and_public_visibility(tmp_path):
    a = _store(tmp_path, "userA")
    b = _store(tmp_path, "userB")
    tid = a.save_template(template_name="我的链", chain_json=_CHAIN,
                          sector="GPU/AI算力", end_product="GPU")

    # (1) A 私有：A 见，B 看不到
    assert [t["template_name"] for t in a.list_my_templates()] == ["我的链"]
    assert b.list_visible_templates() == []
    assert b.get_template(tid) is None            # 他人私有 → 不可读

    # (2) A 设公开：B 可见、可读，但删/改公开被 owner 护栏拒
    assert a.set_template_public(tid, True)
    vis = b.list_visible_templates()
    assert [t["template_name"] for t in vis] == ["我的链"]
    assert b.get_template(tid) is not None          # 公开 → 可读复用
    assert not b.delete_template(tid)               # 非 owner 删不动
    assert not b.set_template_public(tid, False)    # 非 owner 改不动
    assert a.get_template(tid) is not None           # A 的还在

    # (3) 快照可重建为 ChainGraph（chain_json 权威）
    cg = ChainGraph(**a.get_template(tid)["chain_json"])
    assert cg.end_product == "GPU" and cg.nodes[0].name == "HBM"

    # (4) owner 自己删得掉
    assert a.delete_template(tid)
    assert a.get_template(tid) is None


def test_visible_dedup_own_public_not_doubled(tmp_path):
    """自己的公开项在 list_visible 里只出现一次（mine ∪ public 按 id 去重）。"""
    a = _store(tmp_path, "userA")
    a.save_template(template_name="链1", chain_json=_CHAIN, is_public=True)
    a.save_template(template_name="链2", chain_json=_CHAIN, is_public=False)
    vis = a.list_visible_templates()
    names = sorted(t["template_name"] for t in vis)
    assert names == ["链1", "链2"]                  # 公开的链1 不重复；私有的链2 owner 也见
    assert len(vis) == 2


if __name__ == "__main__":
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as d:
        test_owner_isolation_and_public_visibility(Path(d))
    with tempfile.TemporaryDirectory() as d:
        test_visible_dedup_own_public_not_doubled(Path(d))
    print("test_chain_templates self-check OK")
