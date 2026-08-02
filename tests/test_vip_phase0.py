"""Phase 0 速赢批回归守卫：beta 接线（0-2）+ 持仓 join/催化剂修复（0-5）。

两处此前零覆盖的接线点：
- 0-2：_portfolio_risk_summary 未传 benchmark_returns → portfolio_beta 恒伪数 0.0。
- 0-5：build_account_dossier 从不 join 观察池 → holdings 无 entry_id → 催化剂段恒"暂无"。
"""
import pytest

from bottleneck_hunter.vip import portfolio
from bottleneck_hunter.vip.ingest import BrokerStatement, EquityHolding, ReconResult
from bottleneck_hunter.watchlist.decision_engine import _portfolio_risk_summary
from bottleneck_hunter.watchlist.macro_data import default_benchmark_ticker
from bottleneck_hunter.watchlist.store import WatchlistStore


@pytest.fixture
def wl(tmp_path, monkeypatch):
    from bottleneck_hunter.auth import store as auth_store_mod
    monkeypatch.setattr(auth_store_mod, "_DEFAULT_DB", tmp_path / "auth.db")
    return WatchlistStore(tmp_path / "wl.db").for_user("u1").for_market("us_stock")


def _snaps(ticker, closes):
    """最旧→最新的收盘序列 → 快照 dict（save_snapshots 落共享桶，get_snapshots 同桶按 date DESC 取）。"""
    return [{"ticker": ticker, "date": f"2026-06-{i + 1:02d}", "close": c, "market": "us_stock"}
            for i, c in enumerate(closes)]


def _stmt():
    holds = [
        EquityHolding(ticker="GOOGL", company="Alphabet Inc", quantity=100,
                      market_value_usd=200000.0, nominal_ccy="USD", market_value_nominal=200000.0),
        EquityHolding(ticker="700", company="Tencent (700 HK)", quantity=1194,
                      market_value_usd=65440.92, nominal_ccy="HKD", market_value_nominal=513181.20),
    ]
    total = sum(h.market_value_usd for h in holds)
    return BrokerStatement(content_hash="phase0-h1", period_end="2026-06-30", holdings=holds,
                           cash_balances=[], total_cash_usd=1000.0,
                           recon=ReconResult(holdings_count=2, holdings_total_usd=total,
                                             statement_equities_total_usd=total, delta_usd=0.0, status="ok"))


def test_beta_wired_from_benchmark_snapshots(wl):
    """0-2：喂进基准快照后 portfolio_beta 由真实协方差算出（非伪 0.0）。
    构造股票日收益率 = 2×基准日收益率 → beta 必为 2.0；未喂基准时退回 0.0。"""
    bench_code, _ = default_benchmark_ticker("us_stock")
    # 基准日收益率 [0.1, -0.05, 0.02]；股票严格 2× → [0.2, -0.10, 0.04]
    wl.save_snapshots(_snaps("ZZZ", [50.0, 60.0, 54.0, 56.16]))
    positions = [{"ticker": "ZZZ", "market_value": 56.16, "weight_pct": 100, "sector": "Tech"}]

    before = _portfolio_risk_summary(wl, positions, 56.16)
    assert before["portfolio_beta"] == 0.0   # 无基准快照 → 仍为 0（证明确实靠 benchmark_returns 驱动）

    wl.save_snapshots(_snaps(bench_code, [100.0, 110.0, 104.5, 106.59]))
    after = _portfolio_risk_summary(wl, positions, 56.16)
    assert abs(after["portfolio_beta"] - 2.0) < 0.01   # 接线生效：真实 beta


