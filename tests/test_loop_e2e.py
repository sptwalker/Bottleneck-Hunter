"""Phase 1 验收：闭环端到端 —— 买入→卖出真实落库，realized_pnl 持久化，卖出进入待复盘队列。

对应改进方案 1.1（realized_pnl 持久化）、1.2（闭环走通）、1.3（自动复盘触发）。
用真实临时 DB（非 mock），证明"数据真的产生"而非"代码能跑"。

运行：pytest tests/test_loop_e2e.py -q
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

from bottleneck_hunter.watchlist.store import WatchlistStore
from bottleneck_hunter.watchlist.constraint_validator import ValidationResult
from bottleneck_hunter.watchlist import trade_executor


def _seed_snapshot(store, ticker, close):
    store.save_snapshots([{
        "ticker": ticker, "date": "2026-07-02", "close": close,
        "open": close, "high": close, "low": close, "volume": 1_000_000,
        "market": "us_stock",
    }])


def _make_plan(store, ticker, action, shares, price):
    return store.create_execution_plan(
        tactical_plan_id="", entry_id="",
        ticker=ticker,
        result_json={"action": action, "shares": shares,
                     "target_price": price, "reasoning": "e2e test"},
        status="pending",
    )


def test_buy_sell_loop_persists_realized_pnl():
    """完整闭环：买入建仓 → 卖出平仓 → realized_pnl 落库 → 卖出交易进待复盘队列。"""
    with tempfile.TemporaryDirectory() as d:
        db = str(Path(d) / "loop.db")
        store = WatchlistStore(db_path=db)  # 触发建表 + 迁移
        store = store.for_market("us_stock")

        _seed_snapshot(store, "TEST", 100.0)
        acct = store.get_sim_account()  # 自动创建
        # 保证有足够现金
        store.update_sim_account(cash_balance=1_000_000, initial_capital=1_000_000)

        # 约束校验放行 + 自动复盘替身（隔离 LLM 依赖，仅验证数据闭环）
        review_calls = []

        async def _fake_review(store_, trade_id):
            review_calls.append(trade_id)

        with patch("bottleneck_hunter.watchlist.constraint_validator.validate_execution_plan",
                   lambda *a, **k: ValidationResult()), \
             patch.object(trade_executor, "_auto_review_sell", _fake_review):
            # 1) 买入 50 股 @100
            buy_plan = _make_plan(store, "TEST", "buy", 50, 100.0)
            r_buy = trade_executor.execute_trade(store, buy_plan)
            assert r_buy.get("side") == "buy", r_buy

            # 2) 价格涨到 120，卖出 50 股
            _seed_snapshot(store, "TEST", 120.0)
            sell_plan = _make_plan(store, "TEST", "sell", 50, 120.0)
            r_sell = trade_executor.execute_trade(store, sell_plan)
            assert r_sell.get("side") == "sell", r_sell

        # ── 验收 1：realized_pnl 已计算且为正（120-100)*50 - 手续费 ≈ +986 ──
        assert r_sell["realized_pnl"] > 900, r_sell["realized_pnl"]

        # ── 验收 2：realized_pnl 真的落库（不是只在返回值里）──
        trades = store.get_sim_trades(limit=10)
        sells = [t for t in trades if t.get("side") == "sell"]
        assert len(sells) == 1
        assert sells[0]["realized_pnl"] is not None
        assert sells[0]["realized_pnl"] > 900

        # ── 验收 3：卖出交易进入"待复盘"队列（闭环下一环有数据可复盘）──
        assert review_calls == [sells[0]["id"]], "卖出未触发自动复盘调度"

        # ── 验收 4：账户 win_rate 用 realized_pnl 算出 100%（1 胜 0 负）──
        acct2 = store.get_sim_account()
        assert acct2["win_rate"] == 100.0, acct2["win_rate"]


def test_loss_trade_records_negative_pnl():
    """亏损卖出：realized_pnl 为负，win_rate 反映为 0%。"""
    with tempfile.TemporaryDirectory() as d:
        db = str(Path(d) / "loop.db")
        store = WatchlistStore(db_path=db).for_market("us_stock")
        _seed_snapshot(store, "LOSE", 100.0)
        store.get_sim_account()
        store.update_sim_account(cash_balance=1_000_000, initial_capital=1_000_000)

        async def _noop(store_, tid):
            pass

        with patch("bottleneck_hunter.watchlist.constraint_validator.validate_execution_plan",
                   lambda *a, **k: ValidationResult()), \
             patch.object(trade_executor, "_auto_review_sell", _noop):
            trade_executor.execute_trade(store, _make_plan(store, "LOSE", "buy", 50, 100.0))
            _seed_snapshot(store, "LOSE", 80.0)
            r = trade_executor.execute_trade(store, _make_plan(store, "LOSE", "sell", 50, 80.0))

        assert r["realized_pnl"] < 0, r["realized_pnl"]
        acct = store.get_sim_account()
        assert acct["win_rate"] == 0.0


if __name__ == "__main__":
    test_buy_sell_loop_persists_realized_pnl()
    test_loss_trade_records_negative_pnl()
    print("PASS: loop e2e")
