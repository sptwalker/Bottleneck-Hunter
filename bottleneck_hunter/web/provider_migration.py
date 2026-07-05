"""内置 Provider → 统一 custom_providers 表 的启动迁移。

背景：历史上 provider 分「内置」（PROVIDER_KEY_MAP + .env/user_api_keys + provider_configs 覆盖）
和「自定义」（custom_providers 表）两套，导致配置中心出现无法删除的内置幽灵卡片。

本模块在应用启动时把「已配置了 Key 的内置 provider」一次性迁入 custom_providers 表，
使其成为唯一真源、与自定义端点完全同构（可编辑/删除/测试）。迁移幂等：已存在同名行即跳过。

注意：本迁移不删除 .env 里的 Key（CLI 仍依赖 .env）；运行时优先级由 factory 保证——
已注册到 _CUSTOM_PROVIDERS 的 provider 其 Key 优先于 env，故编辑能即时生效。
"""

from __future__ import annotations

import logging
import os

from dotenv import dotenv_values

from bottleneck_hunter.llm_clients.factory import (
    PROVIDER_KEY_MAP,
    PROVIDER_MODELS,
    _BUILTIN_BASE_URLS,
)

logger = logging.getLogger(__name__)

# 内置 provider 的显示名（迁移时作为 custom_providers.display_name 种子）
_PROVIDER_DISPLAY = {
    "openai": "OpenAI",
    "anthropic": "Anthropic",
    "deepseek": "DeepSeek",
    "google": "Google",
    "qwen": "Qwen (通义)",
    "glm": "GLM (智谱)",
    "minimax": "MiniMax (海螺)",
    "openrouter": "OpenRouter",
    "siliconflow": "SiliconFlow",
    "agnes": "Agnes AI",
    "kimi": "Kimi (月之暗面)",
}


def _find_env_path():
    """定位 .env 路径（与 web/api.py 保持一致：当前工作目录下）。"""
    from pathlib import Path
    return Path.cwd() / ".env"


def migrate_builtin_providers_to_custom(auth_store, wl_store, admin_user_id: str = "") -> int:
    """把已配置 Key 的内置 provider 迁入 custom_providers 表。

    Args:
        auth_store: AuthStore（custom_providers + user_api_keys）
        wl_store:   WatchlistStore（provider_configs 覆盖表）
        admin_user_id: admin 用户 id，用于取其加密存储的 Key 与覆盖配置

    Returns:
        本次新迁移的 provider 数量。
    """
    from bottleneck_hunter.auth.crypto import encrypt, make_hint

    env_path = _find_env_path()
    env_file_vals = dotenv_values(env_path) if env_path.exists() else {}

    # 预取 admin 的加密 Key（provider_id -> {encrypted, hint}），供无明文 env 时复用
    admin_keys: dict[str, dict] = {}
    if admin_user_id:
        try:
            for k in auth_store.get_user_api_keys(admin_user_id):
                admin_keys[k["provider"]] = {
                    "encrypted": k.get("encrypted_key", ""),
                    "hint": k.get("key_hint", ""),
                }
        except Exception as e:
            logger.debug("读取 admin 加密 Key 失败: %s", e)

    migrated = 0
    for pid, env_var in PROVIDER_KEY_MAP.items():
        # 幂等：已存在同名行则跳过
        try:
            if auth_store.get_custom_provider(pid):
                continue
        except Exception:
            continue

        # 解析该 provider 的 Key：优先明文 env（.env / os.environ），否则复用 admin 加密 Key
        raw_key = (os.environ.get(env_var, "") or env_file_vals.get(env_var, "") or "").strip()
        encrypted, hint = "", ""
        if raw_key:
            encrypted, hint = encrypt(raw_key), make_hint(raw_key)
        elif pid in admin_keys and admin_keys[pid]["encrypted"]:
            encrypted = admin_keys[pid]["encrypted"]
            hint = admin_keys[pid]["hint"]

        # 未配置 Key 的内置 provider 不迁移——让它彻底从配置中心消失，不再是幽灵卡片
        if not encrypted:
            continue

        # 模型 / base_url / 显示名：全局覆盖(provider_configs user_id='') 优先，否则用种子常量
        global_cfg = None
        try:
            global_cfg = wl_store.get_provider_config(pid, user_id="")
        except Exception:
            global_cfg = None

        model = (global_cfg and global_cfg.get("default_model")) or PROVIDER_MODELS.get(pid, "") or pid
        base_url = (global_cfg and global_cfg.get("base_url")) or _BUILTIN_BASE_URLS.get(pid, "") or ""
        display = (global_cfg and global_cfg.get("display_name")) or _PROVIDER_DISPLAY.get(pid, pid)

        try:
            auth_store.save_custom_provider(pid, display, base_url, encrypted, hint, model)
            migrated += 1
            logger.info("已迁移内置 provider 至统一管理: %s", pid)
        except Exception as e:
            logger.warning("迁移内置 provider 失败 (%s): %s", pid, e)
            continue

        # 清除该 pid 的 provider_configs 覆盖（全局 + admin 用户级），避免旧覆盖影子解析
        for uid in ("", admin_user_id):
            if uid is None:
                continue
            try:
                wl_store.delete_provider_config(pid, user_id=uid)
            except Exception:
                pass

    if migrated:
        logger.info("内置 provider 统一迁移完成，共迁移 %d 个", migrated)
    return migrated
