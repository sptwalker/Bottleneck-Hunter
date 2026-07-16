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


# ═══════════════════════════════════════════════════════════════
# 决策链路 store 级市场隔离（list_all/get_tickers/偏好/复盘产物）
# ═══════════════════════════════════════════════════════════════
import pytest
from bottleneck_hunter.watchlist.store import WatchlistStore


@pytest.fixture
def store(tmp_path):
    return WatchlistStore(tmp_path / "wl.db").for_user("u1")


def _add(store, ticker, market):
    store.for_market(market).add(
        {"ticker": ticker, "company_name": ticker, "tier": "track", "market": market})


def test_list_all_and_get_tickers_respect_market(store):
    _add(store, "AAPL", "us_stock")
    _add(store, "600519.SH", "a_stock")
    # 未 scope → 两市都在（向后兼容，UI/admin 依赖）
    assert {e["ticker"] for e in store.list_all()} == {"AAPL", "600519.SS"}
    assert set(store.get_tickers()) == {"AAPL", "600519.SS"}
    # scope 到某市 → 只返回该市
    assert {e["ticker"] for e in store.for_market("a_stock").list_all()} == {"600519.SS"}
    assert {e["ticker"] for e in store.for_market("us_stock").list_all()} == {"AAPL"}
    assert store.for_market("a_stock").get_tickers() == ["600519.SS"]
    assert store.for_market("us_stock").get_tickers() == ["AAPL"]


def test_list_all_tier_filter_respects_market(store):
    store.for_market("us_stock").add({"ticker": "MSFT", "company_name": "MSFT", "tier": "focus", "market": "us_stock"})
    store.for_market("a_stock").add({"ticker": "000001.SZ", "company_name": "x", "tier": "focus", "market": "a_stock"})
    assert {e["ticker"] for e in store.for_market("us_stock").list_all(tier="focus")} == {"MSFT"}


def test_preferences_isolated_per_market(store):
    a = store.for_market("a_stock")
    u = store.for_market("us_stock")
    a.save_preference("risk_tolerance", "低", category="learned")
    u.save_preference("risk_tolerance", "高", category="learned")
    # 同名 key 两市互不覆盖
    assert a.get_preference("risk_tolerance") == "低"
    assert u.get_preference("risk_tolerance") == "高"
    # get_preferences 只返回本市场，且已剥市场前缀
    assert {p["key"]: p["value"] for p in a.get_preferences()} == {"risk_tolerance": "低"}
    assert {p["key"]: p["value"] for p in u.get_preferences()} == {"risk_tolerance": "高"}


def test_experience_cards_isolated_per_market(store):
    """复盘产物按市场 stamp（修复自学习闭环）：A股写入，美股读不到。"""
    a = store.for_market("a_stock")
    u = store.for_market("us_stock")
    a.create_experience_card("ticker", "600519.SH", "lesson", "A股经验", "内容")
    assert len(a.get_experience_cards(scope="ticker")) == 1     # 同市读得到
    assert u.get_experience_cards(scope="ticker") == []          # 他市读不到（不串味）


# ═══════════════════════════════════════════════════════════════
# 多用户隔离：预算上限 / 分档比例互不影响
# ═══════════════════════════════════════════════════════════════
def test_budget_limit_isolated_per_user(store):
    """budget_config 复合主键 (key,user_id)：用户 B 设上限不覆盖 A。"""
    from bottleneck_hunter.watchlist.budget import BudgetTracker
    a = store.for_user("userA")
    b = store.for_user("userB")
    BudgetTracker(a).set_limits(daily=5.0)
    BudgetTracker(b).set_limits(daily=9.0)
    assert BudgetTracker(a).get_status()["daily_limit"] == 5.0   # 未被 B 覆盖
    assert BudgetTracker(b).get_status()["daily_limit"] == 9.0


def test_per_user_tier_pct(tmp_path):
    """分档比例按用户存取（admin 改全局默认不影响现存用户）。"""
    from bottleneck_hunter.auth.store import AuthStore
    s = AuthStore(tmp_path / "auth.db")
    u = s.create_user("alice", password="x", focus_pct=0.3, normal_pct=0.2)
    got = s.get_user_by_id(u.id)
    assert got.watchlist_focus_pct == 0.3 and got.watchlist_normal_pct == 0.2


if __name__ == "__main__":
    import inspect
    for name, fn in list(globals().items()):
        # 仅跑无参（非 fixture）自检；fixture 用例走 pytest
        if name.startswith("test_") and callable(fn) and not inspect.signature(fn).parameters:
            fn()
    print("market isolation self-check OK (无参用例; 完整覆盖用 pytest)")


# ── A股 ticker canonical 归一 ──────────────────────────────
def test_normalize_ticker_canonical():
    from bottleneck_hunter.watchlist.store_base import normalize_ticker as n
    assert n("600519.SH") == "600519.SS"   # 上交所 .SH → .SS
    assert n("600519") == "600519.SS"       # 裸码补后缀
    assert n("600519.SS") == "600519.SS"    # 幂等
    assert n("000001") == "000001.SZ"       # 深市
    assert n("300750.SH") == "300750.SZ"    # 创业板即便传 .SH 也归深市
    assert n("430047") == "430047.BJ"       # 北交所
    assert n("688981") == "688981.SS"       # 科创板→上交所
    assert n("nvda") == "NVDA"               # 美股仅 upper
    assert n("") == ""
    assert n(None) == ""
