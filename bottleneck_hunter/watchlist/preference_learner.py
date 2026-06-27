"""用户偏好学习模块 (17D.5)

从用户的确认/拒绝历史、交易记录、持仓行为中归纳偏好，
存入 user_preferences 表，供 L4 执行方案生成时参考。
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import datetime

from bottleneck_hunter.watchlist.store import WatchlistStore

logger = logging.getLogger(__name__)


def learn_preferences(store: WatchlistStore) -> dict[str, str]:
    """从用户的确认/拒绝历史归纳偏好，写入 user_preferences 表。

    统计维度:
      - risk_tolerance: 基于接受的交易金额分布 (conservative / moderate / aggressive)
      - position_size_preference: 基于确认的仓位大小 (small / medium / large)
      - sector_preference: 基于确认最多的板块
      - holding_period: 基于平均持仓时长 (short / medium / long)
      - approval_rate: 确认率百分比
      - preferred_action: 偏好的操作方向 (buy / sell / balanced)

    Returns:
        更新后的偏好字典 {key: value}
    """
    preferences: dict[str, str] = {}

    # ── 1. 收集交易和执行计划数据 ──
    trades = store.get_sim_trades(limit=200)
    feedback = store.get_rejection_patterns(limit=200)
    entries = store.list_all()

    entry_sector_map = {e["ticker"]: e.get("sector", "") for e in entries}

    # ── 2. 风险容忍度 (risk_tolerance) ──
    # 基于已确认交易的金额分布
    trade_amounts = [t.get("amount", 0) for t in trades if t.get("amount", 0) > 0]
    if trade_amounts:
        avg_amount = sum(trade_amounts) / len(trade_amounts)
        max_amount = max(trade_amounts)
        # 根据平均交易金额占总资本比例判断
        account = store.get_sim_account()
        total_equity = account.get("total_equity", 100000)
        if total_equity > 0:
            avg_pct = (avg_amount / total_equity) * 100
            if avg_pct >= 15:
                risk_tolerance = "aggressive"
            elif avg_pct >= 8:
                risk_tolerance = "moderate"
            else:
                risk_tolerance = "conservative"
        else:
            risk_tolerance = "moderate"
        preferences["risk_tolerance"] = risk_tolerance
        logger.info("偏好学习 — risk_tolerance: %s (平均交易 %.0f, 最大 %.0f)",
                    risk_tolerance, avg_amount, max_amount)

    # ── 3. 仓位大小偏好 (position_size_preference) ──
    if trade_amounts:
        account = store.get_sim_account()
        total_equity = account.get("total_equity", 100000)
        if total_equity > 0:
            pct_sizes = [(a / total_equity) * 100 for a in trade_amounts]
            avg_pct = sum(pct_sizes) / len(pct_sizes)
            if avg_pct >= 20:
                size_pref = "large"
            elif avg_pct >= 10:
                size_pref = "medium"
            else:
                size_pref = "small"
            preferences["position_size_preference"] = size_pref
            logger.info("偏好学习 — position_size_preference: %s (平均仓位 %.1f%%)", size_pref, avg_pct)

    # ── 4. 板块偏好 (sector_preference) ──
    # 统计确认交易中各板块的频次
    buy_trades = [t for t in trades if t.get("side") == "buy"]
    if buy_trades:
        sector_counter: Counter[str] = Counter()
        for t in buy_trades:
            ticker = t.get("ticker", "")
            sector = entry_sector_map.get(ticker, "")
            if sector:
                sector_counter[sector] += 1
        if sector_counter:
            top_sectors = sector_counter.most_common(3)
            sector_pref = ", ".join(f"{s}({c})" for s, c in top_sectors)
            preferences["sector_preference"] = sector_pref
            logger.info("偏好学习 — sector_preference: %s", sector_pref)

    # ── 5. 持仓周期 (holding_period) ──
    # 从买入和卖出配对中计算平均持仓天数
    sell_trades = [t for t in trades if t.get("side") == "sell"]
    if buy_trades and sell_trades:
        holding_days_list = []
        # 按 ticker 配对买卖交易
        buy_by_ticker: dict[str, list[dict]] = {}
        for t in buy_trades:
            tk = t.get("ticker", "")
            buy_by_ticker.setdefault(tk, []).append(t)

        for sell in sell_trades:
            tk = sell.get("ticker", "")
            buys = buy_by_ticker.get(tk, [])
            if not buys:
                continue
            # 找最近的买入
            buy = buys[0]  # 最近的（已按 created_at DESC 排序）
            try:
                buy_date = datetime.fromisoformat(buy.get("created_at", "")[:19])
                sell_date = datetime.fromisoformat(sell.get("created_at", "")[:19])
                days = (sell_date - buy_date).days
                if days >= 0:
                    holding_days_list.append(days)
            except (ValueError, TypeError):
                continue

        if holding_days_list:
            avg_days = sum(holding_days_list) / len(holding_days_list)
            if avg_days >= 30:
                holding_pref = "long"
            elif avg_days >= 7:
                holding_pref = "medium"
            else:
                holding_pref = "short"
            preferences["holding_period"] = holding_pref
            logger.info("偏好学习 — holding_period: %s (平均 %.0f 天)", holding_pref, avg_days)

    # ── 6. 确认率 (approval_rate) ──
    rejection_count = len(feedback)
    confirmed_count = len(buy_trades) + len(sell_trades)
    total_decisions = confirmed_count + rejection_count
    if total_decisions > 0:
        rate = (confirmed_count / total_decisions) * 100
        preferences["approval_rate"] = f"{rate:.0f}%"
        logger.info("偏好学习 — approval_rate: %.0f%% (%d/%d)", rate, confirmed_count, total_decisions)

    # ── 7. 操作方向偏好 (preferred_action) ──
    if buy_trades or sell_trades:
        buy_count = len(buy_trades)
        sell_count = len(sell_trades)
        if buy_count > sell_count * 2:
            action_pref = "buy"
        elif sell_count > buy_count * 2:
            action_pref = "sell"
        else:
            action_pref = "balanced"
        preferences["preferred_action"] = action_pref
        logger.info("偏好学习 — preferred_action: %s (买 %d, 卖 %d)",
                    action_pref, buy_count, sell_count)

    # ── 8. 写入 user_preferences 表 ──
    for key, value in preferences.items():
        store.save_preference(key, value, category="learned")

    logger.info("偏好学习完成，共更新 %d 项偏好", len(preferences))
    return preferences
