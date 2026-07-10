"""LLM client factory for BottleneckHunter.

Supports: openai, anthropic, deepseek, google, qwen, glm, minimax, openrouter, siliconflow, agnes, kimi
以及用户自定义的 OpenAI 兼容端点。

严格按用户隔离：API KEY 只从「当前上下文用户」的加密存储解析，无任何全局兜底
（不读 .env / os.environ，不借用其他用户的 KEY）。拿不到用户自己的 KEY 即抛
MissingUserKeyError。仅 KEYLESS 白名单（本地 ollama 等）无需 KEY。
"""

from __future__ import annotations

import logging
import os

from langchain_core.language_models import BaseChatModel

logger = logging.getLogger(__name__)


class MissingUserKeyError(RuntimeError):
    """当前用户未配置该 provider 的 API KEY（严格隔离下不兜底）。"""

    def __init__(self, provider: str):
        self.provider = provider
        super().__init__(f"未配置 {provider} 的 API Key，请先在配置中心填入你自己的 Key")


# 本地/无需 KEY 的 provider 白名单：这些不强制要求用户 KEY
KEYLESS_PROVIDERS = {"ollama"}

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
# 严格隔离：这里只缓存 base_url / default_model（非机密），绝不缓存 api_key。
# api_key 一律走用户级加密存储 + 当前上下文用户解析。
_CUSTOM_PROVIDERS: dict[str, dict] = {}


def register_custom_provider(provider_id: str, base_url: str, api_key: str = "", default_model: str = ""):
    """注册自定义 provider 的元数据（base_url/default_model）到运行时缓存。

    api_key 参数保留仅为向后兼容签名，但**不再被缓存**（严格隔离，禁止全局明文 KEY）。
    """
    _CUSTOM_PROVIDERS[provider_id] = {
        "base_url": base_url,
        "default_model": default_model,
    }
    logger.info("已注册自定义 provider 元数据: %s (%s)", provider_id, base_url)


def unregister_custom_provider(provider_id: str):
    """从运行时缓存移除自定义 provider。"""
    _CUSTOM_PROVIDERS.pop(provider_id, None)


def get_custom_provider(provider_id: str) -> dict | None:
    """查询自定义 provider 信息。"""
    return _CUSTOM_PROVIDERS.get(provider_id)


def list_custom_provider_ids() -> list[str]:
    """列出所有已注册的自定义 provider id。"""
    return list(_CUSTOM_PROVIDERS.keys())


# ── provider 启用/禁用 + 全局「主要」运行时状态 ──────────────────────
# 真源是 custom_providers.is_active / is_primary；由 app 启动 + 管理端点推送到此缓存，
# 供解析层（get_models_for_role / build_fallback_candidates）跳过被禁用、优先主要。
_INACTIVE_PROVIDERS: set[str] = set()
_PRIMARY_PROVIDER: str = ""


def set_provider_status(inactive_ids, primary_id: str = "") -> None:
    """刷新「已禁用 provider 集合」与「全局主要 provider」运行时状态。"""
    global _PRIMARY_PROVIDER
    _INACTIVE_PROVIDERS.clear()
    _INACTIVE_PROVIDERS.update((p or "").lower().strip() for p in (inactive_ids or []) if p)
    _PRIMARY_PROVIDER = (primary_id or "").lower().strip()
    logger.info("provider 状态已刷新: 禁用=%s 主要=%s", sorted(_INACTIVE_PROVIDERS), _PRIMARY_PROVIDER or "(无)")


def is_provider_active(provider_id: str) -> bool:
    """provider 是否启用（未被管理员禁用）。未知的默认视为启用。"""
    return (provider_id or "").lower().strip() not in _INACTIVE_PROVIDERS


def get_primary_provider() -> str:
    """当前全局「主要」provider id（管理员设定），未设则空串。"""
    return _PRIMARY_PROVIDER


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


