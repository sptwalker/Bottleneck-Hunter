"""Task B: 宏观 FRED 优先 —— 验证 FRED 先跑填满其覆盖的 key，yfinance 只补 FRED 缺的。

根因：国内直连 yfinance(Yahoo) 必失败，FRED 借道白名单可靠。故 vix/us_10y_yield/dxy 等
同 key 由 FRED 优先，yfinance 对已被 FRED 填的 key 不再重复打（省 Yahoo 限流预算）。
"""
import asyncio

from bottleneck_hunter.watchlist import macro_data as md


class _FakeStore:
    def save_macro_snapshot(self, *a, **k):
        pass

    def get_latest_macro_snapshots(self):
        return []   # 库空 → 同日短路不触发，走正常 FRED/yfinance 抓取路径


def test_fred_primary_yfinance_skips_covered(monkeypatch):
    yf_called = []

    def fake_yf(symbol):
        yf_called.append(symbol)
        return {"value": 999.0, "change_pct": 0.0}   # yfinance 若被调到 vix，会用这个假值

    async def fake_fred(extra=None):
        return {"vix": {"value": 18.5, "change_pct": -2.1, "label": "VIX 恐慌指数"}}

    monkeypatch.setattr(md, "_fetch_yf_quote", fake_yf)
    monkeypatch.setattr(md, "_fetch_fred_indicators", fake_fred)

    result = asyncio.run(md.fetch_macro_data(_FakeStore(), markets=["us_stock"]))

    # FRED 值胜出（不是 yfinance 的 999）
    assert result["vix"]["value"] == 18.5, result["vix"]
    # yfinance 从未对 vix 的 ^VIX 触发（if key in results: return 生效）
    assert "^VIX" not in yf_called, f"vix 应由 FRED 填、yfinance 不该再打 ^VIX，但被调了: {yf_called}"