def test_dossier_join_populates_entry_id_and_sector(wl):
    """0-5：dossier holdings 逐仓 join 观察池 → 观察池内标的补 entry_id/sector/bottleneck_node，
    entry_id 从此非 None（顺带修好 advisory 催化剂恒空 bug）；非观察池标的诚实降级。"""
    portfolio.normalize_statement(wl, _stmt(), account_ref="A1")
    portfolio.materialize_portfolio(wl, as_of_date="2026-06-30", account_ref="A1", cash_total_usd=1000.0)
    entry_id = wl.add({"ticker": "GOOGL", "company_name": "Alphabet", "market": "us_stock",
                       "tier": "track", "sector": "通信服务", "bottleneck_node": "AI算力"})

    dossier = portfolio.build_account_dossier(wl, account_ref="A1")
    by_tk = {h["ticker"]: h for h in dossier["holdings"]}

    assert by_tk["GOOGL"]["entry_id"] == entry_id
    assert by_tk["GOOGL"]["bottleneck_node"] == "AI算力"
    assert by_tk["GOOGL"]["sector"] == "通信服务"
    # 未入观察池的 700 → entry_id 诚实为 None，不编造
    assert by_tk["700"]["entry_id"] is None
    assert dossier["join_coverage"] == {"covered": 1, "total": 2}


def test_perf_summary_from_value_series():
    """0-3：绩效 KPI 全部由 value_series 期末点 + 流水聚合装配（指示性口径）。"""
    vseries = {
        "series": [
            {"as_of_date": "2026-01-01", "total_equity": 1000.0, "benchmark_value": 1000.0},
            {"as_of_date": "2026-04-01", "total_equity": 1100.0, "benchmark_value": 1050.0},
            {"as_of_date": "2026-07-01", "total_equity": 1050.0, "benchmark_value": 1080.0},
        ],
        "returns": [], "benchmark": {"ticker": "^GSPC", "label": "标普500"},
    }
    totals = {"dividend_income": 20.0, "interest_income": 5.0}
    perf = portfolio._perf_summary(vseries, totals, pos_mv=1000.0)
    assert perf["since_inception_pct"] == 5.0            # (1050-1000)/1000
    assert perf["income_yield_pct"] == 2.5              # (20+5)/1000
    assert perf["excess_vs_benchmark_pct"] == -3.0      # 5.0 - (1080/1000-1)*100=8.0
    assert abs(perf["max_drawdown_pct"] - (-4.55)) < 0.01   # 峰 1100 → 谷 1050
    assert perf["n_points"] == 3 and perf["annualized_pct"] is not None

    # 推算点不计入 + 单点不足以算收益
    thin = portfolio._perf_summary({"series": [{"as_of_date": "2026-01-01", "total_equity": 1000.0},
                                               {"as_of_date": "2026-07-01", "total_equity": 1200.0,
                                                "is_projected": True}]}, {}, pos_mv=1000.0)
    assert thin["since_inception_pct"] is None and thin["n_points"] == 1


def test_sigma_and_risk_coverage(wl):
    """0-6：组合波动率 σ 落地（此前算了 portfolio_returns 却弃用）+ VaR/CVaR 覆盖率诚实标注。
    ZZZ 有快照可入风险、WWW 无快照被排除 → 覆盖率 60%（<90% 触发低估预警）。"""
    wl.save_snapshots(_snaps("ZZZ", [50.0, 60.0, 54.0, 56.16, 55.0]))
    positions = [{"ticker": "ZZZ", "market_value": 60.0, "weight_pct": 60, "sector": "Tech"},
                 {"ticker": "WWW", "market_value": 40.0, "weight_pct": 40, "sector": "Fin"}]

    r = _portfolio_risk_summary(wl, positions, 100.0)
    assert r["portfolio_volatility_pct"] > 0          # σ 真落地，非 0
    assert r["risk_coverage"] == {"priced": 1, "total": 2, "weight_pct": 60.0}
    assert any("覆盖" in w for w in r["warnings"])     # 覆盖率不足显式回传，不静默丢


