"""仓位管理算法模块

提供三种仓位算法：凯利公式、波动率缩放、风险平价。
供 L4 执行方案引用，替代 LLM 直觉式仓位分配。
"""

from __future__ import annotations

import logging
import math

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


def _round_lot(shares: float, market: str) -> int:
    """按市场取整到可成交单位：A股向下取整到 100 股整手；其余按 1 股。不足一手→0。"""
    if shares <= 0:
        return 0
    if (market or "").strip() == "a_stock":
        return int(shares // 100 * 100)
    return int(shares)


def target_shares_for_buy(
    *,
    price: float,
    account_equity: float,
    existing_value: float,
    llm_weight_pct: float,
    floor_pct: float,
    cap_pct: float,
    stock_vol: float = 0.0,
    target_vol: float = 0.15,
    market: str = "us_stock",
) -> int:
    """确定性定股：把「LLM 意图权重 / 波动率风险 / 下限兜底 / 单股上限」折算成可成交股数。

    替代 L4 里 LLM 拍脑袋的 shares（根治 $1M 账户买 5 股 MU=0.44% 这类零头仓）。
    目标总权重 = clamp( max(LLM意图, 下限floor), 单股上限cap, 波动率天花板 )：
      · 下限 floor 顶穿零头仓（B）；
      · 波动率天花板 = target_vol/stock_vol（与 volatility_scaled 同式，高波动→更小，自钳 20%），
        风险叠加（A），但**不得压穿 floor**——floor 是硬性反零头规则，用户明确其优先级高于风险缩放；
      · 单股上限 cap 已是 regime+用户 收紧后的真实上限。
    再按 market 取整到可成交单位（A股 100 手）。返回 0 = 够不到一个可成交仓位 → 上层跳过不开。
    add：只补到目标总权重（existing_value 已含），已达标→0。
    注：现金/板块/beta 由上层 validate_execution_plan / max_compliant_shares 继续「向下」钳制，
    本函数只负责把仓位「顶到」合理下限并设风险上限，两者方向互补、不重复。
    """
    if price <= 0 or account_equity <= 0 or floor_pct <= 0:
        return 0
    target = max(llm_weight_pct, floor_pct)
    if cap_pct and cap_pct > 0:
        target = min(target, cap_pct)
    if stock_vol and stock_vol > 0:
        vol_cap = min(target_vol / stock_vol * 100.0, 20.0)  # 20% 与 volatility_scaled 自钳一致
        target = min(target, max(vol_cap, floor_pct))        # 波动率天花板不压穿下限
    add_value = target / 100.0 * account_equity - max(existing_value, 0.0)
    if add_value <= 0:
        return 0
    return _round_lot(add_value / price, market)


if __name__ == "__main__":
    # ponytail: 自检 —— 下限兜底 / 波动率天花板 / 单股上限 / A股整手 / 加仓补差
    E = 1_000_000
    # 1) LLM 只想买 0.44%（MU 5股场景）→ 被顶到 3% 下限（≈30 股，而非 5）
    n = target_shares_for_buy(price=980.0, account_equity=E, existing_value=0,
                              llm_weight_pct=0.44, floor_pct=3.0, cap_pct=25.0)
    assert 28 <= n <= 32, n
    # 2) LLM 想买 12%，低波动 → 保留 12%（未触 20% 波动帽 / 25% 上限）
    n = target_shares_for_buy(price=100.0, account_equity=E, existing_value=0,
                              llm_weight_pct=12.0, floor_pct=3.0, cap_pct=25.0, stock_vol=0.10)
    assert n == 1200, n
    # 3) 高波动股(年化150%)：波动率天花板 10% 压低 LLM 的 12%
    n = target_shares_for_buy(price=100.0, account_equity=E, existing_value=0,
                              llm_weight_pct=12.0, floor_pct=3.0, cap_pct=25.0, stock_vol=1.5)
    assert n == 1000, n            # min(12, 25, max(0.15/1.5*100=10, 3)=10)=10%
    # 3b) 极端波动(年化600%)：波动率天花板算出 2.5% 但被 3% 下限托住
    n = target_shares_for_buy(price=100.0, account_equity=E, existing_value=0,
                              llm_weight_pct=12.0, floor_pct=3.0, cap_pct=25.0, stock_vol=6.0)
    assert n == 300, n            # 0.15/6*100=2.5% → max(2.5, 3)=3%
    # 4) 单股上限 18% 压过 LLM 的 25% 意图
    n = target_shares_for_buy(price=100.0, account_equity=E, existing_value=0,
                              llm_weight_pct=25.0, floor_pct=3.0, cap_pct=18.0)
    assert n == 1800, n
    # 5) A股整手：3% 目标算出 ≈2536 股 → 向下取整到 2500
    n = target_shares_for_buy(price=11.83, account_equity=E, existing_value=0,
                              llm_weight_pct=3.0, floor_pct=3.0, cap_pct=25.0, market="a_stock")
    assert n == 2500, n
    # 6) 加仓：已持 5%，目标 max(4%,3%)=4% < 5% → 无需加，返回 0
    n = target_shares_for_buy(price=100.0, account_equity=E, existing_value=50_000,
                              llm_weight_pct=4.0, floor_pct=3.0, cap_pct=25.0)
    assert n == 0, n
    # 7) 加仓：已持 2%，目标顶到 3% → 补 1%（100 股）
    n = target_shares_for_buy(price=100.0, account_equity=E, existing_value=20_000,
                              llm_weight_pct=0.5, floor_pct=3.0, cap_pct=25.0)
    assert n == 100, n
    # 8) A股不足一手 → 0（跳过，不生成不可成交零股）
    n = target_shares_for_buy(price=5000.0, account_equity=100_000, existing_value=0,
                              llm_weight_pct=3.0, floor_pct=3.0, cap_pct=25.0, market="a_stock")
    assert n == 0, n
    print("position_sizing 自检通过：下限兜底 + 波动率天花板 + 单股上限 + A股整手 + 加仓补差")
