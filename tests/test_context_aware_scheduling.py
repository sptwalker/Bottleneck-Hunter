"""事前容量选型：重上下文角色不选窗口不足的模型（本次 kimi-8k 踩坑的根治）。"""

from __future__ import annotations

import bottleneck_hunter.llm_clients.factory as F
from bottleneck_hunter.llm_clients.model_context import fits, get_context_window
from bottleneck_hunter.llm_clients.role_registry import get_role


def test_model_context_map():
    assert get_context_window("moonshot-v1-8k") == 8_192
    assert get_context_window("moonshot-v1-128k") == 131_072
    assert get_context_window("deepseek-chat") == 65_536
    assert get_context_window("qwen-turbo") == 8_192
    assert get_context_window("qwen-max") == 32_768
    assert get_context_window("unknown-model-zzz") > 100_000_000   # 未知放行
    assert fits("moonshot-v1-8k", 16_384) is False
    assert fits("deepseek-chat", 16_384) is True
    assert fits("moonshot-v1-8k", 0) is True                        # 角色无要求恒 True


def test_heavy_roles_tagged():
    for rk in ["L1_macro", "committee_value", "pipeline_eval", "pipeline_cross_val", "watchlist_uzi",
               "L2_strategic", "L3_tactical", "L4_execution"]:
        assert get_role(rk).min_context == 16_384, rk
    for rk in ["pipeline_decompose", "watchlist_catalyst", "bottleneck"]:  # 轻角色不设门
        assert get_role(rk).min_context == 0, rk


def test_scheduler_prefers_big_context(monkeypatch):
    """L1_macro(重, 2槽)：即便 kimi 排名靠前，大上下文 deepseek 也占首槽，kimi 只回填、绝不少槽。"""
    monkeypatch.setattr(F, "_load_role_configs_from_db", lambda rk, uid: [])   # 跳过优先级1(矩阵)
    monkeypatch.setattr(F, "list_custom_provider_ids", lambda: ["kimi", "deepseek"])
    monkeypatch.setattr(F, "is_provider_active", lambda p: True)
    monkeypatch.setattr(F, "_user_has_llm_key", lambda p, uid: True)
    monkeypatch.setattr(F, "resolve_provider_model",
                        lambda p, uid="": {"kimi": "moonshot-v1-8k", "deepseek": "deepseek-chat"}.get(p, ""))
    monkeypatch.setattr(F, "create_llm", lambda p, m, **k: f"llm:{p}:{m}")
    monkeypatch.setattr("bottleneck_hunter.llm_clients.health.rank_providers",
                        lambda provs, *a, **k: ["kimi", "deepseek"])   # kimi 故意排前
    monkeypatch.setattr("bottleneck_hunter.llm_clients.health.load_routing_policy", lambda *a, **k: {})

    res = F.get_models_for_role("L1_macro", user_id="u1")
    provs = [p for _, p, _ in res]
    assert provs[0] == "deepseek"   # 大上下文优先占首槽（不因 kimi 排名靠前而先选）
    assert len(res) == 2            # 绝不少槽
    assert provs[1] == "kimi"       # 容量不足者回填次槽（配合 with_fallback/手动重试兜底）


