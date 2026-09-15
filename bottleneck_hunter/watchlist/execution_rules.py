"""多市场交易规则与订单状态机（P2-1）

在「一笔订单」维度上，用**可审计的订单状态机**驱动成交，并施加各市场真实交易规则：
最小变动价位（tick）、最小交易单位（lot/整手）、单日涨跌停、买卖价差、T+1 可卖延迟；
支持**部分成交**（按流动性上限逐轮吃单）与**撤单**（撤未成交余额）。

订单状态严格单调、终态不可逆（NEW → PARTIALLY_FILLED → FILLED / CANCELLED / REJECTED），
每一笔成交都落一条 `Fill` 审计记录（数量/价格/佣金/价差/状态），使「订单状态单调且成交可审计」可执行、可验证。

与既有能力的分工（互补，不替代）：
- `slippage.calc_slippage` 建模**冲击成本**（base + sqrt 参与率）；本模块建模**价差 + 涨跌停 + tick + lot**，
  二者是不同的成本/约束旋钮（勿把 calc_slippage 的 base_bps 与本模块 half_spread_bps 重复叠加）。
- `position_sizing._round_lot` 只在**建仓定量**时向下取整手；本模块把整手规则一般化进 `MarketRules` 并在成交时强制。
- `event_backtest.run_event_backtest` 是组合级全量成交回测；本模块是**单笔订单微结构**（部分成交/状态机/撤单），
  可被其复用，也可独立使用。

纯 stdlib、确定可复现，不引入 numpy/scipy。**诊断/仿真层，不接线进生产成交链**（回退=不调用，
现有 `trade_executor.execute_trade` 全量成交路径完全不变）。默认规则值（tick/lot/涨跌停/价差）是校准旋钮而非硬事实。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum

# ---------------------------------------------------------------------------
# 市场交易规则
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MarketRules:
    """单一市场的交易规则旋钮。默认值贴近常见约定，接实盘时按券商/交易所细则校准。"""

    tick_size: float          # 最小变动价位（元/股）
    lot_size: int             # 最小交易单位（股/手），A股/港股常见 100，美股 1
    price_limit_pct: float    # 单日涨跌停幅度 %（0 = 无涨跌停，如美股/港股）
    half_spread_bps: float    # 半价差（成交价对交易者不利一侧偏移的基点数）
    t_plus: int               # 买入后可卖延迟的交易日数（A股=1，美股/港股=0）
    allow_short: bool = False  # 是否允许裸卖空（A股否）


# 默认规则表：美股 / 港股 / A股主板；A股创业板·科创板走 ±20% 变体。
_RULES: dict[str, MarketRules] = {
    "us_stock": MarketRules(tick_size=0.01, lot_size=1, price_limit_pct=0.0,
                            half_spread_bps=2.0, t_plus=0, allow_short=True),
    # ponytail: 港股 tick 实为按价分档（<0.25 为 0.001 等），此处简化为 0.01；lot 因股而异，默认 100
    "hk_stock": MarketRules(tick_size=0.01, lot_size=100, price_limit_pct=0.0,
                            half_spread_bps=5.0, t_plus=0, allow_short=True),
    "a_stock": MarketRules(tick_size=0.01, lot_size=100, price_limit_pct=10.0,
                           half_spread_bps=3.0, t_plus=1, allow_short=False),
}
_A_STAR = MarketRules(tick_size=0.01, lot_size=100, price_limit_pct=20.0,
                      half_spread_bps=3.0, t_plus=1, allow_short=False)
_STAR_BOARDS = {"star", "star_market", "chinext", "科创板", "创业板"}


def rules_for(market: str, *, board: str | None = None) -> MarketRules:
    """按市场（及 A股板块）取规则。A股创业板/科创板 → ±20% 变体；未知市场回退美股规则。"""
    m = (market or "us_stock").strip().lower()
    if m == "a_stock" and board and str(board).strip().lower() in _STAR_BOARDS:
        return _A_STAR
    return _RULES.get(m, _RULES["us_stock"])


def round_to_tick(price: float, rules: MarketRules) -> float:
    """价格取整到最小变动价位（就近）。tick≤0 或非正价 → 原样/0。"""
    if price <= 0:
        return 0.0
    if rules.tick_size <= 0:
        return round(price, 4)
    return round(round(price / rules.tick_size) * rules.tick_size, 6)


def round_to_lot(shares: float, rules: MarketRules) -> int:
    """数量向下取整到最小交易单位（不足一手 → 0）。lot≤1 → 向下取整到 1 股。"""
    if shares <= 0:
        return 0
    if rules.lot_size <= 1:
        return int(shares)
    return (int(shares) // rules.lot_size) * rules.lot_size


def limit_band(prev_close: float | None, rules: MarketRules) -> tuple[float | None, float | None]:
    """由昨收算涨跌停带 (下限, 上限)，价格对齐到 tick。无涨跌停或无昨收 → (None, None)。"""
    if rules.price_limit_pct <= 0 or not prev_close or prev_close <= 0:
        return (None, None)
    delta = prev_close * rules.price_limit_pct / 100.0
    return (round_to_tick(prev_close - delta, rules), round_to_tick(prev_close + delta, rules))


def can_fill(side: str, ref_price: float, prev_close: float | None, rules: MarketRules) -> bool:
    """封板可成交性：封涨停(ref≥上限)买不到；封跌停(ref≤下限)卖不掉。无涨跌停恒可成交。"""
    lower, upper = limit_band(prev_close, rules)
    if side in ("buy", "add"):
        return upper is None or ref_price < upper
    if side in ("sell", "reduce"):
        return lower is None or ref_price > lower
    return False


def fill_price(ref_price: float, side: str, prev_close: float | None, rules: MarketRules) -> float:
    """成交价：参考价按半价差向不利方向偏移 → 涨跌停带夹取 → tick 取整。"""
    hs = rules.half_spread_bps / 10000.0
    if side in ("buy", "add"):
        px = ref_price * (1 + hs)
    elif side in ("sell", "reduce"):
        px = ref_price * (1 - hs)
    else:
        px = ref_price
    lower, upper = limit_band(prev_close, rules)
    if upper is not None and px > upper:
        px = upper
    if lower is not None and px < lower:
        px = lower
    return round_to_tick(px, rules)


def next_sellable_index(buy_index: int, rules: MarketRules) -> int:
    """买入交易日在交易日历中的下标 → 最早可卖交易日下标（T+t_plus）。调用方按自己的交易日历索引。"""
    return buy_index + max(rules.t_plus, 0)


# ---------------------------------------------------------------------------
# 订单状态机
# ---------------------------------------------------------------------------


class OrderStatus(IntEnum):
    """订单状态。数值即严重度/推进度，状态只增不减；FILLED/CANCELLED/REJECTED 为终态。"""

    NEW = 0
    PARTIALLY_FILLED = 1
    FILLED = 2
    CANCELLED = 3
    REJECTED = 4


_TERMINAL = {OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED}


@dataclass(frozen=True)
class Fill:
    """单笔成交审计记录（不可变）。"""

    seq: int              # 该订单内第几笔成交（从 1 起）
    date: str
    shares: int
    price: float
    gross: float          # 成交金额（shares×price）
    commission: float
    spread_bps: float     # 本笔施加的半价差基点
    status_after: str     # 成交后订单状态名
    note: str = ""


@dataclass
class Order:
    """一笔订单的完整生命周期状态。成交、撤单、拒绝都经守卫，状态严格单调。"""

    order_id: str
    ticker: str
    side: str                 # buy|add|sell|reduce
    total_shares: int
    market: str = "us_stock"
    status: OrderStatus = OrderStatus.NEW
    filled_shares: int = 0
    fills: list[Fill] = field(default_factory=list)
    reason: str = ""

    def __post_init__(self) -> None:
        if self.total_shares <= 0:
            raise ValueError("total_shares 必须为正")
        if self.side not in ("buy", "add", "sell", "reduce"):
            raise ValueError(f"未知交易方向: {self.side}")

    @property
    def remaining(self) -> int:
        return self.total_shares - self.filled_shares

    @property
    def avg_fill_price(self) -> float:
        if self.filled_shares <= 0:
            return 0.0
        return round(sum(f.gross for f in self.fills) / self.filled_shares, 6)

    @property
    def is_terminal(self) -> bool:
        return self.status in _TERMINAL

    @property
    def is_open(self) -> bool:
        return self.status in (OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED)

    def _advance(self, new: OrderStatus) -> None:
        """状态单调推进的唯一入口：绝不允许回退（终态守卫在各动作里先行拦截）。"""
        if new < self.status:
            raise ValueError(f"订单状态单调性被破坏: {self.status.name} → {new.name}")
        self.status = new

    def record_fill(self, shares: int, price: float, *, date: str = "",
                    commission: float = 0.0, spread_bps: float = 0.0, note: str = "") -> Fill:
        """记一笔成交并推进状态。终态订单、非正数量、超额成交一律拒绝（绝不静默）。"""
        if self.is_terminal:
            raise ValueError(f"终态订单不可再成交: {self.status.name}")
        if shares <= 0:
            raise ValueError("成交数量必须为正")
        if shares > self.remaining:
            raise ValueError(f"成交数量 {shares} 超过未成交余额 {self.remaining}")
        self.filled_shares += shares
        status = OrderStatus.FILLED if self.remaining == 0 else OrderStatus.PARTIALLY_FILLED
        fill = Fill(len(self.fills) + 1, date, shares, price, round(shares * price, 4),
                    round(commission, 4), spread_bps, status.name, note)
        self.fills.append(fill)
        self._advance(status)
        return fill

    def cancel(self, reason: str = "") -> None:
        """撤销未成交余额。仅未完结订单可撤；终态订单撤单即报错。"""
        if not self.is_open:
            raise ValueError(f"仅未完结订单可撤单，当前 {self.status.name}")
        self.reason = reason
        self._advance(OrderStatus.CANCELLED)

    def reject(self, reason: str = "") -> None:
        """拒绝订单。仅未发生任何成交的新订单可拒绝；已部分成交应走撤单。"""
        if self.status != OrderStatus.NEW or self.filled_shares > 0:
            raise ValueError("仅未成交的新订单可拒绝；已部分成交请撤单")
        self.reason = reason
        self._advance(OrderStatus.REJECTED)


# ---------------------------------------------------------------------------
# 撮合：规则 + 状态机 + 流动性上限
# ---------------------------------------------------------------------------


def match_against_bar(order: Order, *, ref_price: float | None, prev_close: float | None,
                      bar_volume: float | None, rules: MarketRules,
                      participation_rate: float = 0.1, cash: float | None = None,
                      commission_bps: float = 0.0, stamp_sell_bps: float = 0.0,
                      date: str = "", note: str = "") -> Fill | None:
    """对某一交易日的一根 bar 撮合一次，产出至多一笔（可能部分）成交。

    返回本轮 Fill；因封板/无价/零成交量/现金买不起一手而不能成交时返回 None（订单保持未完结，留待下轮）。
    流动性上限 = bar_volume×participation_rate 向下取整手；买单可再受 cash 上限约束（粗口径，不含佣金）。
    bar_volume=None 表示无量数据（不设流动性上限）；bar_volume<=0 表示当日零成交/停牌（不可成交）。
    """
    if not order.is_open:
        return None
    if not ref_price or ref_price <= 0:
        return None
    if not can_fill(order.side, ref_price, prev_close, rules):
        return None
    px = fill_price(ref_price, order.side, prev_close, rules)
    if px <= 0:
        return None
    cap = order.remaining
    if bar_volume is not None:
        if bar_volume <= 0:
            return None  # 零成交量/停牌当日无法成交（区别于 None=无量数据不设限）
        cap = min(cap, round_to_lot(bar_volume * participation_rate, rules))
    if cash is not None and order.side in ("buy", "add"):
        cap = min(cap, round_to_lot(cash / px, rules))
    if cap <= 0:
        return None
    comm_bps = commission_bps + (stamp_sell_bps if order.side in ("sell", "reduce") else 0.0)
    commission = round(cap * px * comm_bps / 10000.0, 4)
    return order.record_fill(cap, px, date=date, commission=commission,
                             spread_bps=rules.half_spread_bps, note=note)


def simulate_execution(order: Order, bars: list[tuple[str, float, float | None]], rules: MarketRules, *,
                       prev_close: float | None = None, participation_rate: float = 0.1,
                       max_bars: int | None = None, commission_bps: float = 0.0,
                       stamp_sell_bps: float = 0.0, cancel_leftover: bool = True) -> Order:
    """按 bar 序列逐轮撮合一笔订单：部分成交累积到全部成交，跑完仍有余额则撤单（GTC 到期）。

    bars: [(date, ref_price, volume), ...]，昨收由上一根 bar 的价推得（首根用 prev_close）。
    max_bars: 最多撮合的 bar 数（模拟订单有效期）；cancel_leftover: 结束时撤未成交余额。
    现金账本按需由调用方管理（本驱动不做累计扣现，避免与 event_backtest 组合账本重复）。
    """
    prev = prev_close
    for i, (date, ref_price, volume) in enumerate(bars):
        if not order.is_open:
            break
        if max_bars is not None and i >= max_bars:
            break
        match_against_bar(order, ref_price=ref_price, prev_close=prev, bar_volume=volume,
                          rules=rules, participation_rate=participation_rate,
                          commission_bps=commission_bps, stamp_sell_bps=stamp_sell_bps, date=date)
        if ref_price and ref_price > 0:
            prev = ref_price
    if cancel_leftover and order.is_open:
        order.cancel("gtc_expired")
    return order


if __name__ == "__main__":
    # ponytail 自检：市场规则 + 状态机单调 + 部分成交 + 撤单 + 封板/T+1
    a = rules_for("a_stock")
    star = rules_for("a_stock", board="科创板")
    us = rules_for("us_stock")
    hk = rules_for("hk_stock")
    assert (a.price_limit_pct, a.lot_size, a.t_plus) == (10.0, 100, 1), a
    assert star.price_limit_pct == 20.0, star
    assert (us.price_limit_pct, us.lot_size, us.t_plus) == (0.0, 1, 0), us
    assert hk.lot_size == 100 and hk.price_limit_pct == 0.0, hk

    # tick / lot 取整
    assert round_to_tick(10.037, a) == 10.04, round_to_tick(10.037, a)
    assert round_to_lot(250, a) == 200 and round_to_lot(99, a) == 0, "整手向下取整"
    assert round_to_lot(250, us) == 250, "美股 1 股单位"

    # 涨跌停带 + 封板可成交性
    lo, hi = limit_band(100.0, a)
    assert (lo, hi) == (90.0, 110.0), (lo, hi)
    assert can_fill("buy", 110.0, 100.0, a) is False, "封涨停买不到"
    assert can_fill("sell", 90.0, 100.0, a) is False, "封跌停卖不掉"
    assert can_fill("buy", 105.0, 100.0, a) is True
    assert can_fill("buy", 110.0, 100.0, us) is True, "美股无涨跌停恒可成交"

    # 成交价：买加价差、卖减价差、涨停夹取
    assert fill_price(100.0, "buy", None, a) > 100.0, "买单含价差高于参考价"
    assert fill_price(100.0, "sell", None, a) < 100.0, "卖单含价差低于参考价"
    assert fill_price(109.99, "buy", 100.0, a) == 110.0, "价差顶破涨停被夹取到上限"

    # 状态机：部分成交 → 全部成交，审计可重建
    o = Order("o1", "600000.SS", "buy", 1000, market="a_stock")
    o.record_fill(300, 10.0)
    assert o.status == OrderStatus.PARTIALLY_FILLED and o.remaining == 700
    o.record_fill(700, 10.1)
    assert o.status == OrderStatus.FILLED and o.remaining == 0
    assert sum(f.shares for f in o.fills) == o.filled_shares == 1000, "审计数量自洽"
    assert abs(o.avg_fill_price - (300 * 10.0 + 700 * 10.1) / 1000) < 1e-9, o.avg_fill_price

    # 单调性守卫：终态不可再动，超额/回退一律报错
    for bad in (lambda: o.record_fill(1, 10.0), lambda: o.cancel(), lambda: o.reject()):
        try:
            bad()
            raise AssertionError("终态订单动作应报错")
        except ValueError:
            pass
    o2 = Order("o2", "AAPL", "buy", 100, market="us_stock")
    try:
        o2.record_fill(101, 1.0)
        raise AssertionError("超额成交应报错")
    except ValueError:
        pass
    o2.record_fill(50, 1.0)
    try:
        o2.reject()
        raise AssertionError("已部分成交不可拒绝")
    except ValueError:
        pass

    # 部分成交跨多根 bar + 撤单余额（流动性上限：每根 volume×0.1 取整手）
    o3 = Order("o3", "600000.SS", "buy", 1000, market="a_stock")
    simulate_execution(o3, [("d1", 10.0, 3000), ("d2", 10.0, 3000)], a,
                       participation_rate=0.1, max_bars=2)
    # 每根最多 300 手级别 → 两根共 600，未满 1000 → 结束撤单
    assert o3.status == OrderStatus.CANCELLED and 0 < o3.filled_shares < 1000, o3
    assert o3.reason == "gtc_expired"

    # 充足流动性一轮成交
    o4 = Order("o4", "AAPL", "buy", 500, market="us_stock")
    f = match_against_bar(o4, ref_price=100.0, prev_close=None, bar_volume=100000, rules=us,
                          commission_bps=0.0)
    assert o4.status == OrderStatus.FILLED and f is not None and f.shares == 500

    # 封涨停整日买不到 → 撤单，零成交
    o5 = Order("o5", "600000.SS", "buy", 100, market="a_stock")
    simulate_execution(o5, [("d1", 110.0, 999999)], a, prev_close=100.0)
    assert o5.status == OrderStatus.CANCELLED and o5.filled_shares == 0, o5

    # 零成交量当日不可成交（区别于 None 无量数据不设限）
    o6 = Order("o6", "AAPL", "buy", 100, market="us_stock")
    assert match_against_bar(o6, ref_price=100.0, prev_close=None, bar_volume=0, rules=us) is None
    assert match_against_bar(o6, ref_price=100.0, prev_close=None, bar_volume=None, rules=us) is not None

    # T+1 / T+0
    assert next_sellable_index(0, a) == 1 and next_sellable_index(0, us) == 0

    print("execution_rules 自检通过：市场规则 + 订单状态机单调 + 部分成交 + 撤单 + 封板 + T+1")
