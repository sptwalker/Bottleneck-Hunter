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
    """严格隔离：有 env Key 的内置 provider 迁入 custom_providers（仅定义，无全局Key）；
    Key 落到 admin 用户级存储。"""
    auth, wl = stores
    monkeypatch.setenv("ZHIPU_API_KEY", "sk-test-glm-123456")
    for env_var in F.PROVIDER_KEY_MAP.values():
        if env_var != "ZHIPU_API_KEY":
            monkeypatch.delenv(env_var, raising=False)

    auth.create_user("admin", password="x", role="admin")
    admin_id = auth.get_user_by_username("admin").id

    migrated = migrate_builtin_providers_to_custom(auth, wl, admin_user_id=admin_id)
    assert migrated == 1

    row = auth.get_custom_provider("glm")
    assert row is not None
    assert row["display_name"] == "GLM (智谱)"
    assert row["base_url"] == "https://open.bigmodel.cn/api/paas/v4"
    assert row["default_model"] == F.PROVIDER_MODELS["glm"]
    # 严格隔离：custom_providers 不再持有 Key
    assert not row["api_key_encrypted"]

    # Key 落到 admin 用户级，且可解密回原文
    from bottleneck_hunter.auth.crypto import decrypt
    enc = auth.get_user_api_key_encrypted(admin_id, "glm")
    assert enc
    assert decrypt(enc) == "sk-test-glm-123456"


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


def test_build_providers_list_per_user(stores):
    """_build_providers_list：定义来自 custom_providers（共享），但 configured 严格按当前用户。"""
    from bottleneck_hunter.web import ai_config_api as aic
    from bottleneck_hunter.auth.crypto import encrypt, make_hint

    auth, _wl = stores
    auth.save_custom_provider("myglm", "我的 GLM", "https://open.bigmodel.cn/api/paas/v4",
                              "", "", "glm-4-flash")
    auth.create_user("uA", password="x", role="user")
    auth.create_user("uB", password="x", role="user")
    a = auth.get_user_by_username("uA").id
    b = auth.get_user_by_username("uB").id
    # 只有 A 配了 myglm 的 Key
    auth.save_user_api_key(a, "myglm", encrypt("sk-a"), make_hint("sk-a"))

    aic.set_auth_store(auth)
    try:
        la = {p["id"]: p for p in aic._build_providers_list(a)}
        lb = {p["id"]: p for p in aic._build_providers_list(b)}
        assert list(la) == ["myglm"] and list(lb) == ["myglm"]
        # 定义共享，configured 按用户
        assert la["myglm"]["configured"] is True
        assert la["myglm"]["name"] == "我的 GLM"
        assert la["myglm"]["default_model"] == "glm-4-flash"
        assert la["myglm"]["key_hint"]                 # A 有自己的 hint
        assert lb["myglm"]["configured"] is False      # B 没配 → 未配置
        assert lb["myglm"]["key_hint"] == ""           # B 看不到 A 的 hint
    finally:
        aic.set_auth_store(None)


def test_create_llm_strict_isolation(monkeypatch):
    """严格隔离：显式 api_key 仍最优先；无用户 KEY 时不再回退 env/缓存，而是抛错。"""
    from bottleneck_hunter.llm_clients.factory import MissingUserKeyError
    from bottleneck_hunter.auth import current_user as CU
    # env 全局 KEY 存在也不应被使用
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-env-old")
    # 注册 provider 元数据（不再含 KEY）
    F.register_custom_provider("deepseek", "https://api.deepseek.com", default_model="deepseek-chat")
    monkeypatch.setattr(F, "_resolve_user_llm_key", lambda p, u: None)  # 用户无 KEY
    tok = CU.set_current_user("userA")
    try:
        # 无用户 KEY → 严格失败（证明不读 env、不读全局缓存）
        with pytest.raises(MissingUserKeyError):
            F.create_llm("deepseek", "deepseek-chat", with_fallback=False)
        # 显式传入最优先，正常构建
        llm = F.create_llm("deepseek", "deepseek-chat", api_key="sk-explicit", with_fallback=False)
        assert llm.openai_api_key.get_secret_value() == "sk-explicit"
    finally:
        CU.reset_current_user(tok)
        F.unregister_custom_provider("deepseek")

