"""LLM client factory for BottleneckHunter.

Supports: openai, anthropic, deepseek, google, qwen, glm, ollama, openrouter
"""

from __future__ import annotations

import os

from langchain_core.language_models import BaseChatModel


def create_llm(provider: str, model: str, **kwargs) -> BaseChatModel:
    """Create a chat LLM instance for the given provider and model."""
    provider = provider.lower().strip()

    if provider == "openai":
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(model=model, api_key=os.getenv("OPENAI_API_KEY"), **kwargs)

    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(
            model=model,
            api_key=os.getenv("ANTHROPIC_API_KEY"),
            **kwargs,
        )

    if provider == "deepseek":
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(
            model=model,
            api_key=os.getenv("DEEPSEEK_API_KEY"),
            base_url="https://api.deepseek.com",
            **kwargs,
        )

    if provider == "google":
        from langchain_google_genai import ChatGoogleGenerativeAI
        return ChatGoogleGenerativeAI(
            model=model,
            google_api_key=os.getenv("GOOGLE_API_KEY"),
            **kwargs,
        )

    if provider == "ollama":
        from langchain_ollama import ChatOllama
        base_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
        return ChatOllama(model=model, base_url=base_url, **kwargs)

    if provider == "openrouter":
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(
            model=model,
            api_key=os.getenv("OPENROUTER_API_KEY"),
            base_url="https://openrouter.ai/api/v1",
            **kwargs,
        )

    # Generic OpenAI-compatible endpoint
    if provider in ("qwen", "glm", "siliconflow"):
        base_urls = {
            "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "glm": "https://open.bigmodel.cn/api/paas/v4",
            "siliconflow": "https://api.siliconflow.cn/v1",
        }
        key_map = {
            "qwen": "DASHSCOPE_API_KEY",
            "glm": "ZHIPU_API_KEY",
            "siliconflow": "SILICONFLOW_API_KEY",
        }
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(
            model=model,
            api_key=os.getenv(key_map[provider]),
            base_url=base_urls[provider],
            **kwargs,
        )

    raise ValueError(f"Unsupported LLM provider: {provider}")
