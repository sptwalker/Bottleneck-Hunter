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
    # 单点注入超时与重试上限：所有 provider 分支共享 **kwargs，故这里设默认即全链覆盖。
    # 根因修复——挂起的 provider 会占住线程池最长无限久，拖垮全应用的 to_thread，
    # 且熔断器因“挂起不抛异常”永不触发。timeout 是防挂起的关键；每次尝试都受它约束。
    # max_retries 保留 SDK 默认 2：对瞬时 429/5xx 仍有韧性（一次性调用如热点扫描无上层重试，
    # retries=0 会因单次瞬时错误直接失败）；最坏 (2+1)×timeout 仍有界。callers 可覆盖。
    kwargs.setdefault("timeout", float(os.getenv("BH_LLM_TIMEOUT", "60")))
    kwargs.setdefault("max_retries", int(os.getenv("BH_LLM_MAX_RETRIES", "2")))
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
    prefer_primary: bool = False,
) -> list[tuple[BaseChatModel, str, str]]:
    """统一接口: 返回该角色配置的所有模型实例列表。

    优先级: 数据库 ai_role_config(手填矩阵/可覆盖) → [prefer_primary 时: 顶栏主模型直用]
        → 角色注册表默认值(仅单模型位) → 智能调度排序
    （DC_MODEL_* 环境影子配置已退役，不再读取）

    prefer_primary: True 时（供产业链分析/瓶颈评分/供应商评估等重要环节使用）——用户设了顶栏
        「主要模型」即完全由它决定，系统不再走智能调度自选。仅当主模型不可用(禁用/无Key/熔断)
        才回退后续优先级保证可用。注意：对多槽 fan-out 角色(如瓶颈交叉)这会退化为单主模型。
    with_fallback: 默认 False —— 多模型 fan-out 角色（投委会/圆桌/瓶颈交叉/L1）沿用「某成员失败即丢弃」
        语义并保持 provider 多样性，不做跨 provider 自动替换。单模型位（get_llm_for_position）传 True。

    Returns:
        list of (llm_instance, provider_id, model_name)
    """
    from bottleneck_hunter.auth.current_user import get_current_user_id
    uid = user_id or get_current_user_id()
    # 角色元信息：多槽 fan-out 角色需返回 N 个多样化模型（交叉验证）
    role_def = None
    try:
        from bottleneck_hunter.llm_clients.role_registry import get_role
        role_def = get_role(role_key)
    except Exception:  # noqa: BLE001
        pass
    multi = bool(role_def and role_def.multi_model)
    n_slots = (role_def.max_slots if (multi and role_def.max_slots) else 1)
    # 事前容量门：重上下文角色不选窗口不足的模型（本次 kimi-8k 踩坑）。仅作用于系统自动选的
    # 优先级3(默认)/优先级4(调度)，用户手填矩阵(优先级1)尊重其显式选择。
    from bottleneck_hunter.llm_clients.model_context import fits as _ctx_fits
    role_min_ctx = getattr(role_def, "min_context", 0) if role_def else 0

    # 优先级1: 数据库矩阵（手动覆盖，最高优先）
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

    # 优先级1.5（仅 prefer_primary）：顶栏「主要模型」直用 —— 用户设了主模型即由它完全决定，
    # 不走智能调度。仅当主模型可用(启用+有Key+未熔断)才用；否则回退后续优先级保证可用。
    # 供产业链分析/瓶颈评分/供应商评估等重要环节使用（用户诉求：主模型决定这些环节）。
    if prefer_primary:
        prim = (_PRIMARY_PROVIDER or "").lower().strip()
        if prim and is_provider_active(prim) and _user_has_llm_key(prim, uid):
            try:
                from bottleneck_hunter.llm_clients.health import health as _health
                _open = _health.is_open(uid, prim)
            except Exception:  # noqa: BLE001
                _open = False
            if not _open:
                pm = resolve_provider_model(prim, uid)
                if pm:
                    try:
                        _llm = create_llm(prim, pm, temperature=temperature,
                                          with_fallback=with_fallback, user_id=uid)
                        return [(_llm, prim, pm)]  # 多槽角色也退化为单主模型(用户显式要求)
                    except Exception as e:  # noqa: BLE001
                        logger.warning("prefer_primary 主模型 %s/%s 构建失败，回退调度: %s", prim, pm, e)

    # 优先级2（DC_MODEL_* 环境影子配置）已退役：它是旧的静态角色→模型硬绑定，会**无条件**
    # 覆盖智能调度、且单值会让多槽 fan-out 塌成 1 个模型。统一配置只认「手填矩阵(可覆盖)」+
    # 智能调度，不再读 DC_MODEL_（见 docs/MODEL_SCHEDULER_DESIGN.md Phase 2.5、memory project_ai_config_unified）。

    # 优先级3: 角色注册表默认值 —— 仅单模型位（多槽 fan-out 交由优先级4 自动选 N 个多样化模型）。
    # 默认 provider 若正处熔断（近期失效）则跳过，交由优先级4 排序选一个健康的，避免每请求先打死模型白耗一轮。
    if not multi and role_def:
        try:
            from bottleneck_hunter.llm_clients.health import health as _health
            dp = role_def.default_provider
            if is_provider_active(dp) and not _health.is_open(uid, dp) and _user_has_llm_key(dp, uid):
                model = role_def.default_model or resolve_provider_model(dp, uid)
                if model and _ctx_fits(model, role_min_ctx):  # 默认模型容量不足→落到优先级4另选
                    try:
                        return [(create_llm(dp, model, temperature=temperature, with_fallback=with_fallback, user_id=uid),
                                 dp, model)]
                    except Exception:
                        pass
        except Exception:  # noqa: BLE001
            pass

    # 优先级4: 智能调度 —— 候选池 = 主模型 + 所有已注册 provider + 应急链，按
    #   健康度×可靠性×能力先验 + 用户策略 排序；单模型位取 top-1，多槽 fan-out 取 top-N
    #   个不同 provider（保持交叉验证多样性）。无数据/无策略 → 稳定排序退化为静态链。
    try:
        universe = list_custom_provider_ids()
    except Exception:  # noqa: BLE001
        universe = []
    chain = ([_PRIMARY_PROVIDER] if _PRIMARY_PROVIDER else []) + universe + [p for p, _ in _FALLBACK_CHAIN]
    _seen0: set[str] = set()
    chain = [p for p in ((c or "").lower().strip() for c in chain) if p and not (p in _seen0 or _seen0.add(p))]
    policy: dict = {}
    tier_of = None
    try:
        from bottleneck_hunter.llm_clients.health import rank_providers, load_routing_policy, provider_tier
        policy = load_routing_policy(uid, role_key)
        chain = rank_providers(chain, uid, _PRIMARY_PROVIDER, policy=policy, role_key=role_key)
        tier_of = provider_tier
    except Exception:  # noqa: BLE001
        pass
    results = []
    seen_prov: set[str] = set()
    deferred: list[tuple[str, str]] = []  # 容量不足者(provider, model)：够大的选完仍缺槽位才回填
    for provider in chain:
        if not is_provider_active(provider) or provider in seen_prov:
            continue
        if not _user_has_llm_key(provider, uid):
            continue
        model = resolve_provider_model(provider, uid)
        if not model:
            continue
        seen_prov.add(provider)
        if not _ctx_fits(model, role_min_ctx):
            deferred.append((provider, model))  # 容量不足重角色，暂不选
            continue
        try:
            llm = create_llm(provider, model, temperature=temperature, with_fallback=with_fallback, user_id=uid)
        except Exception:
            continue
        results.append((llm, provider, model))
        # 免费→付费回落强提示（仅首个主选，且用户确有免费 provider 的 KEY——否则纯付费用户会被
        # 误报"免费不可用"并每次刷屏；只有"免费本可用但当前熔断/失效"才提示）。
        if (len(results) == 1 and tier_of and policy.get("prefer_tier") == "free"
                and tier_of(provider) == "paid"
                and any(tier_of(c) == "free" and is_provider_active(c) and _user_has_llm_key(c, uid)
                        for c in chain)):
            try:
                from bottleneck_hunter.llm_clients.fallback import push_notice
                push_notice({"type": "tier_fallback", "provider": provider, "model": model,
                             "message": f"⚠️ 免费模型当前不可用，已临时启用付费模型 {provider}/{model}"})
            except Exception:  # noqa: BLE001
                pass
        if len(results) >= n_slots:
            break

    # 够大的模型填不满槽位 → 回填容量不足者（绝不留空/少槽；此时靠运行时 fallback + 手动重试兜底）
    for provider, model in deferred:
        if len(results) >= n_slots:
            break
        try:
            llm = create_llm(provider, model, temperature=temperature, with_fallback=with_fallback, user_id=uid)
        except Exception:
            continue
        results.append((llm, provider, model))

    return results


