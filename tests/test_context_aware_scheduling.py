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
