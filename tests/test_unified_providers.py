"""统一 Provider 管理（内置迁移 + 单一真源列表 + 删除彻底清理）自检。"""

import os
from pathlib import Path

import pytest

from bottleneck_hunter.auth.store import AuthStore
from bottleneck_hunter.llm_clients import factory as F
from bottleneck_hunter.watchlist.store import WatchlistStore
from bottleneck_hunter.web.provider_migration import migrate_builtin_providers_to_custom


@pytest.fixture()
def stores(tmp_path, monkeypatch):
    """独立的临时 AuthStore + WatchlistStore；加密密钥落在临时目录避免污染工作区。"""
    monkeypatch.chdir(tmp_path)  # .env / data/.encryption_key 均相对 cwd
    auth = AuthStore(db_path=tmp_path / "auth.db")
    wl = WatchlistStore(str(tmp_path / "wl.db"))
    return auth, wl


def test_migrate_env_key_creates_row(stores, monkeypatch):
    """有 env Key 的内置 provider 应迁入 custom_providers（含种子 base_url/模型/显示名）。"""
    auth, wl = stores
    monkeypatch.setenv("ZHIPU_API_KEY", "sk-test-glm-123456")
    # 确保其它内置 provider 的 env 干净，只迁移 glm
    for env_var in F.PROVIDER_KEY_MAP.values():
        if env_var != "ZHIPU_API_KEY":
            monkeypatch.delenv(env_var, raising=False)

    migrated = migrate_builtin_providers_to_custom(auth, wl)
    assert migrated == 1

    row = auth.get_custom_provider("glm")
    assert row is not None
    assert row["display_name"] == "GLM (智谱)"
    assert row["base_url"] == "https://open.bigmodel.cn/api/paas/v4"
    assert row["default_model"] == F.PROVIDER_MODELS["glm"]
    assert row["api_key_encrypted"]  # Key 已加密入库

    # 加密 Key 可解密回原文
    from bottleneck_hunter.auth.crypto import decrypt
    assert decrypt(row["api_key_encrypted"]) == "sk-test-glm-123456"


def test_migrate_idempotent(stores, monkeypatch):
    """重复运行迁移不重复建行、不覆盖已有行。"""
    auth, wl = stores
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-ds-1")
    for env_var in F.PROVIDER_KEY_MAP.values():
        if env_var != "DEEPSEEK_API_KEY":
            monkeypatch.delenv(env_var, raising=False)

    assert migrate_builtin_providers_to_custom(auth, wl) == 1
    # 用户随后在 UI 改了模型
    row = auth.get_custom_provider("deepseek")
    auth.save_custom_provider("deepseek", row["display_name"], row["base_url"],
                              row["api_key_encrypted"], row["api_key_hint"], "deepseek-reasoner")
    # 再跑迁移：不新建、不回滚用户的修改
    assert migrate_builtin_providers_to_custom(auth, wl) == 0
    assert auth.get_custom_provider("deepseek")["default_model"] == "deepseek-reasoner"


def test_migrate_skips_unconfigured(stores, monkeypatch):
    """无 Key 的内置 provider 不迁移——幽灵卡片彻底消失。"""
    auth, wl = stores
    for env_var in F.PROVIDER_KEY_MAP.values():
        monkeypatch.delenv(env_var, raising=False)

    assert migrate_builtin_providers_to_custom(auth, wl) == 0
    for pid in F.PROVIDER_KEY_MAP:
        assert auth.get_custom_provider(pid) is None


def test_migrate_prefers_global_override(stores, monkeypatch):
    """provider_configs 全局覆盖优先于种子常量，且迁移后覆盖行被清除（防影子解析）。"""
    auth, wl = stores
    monkeypatch.setenv("MOONSHOT_API_KEY", "sk-kimi-1")
    for env_var in F.PROVIDER_KEY_MAP.values():
        if env_var != "MOONSHOT_API_KEY":
            monkeypatch.delenv(env_var, raising=False)
    wl.upsert_provider_config("kimi", "moonshot-v1-32k", "https://proxy.local/v1", user_id="")

    assert migrate_builtin_providers_to_custom(auth, wl) == 1
    row = auth.get_custom_provider("kimi")
    assert row["default_model"] == "moonshot-v1-32k"
    assert row["base_url"] == "https://proxy.local/v1"
    # 覆盖行已清除
    assert wl.get_provider_config("kimi", user_id="") is None


def test_deleted_provider_not_resurrected(stores, monkeypatch, tmp_path):
    """删除 provider 并清掉 env 后，重启迁移不应复活该卡片。"""
    auth, wl = stores
    monkeypatch.setenv("MINIMAX_API_KEY", "sk-mm-1")
    for env_var in F.PROVIDER_KEY_MAP.values():
        if env_var != "MINIMAX_API_KEY":
            monkeypatch.delenv(env_var, raising=False)

    assert migrate_builtin_providers_to_custom(auth, wl) == 1
    # 模拟删除端点行为：删行 + 清 env（_purge_builtin_residue 的核心动作）
    assert auth.delete_custom_provider("minimax")
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)

    assert migrate_builtin_providers_to_custom(auth, wl) == 0
    assert auth.get_custom_provider("minimax") is None


def test_build_providers_list_unified(stores):
    """_build_providers_list 只从 custom_providers 出数据，全部 configured=True。"""
    from bottleneck_hunter.web import ai_config_api as aic

    auth, _wl = stores
    auth.save_custom_provider("myglm", "我的 GLM", "https://open.bigmodel.cn/api/paas/v4",
                              "", "", "glm-4-flash")
    aic.set_auth_store(auth)
    try:
        lst = aic._build_providers_list()
        assert [p["id"] for p in lst] == ["myglm"]
        p = lst[0]
        assert p["configured"] is True
        assert p["name"] == "我的 GLM"
        assert p["default_model"] == "glm-4-flash"
    finally:
        aic.set_auth_store(None)


def test_create_llm_key_priority_cache_over_env(monkeypatch):
    """统一后 create_llm 的 Key 优先级：显式参数 > 统一缓存 > env。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-env-old")
    F.register_custom_provider("deepseek", "https://api.deepseek.com", "sk-cache-new", "deepseek-chat")
    try:
        llm = F.create_llm("deepseek", "deepseek-chat")
        # ChatOpenAI 的 api_key 为 SecretStr
        assert llm.openai_api_key.get_secret_value() == "sk-cache-new"
        # 显式传入最优先
        llm2 = F.create_llm("deepseek", "deepseek-chat", api_key="sk-explicit")
        assert llm2.openai_api_key.get_secret_value() == "sk-explicit"
    finally:
        F.unregister_custom_provider("deepseek")
