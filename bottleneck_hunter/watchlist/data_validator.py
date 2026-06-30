"""数据异常检测 — 价格快照写入前的质量验证

检测涨跌停、价格跳跃、成交量异常、停牌等情况，
标记异常数据防止脏数据进入决策链。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

_A_STOCK_LIMIT_BOARDS = {
    "main": 10.5,
    "star": 20.5,
    "chinext": 20.5,
    "bse": 30.5,
}


@dataclass
class ValidationResult:
    valid: bool = True
    data_quality: str = "normal"
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def add_warning(self, msg: str):
        self.warnings.append(msg)
        if self.data_quality == "normal":
            self.data_quality = "warning"

    def add_error(self, msg: str):
        self.valid = False
        self.errors.append(msg)
        self.data_quality = "error"

    def mark_suspended(self, msg: str):
        self.data_quality = "suspended"
        self.warnings.append(msg)


def _detect_board(ticker: str) -> str:
    """根据 A 股代码判断板块"""
    code = ticker.split(".")[0] if "." in ticker else ticker
    if code.startswith("688"):
        return "star"
    if code.startswith("300") or code.startswith("301"):
        return "chinext"
    if code.startswith(("83", "87", "43")):
        return "bse"
    return "main"


def validate_snapshot(
    snap: dict,
    prev_snap: dict | None = None,
    market: str = "us_stock",
    is_st: bool = False,
) -> ValidationResult:
    """验证单条价格快照"""
    result = ValidationResult()
    ticker = snap.get("ticker", "?")

    close = snap.get("close")
    high = snap.get("high")
    low = snap.get("low")
    volume = snap.get("volume")
    change_pct = snap.get("change_pct")

    if not close or close <= 0:
        result.add_error(f"{ticker}: close 无效 ({close})")
        return result

    if volume is not None and volume < 0:
        result.add_error(f"{ticker}: volume 为负 ({volume})")
        return result

    if high and low and high < low:
        result.add_error(f"{ticker}: high ({high}) < low ({low})")
        return result

    open_price = snap.get("open")
    if open_price and close:
        if high and (open_price > high * 1.001 or close > high * 1.001):
            result.add_warning(f"{ticker}: open/close 超出 high")
        if low and (open_price < low * 0.999 or close < low * 0.999):
            result.add_warning(f"{ticker}: open/close 低于 low")

    if volume == 0 and (change_pct is None or abs(change_pct or 0) < 0.01):
        result.mark_suspended(f"{ticker}: 疑似停牌（零成交 + 无涨跌）")
        return result

    if market == "a_stock" and change_pct is not None:
        board = _detect_board(ticker)
        limit = _A_STOCK_LIMIT_BOARDS.get(board, 10.5)
        if is_st:
            limit = 5.5
        if abs(change_pct) >= limit:
            st_tag = "ST " if is_st else ""
            result.add_warning(
                f"{ticker}: {st_tag}{'涨' if change_pct > 0 else '跌'}停板 "
                f"({change_pct:+.1f}%，{board}板限制 ±{limit}%)"
            )

    if prev_snap and change_pct is not None:
        if abs(change_pct) > 15:
            result.add_warning(f"{ticker}: 单日涨跌幅异常 ({change_pct:+.1f}%)")

    if prev_snap and volume and prev_snap.get("volume"):
        prev_vol = prev_snap["volume"]
        if prev_vol > 0 and volume > prev_vol * 50:
            result.add_warning(
                f"{ticker}: 成交量异常放大 ({volume:,} vs 前日 {prev_vol:,}，{volume / prev_vol:.0f}倍)"
            )

    if result.warnings:
        logger.info("数据质量警告 %s: %s", ticker, "; ".join(result.warnings))
    if result.errors:
        logger.warning("数据质量错误 %s: %s", ticker, "; ".join(result.errors))

    return result
