"""交易费率模型 — 按证券品种区分佣金与印花税（实盘模拟与回测共用一份）。

病史（2026-11 复核）：费率此前三处各写各的——
  · `trade_executor._execute_buy/_execute_sell`：佣金固定 0.1% 双边，**完全没有印花税**；
  · `event_backtest.COST_CONFIG`：佣金 2.5bp + 卖方印花税 5bp（合理），但**没有任何生产调用方**；
  · `trading_api` 的权益曲线又按 0.1% 反推现金。
后果不只是数字难看：ETF 与可转债法定免印花税，用一组"股票默认值"套到它们上会系统性
高估成本；反过来漏收股票的卖方印花税则系统性低估。决策中心的学习闭环
（trade_reviewer / preference_learner / layer_performance）全部建立在这些数字上。

设计取舍（借鉴 easy_tdx `backtest/fees.py`，按其结论重算）：
  · 印花税差异是**法定事实**（证券投资基金、债券免征），按品种判断是核心价值；
  · 佣金取常见券商的**保守**默认，不追求最优（免得回测虚高）；可显式覆盖；
  · 最低佣金只在实盘模拟里约束（回测按 bps 连续计费，便于对比不同规模）。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class InstrumentKind(Enum):
    """证券品种类型（按代码前缀推断）。"""

    STOCK = "stock"      # A 股股票（含科创板/创业板/北交所）
    ETF = "etf"          # 场内基金 ETF / LOF
    CONVERTIBLE = "convertible"  # 可转债
    B_SHARE = "b_share"  # B 股
    INDEX = "index"      # 指数（不计交易成本）
    OTHER = "other"


#: 每类品种的默认费率。键与 InstrumentKind 的 value 对应，便于序列化。
@dataclass(frozen=True)
class FeeModel:
    commission_rate: float   # 佣金率（双边）
    min_commission: float    # 单笔最低佣金（元）
    stamp_tax_rate: float    # 印花税率（仅卖方）

    def commission(self, amount: float, side: str, *, apply_min: bool = True) -> float:
        """本笔佣金（不含印花税）。amount 取成交金额的绝对值。"""
        amt = abs(float(amount or 0))
        if amt <= 0:
            return 0.0
        fee = amt * self.commission_rate
        if apply_min:
            fee = max(fee, self.min_commission)
        return round(fee, 2)

    def stamp_tax(self, amount: float, side: str) -> float:
        """本笔印花税——只在卖出时收取。"""
        if (side or "").lower() not in ("sell", "reduce"):
            return 0.0
        amt = abs(float(amount or 0))
        return round(amt * self.stamp_tax_rate, 2)

    def total(self, amount: float, side: str, *, apply_min: bool = True) -> float:
        """佣金 + 印花税。"""
        return round(self.commission(amount, side, apply_min=apply_min) + self.stamp_tax(amount, side), 2)


#: A 股各类品种的默认费率（保守口径）。
_A_SHARE_MODELS: dict[InstrumentKind, FeeModel] = {
    InstrumentKind.STOCK: FeeModel(commission_rate=0.0003, min_commission=5.0, stamp_tax_rate=0.0005),
    InstrumentKind.ETF: FeeModel(commission_rate=0.0003, min_commission=5.0, stamp_tax_rate=0.0),
    InstrumentKind.CONVERTIBLE: FeeModel(commission_rate=0.0002, min_commission=1.0, stamp_tax_rate=0.0),
    InstrumentKind.B_SHARE: FeeModel(commission_rate=0.0005, min_commission=5.0, stamp_tax_rate=0.0005),
    InstrumentKind.INDEX: FeeModel(commission_rate=0.0, min_commission=0.0, stamp_tax_rate=0.0),
    InstrumentKind.OTHER: FeeModel(commission_rate=0.0003, min_commission=5.0, stamp_tax_rate=0.0),
}

#: 美股：近似零佣（多数券商），无印花税。留旋钮，接真实费率时在此调。
_US_MODEL = FeeModel(commission_rate=0.0, min_commission=0.0, stamp_tax_rate=0.0)


def detect_instrument_kind(ticker: str, market: str = "a_stock") -> InstrumentKind:
    """按代码前缀推断品种。

    A 股口径（6 位数字码）：
      · 沪市 5 开头 / 深市 1 开头 → ETF/LOF；
      · 11/12 开头（沪）与 12 开头（深）→ 可转债；
      · 900/200 开头 → B 股；
      · 000001 之外的 000/001/002/003/300/301/600/601/603/605/688/689/8/4 → 股票；
      · 000300 这类指数只有在显式标注时才算 INDEX。
    美股（字母 ticker）一律按股票口径（零佣），指数由调用方显式传 INDEX。
    """
    if (market or "").lower() != "a_stock":
        return InstrumentKind.STOCK

    code = _extract_6digit(ticker)
    if not code:
        return InstrumentKind.OTHER

    # 可转债：沪 110/111/113/118、深 12x。先判，避免与股票前缀重叠。
    if code.startswith(("110", "111", "113", "118")) or code.startswith("12"):
        return InstrumentKind.CONVERTIBLE
    # B 股：沪 900、深 200
    if code.startswith(("900", "200")):
        return InstrumentKind.B_SHARE
    # ETF/LOF：沪 5xxxxx、深 15xxxx/16xxxx/18xxxx（LOF/分级）
    if code.startswith("5") or code.startswith(("15", "16", "18")):
        return InstrumentKind.ETF
    return InstrumentKind.STOCK


def _extract_6digit(ticker: str) -> str:
    """从 '600519.SH' / 'SH600519' / '600519' 中取出 6 位数字码。"""
    digits = "".join(ch for ch in (ticker or "") if ch.isdigit())
    return digits[:6] if len(digits) >= 6 else ""


def resolve_fee_model(ticker: str = "", market: str = "a_stock",
                      kind: InstrumentKind | None = None) -> FeeModel:
    """按市场与品种给出费率模型。"""
    if (market or "").lower() != "a_stock":
        return _US_MODEL
    return _A_SHARE_MODELS.get(kind or detect_instrument_kind(ticker, market), _A_SHARE_MODELS[InstrumentKind.STOCK])


if __name__ == "__main__":
    # 自检：印花税差异与最低佣金是核心行为
    etf = resolve_fee_model("510880.SS", "a_stock")
    stock = resolve_fee_model("600519.SS", "a_stock")
    bond = resolve_fee_model("113050.SS", "a_stock")
    assert etf.stamp_tax(100_000, "sell") == 0.0
    assert stock.stamp_tax(100_000, "sell") == 50.0
    assert stock.stamp_tax(100_000, "buy") == 0.0
    assert bond.stamp_tax(100_000, "sell") == 0.0
    assert stock.commission(1000, "buy") == 5.0        # 最低佣金兜底
    assert stock.commission(100_000, "buy") == 30.0
    assert detect_instrument_kind("000001.SZ") is InstrumentKind.STOCK
    assert detect_instrument_kind("300750.SZ") is InstrumentKind.STOCK
    assert detect_instrument_kind("510880.SS") is InstrumentKind.ETF
    assert detect_instrument_kind("159915.SZ") is InstrumentKind.ETF
    assert detect_instrument_kind("113050.SS") is InstrumentKind.CONVERTIBLE
    assert detect_instrument_kind("AAPL", "us_stock") is InstrumentKind.STOCK
    assert resolve_fee_model("AAPL", "us_stock").total(10_000, "sell") == 0.0
    print("fees self-check OK")