def test_derivative_notional_and_leverage(wl):
    """0-7：衍生品名义敞口跨族解析（FCN 直接 notional / 累购 max_nominal_shares×AFP / MLI 诚实 None）
    + 组合杠杆比率 = 名义 / 真实权益（暴露"总权益不含衍生品"下的隐含杠杆）。"""
    from bottleneck_hunter.vip import derivatives as drv
    from bottleneck_hunter.vip.derivatives import DerivativeTerm
    portfolio.normalize_statement(wl, _stmt(), account_ref="A1")
    portfolio.materialize_portfolio(wl, as_of_date="2026-06-30", account_ref="A1", cash_total_usd=1000.0)
    drv.save_derivative_term(wl, DerivativeTerm(product_family="equity_fcn", underlying_symbol="NVDA",
        currency="USD", tenor_days=0, terms={"market_value_usd": 250000.0, "notional": 260000.0}),
        source_file_name="f.pdf", source_file_hash="h-fcn", broker="cmbi", account_ref="A1")
    drv.save_derivative_term(wl, DerivativeTerm(product_family="equity_accumulator", underlying_symbol="MU",
        currency="USD", tenor_days=0, terms={"afp": 100.0, "max_nominal_shares": 1000.0}),
        source_file_name="a.pdf", source_file_hash="h-acc", broker="cmbi", account_ref="A1")
    drv.save_derivative_term(wl, DerivativeTerm(product_family="equity_mli_booster", underlying_symbol="BABA",
        currency="USD", tenor_days=0, terms={"strike_price": 90.0}),
        source_file_name="m.pdf", source_file_hash="h-mli", broker="cmbi", account_ref="A1")

    d = portfolio.build_account_dossier(wl, account_ref="A1")
    ds = d["derivative_summary"]
    assert ds["notional_total_usd"] == 360000.0                    # 260000 + 1000×100
    assert ds["notional_coverage"] == {"computable": 2, "non_usd": 0, "total": 3}
    assert ds["leverage_ratio"] == round(360000.0 / d["total_equity"], 2)
    assert ds["mtm_total_usd"] == 250000.0                          # 仅 FCN 带 MTM，累购/MLI 无
    by = {x["underlying"]: x for x in d["derivative_exposure"]}
    assert by["BABA"]["notional_usd"] is None                       # MLI 无本金参数 → 诚实 None
    assert by["MU"]["notional_usd"] == 100000.0


def test_exposure_breakdown_by_currency_and_asset(wl):
    """0-4：币种 + 资产类别敞口分桶——GOOGL(USD 200000) + 腾讯 700(HKD 名义，USD 基准 65440.92)。
    多币种真账户的汇率敞口首次落到呈现层。"""
    portfolio.normalize_statement(wl, _stmt(), account_ref="A1")
    portfolio.materialize_portfolio(wl, as_of_date="2026-06-30", account_ref="A1", cash_total_usd=1000.0)

    d = portfolio.build_account_dossier(wl, account_ref="A1")
    ex = d["exposure_breakdown"]
    assert ex["by_currency"].get("USD") == 200000.0
    assert abs(ex["by_currency"].get("HKD", 0) - 65440.92) < 0.01     # 港币敞口按 USD 基准计
    assert abs(ex["total_base"] - 265440.92) < 0.01
    assert sum(ex["by_asset_class"].values()) == pytest.approx(ex["total_base"], abs=0.01)


def test_data_as_of_from_statement_period(wl):
    """0-1：dossier.as_of_hint.data_as_of = 持仓快照日（结算单期末 as_of_date），
    与"生成于今天"区分；纯衍生品/无持仓账户退回结单期末，全无则诚实留白。"""
    portfolio.normalize_statement(wl, _stmt(), account_ref="A1")
    portfolio.materialize_portfolio(wl, as_of_date="2026-06-30", account_ref="A1", cash_total_usd=1000.0)

    d = portfolio.build_account_dossier(wl, account_ref="A1")
    assert d["as_of_hint"]["data_as_of"] == "2026-06-30"      # 结算单期末，非今天
    # 空账户 → 诚实留白，不编造今天
    empty = portfolio.build_account_dossier(wl, account_ref="ZZZ")
    assert empty["as_of_hint"]["data_as_of"] == ""


def test_is_derivative_dual_track(wl):
    """0-8：股票 track holdings[i].is_derivative==False（计入总权益）；
    衍生品 track derivative_exposure[j].is_derivative==True（名义敞口，不计入总权益）。"""
    from bottleneck_hunter.vip import derivatives as drv
    from bottleneck_hunter.vip.derivatives import DerivativeTerm
    portfolio.normalize_statement(wl, _stmt(), account_ref="A1")
    portfolio.materialize_portfolio(wl, as_of_date="2026-06-30", account_ref="A1", cash_total_usd=1000.0)
    drv.save_derivative_term(wl, DerivativeTerm(product_family="equity_fcn", underlying_symbol="NVDA",
        currency="USD", tenor_days=0, terms={"notional": 260000.0}),
        source_file_name="f.pdf", source_file_hash="h-fcn", broker="cmbi", account_ref="A1")

    d = portfolio.build_account_dossier(wl, account_ref="A1")
    assert d["holdings"] and all(h["is_derivative"] is False for h in d["holdings"])
    assert d["derivative_exposure"] and all(x["is_derivative"] is True for x in d["derivative_exposure"])