def get_llm_for_position(
    position: str | None = None,
    provider_hint: str | None = None,
    temperature: float = 0.3,
    with_fallback: bool = True,
    prefer_primary: bool = False,
) -> tuple[BaseChatModel | None, str, str]:
    """统一的「按 position 获取 LLM」入口（向后兼容）。

    委托给 get_models_for_role() 取第一个结果。
    provider_hint 作为旧代码的兼容路径保留。
    prefer_primary: True 时（产业链分析/供应商评估等重要环节）用户设了顶栏主模型即由它决定，
        不走智能调度；主模型不可用才回退。透传给 get_models_for_role。
    with_fallback: 默认 True，单模型位获得调用失败自动替换；自管重试链的调用方
        （如 committee._build_llm_chain）传 False 以拿到裸模型。
    返回: (llm_instance, provider_id, model_name) 或 (None, '', '')
    """
    try:
        from bottleneck_hunter.auth.current_user import get_current_user_id
        uid = get_current_user_id()
        if position:
            results = get_models_for_role(position, user_id=uid, temperature=temperature,
                                          with_fallback=with_fallback, prefer_primary=prefer_primary)
            if results:
                return results[0]

        if provider_hint and _user_has_llm_key(provider_hint, uid):
            model = resolve_provider_model(provider_hint, uid)
            if model:
                return create_llm(provider_hint, model, temperature=temperature, with_fallback=with_fallback, user_id=uid), provider_hint, model

        # 无 position / hint 未命中 → 统一智能调度。传一个未注册的通用 role_key，使
        # get_models_for_role 跳过角色专属默认(优先级3)、直接落到优先级4 的全域调度：
        # 主模型 + 用户所有已注册 provider + 应急链，按健康度排序取 top-1。
        # 绝不再只试硬编码 4 条应急链(deepseek/qwen/kimi/glm)——用户配的其它 provider 会被无视。
        results = get_models_for_role("__default__", user_id=uid, temperature=temperature, with_fallback=with_fallback)
        if results:
            return results[0]
    except Exception as e:
        logger.warning("get_llm_for_position 失败 (position=%s): %s", position, e)
    return None, "", ""
