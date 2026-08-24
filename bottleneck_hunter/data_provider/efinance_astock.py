"""efinance（东方财富）A股增强数据适配层 —— 免费源，全用户统一接入、无需 Key。

本模块只做两件事，且严格分离以便离线自检：
1. **纯解析器** `parse_*(df) -> 规整结构`：输入 efinance 返回的 DataFrame，输出对齐既有落库形状的
   list/dict。不碰网络，可用合成 DataFrame 单测。
2. **网络封装** `fetch_*(code) -> 结构 | None`：`asyncio.to_thread` 包同步 efinance 调用，
   东财端点国内间歇不可达 → 一律 try/except 返回 None，绝不抛穿（靠上层 hub.track 记账/降级）。

A股代码提取复用全系统唯一入口 store_base.extract_astock_code。
efinance 返回列名（已核实 efinance 0.5 源码）：
- 十大流通股东 get_top10_stock_holder_info: 股票代码/更新日期/股东代码/股东名称/持股数/持股比例/增减/变动率
- 所属板块     get_belong_board:             股票名称/股票代码/板块代码/板块名称/板块涨幅
- 历史资金流   get_history_bill:             股票名称/股票代码/日期/主力净流入/.../收盘价/涨跌幅

自检：python -m bottleneck_hunter.data_provider.efinance_astock 跑 demo()（全合成，无网络/无 LLM）。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def _astock_code(ticker: str) -> str | None:
    """全系统唯一 A股 6 位代码提取器（600519.SH / SH600519 / 600519 皆可）。"""
    from bottleneck_hunter.watchlist.store_base import extract_astock_code
    return extract_astock_code(ticker)


def _to_float(val, scale: float = 1.0):
    """'6.783亿'/'54.00%'/'--'/数值 → float；不可解析返回 None。"""
    if val is None:
        return None
    s = str(val).strip().replace(",", "").replace("%", "")
    if not s or s in ("--", "-", "nan", "None"):
        return None
    mult = 1.0
    if s.endswith("亿"):
        mult, s = 1e8, s[:-1]
    elif s.endswith("万"):
        mult, s = 1e4, s[:-1]
    try:
        return round(float(s) * mult * scale, 6)
    except (ValueError, TypeError):
        return None


# ─────────────────────── 纯解析器（可离线单测）───────────────────────

def parse_holders(df) -> list[dict]:
    """十大流通股东 DataFrame → institutional_holders 落库形状（对齐 yfinance 13F 那套列）。

    映射：股东名称→holder_name，持股数→shares，持股比例→pct_held，更新日期→date。
    value（市值）东财不给 → 0.0。空/异常 → []。
    """
    if df is None or getattr(df, "empty", True):
        return []
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    out: list[dict] = []
    for _, row in df.iterrows():
        name = str(row.get("股东名称", "") or "").strip()
        if not name:
            continue
        shares = _to_float(row.get("持股数")) or 0.0
        pct = _to_float(row.get("持股比例")) or 0.0
        date = str(row.get("更新日期", "") or "").strip()[:10]
        out.append({
            "holder_name": name,
            "shares": int(shares),
            "value": 0.0,
            "pct_held": round(pct, 4),
            "date": date,
            "fetched_at": now_iso,
        })
    return out


def parse_belong_board(df) -> str | None:
    """所属板块 DataFrame → 首个「行业类」板块名称（东财官方分类，作 sector 事实校验）。

    东财把行业、概念、指数成分（HS300_/上证50_ 等带下划线）混在一起返回；
    取第一个不含 '_' 的板块名当行业标签，够用；找不到返回 None。
    """
    if df is None or getattr(df, "empty", True):
        return None
    for _, row in df.iterrows():
        name = str(row.get("板块名称", "") or "").strip()
        if name and "_" not in name:      # 排除 HS300_/上证50_ 这类指数成分标签
            return name
    return None


def parse_history_bill(df, days: int = 5) -> dict | None:
    """历史资金流 DataFrame → {"main_net_wan": 近N日主力净流入合计(万元), "days": N}。

    主力净流入列单位是元 → /1e4 转万元，对齐 smart_money 既有 fund_flow_net 口径(万元)。
    空/异常 → None。
    """
    if df is None or getattr(df, "empty", True):
        return None
    recent = df.tail(days)
    total = 0.0
    n = 0
    for _, row in recent.iterrows():
        v = _to_float(row.get("主力净流入"))
        if v is not None:
            total += v / 1e4     # 元 → 万元
            n += 1
    if n == 0:
        return None
    return {"main_net_wan": round(total, 2), "days": n}


# ─────────────────────── 网络封装（失败恒 None）───────────────────────

def _fetch_holders_sync(code: str) -> list[dict]:
    import efinance as ef
    try:
        df = ef.stock.get_top10_stock_holder_info(code, 1)   # 最近 1 期
        return parse_holders(df)
    except Exception as e:  # noqa: BLE001 - 东财端点国内间歇不可达，降级不抛穿
        logger.debug("efinance 十大股东获取失败 (%s): %s", code, e)
        return []


def _fetch_belong_board_sync(code: str) -> str | None:
    import efinance as ef
    try:
        return parse_belong_board(ef.stock.get_belong_board(code))
    except Exception as e:  # noqa: BLE001
        logger.debug("efinance 所属板块获取失败 (%s): %s", code, e)
        return None


def _fetch_history_bill_sync(code: str, days: int = 5) -> dict | None:
    import efinance as ef
    try:
        return parse_history_bill(ef.stock.get_history_bill(code), days)
    except Exception as e:  # noqa: BLE001
        logger.debug("efinance 历史资金流获取失败 (%s): %s", code, e)
        return None


async def fetch_astock_holders(ticker: str) -> list[dict]:
    """A股十大流通股东 → institutional_holders 形状 list；非A股/失败 → []。"""
    code = _astock_code(ticker)
    if not code:
        return []
    return await asyncio.to_thread(_fetch_holders_sync, code)


async def fetch_astock_belong_board(ticker: str) -> str | None:
    """A股所属东财行业板块名；非A股/失败 → None。"""
    code = _astock_code(ticker)
    if not code:
        return None
    return await asyncio.to_thread(_fetch_belong_board_sync, code)


async def fetch_astock_moneyflow(ticker: str, days: int = 5) -> dict | None:
    """A股近N日主力资金净流入(万元)；非A股/失败 → None。"""
    code = _astock_code(ticker)
    if not code:
        return None
    return await asyncio.to_thread(_fetch_history_bill_sync, code, days)


# ─────────────────────────── 自检 ───────────────────────────
def demo() -> None:
    """合成 DataFrame 断言解析器；断网路径断言优雅返回空。无需真实网络。"""
    import pandas as pd

    # 1) 十大股东解析
    hdf = pd.DataFrame([
        {"股票代码": "600519", "更新日期": "2021-03-31", "股东名称": "贵州茅台集团",
         "持股数": "6.783亿", "持股比例": "54.00%", "增减": "不变", "变动率": "--"},
        {"股票代码": "600519", "更新日期": "2021-03-31", "股东名称": "香港中央结算",
         "持股数": "1000万", "持股比例": "8.00%", "增减": "增持", "变动率": "1.2%"},
        {"股票代码": "600519", "更新日期": "2021-03-31", "股东名称": "",  # 空名跳过
         "持股数": "0", "持股比例": "0", "增减": "", "变动率": ""},
    ])
    holders = parse_holders(hdf)
    assert len(holders) == 2, holders
    assert holders[0]["holder_name"] == "贵州茅台集团"
    assert holders[0]["shares"] == 678300000, holders[0]["shares"]  # 6.783亿
    assert holders[0]["pct_held"] == 54.0, holders[0]["pct_held"]
    assert holders[1]["shares"] == 10000000, holders[1]["shares"]   # 1000万
    assert holders[0]["date"] == "2021-03-31"

    # 2) 所属板块：跳过带 _ 的指数成分，取第一个行业名
    bdf = pd.DataFrame([
        {"板块代码": "BK0500", "板块名称": "HS300_", "板块涨幅": 0.21},
        {"板块代码": "BK0477", "板块名称": "酿酒行业", "板块涨幅": 0.56},
    ])
    assert parse_belong_board(bdf) == "酿酒行业", parse_belong_board(bdf)

    # 3) 主力资金流：元 → 万元，近3日合计
    fdf = pd.DataFrame([
        {"日期": "2021-03-01", "主力净流入": -3670272.0},
        {"日期": "2021-03-02", "主力净流入": 5952143.0},
        {"日期": "2021-03-03", "主力净流入": 1461528000.0},
    ])
    mf = parse_history_bill(fdf, days=3)
    expected_wan = round((-3670272.0 + 5952143.0 + 1461528000.0) / 1e4, 2)
    assert mf["main_net_wan"] == expected_wan, (mf, expected_wan)
    assert mf["days"] == 3

    # 4) 空/None 优雅降级
    assert parse_holders(None) == [] and parse_holders(pd.DataFrame()) == []
    assert parse_belong_board(None) is None
    assert parse_history_bill(pd.DataFrame()) is None

    # 5) 非A股 ticker → 各 fetch 立即空（不碰网络）
    assert _astock_code("AAPL") is None

    print("efinance_astock demo OK: holders / board / moneyflow parse + graceful-empty all pass")


if __name__ == "__main__":
    demo()
