"""事实核对护栏 —— 宏观指数点位 / 最新股价的「标签锚定」实时核对 + 就地纠正 + 认证标记。

number_guard 只认 `$金额`/`数字%`/带单位裸数，**故意跳过裸数字**（日期/序号/页码），因此裸指数点位
`7674.37` 对它完全隐形；且其 1% 容差也放行 7674.37 vs 7641.16（误差 0.43%）。本模块补这一缺口：

设计四条（见 docs 计划）：
1. **只主攻裸指数点位**（股价的 `$` 令牌 number_guard 已覆盖，此处只做高置信 ticker+$ 补充）。
2. **紧容差** `_INDEX_TOL=0.0015`（0.15%）——抓抄写漂移、放盘中/四舍五入抖动；用户那例 0.43% 正好被抓。
3. **标签锚定提取**：绝不扫「所有裸数字」，只取紧跟在已知指数标签/被点名 ticker 之后小窗口内的数字。
   日期/页码/序号绝不会紧贴「标普500」，故永不入选——与 number_guard 零重叠、互补。
4. **库内优先→可疑（不符/过期）时数据源实时补拉一次**核验；发现有误 **就地改写为系统真值** + `⚠系统核实`，
   命中打 `✓`。核到的权威值并入 guard 白名单，避免 number_guard 二次误标。
"""
from __future__ import annotations

import asyncio
import contextlib
import re

from bottleneck_hunter.watchlist import macro_data
from bottleneck_hunter.watchlist.store_base import _today as _bj_today

# 指数键 → 文本同义词（供匹配）。**同一键内长词在前**，避免「标普」吃掉「标普500」里的 500。
# code/label 从 macro_data._INDEX_CODE_MAP 反查（不另立代码表，随其变更）。
_INDEX_ALIASES: dict[str, list[str]] = {
    "sp500": ["标普500", "标普指数", "标普", "S&P 500", "S&P500", "SPX"],
    "nasdaq": ["纳斯达克综指", "纳斯达克指数", "纳斯达克", "纳指", "NASDAQ", "IXIC"],
    "sse_index": ["上证综指", "上证指数", "上证", "SSE"],
    "csi300": ["沪深300", "沪深三百", "CSI 300", "CSI300"],
    "hsi": ["恒生指数", "恒指", "HSI"],
    "hstech": ["恒生科技指数", "恒生科技", "HSTECH"],
}

_INDEX_TOL = 0.0015  # 0.15% 指数核对容差（远紧于 number_guard 的 1%，抓 0.43% 级抄写漂移）
_NUM = r"-?\d[\d,]*(?:\.\d+)?"           # 裸数（可带千分位/小数/负号）
_GAP = r"[^\d\n]{0,12}?"                 # 标签与数字间的小窗口（吸收「收于/报/收盘：」，非贪婪）
_CORRECT = " ⚠系统核实"
_CERT = " ✓"


def _to_num(s: str) -> float | None:
    try:
        return float(s.replace(",", ""))
    except (ValueError, AttributeError):
        return None


def _fmt_num(v: float) -> str:
    s = f"{v:.2f}"
    return s.rstrip("0").rstrip(".") if "." in s else s


def _rel_diff(a: float, b: float) -> float:
    return abs(a - b) / max(abs(b), 1.0)


def _apply_edits(text: str, edits: list[tuple[int, int, str]]) -> str:
    """按 span 从后往前替换，避免位置漂移（同 number_guard.annotate_unverified 手法）。"""
    for start, end, repl in sorted(edits, key=lambda e: e[0], reverse=True):
        text = text[:start] + repl + text[end:]
    return text


def extract_index_mentions(text: str, keys: set[str]) -> list[tuple[str, str, str, int, int]]:
    """抽出 text 中「已知指数标签 + 紧邻数字」的引用，返回 [(key, alias, number_str, num_start, num_end)]。

    只在给定 keys（当前市场）内匹配；长别名优先且**整段占位**——短别名若落进长别名已占区间则跳过
    （「标普」不会把「标普500」里的 500 误当点位）。这是裸数字匹配安全的前提：非标签邻域的数字永不入选。
    """
    pairs = [(k, a) for k in keys for a in _INDEX_ALIASES.get(k, [])]
    pairs.sort(key=lambda ka: len(ka[1]), reverse=True)  # 长别名先占位
    claimed: list[tuple[int, int]] = []
    out: list[tuple[str, str, str, int, int]] = []
    for k, alias in pairs:
        pat = re.compile(re.escape(alias) + _GAP + "(" + _NUM + ")", re.IGNORECASE)
        for m in pat.finditer(text):
            ms, me = m.start(), m.end()
            if any(not (me <= cs or ms >= ce) for cs, ce in claimed):
                continue  # 与已占（更长别名）区间重叠 → 跳过
            claimed.append((ms, me))
            out.append((k, alias, m.group(1), m.start(1), m.end(1)))
    return out


