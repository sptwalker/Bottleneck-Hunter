"""综合/连通性测试跳过已禁用(is_active=False)的 provider —— 不浪费时间测禁用模型。"""

from __future__ import annotations

import bottleneck_hunter.web.ai_config_api as ac


def test_active_skips_disabled(monkeypatch):
    monkeypatch.setattr(ac, "list_custom_provider_ids", lambda: ["a", "b", "c"])
    monkeypatch.setattr(ac, "get_custom_provider", lambda pid: {"default_model": f"{pid}-model"})
    # b 被 admin 禁用
    monkeypatch.setattr("bottleneck_hunter.llm_clients.factory.is_provider_active",
                        lambda pid: pid != "b")

    res = ac._active_provider_models()          # 综合测试用：跳过禁用
    provs = [p for p, _ in res]
    assert provs == ["a", "c"]                  # 禁用的 b 被跳过
    assert res[0] == ("a", "a-model")
    # 连通性测试用的 _configured_provider_models 仍返回全部(含禁用)，不被窄化
    assert [p for p, _ in ac._configured_provider_models()] == ["a", "b", "c"]


def test_all_active_kept(monkeypatch):
    monkeypatch.setattr(ac, "list_custom_provider_ids", lambda: ["x", "y"])
    monkeypatch.setattr(ac, "get_custom_provider", lambda pid: {"default_model": ""})
    monkeypatch.setattr("bottleneck_hunter.llm_clients.factory.is_provider_active", lambda pid: True)
    res = ac._active_provider_models()
    assert [p for p, _ in res] == ["x", "y"]
    assert res[0] == ("x", "x")   # 无默认模型时回退用 provider id
