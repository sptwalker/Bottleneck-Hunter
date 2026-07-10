"""输出格式校验（智能调度 Phase 2）。

只判「明显坏」，**宁漏勿误杀**（用户决策：先判格式，语义质量后置且在线不可判）：
- 空输出 → 坏
- 看起来是 JSON（以 {/[ 或 ```json 开头）但解析失败 → 坏（核心诉求：先判格式）
- 极短且以拒答话术开头 → 坏（三条底线之一，很保守）
- 其余（含中文自由分析）一律放行——绝不做语义正确性判定。

角色无关：靠内容形态自判，避免把 role 贯穿进 FallbackChatModel。
流式路径不校验（首 token 后无法安全换模型）。
全局 flag BH_SCHEDULER_VALIDATE=0 可一键关闭。
"""
from __future__ import annotations

import json
import os
import re

_REFUSAL_MARKERS = (
    "作为一个ai", "作为ai", "作为人工智能", "我无法提供", "我不能提供",
    "as an ai", "i cannot assist", "i'm unable to", "i am unable to",
)
_FENCE_RE = re.compile(r"^```(?:json|JSON)?\s*(.+?)\s*```$", re.S)


def validation_enabled() -> bool:
    return os.environ.get("BH_SCHEDULER_VALIDATE", "1") != "0"


def _strip_fence(t: str) -> str:
    m = _FENCE_RE.match(t)
    return m.group(1).strip() if m else t


def validate_output(message) -> tuple[bool, str]:
    """(ok, reason)。ok=False 表示输出格式明显坏、应触发换模型。"""
    if not validation_enabled():
        return True, ""
    text = getattr(message, "content", message)
    if not isinstance(text, str):
        return True, ""  # 工具调用/非文本消息不判
    t = text.strip()
    if not t:
        return False, "输出为空"
    body = _strip_fence(t)
    # 看起来是 JSON 但解析失败（剥 code fence 后）
    if body[:1] in ("{", "["):
        try:
            json.loads(body)
        except Exception:  # noqa: BLE001
            return False, "JSON格式损坏"
    # 极短 + 拒答话术开头（很保守，避免误杀正常短答）
    low = t.lower()
    if len(t) < 120 and any(low.startswith(m) or m in low[:40] for m in _REFUSAL_MARKERS):
        return False, "疑似拒答"
    return True, ""


def _selfcheck() -> None:
    from types import SimpleNamespace as NS
    ok = lambda m: validate_output(NS(content=m))[0]  # noqa: E731
    assert ok('{"a": 1}')                      # 合法 JSON
    assert ok('```json\n{"a":1}\n```')          # fenced JSON
    assert ok('这是一段中文分析，认为该公司具备护城河。')  # 中文散文放行
    assert ok('7.5')                            # 短数值放行
    assert not ok('')                           # 空
    assert not ok('{"a": 1,,,')                 # 坏 JSON
    assert not ok('[不完整')                     # 看似数组但坏
    assert not ok('作为AI语言模型，我无法提供投资建议。')  # 拒答
    assert ok('作为行业龙头，该公司在光刻胶领域市占率第一，' * 3)  # 长文含"作为"不误杀
    print("validate selfcheck OK")


if __name__ == "__main__":
    _selfcheck()
