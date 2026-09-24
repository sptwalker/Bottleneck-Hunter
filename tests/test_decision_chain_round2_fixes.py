"""第二轮复核四件套的回归守卫（同批：P0-F / P1-K / P1-L / P2-I）。

夹具按**生产实测**还原，非编造：
- 账户 CMBIS：一个零股票持仓的纯 FCN 账户（`positions` 从不写行），6 份结单按真实上传顺序喂入
  （07-31 → 06-30 → 05-29 → 07-07 → 05-26 → **04-30 最后**），期次与 file_hash 均取自生产库。
- 该账户的衍生品条款 5 行：3 行 NVDA 跨 3 期（同 lot_key）+ 2 行 04-30 的 CMBIGP（到期 2026-05-15，
  05-29 结单已列出卖出）。修复前 `MAX(created_at)` 选中"最后上传"的 04-30/07-07 两份 → MTM 2,099,173.74；
  真值 1,047,068.00。两个数字都与生产逐位一致。
"""
import pytest

from bottleneck_hunter.vip import portfolio
from bottleneck_hunter.vip.ingest import BrokerStatement, EquityHolding, ReconResult
from bottleneck_hunter.watchlist.store import WatchlistStore

# ── 生产 CMBIS 的真实结单期次 ↔ content_hash（只读实测取得）────────────────
_P0731 = "527fc7b7fbc8"
_P0630 = "c4843d6a2007"
_P0529 = "0d56f8a1a653"
_P0707 = "c138154dc734"
_P0526 = "1e836a03d4e7"
_P0430 = "b70ce6127615"

# (期次, hash, 权威总权益, 现金) —— trade_confirm 两期无权威锚(total_equity=None)
_CMBIS_STMTS = [
    ("2026-07-31", _P0731, 1063096.50, 16028.50),
    ("2026-06-30", _P0630, 1057266.50, None),
    ("2026-05-29", _P0529, None, None),
    ("2026-07-07", _P0707, 1057902.50, None),
    ("2026-05-26", _P0526, None, None),
    ("2026-04-30", _P0430, 1065337.14, 8037.40),
]

# (期次, hash, 标的, lot_key, MTM, maturity) —— 上表顺序即生产 created_at 顺序
_CMBIS_TERMS = [
    ("2026-04-30", _P0430, "CMBIGP", "S20250515S917HKD:2026-05-15", 276226.82, "2026-05-15"),
    ("2026-04-30", _P0430, "CMBIGP", "S20250515S916USD:2026-05-15", 781072.92, "2026-05-15"),
    ("2026-06-30", _P0630, "NVDA", "XS3372957897:2026-09-04", 1051838.00, "2026-09-04"),
    ("2026-07-07", _P0707, "NVDA", "XS3372957897:2026-09-04", 1041874.00, "2026-09-04"),
    ("2026-07-31", _P0731, "NVDA", "XS3372957897:2026-09-04", 1047068.00, "2026-09-04"),
]

_OLD_BUGGY_TOTAL = 2099173.74   # 修复前生产实测值
_TRUE_TOTAL = 1047068.00        # 07-31 期真值


@pytest.fixture
def wl(tmp_path, monkeypatch):
    from bottleneck_hunter.auth import store as auth_store_mod

    monkeypatch.setattr(auth_store_mod, "_DEFAULT_DB", tmp_path / "auth.db")
    return WatchlistStore(tmp_path / "wl.db").for_user("u1").for_market("us_stock")


def _deriv_stmt(period_end, content_hash, *, mv=0.0, cash=0.0):
    """纯 FCN 账户的一期结单：零股票持仓（这正是两条旧护栏同时失效的前提）。"""
    return BrokerStatement(content_hash=content_hash, period_end=period_end, holdings=[],
                           cash_balances=[], total_cash_usd=cash,
                           account_summary={"total_value_usd": mv} if mv else {},
                           recon=ReconResult(holdings_count=0, holdings_total_usd=0.0,
                                             statement_equities_total_usd=mv or None,
                                             delta_usd=None, status="ok"))


