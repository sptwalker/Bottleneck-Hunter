"""数字幻觉防护（P0 公共件，裁决 C12 / H3 修正）。

LLM 生成的报告叙事与聊天回答里可能凭空编出金额/百分比。对账只保证**入库数据**可信，
不保证 LLM 不在自由文本里编新数字。故报告(P5)与聊天(P6)在渲染前统一过 `verify_numbers`：
逐个抽出 `$金额` / `数字%`，在可信 facts（materialize 后的持仓快照 / 聊天 facts block）里
核对——命中=verified，未命中=unverified（渲染层就地标注"⚠未核到"）。

设计取舍：宁可偶尔把"经四舍五入的真实数字"标为已核（放行），也要抓住"凭空捏造"的数字。
故采用 相对 1% 容差 + 去逗号子串 双通道匹配——捏造的大额数字极难恰好落进某真实值 1% 内。
"""
from __future__ import annotations

import re

# $金额（可带负号/千分位/小数）| 数字%
_TOKEN_RE = re.compile(r"(?:[\$＄]\s?-?\d[\d,]*(?:\.\d+)?)|(?:-?\d[\d,]*(?:\.\d+)?\s?%)")
_NUM_RE = re.compile(r"-?\d[\d,]*(?:\.\d+)?")
_REL_TOL = 0.01  # 1% 相对容差，吸收四舍五入


def _to_float(s: str) -> float | None:
    s = s.replace("$", "").replace("＄", "").replace("%", "").replace(",", "").replace(" ", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def _facts_text(facts) -> str:
    if isinstance(facts, str):
        return facts
    try:
        import json
        return json.dumps(facts, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        return str(facts)


def verify_numbers(text: str, facts) -> list[dict]:
    """抽出 text 中的金额/百分比 token，逐个在 facts 中核对。

    Returns: [{"token": str, "value": float|None, "status": "verified"|"unverified"}]
    facts 可为 str / dict / list（非 str 自动 JSON 序列化后匹配）。
    """
    if not text:
        return []
    fx = _facts_text(facts)
    fx_nocomma = fx.replace(",", "")
    fact_nums = [_to_float(m.group(0)) for m in _NUM_RE.finditer(fx)]
    fact_nums = [n for n in fact_nums if n is not None]

    out: list[dict] = []
    for m in _TOKEN_RE.finditer(text):
        tok = m.group(0)
        v = _to_float(tok)
        status = "unverified"
        # 通道1：去逗号子串直接命中（数字串原样出现在 facts）
        digits = tok.replace("$", "").replace("＄", "").replace("%", "").replace(",", "").replace(" ", "").strip().lstrip("-")
        if digits and digits in fx_nocomma:
            status = "verified"
        # 通道2：数值 1% 相对容差匹配任一 facts 数字
        elif v is not None:
            for f in fact_nums:
                denom = max(abs(f), 1.0)
                if abs(v - f) / denom <= _REL_TOL:
                    status = "verified"
                    break
        out.append({"token": tok, "value": v, "status": status})
    return out


def annotate_unverified(text: str, facts, marker: str = " ⚠未核到") -> str:
    """把 text 中未核到的金额/百分比就地追加标记，供报告/聊天渲染层直接用。"""
    results = {r["token"]: r["status"] for r in verify_numbers(text, facts)}
    # 从后往前替换，避免位置漂移；只标 unverified，且每处只标一次
    marked = text
    for m in reversed(list(_TOKEN_RE.finditer(text))):
        tok = m.group(0)
        if results.get(tok) == "unverified":
            marked = marked[:m.end()] + marker + marked[m.end():]
    return marked


def demo() -> None:
    facts = "持仓 GOOGL 市值 $1,205,022.50，占比 60.86%，未实现盈亏 $656,223.00"
    # 报告叙事：前两个数字真实，第三个是 LLM 编的
    text = "组合中 GOOGL 市值约 $1,205,022.50（占 60.86%），另有一笔 $9,999,999.00 的臆造收益。"
    res = verify_numbers(text, facts)
    by = {r["token"]: r["status"] for r in res}
    assert by.get("$1,205,022.50") == "verified", res
    assert by.get("60.86%") == "verified", res
    assert by.get("$9,999,999.00") == "unverified", res
    # 四舍五入应放行（1% 容差）
    assert verify_numbers("市值 $1,205,000", facts)[0]["status"] == "verified"
    # 标注
    marked = annotate_unverified(text, facts)
    assert "$9,999,999.00 ⚠未核到" in marked
    assert "$1,205,022.50 ⚠未核到" not in marked  # 真实数字不标
    print("number_guard 自检通过")


if __name__ == "__main__":
    demo()