def _resolve_user_llm_key(provider: str, user_id: str) -> str | None:
    """解析某用户自己配置的 provider API KEY（加密表 → 解密），无则 None。严格：不读 env、不借他人。"""
    if not user_id:
        return None
    try:
        from bottleneck_hunter.web.user_api import resolve_user_api_key
        return resolve_user_api_key(user_id, provider)
    except Exception as e:  # noqa: BLE001
        logger.debug("解析用户 LLM KEY 失败 (%s/%s): %s", user_id[:8] if user_id else "", provider, e)
        return None


def _user_has_llm_key(provider: str, user_id: str) -> bool:
    """当前用户是否配置了该 provider 的 KEY（KEYLESS provider 视为始终可用）。"""
    if provider in KEYLESS_PROVIDERS:
        return True
    return bool(_resolve_user_llm_key(provider, user_id))


def create_llm(
    provider: str,
    model: str,
    api_key: str | None = None,
    base_url: str | None = None,
    user_id: str = "",
    with_fallback: bool = True,
    **kwargs,
) -> BaseChatModel:
    """Create a chat LLM instance for the given provider and model.

    Args:
        provider: LLM 服务商标识（内置或自定义）
        model: 模型名称；留空则经 resolve_provider_model 解析（provider_configs → 种子）
        api_key: 用户级 API KEY（优先级高于 .env 全局 KEY）
        base_url: 显式端点（最高优先级）；否则经 resolve_provider_base_url 解析
        user_id: 传入则优先用该用户的 provider_configs 覆盖
        with_fallback: True(默认) 则包一层 FallbackChatModel（调用失败自动换备选模型并提示）。
            测试/自检类调用（要测这一个具体模型）应传 False。
    """
    llm = _create_raw_llm(provider, model, api_key=api_key, base_url=base_url, user_id=user_id, **kwargs)
    if not with_fallback:
        return llm

    from bottleneck_hunter.llm_clients.fallback import FallbackChatModel, build_fallback_candidates
    resolved_model = model or resolve_provider_model(provider, user_id)
    temperature = kwargs.get("temperature", 0.3)
    backups = build_fallback_candidates(provider, resolved_model, user_id, temperature)
    if not backups:
        return llm  # 无可用备选 → 不套壳，保持原样
    return FallbackChatModel(candidates=[(llm, provider.lower().strip(), resolved_model), *backups])


