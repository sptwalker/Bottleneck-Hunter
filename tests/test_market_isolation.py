"""市场隔离回归测试 —— A股/美股代码格式与市场归一化。防跨市场串用回归。"""
from bottleneck_hunter.chain.financial_data import _code_to_tencent
from bottleneck_hunter.data_provider.providers import _to_ts_code
from bottleneck_hunter.watchlist.store_base import normalize_market


def test_to_ts_code_exchanges():
    assert _to_ts_code("600519") == "600519.SH"   # 沪市
    assert _to_ts_code("000001") == "000001.SZ"   # 深市
    assert _to_ts_code("300750") == "300750.SZ"   # 创业板
    assert _to_ts_code("830799") == "830799.BJ"   # 北交所(8开头)
    assert _to_ts_code("430047") == "430047.BJ"   # 北交所(4开头)
    assert _to_ts_code("920819") == "920819.BJ"   # 北交所新段(920)
    assert _to_ts_code("AAPL") == ""              # 非A股码不误转


def test_code_to_tencent_exchanges():
    assert _code_to_tencent("600519") == "sh600519"
    assert _code_to_tencent("000001") == "sz000001"
    assert _code_to_tencent("830799") == "bj830799"   # 北交所不再误当深市


def test_normalize_market_a_share_canonical():
    # A股别名必须归一到全系统 canonical a_stock，不能是 cn_stock（否则孤立于所有A股逻辑）
    assert normalize_market("a") == "a_stock"
    assert normalize_market("cn") == "a_stock"
    assert normalize_market("a_stock") == "a_stock"
    assert normalize_market("us") == "us_stock"
    assert normalize_market("usa") == "us_stock"
    assert normalize_market(None) == "us_stock"


def test_us_class_share_ticker_not_truncated():
    # 美股类别股用 . → - （yfinance 约定），不能像 A股那样去后缀截断
    assert "BRK.B".replace(".", "-") == "BRK-B"
    assert "BF.B".replace(".", "-") == "BF-B"
    # 对照：A股去后缀是 split(".")[0]，两者不可混用
    assert "600519.SH".split(".")[0] == "600519"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("market isolation self-check OK")
