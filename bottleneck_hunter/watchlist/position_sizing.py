"""仓位管理算法模块

提供三种仓位算法：凯利公式、波动率缩放、风险平价。
供 L4 执行方案引用，替代 LLM 直觉式仓位分配。
"""

from __future__ import annotations

import math
import logging

logger = logging.getLogger(__name__)


class PositionSizer:
    """仓位管理算法集合。"""

    @staticmethod
    def kelly_fraction(
        win_rate: float,
        avg_win: float,
        avg_loss: float,
        half_kelly: bool = True,
    ) -> float:
        """凯利公式：f* = (p * b - q) / b

        p = 胜率, q = 1-p, b = 盈亏比 (avg_win / avg_loss)
        实际使用半凯利（f*/2）降低波动。

        返回建议仓位比例 [0, 1]。
        """
        if avg_loss <= 0 or win_rate <= 0 or win_rate >= 1:
            return 0.0

        b = avg_win / avg_loss
        p = win_rate
        q = 1 - p
        f = (p * b - q) / b

        if f <= 0:
            return 0.0

        if half_kelly:
            f /= 2

        return round(min(f, 0.25), 4)

    @staticmethod
    def volatility_scaled(
        target_vol: float,
        stock_vol: float,
        account_equity: float,
        stock_price: float,
    ) -> dict:
        """波动率缩放：高波动率股票分配更少资金。

        target_vol: 目标年化波动率（如 0.15 = 15%）
        stock_vol: 股票年化波动率
        account_equity: 账户总权益
        stock_price: 股票当前价格

        返回建议仓位金额和股数。
        """
        if stock_vol <= 0 or stock_price <= 0 or account_equity <= 0:
            return {"amount": 0, "shares": 0, "weight_pct": 0}

        position_value = target_vol / stock_vol * account_equity
        position_value = min(position_value, account_equity * 0.20)

        shares = int(position_value / stock_price)
        actual_amount = shares * stock_price
        weight_pct = round(actual_amount / account_equity * 100, 2)

        return {
            "amount": round(actual_amount, 2),
            "shares": shares,
            "weight_pct": weight_pct,
        }

    @staticmethod
    def risk_parity(
        positions: list[dict],
        volatilities: dict[str, float],
        account_equity: float,
    ) -> dict[str, dict]:
        """风险平价：使每个持仓对组合风险的贡献相等。

        positions: [{"ticker": "AAPL", ...}, ...]
        volatilities: {"AAPL": 0.25, ...}  # 年化波动率
        account_equity: 账户总权益

        返回 {ticker: {"weight_pct": ..., "amount": ..., "shares": ...}}
        """
        tickers = [p.get("ticker", "") for p in positions]
        vols = []
        for t in tickers:
            v = volatilities.get(t, 0)
            if v <= 0:
                v = 0.30  # 默认 30% 波动率
            vols.append(v)

        if not vols:
            return {}

        inv_vols = [1 / v for v in vols]
        total_inv = sum(inv_vols)
        if total_inv <= 0:
            return {}

        result = {}
        for i, ticker in enumerate(tickers):
            weight = inv_vols[i] / total_inv
            weight = min(weight, 0.25)
            amount = weight * account_equity
            price = 0
            for p in positions:
                if p.get("ticker") == ticker:
                    price = p.get("current_price", 0) or p.get("avg_cost", 0)
                    break

            shares = int(amount / price) if price > 0 else 0
            result[ticker] = {
                "weight_pct": round(weight * 100, 2),
                "amount": round(amount, 2),
                "shares": shares,
            }

        return result

    @staticmethod
    def compute_stock_volatility(daily_returns: list[float]) -> float:
        """从日收益率序列计算年化波动率。"""
        if len(daily_returns) < 5:
            return 0.0
        mean = sum(daily_returns) / len(daily_returns)
        variance = sum((r - mean) ** 2 for r in daily_returns) / len(daily_returns)
        daily_vol = math.sqrt(variance)
        return round(daily_vol * math.sqrt(252), 4)

    @staticmethod
    def suggest(
        method: str,
        account_equity: float,
        stock_price: float,
        win_rate: float = 0.0,
        avg_win: float = 0.0,
        avg_loss: float = 0.0,
        stock_vol: float = 0.0,
        target_vol: float = 0.15,
    ) -> dict:
        """综合建议：结合多种算法给出加权建议。"""
        sizer = PositionSizer()
        suggestions = {}

        if win_rate > 0 and avg_loss > 0:
            kelly_f = sizer.kelly_fraction(win_rate, avg_win, avg_loss)
            kelly_amount = kelly_f * account_equity
            kelly_shares = int(kelly_amount / stock_price) if stock_price > 0 else 0
            suggestions["kelly"] = {
                "fraction": kelly_f,
                "amount": round(kelly_amount, 2),
                "shares": kelly_shares,
            }

        if stock_vol > 0:
            vol_result = sizer.volatility_scaled(target_vol, stock_vol, account_equity, stock_price)
            suggestions["volatility_scaled"] = vol_result

        if suggestions:
            amounts = [s.get("amount", 0) for s in suggestions.values() if s.get("amount", 0) > 0]
            if amounts:
                avg_amount = sum(amounts) / len(amounts)
                avg_shares = int(avg_amount / stock_price) if stock_price > 0 else 0
                suggestions["recommended"] = {
                    "amount": round(avg_amount, 2),
                    "shares": avg_shares,
                    "weight_pct": round(avg_amount / account_equity * 100, 2),
                    "method": "ensemble",
                }

        return suggestions
