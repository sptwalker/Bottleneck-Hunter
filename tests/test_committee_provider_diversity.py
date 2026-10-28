"""P1-5（2026-10 复核）：委员指定 provider 优先，避免全员降级到同一家导致独立性闸误杀。

病史：10-20 起 qwen/kimi/glm 相继欠费，四位委员全部回落 deepseek；独立性守卫把 13 笔
已批准买入按「交叉验证失效」拦下，其中包含一笔已全票通过（78%）的 688383 止损卖单。
"""
from unittest.mock import patch

from bottleneck_hunter.watchlist import committee as C


def _member(hint="qwen"):
    return {"role": "growth_investor", "label": "成长", "prompt_file": "committee_growth",
            "provider_hint": hint, "config_key": "committee_growth"}


def test_chain_prefers_designated_provider_over_scheduler():
    """指定 provider 健在时，链首必须是它——不能先被角色矩阵的调度结果顶掉。"""
    calls = []

    def fake_get(provider_hint=None, position=None, **_):
        calls.append((provider_hint, position))
        if provider_hint == "qwen":
            return ("LLM-Q", "qwen", "qwen3.8-max")
        if position == "committee_growth":
            return ("LLM-D", "deepseek", "deepseek-v4-flash")
        return (None, "", "")

    with patch.object(C, "get_llm_for_position", side_effect=fake_get), \
         patch("bottleneck_hunter.llm_clients.health.health.is_open", return_value=False):
        chain = C._build_llm_chain(_member())

    assert chain[0][1] == "qwen"
    assert calls[0] == ("qwen", None)          # 先按 provider_hint 取
    assert chain[-1][1] != "qwen" or len(chain) == 1


def test_chain_falls_back_to_role_then_backup_when_hint_dead():
    def fake_get(provider_hint=None, position=None, **_):
        if provider_hint == "qwen":
            return (None, "", "")              # 指定 provider 已死
        if position == "committee_growth":
            return ("LLM-D", "deepseek", "deepseek-v4-flash")
        if provider_hint == "glm":
            return ("LLM-G", "glm", "glm-5.3")
        return (None, "", "")

    with patch.object(C, "get_llm_for_position", side_effect=fake_get), \
         patch("bottleneck_hunter.llm_clients.health.health.is_open", return_value=False), \
         patch("bottleneck_hunter.llm_clients.factory.list_custom_provider_ids", return_value=["qwen", "glm"]):
        chain = C._build_llm_chain(_member())

    providers = [p for _, p, _ in chain]
    assert providers[0] == "deepseek"           # 指定 provider 死了 → 角色配置
    assert any(p != "deepseek" for p in providers)  # 链里仍有异家备用


def test_downgrade_reason_lists_disabled_providers():
    """拒因要写明是哪几家不可用，别只留一句"独立性不足"。"""
    with patch("bottleneck_hunter.llm_clients.provider_gate.disabled_info",
               side_effect=lambda uid, p: {"status": "arrears"} if p in ("qwen", "kimi") else None):
        why = C._provider_downgrade_reason("deepseek")
    assert "qwen" in why and "kimi" in why and "deepseek" in why


def test_downgrade_reason_empty_when_no_info():
    with patch("bottleneck_hunter.llm_clients.provider_gate.disabled_info", return_value=None):
        assert C._provider_downgrade_reason("deepseek") == ""
