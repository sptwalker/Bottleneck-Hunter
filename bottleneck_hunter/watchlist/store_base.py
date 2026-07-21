"""WatchlistStore 的模块级底层 helper（从 store.py 抽出，供 store.py 与各 mixin 共享）。"""

from __future__ import annotations

import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


_DEFAULT_DB = Path(__file__).resolve().parents[2] / "data" / "watchlist.db"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _today() -> str:
    # "今天"按北京时间取（A股交易日/用户视角），避免 UTC 在北京凌晨算成昨天
    return datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")


# 规范市场枚举。历史数据曾出现裸 "us"，与 "us_stock" 无法跨表关联（导致 composite_score=0、
# scenario_valuations 写入静默失败）。所有写入路径统一经此归一化。
# 注意：A股 canonical 是 "a_stock"（全系统 MarketRegion.A_STOCK / scheduler cn_* job / fetcher 均用它），
# 故 "a"/"cn" 必须归一到 "a_stock"，不能是 "cn_stock"（后者无任何消费方，会把 A股 entry 孤立于所有 A股逻辑）。
_MARKET_ALIASES = {"us": "us_stock", "usa": "us_stock", "cn": "a_stock",
                   "a": "a_stock", "hk": "hk_stock"}


def normalize_market(market: str | None) -> str:
    m = (market or "us_stock").strip().lower()
    return _MARKET_ALIASES.get(m, m)


# A股 ticker 全系统唯一 canonical：6位裸码 + 交易所后缀（.SS 上交所 / .SZ 深交所 / .BJ 北交所），
# 与唯一 producer supplier_search._code_to_ticker 一致。历史上 LLM 自由发挥用 .SH → 与观察池存的
# .SS 精确匹配失败，导致 L2/L3/L4 连接漏配、场景估值跳过、持仓重复/误报持仓不足、自进化反馈丢失。
# 所有 ticker 写入/比较入口统一经此归一，杜绝跨来源后缀不一致。美股(字母 ticker)只做 upper。
# 提取正则容纳全部输入形态：600519 / 600519.SH/.SS/.SZ / SH600519 / SH.600519（大小写不敏感）。
_ASTOCK_CODE_RE = re.compile(r"^\s*(?:(?:SH|SS|SZ|BJ)\.?)?(\d{6})(?:\.(?:SH|SS|SZ|BJ))?\s*$", re.IGNORECASE)


def extract_astock_code(ticker: str | None) -> str | None:
    """从任意 A股 ticker 形态提取 6 位纯数字代码；非 A股(美股字母 ticker 等) → None。
    全系统唯一的 A股代码提取器（此前分散在 ~7 处重复实现，且部分不认 SH600519 前缀形态）。"""
    if not ticker:
        return None
    m = _ASTOCK_CODE_RE.match(str(ticker).strip())
    return m.group(1) if m else None


def _astock_suffix(code: str) -> str:
    """6位码 → 交易所后缀。6/9→上交所(.SS)，0/2/3→深交所(.SZ)，4/8→北交所(.BJ)。"""
    c0 = code[0]
    if c0 in ("6", "9"):
        return ".SS"
    if c0 in ("4", "8"):
        return ".BJ"
    return ".SZ"  # 0/2/3 及其它默认深市


def normalize_ticker(ticker: str | None, market: str = "") -> str:
    """统一 ticker 规范形。A股 → 6位码 + .SS/.SZ/.BJ（.SH 归一为 .SS，裸码按首位补后缀）；
    美股/其它 → strip+upper。空/无法解析 → 原样(strip)返回。幂等。"""
    if not ticker:
        return ticker or ""
    t = str(ticker).strip()
    if not t:
        return t
    code = extract_astock_code(t)   # A股：6位码 + canonical 后缀
    if code:
        return code + _astock_suffix(code)
    # 非 A股（美股字母 ticker 等）：仅规范大小写
    return t.upper()


# 美股代码：1-5 位字母/数字根 + 可选 .X/-X 类别股后缀（如 BRK.B、BRK-B）。
# 覆盖 AAPL/GOOGL/TSM/SPCX 等；拦住把公司名当代码存进来（如 "SPACEX" 6 字母无分隔）。
_US_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9]{0,4}([.\-][A-Z]{1,2})?$")


def validate_ticker(ticker: str, market: str = "") -> None:
    """校验（已 normalize 的）ticker 是否像该市场的合法代码，不合法抛 ValueError。

    只在写入口（store add）调用，不拦读——避免历史脏数据被读操作误伤。
    仅强校验 us_stock：公司名（如 "SPACEX"）会被拒，根因见 SPACEX/SPCX 事故——
    名字当代码存进去，yfinance 永远解析不到。A股走 6 位数字码、由列表选取，不在此拦
    （容量测试等会用非 6 位假码）；其它市场亦不强校验。

    诚实边界：纯格式只拦得住明显不像代码的（6+ 字母、含空格）；恰好 5 字母的名字
    （如 "TESLA"）与合法代码（"GOOGL"）无法区分，拦不住。
    """
    t = (ticker or "").strip()
    if not t:
        raise ValueError("代码不能为空")
    mkt = normalize_market(market) if market else ""
    if mkt == "us_stock" and not _US_TICKER_RE.match(t):
        raise ValueError(
            f"'{t}' 不像美股代码——请填交易所代码（如 SPCX），而非公司名（如 SPACEX）"
        )


_DB_LOCKS: dict[str, threading.Lock] = {}


_DB_LOCKS_GUARD = threading.Lock()


def _get_db_lock(db_path: str) -> threading.Lock:
    with _DB_LOCKS_GUARD:
        if db_path not in _DB_LOCKS:
            _DB_LOCKS[db_path] = threading.Lock()
        return _DB_LOCKS[db_path]
