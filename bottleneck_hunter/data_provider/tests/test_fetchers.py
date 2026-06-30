"""快速验证三个新数据源的可用性"""
import sys

# === 1. efinance ===
print("=" * 50)
print("Testing efinance (A-stock)...")
try:
    import efinance as ef
    df = ef.stock.get_quote_history("600519", klt=101)
    if df is not None and not df.empty:
        print(f"  OK: {len(df)} rows")
        print(f"  Columns: {list(df.columns)}")
        print(f"  Latest row: {df.iloc[-1].to_dict()}")
    else:
        print("  FAIL: empty result")
except Exception as e:
    print(f"  ERROR: {type(e).__name__}: {e}")

print()

# === 2. efinance realtime ===
print("Testing efinance realtime quotes...")
try:
    df_rt = ef.stock.get_realtime_quotes(["600519"])
    if df_rt is not None and not df_rt.empty:
        print(f"  OK: {len(df_rt)} rows")
        print(f"  Columns: {list(df_rt.columns)}")
        row = df_rt.iloc[0]
        print(f"  600519: price={row.get('最新价')}, PE={row.get('市盈率')}, PB={row.get('市净率')}")
        print(f"  volume_ratio={row.get('量比')}, turnover={row.get('换手率')}")
    else:
        print("  FAIL: empty result")
except Exception as e:
    print(f"  ERROR: {type(e).__name__}: {e}")

print()

# === 3. pytdx ===
print("=" * 50)
print("Testing pytdx (A-stock realtime)...")
try:
    from pytdx.hq import TdxHq_API
    api = TdxHq_API()
    hosts = [
        ("119.147.212.81", 7709),
        ("218.75.126.9", 7709),
        ("115.238.90.165", 7709),
    ]
    connected = False
    for host, port in hosts:
        try:
            api.connect(host, port)
            connected = True
            print(f"  Connected to {host}:{port}")
            break
        except Exception:
            continue
    if connected:
        data = api.get_security_quotes([(0, "000001"), (1, "600519")])
        if data:
            for item in data:
                print(f"  {item['code']}: price={item.get('price')}, vol={item.get('vol')}")
            print(f"  OK: {len(data)} quotes")
        else:
            print("  FAIL: no quote data")
        api.disconnect()
    else:
        print("  FAIL: cannot connect to any TDX server")
except Exception as e:
    print(f"  ERROR: {type(e).__name__}: {e}")

print()

# === 4. finnhub ===
print("=" * 50)
print("Testing finnhub (US-stock)...")
try:
    import os
    api_key = os.environ.get("FINNHUB_API_KEY", "")
    if not api_key:
        from dotenv import load_dotenv
        load_dotenv()
        api_key = os.environ.get("FINNHUB_API_KEY", "")

    if not api_key:
        print("  SKIP: FINNHUB_API_KEY not configured in .env")
        print("  (Free key available at https://finnhub.io/register)")
    else:
        import finnhub
        client = finnhub.Client(api_key=api_key)
        quote = client.quote("AAPL")
        if quote and quote.get("c", 0) > 0:
            print(f"  OK: AAPL current={quote['c']}, high={quote['h']}, low={quote['l']}")
        else:
            print(f"  FAIL: empty quote: {quote}")

        financials = client.company_basic_financials("AAPL", "all")
        metrics = financials.get("metric", {})
        if metrics:
            print(f"  Financials OK: PE={metrics.get('peBasicExclExtraTTM')}, PB={metrics.get('pbQuarterly')}")
        else:
            print("  Financials: no metric data")
except Exception as e:
    print(f"  ERROR: {type(e).__name__}: {e}")

print()
print("=" * 50)
print("Data source testing complete.")
