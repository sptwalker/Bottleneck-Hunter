"""智能模型调度系统单元测试（health 排序/熔断、输出校验、看板 assignments 端点）。

覆盖审查指出的"model scheduler 无覆盖测试"缺口，守住关键不变量：
- 无数据退化为原顺序；熔断压过一切加成（W1 回归）；能力先验；免费/付费策略。
- ProviderHealth 按用户隔离、TTL、取消不惩罚。
- validate_output 只判明显坏、不误杀散文/合法JSON+尾注。
- /model-assignments 契约：每角色 source + picks，多槽多 pick。
"""
import asyncio

import pytest

from bottleneck_hunter.llm_clients import health as H
from bottleneck_hunter.llm_clients.validate import validate_output


# ── ProviderHealth 熔断 ────────────────────────────────
def test_health_cooldown_and_isolation():
    h = H.ProviderHealth()
    h.record_failure("u1", "deepseek", "认证失败(密钥无效)")
    assert h.is_open("u1", "deepseek")
    assert not h.is_open("u1", "qwen")       # 未失败的不熔断
    assert not h.is_open("u2", "deepseek")   # 按用户隔离
    h.record_success("u1", "deepseek")
    assert not h.is_open("u1", "deepseek")   # 成功即清除
    h.record_failure("u1", "kimi", "调用被取消")
    assert not h.is_open("u1", "kimi")       # 取消不惩罚


# ── rank_providers 排序 ────────────────────────────────
def test_rank_no_data_degrades_to_original_order():
    H.health.reset()
    assert H.rank_providers(["a", "b", "c"], "u", stats={}) == ["a", "b", "c"]


def test_rank_low_success_sinks():
    H.health.reset()
    st = {"a": {"calls": 10, "ok_rate": 20.0}, "b": {"calls": 10, "ok_rate": 99.0}}
    assert H.rank_providers(["a", "b"], "u", stats=st)[0] == "b"


def test_rank_circuit_breaker_dominates_all_bonuses():
    """W1 回归：已熔断的免费主模型 + 满能力分 + free/price 策略，仍须排在健康付费之后。"""
    H.health.reset()
    H.health.record_failure("u", "deepseek", "认证失败(密钥无效)")
    r = H.rank_providers(
        ["deepseek", "openai"], "u", primary_provider="deepseek",
        policy={"prefer_tier": "free", "optimize_for": "price"},
        caps={"deepseek": 10.0, "openai": 5.0}, stats={})
    assert r == ["openai", "deepseek"], r
    H.health.reset()


def test_rank_capability_prior():
    H.health.reset()
    caps = {"deepseek": 9.0, "qwen": 3.0}
    assert H.rank_providers(["qwen", "deepseek"], "u", caps=caps, stats={})[0] == "deepseek"


def test_rank_free_preference_and_fallback_to_paid():
    H.health.reset()
    # 免费优先：健康时免费(deepseek)排付费(openai)前
    r = H.rank_providers(["openai", "deepseek"], "u", policy={"prefer_tier": "free"}, stats={})
    assert r[0] == "deepseek"
    # 免费熔断 → 回落付费
    H.health.record_failure("u", "deepseek", "认证失败(密钥无效)")
    r2 = H.rank_providers(["deepseek", "openai"], "u", policy={"prefer_tier": "free"}, stats={})
    assert r2[0] == "openai"
    H.health.reset()


def test_rank_feature_flag_off(monkeypatch):
    monkeypatch.setenv("BH_SCHEDULER_RANK", "0")
    st = {"a": {"calls": 10, "ok_rate": 10.0}, "b": {"calls": 10, "ok_rate": 99.0}}
    assert H.rank_providers(["a", "b"], "u", stats=st) == ["a", "b"]  # 关闭→原顺序


def test_provider_tier_map():
    assert H.provider_tier("deepseek") == "free"
    assert H.provider_tier("openai") == "paid"
    assert H.provider_tier("unknownxyz") == ""


# ── 输出格式校验 ───────────────────────────────────────
class _Msg:
    def __init__(self, content):
        self.content = content


@pytest.mark.parametrize("text,ok", [
    ('{"score": 8}', True),
    ('```json\n{"a":1}\n```', True),
    ('{"score": 8}\n\n这是基于护城河的分析。', True),   # 合法JSON+尾注不误杀
    ('该公司在光刻胶领域具备技术护城河。', True),          # 中文散文放行
    ('7.5', True),
    ('', False),                                          # 空
    ('{"a": 1,,,', False),                                # 坏JSON
    ('[不完整', False),                                    # 坏数组
    ('作为AI语言模型，我无法提供投资建议。', False),        # 拒答
])
def test_validate_output(text, ok):
    assert validate_output(_Msg(text))[0] is ok


# ── /model-assignments 端点契约 ────────────────────────
def test_model_assignments_endpoint_contract(tmp_path, monkeypatch):
    from bottleneck_hunter.watchlist.store import WatchlistStore
    import bottleneck_hunter.web.decision_api as D

    D.set_store(WatchlistStore(str(tmp_path / "t.db")))
    # 注入：L2_strategic 有手填矩阵配置（factory 自建默认 store，故 monkeypatch 其读取函数）
    def fake_load(role_key, uid):
        return [{"provider": "deepseek", "model": "deepseek-chat"}] if role_key == "L2_strategic" else []
    monkeypatch.setattr(
        "bottleneck_hunter.llm_clients.factory._load_role_configs_from_db", fake_load)

    res = asyncio.run(D.get_model_assignments(user={"sub": "", "role": "admin"}))
    a = {r["role_key"]: r for r in res["assignments"]}
    assert len(a) >= 20                       # 覆盖全部注册角色
    assert a["L2_strategic"]["source"] == "manual"   # 有手填配置 → manual
    assert a["bottleneck"]["source"] == "auto"       # 无配置 → 智能调度
    assert a["bottleneck"]["multi"] is True
    # 每条结构完整
    for r in res["assignments"]:
        assert {"role_key", "label", "group", "multi", "source", "picks"} <= set(r)
        assert isinstance(r["picks"], list)