async def _resolve_index(snaps: dict, key: str) -> tuple[float, str, str] | None:
    """权威值 = 库内最新快照（已按真实交易日 as_of 落库，即官方收盘）。返回 (值, 截至日期, 来源) 或 None。

    **不做「过期/不符→实时补拉」**：行情最新收盘几乎永远不是今日(周末/节假日/开盘前)，用「date<今日=过期」
    触发盘中实时补拉，会用移动的盘中价去否决正确的收盘价、造成假纠正并污染库(实测纳指正确收盘被改成盘中值)。
    库内快照本就是权威收盘(收盘归收盘由 macro_data 按 as_of 落库保证)。仅当**全无库内值**时兜底实时拉一次。
    """
    row = snaps.get(key) or {}
    v_snap = row.get("value")
    if v_snap is not None:
        return float(v_snap), (row.get("date") or "库内"), "库内快照"
    code = macro_data._INDEX_CODE_MAP.get(key, (None, None))[0]  # 无任何库内收盘 → 兜底实时补拉一次
    if code:
        try:
            live = await asyncio.to_thread(macro_data._fetch_yf_quote, code)
        except Exception:  # noqa: BLE001  补拉失败不崩
            live = None
        if live and live.get("value") is not None:
            return float(live["value"]), (live.get("as_of") or _bj_today()), "yfinance实时"
    return None


def _save_live(wl_store, key: str, value: float) -> None:
    with contextlib.suppress(Exception):  # 落库失败不影响核对结果
        wl_store.save_macro_snapshot(key, _bj_today(), value)


async def reconcile_indices(text: str, wl_store, *, market: str, certify: bool = True) -> tuple[str, list[dict]]:
    """核对并就地纠正 text 中的宏观指数点位。返回 (新文本, items)。

    只核当前市场自有指数键（守市场隔离，sp500 不串入 A股/港股）。逐条：无从核实→`？未核`（不动文本）；
    容差内→`✓认证`（数字后补 ✓，certify=False 时不补）；超容差→`⚠纠正`（改写为系统真值 + 标记）。
    """
    keys = set(macro_data.MARKET_INDEX_KEYS.get(market or "us_stock", ["sp500"]))
    mentions = extract_index_mentions(text, keys)
    if not mentions:
        return text, []
    try:
        snaps = {s["indicator"]: s for s in (wl_store.get_latest_macro_snapshots() or [])}
    except Exception:  # noqa: BLE001
        snaps = {}
    resolved: dict[str, tuple[float, str, str] | None] = {}
    items: list[dict] = []
    edits: list[tuple[int, int, str]] = []
    for key, _alias, num_str, ns, ne in mentions:
        llm_v = _to_num(num_str)
        if llm_v is None:
            continue
        if key not in resolved:
            resolved[key] = await _resolve_index(snaps, key)
            r = resolved[key]
            if r and r[2] == "yfinance实时":   # 补拉到的新值落库（每键一次）
                _save_live(wl_store, key, r[0])
        r = resolved[key]
        label = macro_data._INDEX_CODE_MAP.get(key, (None, key))[1]
        if not r:
            items.append({"label": label, "llm_value": num_str, "authoritative": None,
                          "verdict": "？未核", "as_of": "", "source": ""})
            continue
        auth, as_of, source = r
        if _rel_diff(llm_v, auth) <= _INDEX_TOL:
            items.append({"label": label, "llm_value": num_str, "authoritative": _fmt_num(auth),
                          "verdict": "✓认证", "as_of": as_of, "source": source})
            if certify:
                edits.append((ne, ne, _CERT))
        else:
            items.append({"label": label, "llm_value": num_str, "authoritative": _fmt_num(auth),
                          "verdict": "⚠纠正", "as_of": as_of, "source": source})
            edits.append((ns, ne, _fmt_num(auth) + _CORRECT))
    return _apply_edits(text, edits), items


