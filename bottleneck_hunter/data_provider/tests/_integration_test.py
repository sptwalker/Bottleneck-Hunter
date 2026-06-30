"""集成验证：测试 FetcherManager 在 price_pipeline 和 financial_data 中的工作。"""
import asyncio
import logging

logging.basicConfig(level=logging.INFO, format="%(name)s | %(message)s")

from bottleneck_hunter.data_provider import get_fetcher_manager


async def test_integration():
    mgr = get_fetcher_manager()

    print("=" * 60)
    print("1. FetcherManager 状态")
    for s in mgr.get_status():
        print(f"   {s['name']}: priority={s['priority']}, markets={s['markets']}")

    print()
    print("=" * 60)
    print("2. A股日K线 (600519 via FetcherManager)")
    df = await mgr.fetch_daily("600519", "a_stock", 5)
    if df is not None and not df.empty:
        print(f"   OK: {len(df)} rows, cols={list(df.columns)}")
    else:
        print("   FAIL")

    print()
    print("3. A股实时行情 (600519)")
    q = await mgr.fetch_realtime("600519", "a_stock")
    if q:
        print(f"   OK: price={q.price}, PE={q.pe_ratio}, turnover={q.turnover_rate}, src={q.source}")
    else:
        print("   FAIL")

    print()
    print("4. 美股日K线 (AAPL via FetcherManager)")
    df2 = await mgr.fetch_daily("AAPL", "us_stock", 5)
    if df2 is not None and not df2.empty:
        print(f"   OK: {len(df2)} rows, cols={list(df2.columns)}")
    else:
        print("   FAIL (yfinance may be slow)")

    print()
    print("5. financial_data.fetch_kline (A股 via FetcherManager)")
    from bottleneck_hunter.chain.financial_data import fetch_kline
    kline = await fetch_kline("600519", "a_stock")
    if kline:
        print(f"   OK: {len(kline)} bars, last={kline[-1]}")
    else:
        print("   FAIL")

    print()
    print("6. 降级测试: 模拟 efinance 熔断后使用 akshare")
    efinance_state = mgr._states.get("efinance")
    if efinance_state:
        import time
        efinance_state.fail_count = 5
        efinance_state.last_fail_time = time.time()
        print(f"   efinance circuit_open={efinance_state.is_circuit_open}")
        df3 = await mgr.fetch_daily("600519", "a_stock", 3)
        if df3 is not None and not df3.empty:
            print(f"   OK: 降级成功, {len(df3)} rows")
        else:
            print("   FAIL: 降级失败")
        # 恢复
        efinance_state.fail_count = 0

    print()
    print("=" * 60)
    print("7. 最终状态")
    for s in mgr.get_status():
        name = s["name"]
        calls = s["total_calls"]
        fails = s["total_failures"]
        circ = s["circuit_open"]
        print(f"   {name}: calls={calls}, fails={fails}, circuit_open={circ}")

    print()
    print("集成验证完成!")


asyncio.run(test_integration())
