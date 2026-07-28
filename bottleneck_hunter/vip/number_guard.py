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
_USD_CCY = {"", "usd", "us$", "$", "＄", "美元"}  # 视为美元口径（或未知→按美元处理，留在可信池）


def foreign_derivative_values(dossier) -> list[float]:
    """从账户档案的衍生品敞口里抽出「非美元」条款价格（afp/knock_out_price）。

    这些是标的原生币种（如 HKD）的每股价格，与叙述的统一美元口径不同币种；档案把币种
    (`currency`) 与价格放在**不同字段**，扁平化后防伪器看不到二者相邻，故须在这里按结构取出。
    传给 verify_numbers 后，$（美元）令牌不再被这些外币数字误核——否则 HK$3.45 的敲出价会
    "核实"掉一个凭空捏造的 $3.45 美元断言（跨币纯数值容差误判）。
    """
    out: list[float] = []
    if not isinstance(dossier, dict):
        return out
    for d in (dossier.get("derivative_exposure") or []):
        if str(d.get("currency", "")).strip().lower() in _USD_CCY:
            continue  # 美元/未知币种条款价格是美元口径，保留在可信池
        for k in ("afp", "knock_out_price"):
            v = d.get(k)
            try:
                if v is not None:
                    out.append(float(v))
            except (TypeError, ValueError):
                pass
    return out


def _num_forms(v: float) -> set[str]:
    """一个外币数值在扁平化 JSON facts 里可能出现的文本形态（供剔除用）。"""
    forms = {repr(v), f"{v}"}
    if v == int(v):
        forms.add(str(int(v)))
        forms.add(f"{int(v)}.0")
    return forms


def _approx_in(val: float, pool: list[float]) -> bool:
    return any(abs(val - f) / max(abs(f), 1.0) <= _REL_TOL for f in pool)


def _strip_values(text: str, values: list[float]) -> str:
    """把外币数值的所有文本形态从 facts 文本中抹除（数字/点边界防误删 13.45 里的 3.45）。"""
    for v in values:
        for form in _num_forms(v):
            text = re.sub(r"(?<![\d.])" + re.escape(form) + r"(?![\d.])", " ", text)
    return text


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


def verify_numbers(text: str, facts, foreign_values: list[float] | None = None) -> list[dict]:
    """抽出 text 中的金额/百分比 token，逐个在 facts 中核对。

    Returns: [{"token": str, "value": float|None, "status": "verified"|"unverified"}]
    facts 可为 str / dict / list（非 str 自动 JSON 序列化后匹配）。
    foreign_values：非美元口径的 facts 数值（如 HKD 衍生品条款价，见 foreign_derivative_values）。
      $（美元）令牌**不得**用这些外币数字核实——避免 HK$ 数量级的捏造美元断言被跨币容差放行。
      非 $ 令牌（%/股数/净值）不受影响，仍用全量 facts。
    """
    if not text:
        return []
    fx = _facts_text(facts)
    fx_nocomma = fx.replace(",", "")
    fact_nums = [_to_float(m.group(0)) for m in _NUM_RE.finditer(fx)]
    fact_nums = [n for n in fact_nums if n is not None]
    # 美元口径可信池 = 全量 facts 剔除外币数值（子串通道用抹除后的文本，容差通道用剔除后的数字池）
    fv = [f for f in (foreign_values or []) if f is not None]
    usd_fx_nocomma = _strip_values(fx_nocomma, fv).replace(",", "") if fv else fx_nocomma
    usd_fact_nums = [n for n in fact_nums if not _approx_in(n, fv)] if fv else fact_nums

    out: list[dict] = []
    for m in _TOKEN_RE.finditer(text):
        tok = m.group(0)
        v = _to_float(tok)
        status = "unverified"
        is_usd = tok[0] in "$＄"  # 美元令牌走剔除外币后的可信池
        nocomma = usd_fx_nocomma if is_usd else fx_nocomma
        pool = usd_fact_nums if is_usd else fact_nums
        # 通道1：数字串（去单位/符号/逗号）原样出现在 facts
        v_str = "" if v is None else (repr(v) if v != int(v) else str(int(v)))
        digits = v_str.lstrip("-")
        if digits and digits in nocomma:
            status = "verified"
        # 通道2：数值 1% 相对容差匹配任一 facts 数字
        elif v is not None:
            for f in pool:
                denom = max(abs(f), 1.0)
                if abs(v - f) / denom <= _REL_TOL:
                    status = "verified"
                    break
        out.append({"token": tok, "value": v, "status": status})
    return out


def annotate_unverified(text: str, facts, marker: str = " ⚠未核到",
                        foreign_values: list[float] | None = None) -> str:
    """把 text 中未核到的金额/百分比就地追加标记，供报告/聊天渲染层直接用。"""
    results = {r["token"]: r["status"] for r in verify_numbers(text, facts, foreign_values)}
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

    # 跨币防误核：HKD 衍生品条款价（afp 3.45 / KO 500）不得核验叙述里的美元 $3.45 / $500
    dossier = {"total_equity": 1205022.5, "derivative_exposure": [
        {"currency": "HKD", "afp": 3.45, "knock_out_price": 500.0},
        {"currency": "USD", "afp": 7.89}]}  # USD 条款价留在可信池
    fv = foreign_derivative_values(dossier)
    assert set(fv) == {3.45, 500.0}, fv                      # 仅取非美元条款价
    fdx = _facts_text(dossier)
    # 无 foreign_values → 旧行为：$3.45 被 HKD afp 误核为 verified
    assert verify_numbers("每股 $3.45", fdx)[0]["status"] == "verified"
    # 带 foreign_values → $3.45 / $500（美元断言）被拦为 unverified（子串 + 容差两通道都堵）
    assert verify_numbers("每股 $3.45", fdx, fv)[0]["status"] == "unverified"
    assert verify_numbers("敲出 $500", fdx, fv)[0]["status"] == "unverified"
    # 真实美元总权益仍可核；USD 币种条款价 $7.89 不误伤（未进 foreign）
    assert verify_numbers("总权益 $1,205,022.50", fdx, fv)[0]["status"] == "verified"
    assert verify_numbers("每股 $7.89", fdx, fv)[0]["status"] == "verified"
    # 非 $ 令牌（%/裸数）不受 foreign_values 影响：500 股仍按全池核（此处无 500 股事实→unverified 属正常）
    assert verify_numbers("占比 3.45%", fdx, fv)[0]["status"] == "verified"  # 3.45 在 facts 里，% 走全池
    print("number_guard 自检通过")


if __name__ == "__main__":
    demo()
