"""一次性迁移：把历史全局 API KEY 收敛到 admin 用户级加密存储，然后清除全局。

背景：旧版本 admin 存 KEY 会写 .env/os.environ（全局兜底），custom_providers 表也存
全局密钥。严格按用户隔离后这些全局态被删除，此迁移确保升级不丢 admin 已配的 KEY。

幂等：迁移完成后写入 system_config 标记，后续启动跳过；且迁移只在目标 provider
在 admin 用户级尚无 KEY 时写入（不覆盖用户已手动配置的）。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

_MIGRATION_FLAG = "key_isolation_migrated_v1"

# env_var → provider_id（与 web.api.PROVIDER_REGISTRY 对齐）
_ENV_TO_PROVIDER = {
    "OPENAI_API_KEY": "openai",
    "ANTHROPIC_API_KEY": "anthropic",
    "DEEPSEEK_API_KEY": "deepseek",
    "GOOGLE_API_KEY": "google",
    "DASHSCOPE_API_KEY": "qwen",
    "ZHIPU_API_KEY": "glm",
    "MINIMAX_API_KEY": "minimax",
    "OPENROUTER_API_KEY": "openrouter",
    "SILICONFLOW_API_KEY": "siliconflow",
    "AGNES_API_KEY": "agnes",
    "MOONSHOT_API_KEY": "kimi",
}


def migrate_global_keys_to_admin(auth_store, admin_user_id: str = "") -> int:
    """把 .env/os.environ 与 custom_providers 里的全局密钥迁到 admin 用户级存储。

    返回迁移的 KEY 数量。无 admin 或已迁移则跳过。
    """
    if not auth_store or not admin_user_id:
        return 0
    try:
        if auth_store.get_config(_MIGRATION_FLAG) == "1":
            return 0
    except Exception:
        pass  # 无 system_config 读接口时按未迁移处理

    from bottleneck_hunter.auth.crypto import encrypt, make_hint, decrypt

    migrated = 0

    def _maybe_save(provider_id: str, plaintext: str):
        nonlocal migrated
        if not plaintext:
            return
        # 不覆盖 admin 已手动配置的 KEY
        if auth_store.get_user_api_key_encrypted(admin_user_id, provider_id):
            return
        auth_store.save_user_api_key(admin_user_id, provider_id, encrypt(plaintext), make_hint(plaintext))
        migrated += 1
        logger.info("迁移全局 KEY → admin 用户级: %s", provider_id)

    # 1) 内置 provider 的 .env / os.environ 全局 KEY
    for env_var, pid in _ENV_TO_PROVIDER.items():
        val = (os.environ.get(env_var) or "").strip()
        if val:
            _maybe_save(pid, val)

    # 2) custom_providers 表里的全局密钥
    try:
        for cp in auth_store.list_custom_providers():
            detail = auth_store.get_custom_provider(cp["provider_id"])
            enc = detail.get("api_key_encrypted") if detail else ""
            if enc:
                try:
                    _maybe_save(cp["provider_id"], decrypt(enc))
                except Exception:
                    pass
                # 清除 custom_providers 里的全局密钥（保留定义）
                try:
                    auth_store.save_custom_provider(
                        cp["provider_id"], detail.get("display_name", cp["provider_id"]),
                        detail.get("base_url", ""), "", "", detail.get("default_model", ""),
                    )
                except Exception as e:
                    logger.warning("清除 custom_provider 全局密钥失败 %s: %s", cp["provider_id"], e)
    except Exception as e:
        logger.warning("迁移 custom_providers 密钥失败: %s", e)

    # 3) 清除 .env 全局 KEY + os.environ
    try:
        env_path = Path.cwd() / ".env"
        from dotenv import set_key
        for env_var in _ENV_TO_PROVIDER:
            if os.environ.pop(env_var, None) is not None and env_path.exists():
                try:
                    set_key(str(env_path), env_var, "")
                except Exception:
                    pass
    except Exception as e:
        logger.warning("清除 .env 全局 KEY 失败: %s", e)

    try:
        auth_store.set_config(_MIGRATION_FLAG, "1")
    except Exception:
        pass

    if migrated:
        logger.info("全局 KEY 隔离迁移完成：%d 个 KEY 收敛到 admin 用户级存储", migrated)
    return migrated