def _import(wl, period_end, content_hash, *, mv=0.0, cash=0.0):
    """走真实导入链路（normalize → materialize → create_vip_import），顺序与生产一致。"""
    stmt = _deriv_stmt(period_end, content_hash, mv=mv, cash=cash)
    portfolio.normalize_statement(wl, stmt, account_ref="CMBIS")
    wl.ensure_vip_account("CMBIS")
    mat = portfolio.materialize_portfolio(wl, as_of_date=period_end, account_ref="CMBIS",
                                          cash_total_usd=cash, account_total_usd=mv or None)
    wl.create_vip_import(file_name=f"M-{period_end}", file_hash=content_hash, file_type="pdf",
                         detected_kind="monthly_statement", status="imported",
                         key_metrics={"period_end": period_end,
                                      **({"total_equity": mv} if mv else {})},
                         account_ref="CMBIS")
    return mat


def _save_terms(wl, rows):
    """按给定顺序写入条款行（created_at 由 save 现场取 _now_iso，故顺序即 created_at 顺序）。"""
    from bottleneck_hunter.vip.derivatives import DerivativeTerm, save_derivative_term

    for _pe, h, sym, lot, mv, mat in rows:
        save_derivative_term(wl, DerivativeTerm("equity_fcn", sym, "USD", 60,
                                                {"market_value_usd": mv, "maturity": mat}),
                             source_file_name=f"stmt-{h}", source_file_hash=h, broker="cmbi",
                             account_ref="CMBIS", lot_key=lot)


# ── P1-K：零持仓账户的净值必须由「结单期次」决定，而非「上传顺序」──────────────

def test_zero_holding_account_stale_upload_blocked(wl):
    """P1-K：真实上传顺序（最旧一期最后上传）下，末值必须是 07-31 期真值，而非 04-30 期的值。
    修复前必红：末值 1,065,337.14（= 04-30 期真值覆盖了当前生效值）。"""
    last = None
    for pe, h, mv, cash in _CMBIS_STMTS:
        last = _import(wl, pe, h, mv=mv or 0.0, cash=cash or 0.0)
        wl.update_sim_account(account_ref="CMBIS",
                              total_equity=last["total_equity"]) if last["total_equity"] else None

    acct = wl.get_sim_account(account_ref="CMBIS")
    assert acct["total_equity"] == pytest.approx(1063096.50), \
        f"最旧一期(04-30)不该覆盖当前生效值；得到 {acct['total_equity']}"
    # 且不是静默跳过：04-30 这份必须带可读原因
    assert last["guard_skipped"] == "stale_snapshot:2026-04-30"


def test_stale_reason_is_not_silent(wl):
    """P1-K 验证 2：旧单晚传被拒时**留下可读原因**，不是静默跳过。"""
    _import(wl, "2026-07-31", _P0731, mv=1063096.50, cash=16028.50)
    stale = _import(wl, "2026-04-30", _P0430, mv=1065337.14, cash=8037.40)
    assert stale["guard_skipped"].startswith("stale_snapshot:")
    assert stale["guard_skipped"].endswith("2026-04-30")
    # 历史期仍进 vip_imports 作历史锚（曲线口径不受影响）
    assert {r["key_metrics"].get("period_end") for r in wl.list_vip_imports(account_ref="CMBIS")} \
        == {"2026-07-31", "2026-04-30"}


def test_forward_order_and_equal_period_not_blocked(wl):
    """P1-K 回归：正序上传(旧→新)不受影响；同一份文件重导(期次相等)不自锁。"""
    fwd = _import(wl, "2026-06-30", _P0630, mv=1057266.50)
    assert not fwd.get("guard_skipped")
    newer = _import(wl, "2026-07-31", _P0731, mv=1063096.50, cash=16028.50)
    assert not newer.get("guard_skipped")
    redo = _import(wl, "2026-07-31", _P0731, mv=1063096.50, cash=16028.50)
    assert not redo.get("guard_skipped"), "重导同一期不得被自己的期次挡下"


def test_account_with_holdings_unchanged(wl):
    """P1-K 回归 3：有持仓账户的正常新一期仍照常落库（护栏没被拧得过紧）。"""
    holds = [EquityHolding(ticker="GOOGL", company="Alphabet Inc", quantity=100,
                           market_value_usd=200000.0, nominal_ccy="USD", market_value_nominal=200000.0)]
    stmt = BrokerStatement(content_hash="norm-1", period_end="2026-07-31", holdings=holds,
                           cash_balances=[], total_cash_usd=1000.0,
                           recon=ReconResult(holdings_count=1, holdings_total_usd=200000.0,
                                             statement_equities_total_usd=200000.0, delta_usd=0.0, status="ok"))
    portfolio.normalize_statement(wl, stmt, account_ref="A1")
    wl.ensure_vip_account("A1")
    mat = portfolio.materialize_portfolio(wl, as_of_date="2026-07-31", account_ref="A1", cash_total_usd=1000.0)
    assert not mat.get("guard_skipped")
    assert mat["n_positions"] == 1
    assert mat["total_equity"] == pytest.approx(201000.0)


