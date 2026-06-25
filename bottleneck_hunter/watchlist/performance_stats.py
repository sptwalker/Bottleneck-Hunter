"""绩效统计计算模块 — 从交易记录和复盘数据聚合绩效指标。"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timezone

from bottleneck_hunter.watchlist.store import WatchlistStore

logger = logging.getLogger(__name__)


class PerformanceCalculator:

    def __init__(self, store: WatchlistStore):
        self._store = store

    def compute_overview(self) -> dict:
        """总览指标：总交易数、胜率、总收益率、平均持仓天数、最佳/最差交易"""
        trades = self._store.get_sim_trades(limit=10000)
        reviews = self._store.get_auto_reviews(limit=10000)

        sell_trades = [t for t in trades if t.get("side") == "sell"]
        buy_trades = [t for t in trades if t.get("side") == "buy"]
        total_trades = len(sell_trades)

        if not reviews:
            wins = sum(1 for t in sell_trades if self._calc_return(t, buy_trades) > 0)
            losses = total_trades - wins
        else:
            wins = sum(1 for r in reviews if (r.get("return_pct") or 0) > 0)
            losses = len(reviews) - wins
            total_trades = max(total_trades, len(reviews))

        win_rate = round(wins / total_trades * 100, 1) if total_trades > 0 else 0.0

        returns = []
        for r in reviews:
            rp = r.get("return_pct")
            if rp is not None:
                returns.append(rp)

        avg_return = round(sum(returns) / len(returns), 2) if returns else 0.0
        best_trade = round(max(returns), 2) if returns else 0.0
        worst_trade = round(min(returns), 2) if returns else 0.0

        avg_holding_days = self._avg_holding_days(trades)

        account = self._store.get_sim_account()
        total_return_pct = account.get("total_return_pct", 0.0)

        return {
            "total_trades": total_trades,
            "wins": wins,
            "losses": losses,
            "win_rate": win_rate,
            "total_return_pct": round(total_return_pct, 2),
            "avg_return_pct": avg_return,
            "best_trade_pct": best_trade,
            "worst_trade_pct": worst_trade,
            "avg_holding_days": avg_holding_days,
        }

    def compute_monthly_series(self, months: int = 6) -> list[dict]:
        """近 N 月绩效序列"""
        reviews = self._store.get_auto_reviews(limit=10000)
        trades = self._store.get_sim_trades(limit=10000)

        monthly: dict[str, list] = defaultdict(list)
        for r in reviews:
            month_key = (r.get("created_at") or "")[:7]
            if month_key:
                monthly[month_key].append(r.get("return_pct", 0))

        if not monthly:
            sell_by_month: dict[str, list] = defaultdict(list)
            buy_trades = [t for t in trades if t.get("side") == "buy"]
            for t in trades:
                if t.get("side") == "sell":
                    mk = (t.get("created_at") or "")[:7]
                    if mk:
                        ret = self._calc_return(t, buy_trades)
                        sell_by_month[mk].append(ret)
            monthly = sell_by_month

        sorted_months = sorted(monthly.keys(), reverse=True)[:months]
        sorted_months.reverse()

        result = []
        for mk in sorted_months:
            rets = monthly[mk]
            wins = sum(1 for r in rets if r > 0)
            total = len(rets)
            result.append({
                "month": mk,
                "trades": total,
                "wins": wins,
                "win_rate": round(wins / total * 100, 1) if total > 0 else 0.0,
                "avg_return_pct": round(sum(rets) / total, 2) if total > 0 else 0.0,
                "total_return_pct": round(sum(rets), 2),
            })
        return result

    def compute_drawdown(self) -> dict:
        """最大回撤（从权益曲线计算）"""
        account = self._store.get_sim_account()
        initial = account.get("initial_capital", 100000)
        trades = self._store.get_sim_trades(limit=10000)

        daily_cf: dict[str, float] = defaultdict(float)
        for t in trades:
            date = (t.get("created_at") or "")[:10]
            if not date:
                continue
            if t.get("side") == "buy":
                daily_cf[date] -= t.get("amount", 0)
            else:
                daily_cf[date] += t.get("amount", 0)

        if not daily_cf:
            return {"max_drawdown_pct": 0.0, "peak_date": "", "trough_date": ""}

        sorted_dates = sorted(daily_cf.keys())
        equity = initial
        peak = equity
        peak_date = sorted_dates[0]
        max_dd = 0.0
        dd_peak_date = ""
        dd_trough_date = ""

        for d in sorted_dates:
            equity += daily_cf[d]
            if equity > peak:
                peak = equity
                peak_date = d
            dd = (peak - equity) / peak if peak > 0 else 0
            if dd > max_dd:
                max_dd = dd
                dd_peak_date = peak_date
                dd_trough_date = d

        return {
            "max_drawdown_pct": round(max_dd * 100, 2),
            "peak_date": dd_peak_date,
            "trough_date": dd_trough_date,
        }

    def compute_by_ticker(self) -> list[dict]:
        """按标的分组统计"""
        reviews = self._store.get_auto_reviews(limit=10000)

        by_ticker: dict[str, list] = defaultdict(list)
        for r in reviews:
            ticker = r.get("ticker", "")
            if ticker:
                by_ticker[ticker].append(r.get("return_pct", 0))

        if not by_ticker:
            trades = self._store.get_sim_trades(limit=10000)
            buy_trades = [t for t in trades if t.get("side") == "buy"]
            for t in trades:
                if t.get("side") == "sell":
                    ticker = t.get("ticker", "")
                    if ticker:
                        by_ticker[ticker].append(self._calc_return(t, buy_trades))

        result = []
        for ticker, rets in sorted(by_ticker.items()):
            wins = sum(1 for r in rets if r > 0)
            total = len(rets)
            result.append({
                "ticker": ticker,
                "trades": total,
                "wins": wins,
                "win_rate": round(wins / total * 100, 1) if total > 0 else 0.0,
                "avg_return_pct": round(sum(rets) / total, 2) if total > 0 else 0.0,
                "best_pct": round(max(rets), 2) if rets else 0.0,
                "worst_pct": round(min(rets), 2) if rets else 0.0,
            })
        return sorted(result, key=lambda x: x["trades"], reverse=True)

    def compute_review_summary(self) -> dict:
        """复盘摘要：平均质量评分、常见教训"""
        reviews = self._store.get_auto_reviews(limit=100)
        if not reviews:
            return {"avg_quality_score": 0.0, "total_reviews": 0, "common_lessons": []}

        scores = []
        lessons_count: dict[str, int] = defaultdict(int)
        for r in reviews:
            rj = r.get("result_json", {})
            if isinstance(rj, dict):
                qs = rj.get("trade_quality_score")
                if qs is not None:
                    scores.append(float(qs))
                for lesson in rj.get("key_lessons", []):
                    if lesson:
                        lessons_count[lesson] += 1

        top_lessons = sorted(lessons_count.items(), key=lambda x: x[1], reverse=True)[:5]
        return {
            "avg_quality_score": round(sum(scores) / len(scores), 1) if scores else 0.0,
            "total_reviews": len(reviews),
            "common_lessons": [{"lesson": l, "count": c} for l, c in top_lessons],
        }

    def compute_cost_summary(self) -> dict:
        """LLM 成本统计"""
        daily = self._store.get_daily_usage()
        monthly = self._store.get_monthly_usage()
        limits = self._store.get_budget_limits()
        return {
            "daily_cost": daily.get("cost", 0.0),
            "daily_limit": limits.get("daily_limit_usd", 2.0),
            "monthly_cost": monthly.get("cost", 0.0),
            "monthly_limit": limits.get("monthly_limit_usd", 30.0),
            "daily_tokens": daily.get("input_tokens", 0) + daily.get("output_tokens", 0),
            "monthly_tokens": monthly.get("input_tokens", 0) + monthly.get("output_tokens", 0),
        }

    def _calc_return(self, sell_trade: dict, buy_trades: list[dict]) -> float:
        """从 buy/sell 配对计算收益率"""
        ticker = sell_trade.get("ticker", "")
        exit_price = sell_trade.get("price", 0)
        buys = [b for b in buy_trades if b.get("ticker") == ticker]
        if buys and exit_price:
            entry_price = buys[0].get("price", 0)
            if entry_price > 0:
                return round((exit_price / entry_price - 1) * 100, 2)
        return 0.0

    def _avg_holding_days(self, trades: list[dict]) -> int:
        """计算平均持仓天数"""
        buy_dates: dict[str, str] = {}
        holding_days = []
        for t in trades:
            ticker = t.get("ticker", "")
            if t.get("side") == "buy":
                buy_dates[ticker] = t.get("created_at", "")
            elif t.get("side") == "sell" and ticker in buy_dates:
                try:
                    bd = datetime.fromisoformat(buy_dates[ticker].replace("Z", "+00:00"))
                    sd = datetime.fromisoformat(t.get("created_at", "").replace("Z", "+00:00"))
                    holding_days.append((sd - bd).days)
                except (ValueError, TypeError):
                    pass
        return round(sum(holding_days) / len(holding_days)) if holding_days else 0