def _create_raw_llm(
    provider: str,
    model: str,
    api_key: str | None = None,
    base_url: str | None = None,
    user_id: str = "",
    **kwargs,
) -> BaseChatModel:
    """构建单个裸 LLM 实例（不含自动替换包装）。"""
    provider = provider.lower().strip()
    # 严格按用户隔离的 KEY 解析：显式传入（测试端点）> 当前上下文用户的加密 KEY。
    # 绝不读 _CUSTOM_PROVIDERS 明文缓存、绝不读 os.getenv、绝不借他人 KEY。
    from bottleneck_hunter.auth.current_user import get_current_user_id
    key = api_key
    uid = user_id or get_current_user_id()
    if not key and uid:
        key = _resolve_user_llm_key(provider, uid)
    if not model:
        model = resolve_provider_model(provider, user_id)
    resolved_base = base_url or resolve_provider_base_url(provider, user_id)

    # 无 KEY 且非 KEYLESS provider（如 ollama）→ 严格失败，不兜底
    if not key and provider not in KEYLESS_PROVIDERS:
        raise MissingUserKeyError(provider)

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
    with_fallback: bool = False,
) -> list[tuple[BaseChatModel, str, str]]:
    """统一接口: 返回该角色配置的所有模型实例列表。

    优先级: 数据库 ai_role_config → DC_MODEL_* 环境变量 → 角色注册表默认值 → fallback 链

    with_fallback: 默认 False —— 多模型 fan-out 角色（投委会/圆桌/瓶颈交叉/L1）沿用「某成员失败即丢弃」
        语义并保持 provider 多样性，不做跨 provider 自动替换。单模型位（get_llm_for_position）传 True。

    Returns:
        list of (llm_instance, provider_id, model_name)
    """
    from bottleneck_hunter.auth.current_user import get_current_user_id
    uid = user_id or get_current_user_id()
    # 优先级1: 数据库
    configs = _load_role_configs_from_db(role_key, uid)
    if configs:
        results = []
        for cfg in configs:
            if not is_provider_active(cfg["provider"]):
                continue  # 跳过已被管理员禁用的 provider（其它优先级会兜底到主要/可用模型）
            try:
                llm = create_llm(cfg["provider"], cfg["model"], temperature=temperature, with_fallback=with_fallback, user_id=uid)
                results.append((llm, cfg["provider"], cfg["model"]))
            except Exception as e:
                logger.warning("create_llm 失败 %s/%s: %s", cfg["provider"], cfg["model"], e)
        if results:
            return results

    # 优先级2: 环境变量（仅选模型，KEY 仍需当前用户自己的）
    env_val = os.environ.get(f"DC_MODEL_{role_key.upper()}", "").strip()
    if env_val and ":" in env_val:
        p, m = env_val.split(":", 1)
        try:
            return [(create_llm(p, m, temperature=temperature, with_fallback=with_fallback, user_id=uid), p, m)]
        except Exception:
            pass

    # 优先级3: 角色注册表默认值（当前用户须已配置该 provider 的 KEY）
    try:
        from bottleneck_hunter.llm_clients.role_registry import get_role
        role_def = get_role(role_key)
        if role_def and is_provider_active(role_def.default_provider) and _user_has_llm_key(role_def.default_provider, uid):
            model = role_def.default_model or resolve_provider_model(role_def.default_provider, uid)
            if model:
                try:
                    return [(create_llm(role_def.default_provider, model, temperature=temperature, with_fallback=with_fallback, user_id=uid),
                             role_def.default_provider, model)]
                except Exception:
                    pass
    except Exception:
        pass

    # 优先级4: 主要模型优先 + 应急兜底链（均跳过被禁用 provider）
    chain = ([_PRIMARY_PROVIDER] if _PRIMARY_PROVIDER else []) + [p for p, _ in _FALLBACK_CHAIN]
    seen: set[str] = set()
    for provider in chain:
        provider = (provider or "").lower().strip()
        if not provider or provider in seen:
            continue
        seen.add(provider)
        if not is_provider_active(provider):
            continue
        if _user_has_llm_key(provider, uid):
            model = resolve_provider_model(provider, uid)
            if not model:
                continue
            try:
                return [(create_llm(provider, model, temperature=temperature, with_fallback=with_fallback, user_id=uid), provider, model)]
            except Exception:
                continue

    return []


def get_llm_for_position(
    position: str | None = None,
    provider_hint: str | None = None,
    temperature: float = 0.3,
    with_fallback: bool = True,
) -> tuple[BaseChatModel | None, str, str]:
    """统一的「按 position 获取 LLM」入口（向后兼容）。

    委托给 get_models_for_role() 取第一个结果。
    provider_hint 作为旧代码的兼容路径保留。
    with_fallback: 默认 True，单模型位获得调用失败自动替换；自管重试链的调用方
        （如 committee._build_llm_chain）传 False 以拿到裸模型。
    返回: (llm_instance, provider_id, model_name) 或 (None, '', '')
    """
    try:
        from bottleneck_hunter.auth.current_user import get_current_user_id
        uid = get_current_user_id()
        if position:
            results = get_models_for_role(position, user_id=uid, temperature=temperature, with_fallback=with_fallback)
            if results:
                return results[0]

        if provider_hint and _user_has_llm_key(provider_hint, uid):
            model = resolve_provider_model(provider_hint, uid)
            if model:
                return create_llm(provider_hint, model, temperature=temperature, with_fallback=with_fallback, user_id=uid), provider_hint, model

        for provider, _key_env in _FALLBACK_CHAIN:
            if _user_has_llm_key(provider, uid):
                model = resolve_provider_model(provider, uid)
                if model:
                    return create_llm(provider, model, temperature=temperature, with_fallback=with_fallback, user_id=uid), provider, model
    except Exception as e:
        logger.warning("get_llm_for_position 失败 (position=%s): %s", position, e)
    return None, "", ""
