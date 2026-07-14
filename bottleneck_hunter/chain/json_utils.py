"""公共 JSON 提取工具 — 处理 LLM 返回的带 markdown code fence 的 JSON 文本。

对 LLM 常见毛病容错：闭合/未闭合 code fence、前后夹带说明文字、深层嵌套、结尾多余逗号。
关键：用**平衡括号扫描**而非简易正则，避免嵌套对象被截断（曾致投委会评审反复解析失败：
委员输出被 ```json 围栏包裹且结构较深，旧正则只能匹配一层嵌套 → 提取失败）。
"""

from __future__ import annotations

import json
import re

_OPEN_FENCE = re.compile(r"```(?:json)?[ \t]*\r?\n?")
_TRAILING_COMMA = re.compile(r",(\s*[}\]])")


def strip_fences(text: str) -> str:
    """去除 markdown code fence 包裹，返回纯文本。仅处理**闭合**围栏（保持既有契约）。"""
    text = text.strip()
    fence_match = re.search(r"```(?:json)?\s*\n?(.*?)```", text, re.DOTALL)
    if fence_match:
        return fence_match.group(1).strip()
    return text


def _strip_trailing_commas(text: str) -> str:
    """去掉对象/数组结尾多余逗号（LLM 常见小错）。

    ponytail: 简单正则；极少数字符串内恰好出现 ",}"/",]" 的情况忽略不计。
    """
    return _TRAILING_COMMA.sub(r"\1", text)


def _first_balanced(text: str, open_ch: str, close_ch: str) -> str | None:
    """返回从首个 open_ch 起、括号平衡的第一段子串（正确处理字符串与转义）。

    支持任意嵌套深度，取代只能匹配一层的简易正则；串内的括号不计数。未闭合返回 None。
    """
    start = text.find(open_ch)
    if start == -1:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == open_ch:
            depth += 1
        elif c == close_ch:
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def _iter_balanced(text: str, open_ch: str, close_ch: str):
    """依次产出所有顶层平衡子串（从夹带文本中逐个抠对象）。"""
    i = 0
    while i < len(text):
        nxt = text.find(open_ch, i)
        if nxt == -1:
            return
        block = _first_balanced(text[nxt:], open_ch, close_ch)
        if block is None:
            return
        yield block
        i = nxt + len(block)


def _fence_candidates(text: str) -> list[str]:
    """待解析候选：原文、去闭合围栏、去未闭合起始围栏（模型截断/忘收尾）。"""
    text = text.strip()
    out = [text]
    stripped = strip_fences(text)
    if stripped != text:
        out.append(stripped)
    m = _OPEN_FENCE.match(text)
    if m:
        tail = text[m.end():].strip()
        if tail and tail not in out:
            out.append(tail)
    return out


def extract_json_object(text: str) -> dict:
    """从 LLM 输出中提取 JSON 对象，容忍 code fence（含未闭合）、夹带文字、深层嵌套、结尾逗号。

    对每个候选文本：直接/去尾逗号解析 → 平衡括号扫描首个 { ... }。取第一个成功的 dict。
    """
    for t in _fence_candidates(text):
        for cand in (t, _strip_trailing_commas(t)):
            try:
                obj = json.loads(cand)
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                pass
        block = _first_balanced(t, "{", "}")
        if block:
            for cand in (block, _strip_trailing_commas(block)):
                try:
                    obj = json.loads(cand)
                    if isinstance(obj, dict):
                        return obj
                except json.JSONDecodeError:
                    pass
    raise ValueError(f"无法从 LLM 输出中提取有效 JSON: {text.strip()[:200]}")


def extract_json_array(text: str) -> list[dict] | None:
    """从 LLM 输出中提取 JSON 数组，容忍 code fence、夹带文字、嵌套对象、结尾逗号。

    尝试顺序：去围栏后直接解析 → 平衡扫描 [ ... ] → 逐个平衡扫描 { ... }。返回 None 表示无法提取。
    """
    candidates = _fence_candidates(text)

    # 尝试 1: 直接解析（list 直接返回；顶层是 dict 则视为“不是数组”返回 None）
    for t in candidates:
        for cand in (t, _strip_trailing_commas(t)):
            try:
                result = json.loads(cand)
                if isinstance(result, list):
                    return result
                if isinstance(result, dict):
                    return None
            except json.JSONDecodeError:
                pass

    # 尝试 2: 平衡扫描第一个 [ ... ]
    for t in candidates:
        block = _first_balanced(t, "[", "]")
        if block:
            for cand in (block, _strip_trailing_commas(block)):
                try:
                    result = json.loads(cand)
                    if isinstance(result, list):
                        return result
                except json.JSONDecodeError:
                    pass

    # 尝试 3: 从夹带文本中逐个抠 { ... } 对象（跳过坏块）
    for t in candidates:
        items: list[dict] = []
        for block in _iter_balanced(t, "{", "}"):
            for cand in (block, _strip_trailing_commas(block)):
                try:
                    items.append(json.loads(cand))
                    break
                except json.JSONDecodeError:
                    continue
        if items:
            return items

    return None
