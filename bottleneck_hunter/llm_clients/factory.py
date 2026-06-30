"""LLM client factory for BottleneckHunter.

Supports: openai, anthropic, deepseek, google, qwen, glm, minimax, openrouter, siliconflow, agnes, kimi
以及用户自定义的 OpenAI 兼容端点。

Per-user API KEY: 传入 api_key 参数即可覆盖 .env 中的全局 KEY。
"""

from __future__ import annotations

import logging
import os

from langchain_core.language_models import BaseChatModel

logger = logging.getLogger(__name__)

# provider → 环境变量名映射，供外部查询
PROVIDER_KEY_MAP: dict[str, str] = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "google": "GOOGLE_API_KEY",
    "qwen": "DASHSCOPE_API_KEY",
    "glm": "ZHIPU_API_KEY",
    "siliconflow": "SILICONFLOW_API_KEY",
    "agnes": "AGNES_API_KEY",
    "kimi": "MOONSHOT_API_KEY",
    "minimax": "MINIMAX_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
}

# ── 自定义 provider 运行时缓存 ──────────────────────────
_CUSTOM_PROVIDERS: dict[str, dict] = {}


def register_custom_provider(provider_id: str, base_url: str, api_key: str, default_model: str):
    """注册一个自定义 OpenAI 兼容 provider 到运行时缓存。"""
    _CUSTOM_PROVIDERS[provider_id] = {
        "base_url": base_url,
        "api_key": api_key,
        "default_model": default_model,
    }
    logger.info("已注册自定义 provider: %s (%s)", provider_id, base_url)


def unregister_custom_provider(provider_id: str):
    """从运行时缓存移除自定义 provider。"""
    _CUSTOM_PROVIDERS.pop(provider_id, None)


def get_custom_provider(provider_id: str) -> dict | None:
    """查询自定义 provider 信息。"""
    return _CUSTOM_PROVIDERS.get(provider_id)


def list_custom_provider_ids() -> list[str]:
    """列出所有已注册的自定义 provider id。"""
    return list(_CUSTOM_PROVIDERS.keys())


def _resolve_key(provider: str, api_key: str | None = None) -> str | None:
    """解析 API KEY：优先用户传入 → 其次环境变量。"""
    if api_key:
        return api_key
    env_var = PROVIDER_KEY_MAP.get(provider, "")
    return os.getenv(env_var) if env_var else None


def create_llm(
    provider: str,
    model: str,
    api_key: str | None = None,
    base_url: str | None = None,
    **kwargs,
) -> BaseChatModel:
    """Create a chat LLM instance for the given provider and model.

    Args:
        provider: LLM 服务商标识（内置或自定义）
        model: 模型名称
        api_key: 用户级 API KEY（优先级高于 .env 全局 KEY）
        base_url: 自定义 API 端点地址（优先级高于内置配置）
    """
    provider = provider.lower().strip()
    key = _resolve_key(provider, api_key)

    if provider == "openai":
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(model=model, api_key=key, **kwargs)

    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(
            model=model,
            api_key=key,
            **kwargs,
        )

    if provider == "deepseek":
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(
            model=model,
            api_key=key,
            base_url="https://api.deepseek.com",
            **kwargs,
        )

    if provider == "google":
        from langchain_google_genai import ChatGoogleGenerativeAI
        return ChatGoogleGenerativeAI(
            model=model,
            google_api_key=key,
            **kwargs,
        )

    if provider == "openrouter":
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(
            model=model,
            api_key=key,
            base_url="https://openrouter.ai/api/v1",
            **kwargs,
        )

    # Generic OpenAI-compatible endpoint (内置)
    if provider in ("qwen", "glm", "siliconflow", "agnes", "kimi", "minimax"):
        base_urls = {
            "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "glm": "https://open.bigmodel.cn/api/paas/v4",
            "siliconflow": "https://api.siliconflow.cn/v1",
            "agnes": "https://apihub.agnes-ai.com/v1",
            "kimi": "https://api.moonshot.cn/v1",
            "minimax": "https://api.minimax.chat/v1",
        }
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(
            model=model,
            api_key=key,
            base_url=base_urls[provider],
            **kwargs,
        )

    # 自定义 provider（运行时缓存）
    custom = _CUSTOM_PROVIDERS.get(provider)
    if custom:
        from langchain_openai import ChatOpenAI
        final_key = key or custom.get("api_key") or "not-needed"
        return ChatOpenAI(
            model=model,
            api_key=final_key,
            base_url=custom["base_url"],
            **kwargs,
        )

    # 显式传入 base_url 的兜底
    if base_url:
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(
            model=model,
            api_key=key or "not-needed",
            base_url=base_url,
            **kwargs,
        )

    raise ValueError(f"不支持的 LLM provider: {provider}")