def test_chair_summary_deterministic():
    """0-9：主席综述行确定性拼装 verdict+票数+护栏+现金容量，无 LLM。"""
    from bottleneck_hunter.vip.advisory import chair_summary
    ok = chair_summary({"verdict": "approve", "approve": 3, "reject": 1})
    assert ok.startswith("投委会加权表决：通过（赞成 3 / 否决 1）") and ok.endswith("。")
    veto = chair_summary({"verdict": "split", "approve": 3, "reject": 1, "risk_veto": True,
                          "diversity_warning": "全 glm"},
                         {"requested_new_buy": 60000.0, "available_cash": 50000.0, "fits": False,
                          "overcommit_pct": 20.0, "unquantified_adds": 2})
    assert "风控委员否决" in veto and "独立性降级" in veto
    assert "超可投资现金 $50,000" in veto and "另有 2 项未量化" in veto


def test_verification_receipt_green_and_degradations():
    """0-10：三项皆过→green；有未核数/超时效/无快照日→green=False 且逐项标未过。"""
    from bottleneck_hunter.vip.advisory import verification_receipt
    green = verification_receipt({"unverified": [], "data_as_of": "2026-07-20",
                                  "generated_at": "2026-08-02T00:00:00+00:00"}, stale_days=45)
    assert green["green"] is True and all(c["ok"] for c in green["checks"])
    bad = verification_receipt({"unverified": ["$999"], "data_as_of": "2026-07-20",
                                "generated_at": "2026-08-02T00:00:00+00:00"}, stale_days=45)
    assert bad["green"] is False and bad["checks"][0]["ok"] is False
    stale = verification_receipt({"unverified": [], "data_as_of": "2026-01-01",
                                  "generated_at": "2026-08-02T00:00:00+00:00"}, stale_days=45)
    assert stale["green"] is False and stale["checks"][2]["ok"] is False
    blank = verification_receipt({"unverified": [], "data_as_of": "",
                                  "generated_at": "2026-08-02T00:00:00+00:00"})
    assert blank["green"] is False and blank["checks"][1]["ok"] is False


# ── Phase 0 评审整改回归（对抗式多镜审计确认项）─────────────────────────────

def test_derivative_notional_dedup_across_periods(wl):
    """审计·三倍虚增：同一笔 FCN 跨 3 期月结单(同 family/underlying/lot_key、不同 file_hash)重导 →
    名义/杠杆按去重「当前条款」只计一次，与已去重的 MTM 同源一致，不 3×。"""
    from bottleneck_hunter.vip import derivatives as drv
    from bottleneck_hunter.vip.derivatives import DerivativeTerm
    portfolio.normalize_statement(wl, _stmt(), account_ref="A1")
    portfolio.materialize_portfolio(wl, as_of_date="2026-06-30", account_ref="A1", cash_total_usd=1000.0)
    for h in ("h-may", "h-jun", "h-jul"):   # 三期结单各落一行（幂等键含 file_hash）
        drv.save_derivative_term(wl, DerivativeTerm(product_family="equity_fcn", underlying_symbol="NVDA",
            currency="USD", tenor_days=0, terms={"market_value_usd": 250000.0, "notional": 260000.0}),
            source_file_name=f"{h}.pdf", source_file_hash=h, broker="cmbi", account_ref="A1")

    d = portfolio.build_account_dossier(wl, account_ref="A1")
    assert len(d["derivative_exposure"]) == 1                    # 去重到一笔，非三笔
    ds = d["derivative_summary"]
    assert ds["notional_total_usd"] == 260000.0                 # 计一次，非 780000
    assert ds["mtm_total_usd"] == 250000.0                      # 与去重 MTM 一致（同源不再自相矛盾）
    assert ds["notional_coverage"]["total"] == 1


