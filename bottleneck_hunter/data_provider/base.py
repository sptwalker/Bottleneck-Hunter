"""数据源抽象层基类和标准化数据模型。"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import datetime, timezone

import pandas as pd
from pydantic import BaseModel

logger = logging.getLogger(__name__)


def safe_float(val) -> float | None:
    """安全浮点转换，处理 None/空字符串/NaN。供所有 fetcher 共用。"""
    if val is None or val == "" or val == "-":
        return None
    try:
        v = float(val)
        if pd.isna(v):
            return None
        return v
    except (ValueError, TypeError):
        return None


class StandardQuote(BaseModel):
    """标准化实时报价，所有 fetcher 统一输出格式。"""

    ticker: str
    name: str = ""
    price: float
    change_pct: float = 0.0
    volume: int = 0
    amount: float = 0.0
    pe_ratio: float | None = None
    pb_ratio: float | None = None
    volume_ratio: float | None = None
    turnover_rate: float | None = None
    market_cap: float | None = None
    source: str = ""
    timestamp: str = ""


class BaseFetcher(ABC):
    """数据源基类。所有 fetcher 继承此类并实现抽象方法。"""

    name: str = "base"
    priority: int = 99
    supported_markets: set[str] = set()

    def __init__(self):
        self._available = True

    @abstractmethod
    async def fetch_daily(self, ticker: str, days: int = 180) -> pd.DataFrame | None:
        """获取日K线数据。返回 DataFrame 包含 date/open/high/low/close/volume 列。"""

    @abstractmethod
    async def fetch_realtime(self, ticker: str) -> StandardQuote | None:
        """获取实时行情报价。"""

    async def health_check(self) -> bool:
        """检查数据源是否可用。默认返回 True，子类可覆盖。"""
        return self._available

    def supports(self, market: str) -> bool:
        return market in self.supported_markets
