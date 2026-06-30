"""边界情况测试"""
import asyncio
from bottleneck_hunter.data_provider import get_fetcher_manager


async def test():
    mgr = get_fetcher_manager()

    print("=== Individual Fetcher Tests ===")
    df = await mgr.fetch_daily("600519", "a_stock", 3)
    print("efinance daily:", len(df) if df is not None else 0, "rows")

    q = await mgr.fetch_realtime("600519", "a_stock")
    print("efinance realtime: price=", q.price if q else "None")

    df2 = await mgr.fetch_daily("AAPL", "us_stock", 3)
    print("yfinance daily:", len(df2) if df2 is not None else 0, "rows")

    print()
    print("=== Edge Cases ===")

    df3 = await mgr.fetch_daily("INVALID_TICKER_999", "us_stock", 3)
    print("Invalid US ticker:", "OK (None)" if df3 is None else "UNEXPECTED DATA")

    df4 = await mgr.fetch_daily("999999", "a_stock", 3)
    is_empty = df4 is None or df4.empty
    print("Invalid A-stock:", "OK (empty)" if is_empty else str(len(df4)) + " rows")

    df5 = await mgr.fetch_daily("TEST", "hk_stock", 3)
    print("Unknown market:", "OK (None)" if df5 is None else "UNEXPECTED")

    # Test ticker format variations
    print()
    print("=== Ticker Format Tests ===")
    for t in ["600519.SH", "SH600519", "600519", "sh600519"]:
        df = await mgr.fetch_daily(t, "a_stock", 2)
        ok = df is not None and not df.empty
        print(f"  {t}: {'OK' if ok else 'FAIL'}")

    print()
    print("=== Final Status ===")
    for s in mgr.get_status():
        n = s["name"]
        c = s["total_calls"]
        f = s["total_failures"]
        print(f"  {n}: calls={c} fails={f}")


asyncio.run(test())