# ── 统一 LLM 获取入口 ──────────────────────────────────

_FALLBACK_CHAIN = [
    ("deepseek", "deepseek-chat", "DEEPSEEK_API_KEY"),
    ("qwen", "qwen-plus", "DASHSCOPE_API_KEY"),
    ("kimi", "moonshot-v1-8k", "MOONSHOT_API_KEY"),
    ("glm", "glm-4-flash", "ZHIPU_API_KEY"),
]


def _load_role_configs_from_db(role_key: str, user_id: str = "") -> list[dict]:
    """从数据库 ai_role_config 表读取角色配置。"""
    try:
        from bottleneck_hunter.watchlist.store import WatchlistStore
        store = WatchlistStore()
        return store.get_role_configs(role_key=role_key, user_id=user_id)
    except Exception:
        return []


def get_models_for_role(
    role_key: str,
    user_id: str = "",
    temperature: float = 0.3,
) -> list[tuple[BaseChatModel, str, str]]:
    """统一接口: 返回该角色配置的所有模型实例列表。

    优先级: 数据库 ai_role_config → DC_MODEL_* 环境变量 → 角色注册表默认值 → fallback 链

    Returns:
        list of (llm_instance, provider_id, model_name)
    """
    # 优先级1: 数据库
    configs = _load_role_configs_from_db(role_key, user_id)
    if configs:
        results = []
        for cfg in configs:
            try:
                llm = create_llm(cfg["provider"], cfg["model"], temperature=temperature)
                results.append((llm, cfg["provider"], cfg["model"]))
            except Exception as e:
                logger.warning("create_llm 失败 %s/%s: %s", cfg["provider"], cfg["model"], e)
        if results:
            return results

    # 优先级2: 环境变量
    env_val = os.environ.get(f"DC_MODEL_{role_key.upper()}", "").strip()
    if env_val and ":" in env_val:
        p, m = env_val.split(":", 1)
        try:
            return [(create_llm(p, m, temperature=temperature), p, m)]
        except Exception:
            pass

    # 优先级3: 角色注册表默认值
    try:
        from bottleneck_hunter.llm_clients.role_registry import get_role
        role_def = get_role(role_key)
        if role_def:
            env_key = PROVIDER_KEY_MAP.get(role_def.default_provider, "")
            if env_key and os.getenv(env_key):
                try:
                    return [(create_llm(role_def.default_provider, role_def.default_model,
                                       temperature=temperature),
                             role_def.default_provider, role_def.default_model)]
                except Exception:
                    pass
    except Exception:
        pass

    # 优先级4: fallback 链
    for provider, model, key_env in _FALLBACK_CHAIN:
        if os.getenv(key_env):
            try:
                return [(create_llm(provider, model, temperature=temperature), provider, model)]
            except Exception:
                continue

    return []


def get_llm_for_position(
    position: str | None = None,
    provider_hint: str | None = None,
    temperature: float = 0.3,
) -> tuple[BaseChatModel | None, str, str]:
    """统一的「按 position 获取 LLM」入口（向后兼容）。

    委托给 get_models_for_role() 取第一个结果。
    provider_hint 作为旧代码的兼容路径保留。
    返回: (llm_instance, provider_id, model_name) 或 (None, '', '')
    """
    try:
        if position:
            results = get_models_for_role(position, temperature=temperature)
            if results:
                return results[0]

        if provider_hint:
            hint_defaults = {
                "deepseek": ("deepseek", "deepseek-chat", "DEEPSEEK_API_KEY"),
                "qwen": ("qwen", "qwen-plus", "DASHSCOPE_API_KEY"),
                "kimi": ("kimi", "moonshot-v1-8k", "MOONSHOT_API_KEY"),
                "glm": ("glm", "glm-4-flash", "ZHIPU_API_KEY"),
            }
            cfg = hint_defaults.get(provider_hint)
            if cfg and os.getenv(cfg[2]):
                return create_llm(cfg[0], cfg[1], temperature=temperature), cfg[0], cfg[1]

        for provider, model, key_env in _FALLBACK_CHAIN:
            if os.getenv(key_env):
                return create_llm(provider, model, temperature=temperature), provider, model
    except Exception as e:
        logger.warning("get_llm_for_position 失败 (position=%s): %s", position, e)
    return None, "", ""
