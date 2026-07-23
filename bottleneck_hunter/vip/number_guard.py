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

# 校验对象三类，其余裸数字（日期/序号/页码）一律不校：
#   1) $金额（可带负号/千分位/小数）  2) 数字%  3) 带单位的裸数（N 股 / N contracts / 净值 N 等）
# 带单位裸数：数字后紧跟单位词，或"净值/单价/成本/市值 + 数字"。日期(30JUN26)/纯序号不带这些单位，天然排除。
_UNIT_AFTER = r"(?:股|份|手|张|contracts?|shares?|lots?|units?)"
_UNIT_BEFORE = r"(?:净值|单价|成本|市值|价格|price|nav|cost)"
_TOKEN_RE = re.compile(
    r"(?:[\$＄]\s?-?\d[\d,]*(?:\.\d+)?)"                                  # $金额
    r"|(?:-?\d[\d,]*(?:\.\d+)?\s?%)"                                       # 数字%
    r"|(?:-?\d[\d,]*(?:\.\d+)?\s?" + _UNIT_AFTER + r")"                    # N 股/contracts…
    r"|(?:" + _UNIT_BEFORE + r"[:：\s]?\s?-?\d[\d,]*(?:\.\d+)?)",          # 净值/成本 N
    re.IGNORECASE,
)
_NUM_RE = re.compile(r"-?\d[\d,]*(?:\.\d+)?")
_REL_TOL = 0.01  # 1% 相对容差，吸收四舍五入


def _to_float(s: str) -> float | None:
    """从 token 里剥掉货币符/百分号/单位词/千分位，取出纯数值。"""
    s = re.sub(_UNIT_BEFORE, "", s, flags=re.IGNORECASE)
    s = re.sub(_UNIT_AFTER, "", s, flags=re.IGNORECASE)
    s = re.sub(r"[\$＄%,:：\s]", "", s).strip()
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
        # 通道1：数字串（去单位/符号/逗号）原样出现在 facts
        v_str = "" if v is None else (repr(v) if v != int(v) else str(int(v)))
        digits = v_str.lstrip("-")
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
    facts = "持仓 GOOGL 数量 1030 股，市值 $1,205,022.50，占比 60.86%，未实现盈亏 $656,223.00；期权 5 contracts"
    # 报告叙事：真实数字 + 一个编造金额
    text = "组合中 GOOGL 市值约 $1,205,022.50（占 60.86%），持 1030 股，另有一笔 $9,999,999.00 的臆造收益。"
    res = verify_numbers(text, facts)
    by = {r["token"]: r["status"] for r in res}
    assert by.get("$1,205,022.50") == "verified", res
    assert by.get("60.86%") == "verified", res
    assert by.get("1030 股") == "verified", res           # 带单位裸数命中
    assert by.get("$9,999,999.00") == "unverified", res
    # 四舍五入应放行（1% 容差）
    assert verify_numbers("市值 $1,205,000", facts)[0]["status"] == "verified"
    # 编造股数应被抓
    assert verify_numbers("持 8888 股", facts)[0]["status"] == "unverified"
    # 日期/序号不带单位 → 不被当作校验对象
    assert verify_numbers("成交日 30JUN26 页 3", facts) == []
    # 标注：只标未核到
    marked = annotate_unverified(text, facts)
    assert "$9,999,999.00 ⚠未核到" in marked
    assert "$1,205,022.50 ⚠未核到" not in marked
    assert "1030 股 ⚠未核到" not in marked
    print("number_guard 自检通过")


if __name__ == "__main__":
    demo()