def test_derivative_non_usd_excluded_from_usd_leverage(wl):
    """审计·跨币虚高杠杆：港币 FCN 名义在原币种 → notional_usd=None（不冒充美元）、
    不并入 notional_total_usd / 不参与除美元权益算杠杆；仅留 notional_native+币种，non_usd 单列。"""
    from bottleneck_hunter.vip import derivatives as drv
    from bottleneck_hunter.vip.derivatives import DerivativeTerm
    portfolio.normalize_statement(wl, _stmt(), account_ref="A1")
    portfolio.materialize_portfolio(wl, as_of_date="2026-06-30", account_ref="A1", cash_total_usd=1000.0)
    drv.save_derivative_term(wl, DerivativeTerm(product_family="equity_fcn", underlying_symbol="9988",
        currency="HKD", tenor_days=0, terms={"notional": 7800000.0, "notional_ccy": "HKD"}),
        source_file_name="hk.pdf", source_file_hash="h-hk", broker="cmbi", account_ref="A1")

    d = portfolio.build_account_dossier(wl, account_ref="A1")
    leg = {x["underlying"]: x for x in d["derivative_exposure"]}["9988"]
    assert leg["notional_usd"] is None                          # 非美元不冒充美元
    assert leg["notional_native"] == 7800000.0                  # 原币种名义仍保留供呈现
    ds = d["derivative_summary"]
    assert ds["notional_total_usd"] == 0.0                      # 港币腿不并入美元汇总
    assert ds["leverage_ratio"] is None                         # 无美元名义 → 不算杠杆（不虚高 ~7.8×）
    assert ds["notional_coverage"] == {"computable": 0, "non_usd": 1, "total": 1}


def test_beta_aligns_by_recent_tail_not_head(wl):
    """审计·稀疏史错配：持仓快照史比基准短时 beta 按「最近尾部」对齐(截至最新日)而非头部。
    构造稀疏持仓收益 = 2×基准「最近 3 日」收益 → 尾部对齐必得 2.0；旧的头部对齐取错窗得别的值。"""
    bench_code, _ = default_benchmark_ticker("us_stock")
    # 基准 6 点 → 收益 [0.01,0.02,0.10,-0.05,0.02]，最近 3 = [0.10,-0.05,0.02]
    wl.save_snapshots(_snaps(bench_code, [100.0, 101.0, 103.02, 113.322, 107.6559, 109.808018]))
    # 稀疏持仓 4 点 → 收益 [0.20,-0.10,0.04] = 2×基准最近 3
    wl.save_snapshots(_snaps("SPARSE", [50.0, 60.0, 54.0, 56.16]))
    positions = [{"ticker": "SPARSE", "market_value": 56.16, "weight_pct": 100, "sector": "Tech"}]

    r = _portfolio_risk_summary(wl, positions, 56.16)
    assert abs(r["portfolio_beta"] - 2.0) < 0.01                # 尾部对齐生效（头部对齐会得别的数）


def test_volatility_none_when_insufficient_points(wl):
    """审计·冒充 0% 波动：可算日收益点 < 2 时组合波动率诚实 None，不冒充 0%（会误判无风险）。"""
    wl.save_snapshots(_snaps("PP", [50.0, 55.0]))              # 2 点 → 1 收益 → 不足以算 σ
    positions = [{"ticker": "PP", "market_value": 55.0, "weight_pct": 100, "sector": "Tech"}]
    r = _portfolio_risk_summary(wl, positions, 55.0)
    assert r["portfolio_volatility_pct"] is None                # None 而非 0.0


def test_chair_summary_zero_cash_no_bogus_pct():
    """审计·自相矛盾文案：现金恰为 0 且有量化新增 → 说「无可投资现金」，不印「超 0%」。"""
    from bottleneck_hunter.vip.advisory import chair_summary
    line = chair_summary({"verdict": "split", "approve": 2, "reject": 2},
                         {"requested_new_buy": 30000.0, "available_cash": 0.0,
                          "fits": False, "overcommit_pct": 0.0})
    assert "无可投资现金" in line and "超 0%" not in line



