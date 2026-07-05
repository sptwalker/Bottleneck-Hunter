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

# 各 provider 的默认模型 —— 仅作首启种子/最终兜底；运行时一律经 resolve_provider_model 读取，可被 provider_configs 覆盖
PROVIDER_MODELS: dict[str, str] = {
    "openai": "gpt-4o",
    "anthropic": "claude-sonnet-4-6",
    "deepseek": "deepseek-chat",
    "google": "gemini-2.5-flash",
    "qwen": "qwen-plus",
    "glm": "glm-4-flash",
    "minimax": "MiniMax-Text-01",
    "openrouter": "deepseek/deepseek-chat",
    "siliconflow": "deepseek-ai/DeepSeek-V3",
    "agnes": "agnes-2.0-flash",
    "kimi": "moonshot-v1-8k",
}

# 内置 OpenAI 兼容 provider 的官方端点 —— 种子；可被 provider_configs.base_url 覆盖。
# openai/anthropic/google 走各自 SDK 默认端点，不在此表。
_BUILTIN_BASE_URLS: dict[str, str] = {
    "deepseek": "https://api.deepseek.com",
    "openrouter": "https://openrouter.ai/api/v1",
    "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "glm": "https://open.bigmodel.cn/api/paas/v4",
    "siliconflow": "https://api.siliconflow.cn/v1",
    "agnes": "https://apihub.agnes-ai.com/v1",
    "kimi": "https://api.moonshot.cn/v1",
    "minimax": "https://api.minimax.chat/v1",
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


# ── provider 覆盖（全局/user_id='' 行的运行时缓存）+ 模型/base_url 解析 ──────────
_PROVIDER_OVERRIDES: dict[str, dict] = {}  # {provider_id: {default_model, base_url}}


def register_provider_override(provider_id: str, default_model: str = "", base_url: str = ""):
    """写入/刷新单个 provider 的全局覆盖到运行时缓存（保存后调用）。"""
    _PROVIDER_OVERRIDES[provider_id] = {"default_model": default_model or "", "base_url": base_url or ""}


def refresh_provider_overrides():
    """从 DB 加载全局(user_id='')provider_configs 到运行时缓存（应用启动时调用）。"""
    try:
        from bottleneck_hunter.watchlist.store import WatchlistStore
        rows = WatchlistStore().get_provider_configs(user_id="")
        _PROVIDER_OVERRIDES.clear()
        for r in rows:
            _PROVIDER_OVERRIDES[r["provider_id"]] = {
                "default_model": r.get("default_model") or "",
                "base_url": r.get("base_url") or "",
            }
        logger.info("已加载 %d 条 provider 覆盖配置", len(_PROVIDER_OVERRIDES))
    except Exception as e:
        logger.debug("加载 provider 覆盖失败: %s", e)


def _load_provider_config_from_db(provider_id: str, user_id: str) -> dict | None:
    try:
        from bottleneck_hunter.watchlist.store import WatchlistStore
        return WatchlistStore().get_provider_config(provider_id, user_id=user_id)
    except Exception:
        return None


def resolve_provider_model(provider: str, user_id: str = "") -> str:
    """解析某 provider 应使用的模型：用户覆盖 → 全局覆盖 → 自定义端点 → 种子常量。"""
    provider = (provider or "").lower().strip()
    if user_id:
        cfg = _load_provider_config_from_db(provider, user_id)
        if cfg and cfg.get("default_model"):
            return cfg["default_model"]
    ov = _PROVIDER_OVERRIDES.get(provider)
    if ov and ov.get("default_model"):
        return ov["default_model"]
    custom = _CUSTOM_PROVIDERS.get(provider)
    if custom and custom.get("default_model"):
        return custom["default_model"]
    return PROVIDER_MODELS.get(provider, "")


def resolve_provider_base_url(provider: str, user_id: str = "") -> str | None:
    """解析某 provider 的 base_url：用户覆盖 → 全局覆盖 → 自定义端点 → 内置官方端点种子。

    返回 None 表示走该 provider 的 SDK 默认端点（openai/anthropic/google）。
    """
    provider = (provider or "").lower().strip()
    if user_id:
        cfg = _load_provider_config_from_db(provider, user_id)
        if cfg and cfg.get("base_url"):
            return cfg["base_url"]
    ov = _PROVIDER_OVERRIDES.get(provider)
    if ov and ov.get("base_url"):
        return ov["base_url"]
    custom = _CUSTOM_PROVIDERS.get(provider)
    if custom and custom.get("base_url"):
        return custom["base_url"]
    return _BUILTIN_BASE_URLS.get(provider)


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
    user_id: str = "",
    **kwargs,
) -> BaseChatModel:
    """Create a chat LLM instance for the given provider and model.

    Args:
        provider: LLM 服务商标识（内置或自定义）
        model: 模型名称；留空则经 resolve_provider_model 解析（provider_configs → 种子）
        api_key: 用户级 API KEY（优先级高于 .env 全局 KEY）
        base_url: 显式端点（最高优先级）；否则经 resolve_provider_base_url 解析
        user_id: 传入则优先用该用户的 provider_configs 覆盖
    """
    provider = provider.lower().strip()
    # Key 优先级：显式传入 > 统一 provider 缓存（含迁移来的原内置）> env 兜底（CLI 无 UI 时）。
    # 缓存优先于 env，保证在 UI 里改 Key 后立即生效、不被旧的 .env 值盖住。
    key = api_key
    if not key:
        custom = _CUSTOM_PROVIDERS.get(provider)
        if custom and custom.get("api_key"):
            key = custom["api_key"]
    if not key:
        key = _resolve_key(provider, None)
    if not model:
        model = resolve_provider_model(provider, user_id)
    resolved_base = base_url or resolve_provider_base_url(provider, user_id)

    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic
        # anthropic SDK 支持自定义端点（base_url 覆盖时透传）
        if resolved_base:
            return ChatAnthropic(model=model, api_key=key, base_url=resolved_base, **kwargs)
        return ChatAnthropic(model=model, api_key=key, **kwargs)

    if provider == "google":
        from langchain_google_genai import ChatGoogleGenerativeAI
        return ChatGoogleGenerativeAI(model=model, google_api_key=key, **kwargs)

    # 其余全部走 OpenAI 兼容：openai 官方端点(base_url=None) / 内置(deepseek/qwen/…) / 自定义 / 显式 base_url
    from langchain_openai import ChatOpenAI
    if provider == "openai" and not resolved_base:
        return ChatOpenAI(model=model, api_key=key, **kwargs)
    if not resolved_base:
        raise ValueError(f"不支持的 LLM provider: {provider}（无 base_url，且非 openai/anthropic/google）")
    return ChatOpenAI(model=model, api_key=key or "not-needed", base_url=resolved_base, **kwargs)


