"""模型上下文窗口(token容量)映射 —— 供智能调度做「事前正确选型」：重上下文角色不选容量不足的模型。

原则：**只排除已知的小模型**，未知模型一律当"够大"放行（宁可漏排、由运行时 fallback 兜底，
也不误伤没收录的大模型）。表需随模型更新维护，但粗粒度 + 保守默认即可用。
"""

from __future__ import annotations

# (模型名子串, 上下文窗口 tokens)。顺序敏感：**具体在前、泛化在后**，命中首个即返回。
_CONTEXT_MAP: list[tuple[str, int]] = [
    # Kimi / Moonshot（本次踩坑：moonshot-v1-8k 仅 8192）
    ("moonshot-v1-8k", 8_192),
    ("moonshot-v1-32k", 32_768),
    ("moonshot-v1-128k", 131_072),
    ("kimi", 131_072),           # kimi 新版多为大窗口（moonshot-v1-8k 已在上面先匹配）
    # 通义千问
    ("qwen-turbo", 8_192),
    ("qwen-long", 1_000_000),
    ("qwen", 32_768),
    # DeepSeek
    ("deepseek", 65_536),
    # 智谱 GLM
    ("glm-3", 8_192),
    ("glm-4", 131_072),
    ("glm", 131_072),
    # OpenAI
    ("gpt-3.5", 16_385),
    ("gpt-4o", 131_072),
    ("gpt-4-turbo", 131_072),
    ("gpt-4.1", 1_000_000),
    ("gpt-4", 8_192),            # 老 gpt-4 基础版 8k（gpt-4o/turbo 已在上面先匹配）
    ("o1", 128_000),
    ("o3", 128_000),
    # Anthropic / Google
    ("claude", 200_000),
    ("gemini-1.5", 1_000_000),
    ("gemini-2", 1_000_000),
    ("gemini", 32_768),
    # 其它常见
    ("ernie", 8_192),
    ("doubao", 32_768),
    ("hunyuan", 32_768),
]

# 未知模型：当"够大"，绝不因未收录而排除（只信"已知小"，避免误伤）。
_UNKNOWN_CONTEXT = 1_000_000_000


def get_context_window(model: str) -> int:
    """返回模型上下文窗口(tokens)；未知返回极大值(视为够大，不被容量门排除)。"""
    m = (model or "").lower().strip()
    if not m:
        return _UNKNOWN_CONTEXT
    for pat, ctx in _CONTEXT_MAP:
        if pat in m:
            return ctx
    return _UNKNOWN_CONTEXT


def fits(model: str, min_context: int) -> bool:
    """模型上下文是否满足角色最低需求。min_context<=0 视为无要求(恒 True)。"""
    if not min_context or min_context <= 0:
        return True
    return get_context_window(model) >= min_context


if __name__ == "__main__":
    # ponytail 自检：已知小模型被识别、未知放行、门槛判断正确
    assert get_context_window("moonshot-v1-8k") == 8_192
    assert get_context_window("deepseek-chat") == 65_536
    assert get_context_window("some-unknown-model-x") == _UNKNOWN_CONTEXT
    assert fits("moonshot-v1-8k", 16_384) is False       # 8k 装不下重角色
    assert fits("deepseek-chat", 16_384) is True
    assert fits("moonshot-v1-8k", 0) is True             # 无要求恒 True
    assert fits("unknown-x", 131_072) is True            # 未知放行
    print("model_context 自检通过")
