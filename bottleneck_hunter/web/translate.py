"""按需翻译服务：缓存优先，未命中批量走用户配置的 LLM，结果回缓存（全局复用）。

用于新闻中英对照等。失败优雅降级（返回已命中缓存的，缺失原样留空由前端显示原文）。
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

_store = None


def set_store(store) -> None:
    global _store
    _store = store


async def _llm_translate(texts: list[str], target: str) -> dict[str, str]:
    """一次 LLM 调用批量翻译，index 对齐返回 {源文: 译文}。失败返回空。"""
    from bottleneck_hunter.llm_clients.factory import get_llm_for_position
    llm, _provider, _model = get_llm_for_position()  # 用户当前配置的默认模型（按 current_user 解析）
    if llm is None:
        return {}
    lang = "简体中文" if target == "zh" else "English"
    # 用编号 + 明确 JSON 数组要求；解析侧做多重容错（对象数组/数量不符/含 fence 均能降级提取）
    numbered = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(texts))
    prompt = (f"把下列每条文本翻译成{lang}，只输出译文本身、不要解释或编号。"
              f"严格返回一个 JSON 字符串数组（每个元素是纯译文字符串，不是对象），"
              f"顺序与输入一一对应、数量必须相同，形如 [\"译文1\",\"译文2\"]。\n\n{numbered}")
    try:
        from langchain_core.messages import HumanMessage
        resp = await llm.ainvoke([HumanMessage(content=prompt)])
        text = (resp.content if hasattr(resp, "content") else str(resp)).strip()
        arr = _extract_json_array(text)
        if isinstance(arr, list) and len(arr) == len(texts):
            out = {}
            for src, tr in zip(texts, arr, strict=False):
                s = _coerce_translation(tr)   # 只接受字符串/取对象里的译文字段，杜绝缓存 dict repr
                if s and s.strip():
                    out[src] = s.strip()
            return out
        logger.warning("翻译返回数量不匹配: 期望 %d 得 %d", len(texts), len(arr) if isinstance(arr, list) else -1)
    except Exception as e:  # noqa: BLE001
        logger.warning("LLM 翻译失败: %s", e)
    return {}


def _extract_json_array(text: str):
    """从 LLM 输出提取 JSON 数组：去 ``` 围栏后，取第一个 '[' 到最后一个 ']'（容忍前后杂字/内部含反引号）。"""
    t = text.strip()
    if t.startswith("```"):
        # 去掉首个围栏行（```/```json）与结尾围栏，不用 split 以免正文含 ``` 被截断
        nl = t.find("\n")
        if nl != -1:
            t = t[nl + 1:]
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    i, j = t.find("["), t.rfind("]")
    if i != -1 and j != -1 and j > i:
        t = t[i:j + 1]
    try:
        return json.loads(t)
    except Exception:  # noqa: BLE001
        return None


def _coerce_translation(tr) -> str:
    """把单个数组元素规约成译文字符串：字符串直用；对象取常见译文字段；其它→空(丢弃，不缓存 repr)。"""
    if isinstance(tr, str):
        return tr
    if isinstance(tr, dict):
        for k in ("translation", "translated", "text", "zh", "en", "value"):
            v = tr.get(k)
            if isinstance(v, str) and v.strip():
                return v
    return ""


async def translate_texts(texts: list[str], target: str = "zh") -> dict[str, str]:
    """返回 {源文: 译文}。缓存命中直接用；未命中批量翻译并回缓存。store 未初始化则仅尝试 LLM。"""
    uniq = list(dict.fromkeys(t for t in texts if t and t.strip()))
    if not uniq:
        return {}
    cached = _store.get_cached_translations(uniq, target) if _store else {}
    missing = [t for t in uniq if t not in cached]
    if missing:
        fresh = await _llm_translate(missing, target)
        if fresh and _store:
            try:
                _store.save_translations(fresh, target)
            except Exception as e:  # noqa: BLE001
                logger.debug("翻译缓存写入失败: %s", e)
        cached.update(fresh)
    return cached