# ── 统一 LLM 获取入口 ──────────────────────────────────

# 应急回退顺序：(provider, env_var)。模型不写死，一律经 resolve_provider_model 解析。
_FALLBACK_CHAIN = [
    ("deepseek", "DEEPSEEK_API_KEY"),
    ("qwen", "DASHSCOPE_API_KEY"),
    ("kimi", "MOONSHOT_API_KEY"),
    ("glm", "ZHIPU_API_KEY"),
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

    # 优先级3: 角色注册表默认值（模型经解析器：role.default_model → provider_configs → 种子）
    try:
        from bottleneck_hunter.llm_clients.role_registry import get_role
        role_def = get_role(role_key)
        if role_def:
            env_key = PROVIDER_KEY_MAP.get(role_def.default_provider, "")
            if env_key and os.getenv(env_key):
                model = role_def.default_model or resolve_provider_model(role_def.default_provider)
                if model:
                    try:
                        return [(create_llm(role_def.default_provider, model, temperature=temperature),
                                 role_def.default_provider, model)]
                    except Exception:
                        pass
    except Exception:
        pass

    # 优先级4: fallback 链（模型经解析器，不写死）
    for provider, key_env in _FALLBACK_CHAIN:
        if os.getenv(key_env):
            model = resolve_provider_model(provider)
            if not model:
                continue
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
            env_key = PROVIDER_KEY_MAP.get(provider_hint, "")
            if env_key and os.getenv(env_key):
                model = resolve_provider_model(provider_hint)
                if model:
                    return create_llm(provider_hint, model, temperature=temperature), provider_hint, model

        for provider, key_env in _FALLBACK_CHAIN:
            if os.getenv(key_env):
                model = resolve_provider_model(provider)
                if model:
                    return create_llm(provider, model, temperature=temperature), provider, model
    except Exception as e:
        logger.warning("get_llm_for_position 失败 (position=%s): %s", position, e)
    return None, "", ""