# ── P1-L：衍生品当期条款按「结单期次」选，且到期条款不并入 ────────────────────

def test_current_derivative_rows_picks_by_period_not_upload_order(wl):
    """P1-L 验证 1：5 行条款（3 期 NVDA + 2 笔已到期 CMBIGP）→ 当期须选中 07-31 期 NVDA。
    修复前必红：得 2,099,173.74（= 04-30 两笔 + 07-07 的 NVDA）。"""
    for pe, h, mv, cash in _CMBIS_STMTS:
        _import(wl, pe, h, mv=mv or 0.0, cash=cash or 0.0)
    _save_terms(wl, _CMBIS_TERMS)

    rows = portfolio._current_derivative_rows(wl, "CMBIS")
    assert [(r["underlying_symbol"], r["lot_key"]) for r in rows] == [("NVDA", "XS3372957897:2026-09-04")]
    assert portfolio._derivative_mtm_total(wl, "CMBIS") == pytest.approx(_TRUE_TOTAL)
    assert portfolio._derivative_mtm_total(wl, "CMBIS") != pytest.approx(_OLD_BUGGY_TOTAL)


def test_matured_lots_excluded_from_holdings(wl):
    """P1-L 验证 2：两笔已到期 CMBIGP 不出现在持仓构成里（到期日早于账户最新期次）。"""
    for pe, h, mv, cash in _CMBIS_STMTS:
        _import(wl, pe, h, mv=mv or 0.0, cash=cash or 0.0)
    _save_terms(wl, _CMBIS_TERMS)

    tickers = [h["ticker"] for h in portfolio._derivative_holdings(wl, "CMBIS")]
    assert tickers == ["NVDA·结构性"], f"已到期的 CMBIGP 不该并入构成；得到 {tickers}"


def test_still_live_lot_kept_even_if_maturity_soon(wl):
    """P1-L 关键回归：判据基准是**账户最新期次**而非"今天"。
    07-31 期仍在册的 NVDA 到期 2026-09-04（相对今天已过）必须保留，否则账户 MTM 被清成 0。"""
    _import(wl, "2026-07-31", _P0731, mv=1063096.50, cash=16028.50)
    _save_terms(wl, [("2026-07-31", _P0731, "NVDA", "XS3372957897:2026-09-04", 1047068.00, "2026-09-04")])

    assert portfolio._derivative_mtm_total(wl, "CMBIS") == pytest.approx(1047068.00)


def test_unassociated_and_non_iso_rows_kept(wl):
    """P1-L 保守性：关联不到结单的历史条款、以及非 ISO 到期日(野村旧版式)一律保留，不臆断失效。"""
    _import(wl, "2026-07-31", _P0731, mv=1063096.50, cash=16028.50)
    from bottleneck_hunter.vip.derivatives import DerivativeTerm, save_derivative_term

    save_derivative_term(wl, DerivativeTerm("equity_fcn", "AAA", "USD", 60,
                                            {"market_value_usd": 100.0, "maturity": "2099-12-31"}),
                         source_file_name="termsheet_aaa.pdf", source_file_hash="no-such-stmt",
                         broker="cmbi", account_ref="CMBIS", lot_key="AAA:2099-12-31")
    save_derivative_term(wl, DerivativeTerm("equity_accumulator", "BBB", "USD", 365,
                                            {"market_value_usd": 200.0, "maturity": "15.05.2026"}),
                         source_file_name="termsheet_bbb.pdf", source_file_hash="no-such-stmt",
                         broker="cmbi", account_ref="CMBIS", lot_key="BBB:15052026")

    got = {r["underlying_symbol"] for r in portfolio._current_derivative_rows(wl, "CMBIS")}
    assert got == {"AAA", "BBB"}


def test_indicative_still_excluded(wl):
    """P1-L 不放松既有过滤：推介稿(is_indicative=1)仍不得进入当期条款。"""
    _import(wl, "2026-07-31", _P0731, mv=1063096.50, cash=16028.50)
    from bottleneck_hunter.vip.derivatives import DerivativeTerm, save_derivative_term

    save_derivative_term(wl, DerivativeTerm("equity_fcn", "PLTR", "USD", 60,
                                            {"market_value_usd": 999999.0, "maturity": "2099-12-31"}),
                         source_file_name="Indicative Terms PLTR.pdf", source_file_hash=_P0731,
                         broker="cmbi", account_ref="CMBIS", lot_key="PLTR:2099-12-31",
                         is_indicative=True)
    assert portfolio._current_derivative_rows(wl, "CMBIS") == []