def reconcile_prices(text: str, quotes: list[dict], *, certify: bool = True) -> tuple[str, list[dict]]:
    """高置信股价核对：ticker + 紧邻 `$金额` 才处理（美元现价，来自已取的 live quotes，零额外网络）。

    要求「$」在窗口内 —— 避免「NVDA 2026年」把年份误当价格。裸价/外币价不在此处理（number_guard 覆盖 $ 令牌）。
    ponytail: 只做 ticker+$ 高置信路径；裸价核对需价格线索词判定，留待有需求再加。
    """
    usd = {q["ticker"].upper(): q for q in (quotes or [])
           if q.get("price") and str(q.get("currency", "")).strip().lower() in ("", "usd", "us$", "$", "美元")}
    if not usd or not text:
        return text, []
    items: list[dict] = []
    edits: list[tuple[int, int, str]] = []
    claimed: list[tuple[int, int]] = []
    for tk, q in usd.items():
        auth = float(q["price"])
        pat = re.compile(r"\b" + re.escape(tk) + r"\b" + r"[^\d\n]{0,10}?[\$＄]\s?(" + _NUM + ")", re.IGNORECASE)
        for m in pat.finditer(text):
            ms, me, ns, ne = m.start(), m.end(), m.start(1), m.end(1)
            if any(not (me <= cs or ms >= ce) for cs, ce in claimed):
                continue
            claimed.append((ms, me))
            llm_v = _to_num(m.group(1))
            if llm_v is None:
                continue
            if _rel_diff(llm_v, auth) <= _INDEX_TOL:
                items.append({"label": tk, "llm_value": m.group(1), "authoritative": _fmt_num(auth),
                              "verdict": "✓认证", "as_of": "现价", "source": "实时行情"})
                if certify:
                    edits.append((ne, ne, _CERT))
            else:
                items.append({"label": tk, "llm_value": m.group(1), "authoritative": _fmt_num(auth),
                              "verdict": "⚠纠正", "as_of": "现价", "source": "实时行情"})
                edits.append((ns, ne, _fmt_num(auth) + _CORRECT))
    return _apply_edits(text, edits), items


async def reconcile(text: str, wl_store, *, market: str, quotes: list[dict] | None = None) -> tuple[str, dict]:
    """指数 + 股价一并核对，返回 (新文本, certification)。

    certification = {items:[{label,llm_value,authoritative,verdict,as_of,source}], corrected, certified, unresolved}。
    """
    text, idx_items = await reconcile_indices(text, wl_store, market=market)
    text, px_items = reconcile_prices(text, quotes or [])
    items = idx_items + px_items
    cert = {
        "items": items,
        "corrected": sum(1 for it in items if it["verdict"] == "⚠纠正"),
        "certified": sum(1 for it in items if it["verdict"] == "✓认证"),
        "unresolved": sum(1 for it in items if it["verdict"] == "？未核"),
    }
    return text, cert


def corpus_line(cert: dict) -> str:
    """把已核实到的权威值拼成一行并入 guard 白名单，避免 number_guard 二次误标为「未核到」。"""
    vals = [f"{it['label']} {it['authoritative']}" for it in (cert.get("items") or []) if it.get("authoritative")]
    return ("【系统核实数值】" + "；".join(vals)) if vals else ""


