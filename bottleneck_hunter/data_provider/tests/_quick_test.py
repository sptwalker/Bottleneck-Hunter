import asyncio
from bottleneck_hunter.data_provider import get_fetcher_manager


async def test():
    m = get_fetcher_manager()

    print("=== A-stock daily (600519) ===")
    df = await m.fetch_daily("600519", "a_stock", 5)
    if df is not None:
        print(f"OK: {len(df)} rows, columns={list(df.columns)}")
        print(df.tail(2).to_string())
    else:
        print("FAIL: no data")

    print()
    print("=== A-stock realtime (600519) ===")
    q = await m.fetch_realtime("600519", "a_stock")
    if q:
        print(f"OK: price={q.price}, PE={q.pe_ratio}, turnover={q.turnover_rate}, source={q.source}")
    else:
        print("FAIL: no quote")

    print()
    print("=== Status ===")
    for s in m.get_status():
        name = s["name"]
        calls = s["total_calls"]
        fails = s["total_failures"]
        print(f"  {name}: calls={calls}, fails={fails}")


asyncio.run(test())