# ── P0-F：created_at 必须走 UTC（与全库同口径）────────────────────────────────

def test_save_derivative_term_writes_utc(wl):
    """P0-F：created_at 曾是全库唯一的裸本地时间写入(比 UTC 早 8 小时)，而它是「哪条条款更新」的排序键。
    判据二：与同一次导入的 vip_imports.created_at 相差 < 5 秒(修复前相差 8 小时，必红)。"""
    from datetime import datetime, timedelta, timezone

    _import(wl, "2026-07-31", _P0731, mv=1063096.50, cash=16028.50)
    _save_terms(wl, [("2026-07-31", _P0731, "NVDA", "XS3372957897:2026-09-04", 1.0, "2026-09-04")])
    conn = wl._connect()
    try:
        ts = conn.execute("SELECT created_at FROM vip_derivative_terms").fetchone()["created_at"]
        imp_ts = conn.execute("SELECT created_at FROM vip_imports").fetchone()["created_at"]
    finally:
        conn.close()

    parsed = datetime.fromisoformat(ts)
    assert parsed.tzinfo is not None, f"未带时区，无法判口径: {ts}"
    # 直接断言偏移为零(UTC)。裸本地时间在北京为 +08:00，此断言在修复前必红。
    assert parsed.utcoffset() == timedelta(0), f"非 UTC 偏移: {ts}"
    assert abs((datetime.now(timezone.utc) - parsed).total_seconds()) < 120, f"时间戳明显偏离当前时刻: {ts}"
    # 与同一次导入的 vip_imports 时间戳同口径 → 相差秒级(修复前相差 8 小时)
    assert abs((parsed - datetime.fromisoformat(imp_ts)).total_seconds()) < 5, \
        f"条款与导入时间戳口径不一致: {ts} vs {imp_ts}"


# ── P2-I：墓碑行不得让「同一票两行都算市值」──────────────────────────────────

def test_get_sim_position_any_prefers_live_row_over_tombstone(wl):
    """P2-I：物化把旧持仓清零为墓碑(shares=0)而不删，同票可并存「墓碑 + 活行」；
    无 ORDER BY 时 fetchone 命中哪行由 SQLite 行序决定 → _execute_buy 复用墓碑后两行都 shares>0。

    墓碑**先**插入（行序在前），活行后插入 —— 这正是生产上命中墓碑的形态。"""
    wl.ensure_vip_account("A1")
    acct = wl.get_sim_account(account_ref="A1")
    aid = acct["id"]
    with wl._write_conn() as conn:
        for i, (shares, avg) in enumerate([(0, 200.0), (200, 150.0)]):
            conn.execute(
                "INSERT INTO sim_positions (id, account_id, ticker, shares, avg_cost, current_price,"
                " market_value, unrealized_pnl, weight_pct, opened_at, updated_at, user_id, market)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"p{i}", aid, "AAPL", shares, avg, avg, shares * avg, 0.0, 0.0,
                 "2026-07-01T00:00:00+00:00", "2026-07-01T00:00:00+00:00", "u1", "us_stock"))

    row = wl.get_sim_position_any(aid, "AAPL")
    assert row is not None
    assert row["shares"] == 200, f"必须命中活行，得到 shares={row['shares']}（墓碑）"
    assert row["id"] == "p1"


def test_get_sim_position_ignores_tombstone(wl):
    """P2-I：get_sim_position 只认活仓，墓碑不该被当作持仓返回。"""
    wl.ensure_vip_account("A1")
    aid = wl.get_sim_account(account_ref="A1")["id"]
    with wl._write_conn() as conn:
        conn.execute(
            "INSERT INTO sim_positions (id, account_id, ticker, shares, avg_cost, current_price,"
            " market_value, unrealized_pnl, weight_pct, opened_at, updated_at, user_id, market)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("tomb", aid, "MSFT", 0, 300.0, 300.0, 0.0, 0.0, 0.0,
             "2026-07-01T00:00:00+00:00", "2026-07-01T00:00:00+00:00", "u1", "us_stock"))

    assert wl.get_sim_position(aid, "MSFT") is None
