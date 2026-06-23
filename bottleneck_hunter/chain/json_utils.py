"""公共 JSON 提取工具 — 处理 LLM 返回的带 markdown code fence 的 JSON 文本。"""

from __future__ import annotations

import json
import re


def strip_fences(text: str) -> str:
    """去除 markdown code fence 包裹，返回纯文本。"""
    text = text.strip()
    fence_match = re.search(r"```(?:json)?\s*\n?(.*?)```", text, re.DOTALL)
    if fence_match:
        return fence_match.group(1).strip()
    return text


def extract_json_object(text: str) -> dict:
    """从 LLM 输出中提取 JSON 对象，容忍 markdown 代码块和额外文本。

    尝试顺序：code fence 剥离 → 直接解析 → 正则匹配 { ... }。
    """
    text = text.strip()

    # 尝试 1: code fence 内容
    fence_match = re.search(r"```(?:json)?\s*\n?(.*?)```", text, re.DOTALL)
    if fence_match:
        try:
            return json.loads(fence_match.group(1).strip())
        except json.JSONDecodeError:
            pass

    # 尝试 2: 直接解析
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 尝试 3: 匹配第一个 { ... } 块（支持一层嵌套）
    brace_match = re.search(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", text, re.DOTALL)
    if brace_match:
        try:
            return json.loads(brace_match.group())
        except json.JSONDecodeError:
            pass

    raise ValueError(f"无法从 LLM 输出中提取有效 JSON: {text[:200]}")


def extract_json_array(text: str) -> list[dict] | None:
    """从 LLM 输出中提取 JSON 数组，容忍 markdown 代码块和额外文本。

    尝试顺序：code fence 剥离 → 直接解析 → 正则匹配 [ ... ] → 逐个匹配 { ... }。
    返回 None 表示无法提取。
    """
    text = text.strip()

    # 去除 code fence
    fence_match = re.search(r"```(?:json)?\s*\n?(.*?)```", text, re.DOTALL)
    if fence_match:
        text = fence_match.group(1).strip()

    # 尝试 1: 直接解析
    try:
        result = json.loads(text)
        if isinstance(result, list):
            return result
        return None
    except json.JSONDecodeError:
        pass

    # 尝试 2: 匹配 [ ... ]
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if match:
        try:
            result = json.loads(match.group())
            if isinstance(result, list):
                return result
        except json.JSONDecodeError:
            pass

    # 尝试 3: 逐个匹配 { ... } 对象
    items = []
    for m in re.finditer(r"\{[^{}]+\}", text):
        try:
            items.append(json.loads(m.group()))
        except json.JSONDecodeError:
            continue
    return items if items else None
