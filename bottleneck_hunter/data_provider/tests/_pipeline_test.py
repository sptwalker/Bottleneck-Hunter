import asyncio
from bottleneck_hunter.watchlist.price_pipeline import _fetch_via_manager


async def test():
    result = await _fetch_via_manager("600519", 180, "a_stock")
    if result:
        print("Snapshots:", len(result))
        last = result[-1]
        print("Last snap keys:", list(last.keys()))
        print("RSI:", last.get("rsi_14"))
        print("SMA20:", last.get("sma_20"))
        print("SMA50:", last.get("sma_50"))
        print("PE:", last.get("pe_ratio"))
        print("MarketCap:", last.get("market_cap"))
        print("MACD:", last.get("macd"))
        print("Close:", last.get("close"))
        print("Date:", last.get("date"))
    else:
        print("FAIL: no snapshots")


asyncio.run(test())
