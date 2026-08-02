"""特性二 P1 · 对话内实时行情立查（路线 A：确定性预取，零管线改造）。

在 stream_vip_chat 生成 facts 后、prompt.format 前插一段现查：抽持仓 symbol → hub.fetch(CAP_QUOTE)
→ 实时值同时并进 facts 文本与 guard 语料（美元现价进美元池、外币现价进 foreign_values 池），
LLM 单趟原样回答。绝不多轮 tool-loop、不写库、不下单——只在用户问答时触发的只读查询。

诚实边界（与 projection.py 一致）：
- 只收 US 形态 ticker（_US_TICKER_RE）；港股数字码/欧洲 ISIN 无稳定 yfinance 映射 → skipped 留空。
- 失败/无映射票列 skipped，绝不塞 0 冒充报价。
- 现查上限 _LIVE_CAP + 整体超时防打爆额度。
- A股 6 位码本期不查（形态另需正则、市场按 us_stock 分流），留 carry-forward。
"""
from __future__ import annotations

import asyncio
import re

from bottleneck_hunter.data_provider.hub import CAP_QUOTE, get_hub
from bottleneck_hunter.vip.projection import _US_TICKER_RE
from bottleneck_hunter.watchlist.store_base import normalize_ticker

# ponytail: 限流是成本旋钮，物理世界需 tuning——单次问答现查票数上限 + 整体超时秒数。
_LIVE_CAP = 8
_LIVE_TIMEOUT = 12.0
_USD_CCY = {"", "usd", "us$", "$", "美元"}
# 裸代码抽取：question 里出现的疑似美股代码（1-6 位大写字母，可含点）。只与持仓取交集，不做并集扩展。
_MENTION_RE = re.compile(r"\b[A-Z]{1,6}(?:\.[A-Z]{1,2})?\b")


def _pick_live_symbols(question: str, holdings: list[dict]) -> list[str]:
    """决定哪些标的现查：持仓中 US 形态可查的票 ∩（用户显式提及 or 未提及则全部）。

    holdings: dossier.holdings（每项含 'ticker' + 'currency'）。返回带原币种标注的 symbol 列表
    上限 _LIVE_CAP。用户随口提的任意代码只与持仓取交集，防无谓外部查询打爆额度（成本死线）。
    """
    priceable = {}
    for h in holdings:
        t = (h.get("ticker") or "").strip().upper()
        if t and _US_TICKER_RE.match(t):
            priceable[t] = (h.get("currency") or "").strip()
    mentioned = set(_MENTION_RE.findall((question or "").upper()))
    hits = [t for t in priceable if t in mentioned]
    syms = hits or list(priceable)   # 提及某些票→只查这些；泛问("我的持仓现在多少")→查全部持仓
    return [(s, priceable[s]) for s in syms[:_LIVE_CAP]]


async def fetch_live_quotes(question: str, holdings: list[dict], *,
                            market: str = "us_stock", user_id: str = "") -> dict:
    """现查选中标的的实时报价。返回 {usd_text, foreign_prices, quotes, skipped}。

    - usd_text: 美元口径现价文本块（并入 facts + guard 美元池）。
    - foreign_prices: 非美元现价数值列表（并入 number_guard foreign_values，防误核美元断言）。
    - skipped: 无映射/失败票（诚实呈现"无实时映射"，不塞 0）。
    同步纯编排，逐票 hub.fetch + 整体超时 + 容错。market != us_stock 直接空返回（本期只做美股）。
    """
    out = {"usd_text": "", "foreign_prices": [], "quotes": [], "skipped": []}
    if market != "us_stock":
        return out
    picks = _pick_live_symbols(question, holdings)
    if not picks:
        return out

    async def _one(sym: str):
        try:
            return sym, await get_hub().fetch(CAP_QUOTE, normalize_ticker(sym, market), market, user_id)
        except Exception:  # noqa: BLE001  取数失败不塞 0，落 skipped
            return sym, None

    try:
        results = await asyncio.wait_for(
            asyncio.gather(*[_one(s) for s, _ in picks]), timeout=_LIVE_TIMEOUT)
    except asyncio.TimeoutError:
        out["skipped"] = [s for s, _ in picks]
        return out

    ccy_of = dict(picks)
    lines = []
    for sym, q in results:
        price = (q or {}).get("price")
        if not q or price in (None, 0):
            out["skipped"].append(sym)
            continue
        chg = q.get("change_pct", 0.0) or 0.0
        ccy = (ccy_of.get(sym) or "").strip()
        out["quotes"].append({"ticker": sym, "price": price, "change_pct": chg, "currency": ccy})
        if ccy.lower() in _USD_CCY:
            lines.append(f"- {sym}：现价 ${price}（涨跌 {chg:+.2f}%）")
        else:   # 外币现价：数值进 foreign_prices 防误核美元断言，文本明示原币种
            out["foreign_prices"].append(float(price))
            lines.append(f"- {sym}：现价 {ccy} {price}（涨跌 {chg:+.2f}%，原币口径）")
    if lines:
        out["usd_text"] = "【实时行情（现查，近实时，源为免费行情通道）】\n" + "\n".join(lines)
    return out


if __name__ == "__main__":
    # fake hub 注入：AAPL 有价、0700.HK 形态过滤不进 fetch、缺映射票→None
    import bottleneck_hunter.data_provider.hub as hubmod

    class _FakeHub:
        async def fetch(self, cap, ticker, market, user_id=""):
            return {"price": 250.0, "change_pct": 1.5} if ticker == "AAPL" else None
    hubmod._hub = _FakeHub()

    hold = [{"ticker": "AAPL", "currency": "USD"},
            {"ticker": "0700.HK", "currency": "HKD"},   # 数字打头，_US_TICKER_RE 不匹配
            {"ticker": "MISS", "currency": "USD"}]       # 形态可查但 fetch 返 None
    # 港股票不进 picks（形态过滤）
    picks = dict(_pick_live_symbols("我的持仓现在多少钱", hold))
    assert "0700.HK" not in picks and "AAPL" in picks and "MISS" in picks

    r = asyncio.run(fetch_live_quotes("我的持仓现在多少钱", hold, user_id="u1"))
    prices = {q["ticker"]: q["price"] for q in r["quotes"]}
    assert prices == {"AAPL": 250.0}, prices          # 只有 AAPL 拿到价
    assert "MISS" in r["skipped"]                       # 失败票落 skipped，不塞 0
    assert "AAPL" in r["usd_text"] and "250" in r["usd_text"]
    assert r["foreign_prices"] == []                   # 无外币现价（港股没进 fetch）

    # 显式提及只查该票（与持仓取交集，随口代码 TSLA 不在持仓→不查）
    picks2 = dict(_pick_live_symbols("AAPL 现在多少？TSLA 呢", hold))
    assert set(picks2) == {"AAPL"}, picks2

    # 外币现价走 foreign_prices（构造持仓币种 HKD 的美股形态票，验证分列）
    hold_fx = [{"ticker": "ABC", "currency": "HKD"}]
    class _FxHub:
        async def fetch(self, cap, ticker, market, user_id=""):
            return {"price": 12.3, "change_pct": 0.0}
    hubmod._hub = _FxHub()
    rf = asyncio.run(fetch_live_quotes("现价", hold_fx, user_id="u1"))
    assert rf["foreign_prices"] == [12.3] and "原币" in rf["usd_text"]

    print("live_quote self-check OK")