def test_scheduler_excludes_small_when_enough_big(monkeypatch):
    """若大上下文模型够填满槽位，小模型完全不入选（单槽角色的清爽排除）。"""
    monkeypatch.setattr(F, "_load_role_configs_from_db", lambda rk, uid: [])
    monkeypatch.setattr(F, "list_custom_provider_ids", lambda: ["kimi", "deepseek", "glm"])
    monkeypatch.setattr(F, "is_provider_active", lambda p: True)
    monkeypatch.setattr(F, "_user_has_llm_key", lambda p, uid: True)
    monkeypatch.setattr(F, "resolve_provider_model",
                        lambda p, uid="": {"kimi": "moonshot-v1-8k", "deepseek": "deepseek-chat",
                                           "glm": "glm-4"}.get(p, ""))
    monkeypatch.setattr(F, "create_llm", lambda p, m, **k: f"llm:{p}:{m}")
    monkeypatch.setattr("bottleneck_hunter.llm_clients.health.rank_providers",
                        lambda provs, *a, **k: ["kimi", "deepseek", "glm"])
    monkeypatch.setattr("bottleneck_hunter.llm_clients.health.load_routing_policy", lambda *a, **k: {})

    # L1_macro 2 槽：deepseek+glm 两个大模型够填满 → kimi(8k) 根本不入选
    res = F.get_models_for_role("L1_macro", user_id="u1")
    provs = [p for _, p, _ in res]
    assert provs == ["deepseek", "glm"]
    assert "kimi" not in provs


def test_matrix_reresolves_live_not_frozen(monkeypatch):
    """根因修复：矩阵条目钉着旧模型(moonshot-v1-8k)，但装配时按 provider **当前**默认(32k)实时解析——
    不再残留冻结快照（系统配置改默认模型即刻对矩阵角色生效）。"""
    # 矩阵里 kimi 槽还钉着历史的 8k（模拟"改配前保存的快照"）
    monkeypatch.setattr(F, "_load_role_configs_from_db",
                        lambda rk, uid: [{"provider": "kimi", "model": "moonshot-v1-8k", "slot_index": 0}])
    # 但 provider 当前默认已是 32k（系统配置已改）
    monkeypatch.setattr(F, "resolve_provider_model", lambda p, uid="": "moonshot-v1-32k" if p == "kimi" else "")
    monkeypatch.setattr(F, "_user_has_llm_key", lambda p, uid: True)
    monkeypatch.setattr(F, "create_llm", lambda p, m, **k: f"llm:{p}:{m}")
    monkeypatch.setattr("bottleneck_hunter.llm_clients.provider_gate.is_disabled", lambda uid, p: False)

    res = F.get_models_for_role("L1_macro", user_id="u1")
    assert res, "应装配出模型"
    assert all(m == "moonshot-v1-32k" for _, _, m in res)   # 用实时默认，非冻结的 8k


def test_matrix_skips_undersized_default(monkeypatch):
    """矩阵 provider 的**当前**默认若对重角色仍欠容量(8k<16k)，跳过该槽交后续优先级——
    杜绝 8k 误装到 L1_macro（Bug2：小窗口误套模板的根因不再复现）。"""
    monkeypatch.setattr(F, "_load_role_configs_from_db",
                        lambda rk, uid: [{"provider": "kimi", "model": "x", "slot_index": 0}])
    # kimi 当前默认仍是 8k（欠容量）；优先级4 调度里 deepseek 兜底
    monkeypatch.setattr(F, "resolve_provider_model",
                        lambda p, uid="": {"kimi": "moonshot-v1-8k", "deepseek": "deepseek-chat"}.get(p, ""))
    monkeypatch.setattr(F, "_user_has_llm_key", lambda p, uid: True)
    monkeypatch.setattr(F, "create_llm", lambda p, m, **k: f"llm:{p}:{m}")
    monkeypatch.setattr(F, "list_custom_provider_ids", lambda: ["deepseek"])
    monkeypatch.setattr("bottleneck_hunter.llm_clients.provider_gate.is_disabled", lambda uid, p: False)
    monkeypatch.setattr("bottleneck_hunter.llm_clients.health.rank_providers", lambda provs, *a, **k: ["deepseek"])
    monkeypatch.setattr("bottleneck_hunter.llm_clients.health.load_routing_policy", lambda *a, **k: {})

    res = F.get_models_for_role("L1_macro", user_id="u1")
    provs = [p for _, p, _ in res]
    assert "kimi" not in provs          # 欠容量的矩阵槽被跳过
    assert "deepseek" in provs          # 落到调度另选够大的
