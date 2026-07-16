"""baostock 数据源包装 — A股日K线（前复权）。

baostock 免费、免 token，走自己的服务器（不依赖东方财富接口），作为 A股 K线的
高可靠兜底源：当 efinance/akshare（东方财富系）连不上时仍能取到历史行情。

注意：
- baostock 使用**全局会话**（login/logout），非线程安全 → 用模块级锁串行化。
- 只提供历史日频，无实时报价 → fetch_realtime 返回 None（实时交给 akshare/efinance/pytdx）。
"""

from __future__ import annotations

import asyncio
import logging
import threading
from datetime import datetime, timedelta

import pandas as pd

from bottleneck_hunter.data_provider.base import BaseFetcher, StandardQuote

logger = logging.getLogger(__name__)

# baostock 全局会话非线程安全：全进程串行化 login→query→logout
# ponytail: global lock，A股 K线非高频，够用；若吞吐成瓶颈再改连接池
_bs_lock = threading.Lock()


def _bs_code(ticker: str) -> str | None:
    """6位代码 → baostock 格式 sh.600xxx / sz.000xxx / sz.30xxxx。"""
    from bottleneck_hunter.watchlist.store_base import extract_astock_code
    code = extract_astock_code(ticker)  # 全系统唯一 A股代码提取器（见 store_base）
    if not code:
        return None
    prefix = "sh" if code[0] == "6" else "sz"  # 6=沪, 0/3=深, 688 科创也在沪(6)
    return f"{prefix}.{code}"


class BaostockFetcher(BaseFetcher):
    name = "baostock"
    priority = 3  # 排在 efinance(0)/akshare(1)/pytdx(2) 之后，作可靠兜底
    supported_markets = {"a_stock"}

    async def fetch_daily(self, ticker: str, days: int = 180) -> pd.DataFrame | None:
        code = _bs_code(ticker)
        if not code:
            return None

        def _fetch():
            try:
                import baostock as bs
            except ImportError:
                logger.warning("baostock 未安装")
                return None

            start_date = (datetime.now() - timedelta(days=max(days, 365))).strftime("%Y-%m-%d")
            end_date = datetime.now().strftime("%Y-%m-%d")
            with _bs_lock:
                lg = bs.login()
                if lg.error_code != "0":
                    logger.debug("baostock 登录失败: %s", lg.error_msg)
                    return None
                try:
                    rs = bs.query_history_k_data_plus(
                        code,
                        "date,open,high,low,close,volume,amount",
                        start_date=start_date, end_date=end_date,
                        frequency="d", adjustflag="2",  # 2=前复权(qfq)，与 akshare 一致
                    )
                    if rs.error_code != "0":
                        logger.debug("baostock 查询失败 %s: %s", code, rs.error_msg)
                        return None
                    rows = []
                    while rs.next():
                        d, o, h, low, c, vol, amt = rs.get_row_data()
                        if not c:  # 停牌日 close 为空 → 跳过
                            continue
                        rows.append({
                            "date": d,
                            "open": float(o or 0),
                            "high": float(h or 0),
                            "low": float(low or 0),
                            "close": float(c),
                            "volume": int(float(vol or 0)),
                            "amount": float(amt or 0),
                        })
                finally:
                    bs.logout()

            if not rows:
                return None
            result = pd.DataFrame(rows)
            if days < len(result):
                result = result.tail(days).reset_index(drop=True)
            return result

        return await asyncio.to_thread(_fetch)

    async def fetch_realtime(self, ticker: str) -> StandardQuote | None:
        # baostock 仅历史数据，无实时报价
        return None
