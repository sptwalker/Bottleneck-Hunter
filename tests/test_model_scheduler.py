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


def _patch_assignments(monkeypatch, manual_role, manual_provider, picks_for_role):
    """给 _build_assignments 注入：某角色有手填配置 + get_models_for_role 返回指定 picks。"""
    import bottleneck_hunter.web.decision_api as D
    from bottleneck_hunter.watchlist.store import WatchlistStore
    import tempfile
    D.set_store(WatchlistStore(str(tempfile.mkdtemp() + "/t.db")))

    def fake_load(role, uid):
        return [{"provider": manual_provider, "model": "x"}] if role == manual_role else []
    def fake_get(role, user_id="", **kw):
        return picks_for_role if role == manual_role else []
    monkeypatch.setattr("bottleneck_hunter.llm_clients.factory._load_role_configs_from_db", fake_load)
    monkeypatch.setattr("bottleneck_hunter.llm_clients.factory.get_models_for_role", fake_get)
    return D


def test_assignments_source_manual_when_picks_match(monkeypatch):
    """手填 deepseek 且实际 picks 也是 deepseek → source=manual（走真正的子集判定，非空兜底）。"""
    D = _patch_assignments(monkeypatch, "L2_strategic", "deepseek",
                           [(None, "deepseek", "deepseek-chat")])
    res = asyncio.run(D.get_model_assignments(user={"sub": "", "role": "admin"}))
    a = {r["role_key"]: r for r in res["assignments"]}
    assert a["L2_strategic"]["source"] == "manual"
    assert a["L2_strategic"]["picks"] == [{"provider": "deepseek", "model": "deepseek-chat"}]


def test_assignments_source_auto_when_manual_bypassed(monkeypatch):
    """手填 deepseek 但实际 picks 落到 qwen（手填被禁用/失败→智能调度）→ 如实标 auto（不误标 manual）。"""
    D = _patch_assignments(monkeypatch, "L2_strategic", "deepseek",
                           [(None, "qwen", "qwen-plus")])
    res = asyncio.run(D.get_model_assignments(user={"sub": "", "role": "admin"}))
    a = {r["role_key"]: r for r in res["assignments"]}
    assert a["L2_strategic"]["source"] == "auto"   # qwen 不属手填的 deepseek → auto


# ── B1 模式测试周期自动化：选型 + 封顶（mock，不发真实 LLM 调用）──
def test_select_stale_models_filters_fresh(monkeypatch):
    import bottleneck_hunter.watchlist.scheduler as S
    from bottleneck_hunter.watchlist.store_base import _now_iso

    class FakeAuth:
        def get_user_api_keys(self, uid):
            return [{"provider": "deepseek"}, {"provider": "qwen"}]
    monkeypatch.setattr(S, "_auth_store", FakeAuth())

    class FakeStore:  # deepseek 刚测过(fresh)，qwen 从没测过(stale)
        def get_test_results(self, user_id=None):
            return [{"provider": "deepseek", "model": "deepseek-chat", "tested_at": _now_iso()}]
    out = dict(S._select_stale_models("u1", FakeStore(), days=30))
    assert "qwen" in out and "deepseek" not in out   # 新鲜的跳过，过期的选中


class _FakeBudget:
    def can_spend(self, **kw):
        return True


def test_job_capability_refresh_caps_at_max(monkeypatch):
    import bottleneck_hunter.watchlist.scheduler as S

    saved = []

    class FakeStore:
        def get_test_results(self, user_id=None):
            return []  # 全部 stale
        def save_test_result(self, prov, model, dim, score, raw, user_id=None):
            saved.append((prov, model, dim))

    class FakeAuth:
        def get_user_api_keys(self, uid):
            # 12 个 provider（>封顶 10）
            return [{"provider": p} for p in
                    ["deepseek", "qwen", "glm", "kimi", "openai", "google",
                     "siliconflow", "agnes", "minimax", "openrouter", "spark_x2", "xiaomimimo"]]

    monkeypatch.setattr(S, "_wl_store", FakeStore())
    monkeypatch.setattr(S, "_auth_store", FakeAuth())
    monkeypatch.setattr(S, "_iter_users",
                        lambda category=None: iter([("u1", FakeStore(), _FakeBudget())]))
    monkeypatch.setattr("bottleneck_hunter.watchlist.schedule_config.is_global_enabled", lambda a: True)

    async def fake_test(prov, model):
        return {"json_output": {"score": 8}}
    monkeypatch.setattr("bottleneck_hunter.web.model_tester.run_comprehensive_test", fake_test)

    asyncio.run(S.job_model_capability_refresh())
    tested = {(p, m) for p, m, _ in saved}
    assert len(tested) == S._CAP_REFRESH_MAX_MODELS   # 封顶生效（10）


def test_job_capability_refresh_budget_gate(monkeypatch):
    """预算不足的用户 → 不发起任何测试。"""
    import bottleneck_hunter.watchlist.scheduler as S
    saved = []

    class FakeStore:
        def get_test_results(self, user_id=None):
            return []
        def save_test_result(self, *a, **k):
            saved.append(a)

    class FakeAuth:
        def get_user_api_keys(self, uid):
            return [{"provider": "deepseek"}]

    class BrokeBudget:
        def can_spend(self, **kw):
            return False

    monkeypatch.setattr(S, "_wl_store", FakeStore())
    monkeypatch.setattr(S, "_auth_store", FakeAuth())
    monkeypatch.setattr(S, "_iter_users",
                        lambda category=None: iter([("u1", FakeStore(), BrokeBudget())]))
    monkeypatch.setattr("bottleneck_hunter.watchlist.schedule_config.is_global_enabled", lambda a: True)
    monkeypatch.setattr("bottleneck_hunter.web.model_tester.run_comprehensive_test",
                        lambda p, m: (_ for _ in ()).throw(AssertionError("预算不足不应发起测试")))
    asyncio.run(S.job_model_capability_refresh())
    assert saved == []   # 预算不足 → 零测试


def test_job_capability_refresh_respects_global_killswitch(monkeypatch):
    import bottleneck_hunter.watchlist.scheduler as S
    monkeypatch.setattr(S, "_wl_store", object())
    monkeypatch.setattr(S, "_auth_store", object())
    monkeypatch.setattr("bottleneck_hunter.watchlist.schedule_config.is_global_enabled", lambda a: False)
    called = {"n": 0}
    monkeypatch.setattr(S, "_iter_users", lambda category=None: (called.__setitem__("n", 1), iter([]))[1])
    asyncio.run(S.job_model_capability_refresh())
    assert called["n"] == 0   # 总开关关 → 直接返回，不遍历用户