if __name__ == "__main__":  # assert 自检（fake store + monkeypatch 补拉，GBK 控制台可直接跑）
    from bottleneck_hunter.vip import number_guard

    class _Store:
        def __init__(self, snaps=None):
            self._snaps = snaps or []
            self.saved = []

        def get_latest_macro_snapshots(self):
            return self._snaps

        def save_macro_snapshot(self, indicator, date, value, fetched_at=None, change_pct=0.0):
            self.saved.append((indicator, value))

    def _set_live(fn):
        macro_data._fetch_yf_quote = fn  # 直接改模块属性（reconcile 走 macro_data._fetch_yf_quote）

    # 1. 提取防火墙：只命中标签邻域数字，日期/页码不入选
    ms = extract_index_mentions("标普500收于7674.37，成交日30JUN26页3", {"sp500", "nasdaq"})
    assert len(ms) == 1 and ms[0][2] == "7674.37", ms
    assert all(m[2] not in ("30", "3", "26") for m in ms), ms

    # 2. 就地纠正：库内 7641.16(权威收盘)、LLM 说 7674.37（超容差）→ 改写为库内真值（不补拉）
    _set_live(lambda code: {"value": 99999.0})   # 若误触发补拉必被 99999 污染 → 断言即失败
    st = _Store([{"indicator": "sp500", "value": 7641.16, "date": _bj_today()}])
    txt, cert = asyncio.run(reconcile("大盘：标普500收于7674.37，走强。", st, market="us_stock"))
    assert "7641.16 ⚠系统核实" in txt and "7674.37" not in txt and "99999" not in txt, txt
    assert cert["items"][0]["verdict"] == "⚠纠正" and cert["corrected"] == 1, cert

    # 3. 容差内 → ✓认证、不改写数字（仅补 ✓）
    st = _Store([{"indicator": "sp500", "value": 7641.16, "date": _bj_today()}])
    txt, cert = asyncio.run(reconcile("标普500约 7641.5。", st, market="us_stock"))
    assert "7641.5 ✓" in txt and cert["items"][0]["verdict"] == "✓认证", (txt, cert)

    # 4. 无从核实：无库内 + 补拉失败 → ？未核、文本不动
    _set_live(lambda code: None)
    st = _Store([])
    txt, cert = asyncio.run(reconcile("标普500 7674.37。", st, market="us_stock"))
    assert txt == "标普500 7674.37。" and cert["items"][0]["verdict"] == "？未核", (txt, cert)

    # 5. 兜底补拉（**全无库内值**才触发）→ 实时值权威 + 落库
    _set_live(lambda code: {"value": 7641.16, "change_pct": 0.4, "as_of": "2026-08-21"})
    st = _Store([])   # 全无库内
    txt, cert = asyncio.run(reconcile("标普500 7000。", st, market="us_stock"))
    assert "7641.16 ⚠系统核实" in txt, txt
    assert ("sp500", 7641.16) in st.saved, st.saved            # 补拉值已落库
    assert cert["items"][0]["source"] == "yfinance实时", cert

    # 5b. 回归护栏：库内快照 date<今日(周末/节假日常态)、分析师引用与之相符 → ✓认证，
    #     绝不因『过期』触发补拉、绝不用盘中价假纠正正确收盘（此前 bug：纳指正确收盘被改成盘中值）。
    _set_live(lambda code: {"value": 99999.0})
    st = _Store([{"indicator": "sp500", "value": 7641.16, "date": "2020-01-01"}])
    txt, cert = asyncio.run(reconcile("标普500 7641.16。", st, market="us_stock"))
    assert "7641.16 ✓" in txt and "99999" not in txt, txt
    assert cert["items"][0]["verdict"] == "✓认证" and not st.saved, (cert, st.saved)

    # 6. 市场隔离：a_stock 下不核 sp500 提及
    txt, cert = asyncio.run(reconcile("标普500 7674.37。", _Store([]), market="a_stock"))
    assert txt == "标普500 7674.37。" and cert["items"] == [], (txt, cert)

    # 7. 承重容差不变量：0.43% 被 _INDEX_TOL 抓、被 number_guard 放
    err = abs(7674.37 - 7641.16) / 7641.16
    assert _INDEX_TOL < err < number_guard._REL_TOL, err

    # 8. 股价高置信核对：ticker + $ 不符 → 改写为真值；相符 → ✓
    q = [{"ticker": "NVDA", "price": 175.2, "currency": "USD"}]
    txt, its = reconcile_prices("NVDA 现价 $180.5，偏高。", q)
    assert "$175.2 ⚠系统核实" in txt and its[0]["verdict"] == "⚠纠正", (txt, its)
    txt2, its2 = reconcile_prices("NVDA 报 $175.3。", q)
    assert "$175.3 ✓" in txt2 and its2[0]["verdict"] == "✓认证", (txt2, its2)

    # 9. corpus_line：权威值并入白名单文本
    _, cert = asyncio.run(reconcile("标普500 7674.37。", _Store(
        [{"indicator": "sp500", "value": 7641.16, "date": _bj_today()}]), market="us_stock"))
    assert "7641.16" in corpus_line(cert), cert

    print("fact_check 自检通过")
