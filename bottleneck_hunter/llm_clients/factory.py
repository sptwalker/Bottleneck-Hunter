"""LLM client factory for BottleneckHunter.

Supports: openai, anthropic, deepseek, google, qwen, glm, minimax, openrouter, siliconflow, agnes, kimi

Per-user API KEY: 传入 api_key 参数即可覆盖 .env 中的全局 KEY。
"""

from __future__ import annotations

import os

from langchain_core.language_models import BaseChatModel

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


def _resolve_key(provider: str, api_key: str | None = None) -> str | None:
    """解析 API KEY：优先用户传入 → 其次环境变量。"""
    if api_key:
        return api_key
    env_var = PROVIDER_KEY_MAP.get(provider, "")
    return os.getenv(env_var) if env_var else None


def create_llm(provider: str, model: str, api_key: str | None = None, **kwargs) -> BaseChatModel:
    """Create a chat LLM instance for the given provider and model.

    Args:
        provider: LLM 服务商标识
        model: 模型名称
        api_key: 用户级 API KEY（优先级高于 .env 全局 KEY）
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

    # Generic OpenAI-compatible endpoint
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

    raise ValueError(f"Unsupported LLM provider: {provider}")
