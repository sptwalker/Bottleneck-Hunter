"""provider_gate 持久熔断（升级处理）测试。

覆盖：认证 1 击即禁 / 限流累计阈值禁 / clear 与 clear_auth_disable 差异 /
按用户隔离 / DB round-trip 一致 / 成功重置限流计数。
真库用 tmp AuthStore（monkeypatch _get_store 注入），不碰生产 data/auth.db。
"""
import pytest

from bottleneck_hunter.auth.store import AuthStore
from bottleneck_hunter.llm_clients import provider_gate as pg


@pytest.fixture
def store(tmp_path, monkeypatch):
    s = AuthStore(tmp_path / "auth.db")
    monkeypatch.setattr(pg, "_get_store", lambda: s)  # 注入 tmp 库
    pg._reset_for_test()                               # 清进程内 strike/缓存
    yield s
    pg._reset_for_test()


def test_auth_one_strike_disables(store):
    pg.record_result("u1", "deepseek", False, pg._AUTH_REASON)
    assert pg.is_disabled("u1", "deepseek")
    assert pg.disabled_info("u1", "deepseek")["status"] == pg._STATUS_AUTH
    assert not pg.is_disabled("u2", "deepseek")  # 按用户隔离


def test_ratelimit_needs_threshold(store):
    for _ in range(pg._RL_STRIKES - 1):
        pg.record_result("u1", "qwen", False, pg._RL_REASON)
    assert not pg.is_disabled("u1", "qwen"), "未达阈值不应禁"
    pg.record_result("u1", "qwen", False, pg._RL_REASON)  # 第 _RL_STRIKES 次
    assert pg.is_disabled("u1", "qwen")
    assert pg.disabled_info("u1", "qwen")["status"] == pg._STATUS_RL


def test_clear_vs_clear_auth(store):
    # clear_auth_disable 只清认证，保留限流（须过流量测试）
    pg.record_result("u1", "qwen", False, pg._AUTH_REASON)
    assert pg.clear_auth_disable("u1", "qwen")
    assert not pg.is_disabled("u1", "qwen")

    for _ in range(pg._RL_STRIKES):
        pg.record_result("u1", "glm", False, pg._RL_REASON)
    assert pg.is_disabled("u1", "glm")
    assert not pg.clear_auth_disable("u1", "glm"), "限流禁用不应被 clear_auth_disable 清"
    assert pg.is_disabled("u1", "glm")
    assert pg.clear("u1", "glm")                 # clear 清任意状态
    assert not pg.is_disabled("u1", "glm")


def test_success_resets_ratelimit_count(store):
    for _ in range(pg._RL_STRIKES - 1):
        pg.record_result("u1", "kimi", False, pg._RL_REASON)
    pg.record_result("u1", "kimi", True)          # 成功清零
    pg.record_result("u1", "kimi", False, pg._RL_REASON)
    assert not pg.is_disabled("u1", "kimi"), "成功后应重新计数"


def test_db_roundtrip(store):
    pg.record_result("uX", "openai", False, pg._AUTH_REASON)
    pg._reset_for_test()                          # 清进程缓存 → 强制读库
    row = store.get_llm_provider_health("uX", "openai")
    assert row and row["status"] == pg._STATUS_AUTH and row["provider"] == "openai"
    assert pg.is_disabled("uX", "openai")         # 缓存清空后仍从库读到


def test_resave_key_clears_auth_disable(store):
    # 重存 key（用户"重新配置"）→ save_user_api_key hook 自动清认证禁用
    pg.record_result("uY", "deepseek", False, pg._AUTH_REASON)
    assert pg.is_disabled("uY", "deepseek")
    store.save_user_api_key("uY", "deepseek", "enc-new-key", "sk-…abcd")
    assert not pg.is_disabled("uY", "deepseek"), "重存 key 应自动解除认证禁用"

    # 但限流禁用不因重存 key 而解除（须过流量测试）
    for _ in range(pg._RL_STRIKES):
        pg.record_result("uY", "qwen", False, pg._RL_REASON)
    assert pg.is_disabled("uY", "qwen")
    store.save_user_api_key("uY", "qwen", "enc-new-key", "sk-…wxyz")
    assert pg.is_disabled("uY", "qwen"), "限流禁用不应因重存 key 而解除"

