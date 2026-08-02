"""Phase 1 回归守卫：P1-1 纲领硬约束确定性对账（check_mandate_compliance）+ 投委会接线。

此前硬约束靠 risk_officer 从叙述里「自由推理」是否破坏（脆弱代理）。P1-1 把集中度/排除/回撤
变确定性结构化对账，喂 _consensus 做 mandate_veto、喂 enforce_mandate_hard 做逐仓硬拦、
折进 build_committee_context 的 portfolio_risk 让 risk_officer 收到结构化信号（零 prompt 文件改动）。
"""
import pytest

from bottleneck_hunter.vip import portfolio
from bottleneck_hunter.vip.advisory import (
    _consensus,
    build_committee_context,
    chair_summary,
    enforce_mandate_hard,
    reconcile_sector_rotation,
)
from bottleneck_hunter.vip.ingest import BrokerStatement, EquityHolding, ReconResult
from bottleneck_hunter.vip.mandate import check_mandate_compliance, save_mandate
from bottleneck_hunter.watchlist.store import WatchlistStore


@pytest.fixture
def wl(tmp_path, monkeypatch):
    from bottleneck_hunter.auth import store as auth_store_mod
    monkeypatch.setattr(auth_store_mod, "_DEFAULT_DB", tmp_path / "auth.db")
    return WatchlistStore(tmp_path / "wl.db").for_user("u1").for_market("us_stock")


# ── check_mandate_compliance：确定性核算持仓 vs 纲领硬约束 ──────────────────

def test_compliance_flags_concentration_exclusion_and_drawdown():
    """保守档 single≤15/sector≤30；构造单仓/板块破坏 + 排除命中 + 回撤逼近未破。
    硬破坏进 violations（compliant False）；逼近仅 warn（ok 仍 True，不误判破坏）。"""
    mdt = {"risk_appetite": "conservative", "max_drawdown_pct": 20,
           "exclusions": "白酒、TSLA", "focus_sectors": "AI 算力"}
    dossier = {
        "holdings": [
            {"ticker": "NVDA", "weight_pct": 40.0, "sector": "半导体", "bottleneck_node": "AI 算力"},
            {"ticker": "MU", "weight_pct": 25.0, "sector": "半导体"},
            {"ticker": "TSLA", "weight_pct": 10.0, "sector": "汽车"},
        ],
        "perf_summary": {"max_drawdown_pct": -17.0, "n_points": 4},  # |−17| ≥ 0.8×20=16 → 逼近未破
    }
    comp = check_mandate_compliance(mdt, dossier)
    assert comp["compliant"] is False
    keys = {v["key"] for v in comp["violations"]}
    assert keys == {"single_concentration", "sector_concentration", "exclusions"}
    ex = next(c for c in comp["checks"] if c["key"] == "exclusions")
    assert "TSLA" in ex["items"]                                  # 代码子串命中排除
    dd = next(c for c in comp["checks"] if c["key"] == "max_drawdown")
    assert dd["ok"] is True and dd["warn"] is True                # 逼近未破 → ok 但 warn
    assert comp["ceilings"] == {"single_pct": 15.0, "sector_pct": 30.0}


def test_compliance_thin_data_is_none_not_false():
    """无持仓 + 无回撤点 → 集中度/回撤判 None（数据不足），compliant True（None 不算硬破坏，不硬凑通过）。"""
    comp = check_mandate_compliance({"risk_appetite": "aggressive", "max_drawdown_pct": 40},
                                    {"holdings": [], "perf_summary": {}})
    assert comp["compliant"] is True and not comp["violations"]
    assert all(c["ok"] is None for c in comp["checks"] if c["key"] != "focus_sectors")


def test_compliance_derivative_underlying_hits_exclusion():
    """排除命中覆盖衍生品标的（underlying/family 子串），不只股票持仓。"""
    comp = check_mandate_compliance(
        {"risk_appetite": "balanced", "exclusions": "BABA"},
        {"holdings": [{"ticker": "AAPL", "weight_pct": 10.0, "sector": "科技"}],
         "derivative_exposure": [{"underlying": "BABA", "family": "equity_fcn"}]})
    ex = next(c for c in comp["checks"] if c["key"] == "exclusions")
    assert ex["ok"] is False and "BABA" in ex["items"]


# ── _consensus：结构化 hard 破坏 → mandate_veto 抑制升级（不靠 LLM 推理）──────

def test_consensus_hard_breach_demotes_approve_to_split():
    reviews = [{"role": "growth_investor", "vote": "approve", "confidence": 3},
               {"role": "value_investor", "vote": "approve", "confidence": 3}]
    mc = {"compliant": False, "violations": [{"key": "single_concentration", "label": "单仓集中度"}]}
    out = _consensus(reviews, mandate_compliance=mc)
    assert out["mandate_veto"] is True and out["verdict"] == "split" and out["caution"] is True
    assert out["mandate_violations"] == ["单仓集中度"]


def test_consensus_compliant_no_veto():
    out = _consensus([{"role": "growth_investor", "vote": "approve", "confidence": 3}],
                     mandate_compliance={"compliant": True, "violations": []})
    assert out["mandate_veto"] is False and out["verdict"] == "approve"


def test_consensus_backward_compatible_without_mandate():
    """缺省不传 mandate_compliance → 退化旧行为，mandate_veto False（recommend 免改路径不受扰）。"""
    assert _consensus([{"role": "growth_investor", "vote": "approve"}])["mandate_veto"] is False


# ── enforce_mandate_hard：逐仓确定性硬拦（加仓→持有）───────────────────────

def test_enforce_hard_downgrades_add_to_hold():
    holdings = [{"ticker": "NVDA", "action": "加仓", "reason": "成长强"},
                {"ticker": "MU", "action": "持有", "reason": "观望"},
                {"ticker": "AAPL", "action": "加仓", "reason": "稳"}]
    compliance = {"violations": [{"key": "single_concentration", "label": "单仓集中度", "items": ["NVDA"]}]}
    blocked = enforce_mandate_hard(holdings, compliance)
    assert blocked == ["NVDA"]
    nvda = next(h for h in holdings if h["ticker"] == "NVDA")
    assert nvda["action"] == "持有" and "下调为持有" in nvda["reason"]
    aapl = next(h for h in holdings if h["ticker"] == "AAPL")   # 未命中 → 原样不动
    assert aapl["action"] == "加仓"


def test_enforce_hard_excluded_hold_gets_review_note_not_blocked():
    """排除命中但原动作已是持有 → 追加审慎提示、不进 blocked（advice-only，不臆造减仓 sizing）。"""
    holdings = [{"ticker": "TSLA", "action": "持有", "reason": "已在观察"}]
    compliance = {"violations": [{"key": "exclusions", "label": "排除清单", "items": ["TSLA"]}]}
    blocked = enforce_mandate_hard(holdings, compliance)
    assert blocked == [] and "请审慎核对" in holdings[0]["reason"]


# ── chair_summary：主席综述确定性拼装纲领硬约束行 ─────────────────────────

def test_chair_summary_surfaces_mandate_line():
    line = chair_summary({"verdict": "split", "approve": 2, "reject": 0,
                          "mandate_veto": True, "mandate_violations": ["单仓集中度", "排除清单"]})
    assert "纲领硬约束" in line and "单仓集中度" in line and "排除清单" in line


# ── build_committee_context：mandate_compliance 折进 portfolio_risk（risk_officer 收结构化信号）──

def _stmt():
    holds = [EquityHolding(ticker="NVDA", company="NVIDIA", quantity=100,
                           market_value_usd=200000.0, nominal_ccy="USD", market_value_nominal=200000.0)]
    total = sum(h.market_value_usd for h in holds)
    return BrokerStatement(content_hash="phase1-h1", period_end="2026-06-30", holdings=holds,
                           cash_balances=[], total_cash_usd=1000.0,
                           recon=ReconResult(holdings_count=1, holdings_total_usd=total,
                                             statement_equities_total_usd=total, delta_usd=0.0, status="ok"))


def test_committee_context_folds_compliance_into_portfolio_risk(wl):
    """P1-1 接线点：mandate_compliance 折进 context['portfolio_risk']（已在 corpus 键 → 委员引用数字不被误标 ⚠）。
    显式传入的 compliance 原样注入；不传时按账户自动核算（recommend 免改也注入）。"""
    portfolio.normalize_statement(wl, _stmt(), account_ref="A1")
    portfolio.materialize_portfolio(wl, as_of_date="2026-06-30", account_ref="A1", cash_total_usd=1000.0)
    save_mandate(wl, {"risk_appetite": "conservative", "max_drawdown_pct": 20}, account_ref="A1")
    dossier = portfolio.build_account_dossier(wl, account_ref="A1")

    # ① 显式传入 → 原样注入
    mc = {"compliant": False, "violations": [{"key": "single_concentration", "label": "单仓集中度"}]}
    ctx = build_committee_context(wl, dossier, "宏观中性", [], market="us_stock", mandate_compliance=mc)
    assert ctx["portfolio_risk"]["mandate_compliance"] is mc

    # ② 不传 → 自动核算并注入（NVDA 单仓 100% > 保守 15% → 结构化违规）
    ctx2 = build_committee_context(wl, dossier, "宏观中性", [], market="us_stock")
    auto = ctx2["portfolio_risk"].get("mandate_compliance")
    assert auto and auto["compliant"] is False
    assert any(v["key"] == "single_concentration" for v in auto["violations"])


# ── P1-3：持仓板块权重 vs L1 板块轮动三桶对照 ──────────────────────────────

def test_sector_rotation_flags_weakening_and_unheld():
    """重仓走弱板块入 in_weakening（含权益%与标的）；'半导体'⊂'半导体板块' 双向子串命中走强；判强零持仓入 unheld。"""
    rot = reconcile_sector_rotation(
        [{"ticker": "NVDA", "weight_pct": 40.0, "sector": "半导体"},
         {"ticker": "XOM", "weight_pct": 30.0, "sector": "能源"},
         {"ticker": "KO", "weight_pct": 10.0, "sector": "未知"}],       # 未知板块跳过，不参与对照
        {"strengthening": ["半导体板块"], "weakening": ["能源"], "neutral": ["医药"]})
    assert rot["available"] is True
    assert [r["sector"] for r in rot["in_weakening"]] == ["能源"]
    assert rot["in_weakening"][0]["tickers"] == ["XOM"] and rot["weakening_weight_pct"] == 30.0
    assert [r["sector"] for r in rot["in_strengthening"]] == ["半导体"]   # 子串近似命中
    assert "半导体板块" not in rot["strengthening_unheld"]                 # 已持有 → 不列 unheld


def test_sector_rotation_chinese_keys_and_unheld():
    """兼容 L1 中文键（看多/看空）；L1 判强但零持仓 → strengthening_unheld（潜在补仓方向）。"""
    rot = reconcile_sector_rotation(
        [{"ticker": "AAPL", "weight_pct": 20.0, "sector": "科技"}],
        {"看多": ["医药", "科技"], "看空": ["地产"]})
    assert "医药" in rot["strengthening_unheld"] and "科技" not in rot["strengthening_unheld"]
    assert [r["sector"] for r in rot["in_strengthening"]] == ["科技"]


def test_sector_rotation_unavailable_without_signal():
    """rotation 仅有 neutral（无强/弱信号）→ available False，不硬凑对照。"""
    rot = reconcile_sector_rotation([{"ticker": "AAPL", "weight_pct": 20.0, "sector": "科技"}],
                                    {"neutral": ["医药"]})
    assert rot["available"] is False and rot["in_weakening"] == []


def test_committee_context_folds_sector_rotation(wl, monkeypatch):
    """P1-3 接线点：sector_rotation 折进 context['portfolio_risk']['sector_rotation_reconcile']（同 corpus 键）。
    NVDA 持仓 sector=半导体，L1 判半导体走弱 → in_weakening 命中；显式传入优先于自动取数。"""
    portfolio.normalize_statement(wl, _stmt(), account_ref="A1")
    portfolio.materialize_portfolio(wl, as_of_date="2026-06-30", account_ref="A1", cash_total_usd=1000.0)
    wl.add({"ticker": "NVDA", "company_name": "NVIDIA", "market": "us_stock",
            "tier": "track", "sector": "半导体"})           # join 补 sector 供板块对照
    dossier = portfolio.build_account_dossier(wl, account_ref="A1")

    ctx = build_committee_context(wl, dossier, "宏观中性", [], market="us_stock",
                                  sector_rotation={"weakening": ["半导体"], "strengthening": ["能源"]})
    rec = ctx["portfolio_risk"].get("sector_rotation_reconcile")
    assert rec and rec["available"] is True
    assert [r["sector"] for r in rec["in_weakening"]] == ["半导体"]
    assert "能源" in rec["strengthening_unheld"]


# ── P1-4：衍生品 KO/KI 状态只读扫描（距障碍缓冲% + 触障 + 剩余名义）──────────────

def test_scan_barriers_buffer_signs_and_touch():
    """纯函数买卖方向缓冲%语义 + 历史触障扫描（钱路径）。up_and_out 需上涨触KO；
    down_and_out/down_and_in 需下跌；缓冲>0=安全垫；历史窗口内越线→触发 True。"""
    from bottleneck_hunter.vip.projection import _scan_barriers
    # 累购 up_and_out：close100/KO120 → 还需 +20%；窗口内有 125 → 已敲出；KI 无 → None
    acc = _scan_barriers({"knock_out_price": 120.0, "knock_out_direction": "up_and_out"},
                         100.0, [{"date": "2026-06-10", "close": 125.0}], "2026-06-01", "2026-06-30")
    assert acc["ko_buffer_pct"] == 20.0 and acc["knock_out"] is True and acc["knock_in"] is None
    # 累沽 down_and_out：close100/KO80 → 还需 -20%；最低 95 未越线 → 未敲出
    dec = _scan_barriers({"knock_out_price": 80.0, "knock_out_direction": "down_and_out"},
                         100.0, [{"date": "2026-06-10", "close": 95.0}], "2026-06-01", "2026-06-30")
    assert dec["ko_buffer_pct"] == 20.0 and dec["knock_out"] is False
    # MLI KI down_and_in：close100/KI70 → 还需 -30%；窗口内有 65 → 已敲入（本金风险激活）
    mli = _scan_barriers({"knock_in_price": 70.0, "knock_in_direction": "down_and_in"},
                         100.0, [{"date": "2026-06-10", "close": 65.0}], "2026-06-01", "2026-06-30")
    assert mli["ki_buffer_pct"] == 30.0 and mli["knock_in"] is True and mli["knock_out"] is None
    # 起始日前的快照不计入触障（2026-05-01 的 65 早于 trade 2026-06-01）
    pre = _scan_barriers({"knock_in_price": 70.0, "knock_in_direction": "down_and_in"},
                         100.0, [{"date": "2026-05-01", "close": 65.0}], "2026-06-01", "2026-06-30")
    assert pre["knock_in"] is False


def test_derivative_barrier_status_end_to_end(wl):
    """端到端：条款 + 行情快照 → available/缓冲%/触障 + 剩余名义（无逐日推算→None）；缺价项 available False+note。"""
    from bottleneck_hunter.vip.derivatives import DerivativeTerm, save_derivative_term
    from bottleneck_hunter.vip.projection import derivative_barrier_status
    term = DerivativeTerm("equity_accumulator", "MU", "USD", 365,
                          {"knock_out_price": 200.0, "knock_out_direction": "up_and_out",
                           "trade_date": "2026-06-01", "expiry_date": "2027-06-01",
                           "max_nominal_shares": 1000.0})
    save_derivative_term(wl, term, source_file_name="t.pdf", source_file_hash="h1",
                         broker="citi", account_ref="A1")
    wl.save_snapshots([{"ticker": "MU", "date": "2026-06-01", "close": 100.0, "market": "us_stock"},
                       {"ticker": "MU", "date": "2026-06-20", "close": 125.0, "market": "us_stock"}])
    items = derivative_barrier_status(wl, "A1", as_of="2026-12-31")
    assert len(items) == 1
    it = items[0]
    assert it["available"] is True and it["symbol"] == "MU" and it["last_close"] == 125.0
    assert it["ko_buffer_pct"] == 60.0 and it["knock_out"] is False       # (200-125)/125*100；125<200 未越线
    assert it["max_nominal_shares"] == 1000.0 and it["remaining_nominal_shares"] is None  # 无逐日推算→诚实 None

    # 无行情标的 → available False + 缺价 note（不硬凑距障碍）
    term2 = DerivativeTerm("equity_accumulator", "ZZZZ", "USD", 365,
                           {"knock_out_price": 50.0, "knock_out_direction": "up_and_out"})
    save_derivative_term(wl, term2, source_file_name="t2.pdf", source_file_hash="h2",
                         broker="citi", account_ref="A1")
    zz = next(i for i in derivative_barrier_status(wl, "A1", as_of="2026-12-31") if i["symbol"] == "ZZZZ")
    assert zz["available"] is False and "缺当日收盘价" in zz["note"]


def test_committee_context_folds_derivative_barriers(wl):
    """P1-4 接线点：available 的障碍状态折进 portfolio_risk['derivative_barriers']（同 corpus 键）。"""
    portfolio.normalize_statement(wl, _stmt(), account_ref="A1")
    portfolio.materialize_portfolio(wl, as_of_date="2026-06-30", account_ref="A1", cash_total_usd=1000.0)
    dossier = portfolio.build_account_dossier(wl, account_ref="A1")
    barriers = [{"symbol": "MU", "family": "equity_accumulator", "available": True, "ko_buffer_pct": 3.0},
                {"symbol": "ZZ", "family": "equity_fcn", "available": False, "note": "缺当日收盘价"}]
    ctx = build_committee_context(wl, dossier, "宏观中性", [], market="us_stock",
                                  derivative_barriers=barriers)
    folded = ctx["portfolio_risk"].get("derivative_barriers")
    assert folded == [barriers[0]]        # 仅 available 项进委员会；缺价项不喂噪声


# ── P1-2：本轮账户统一行动清单（advisory 减/持/加 + recommend 建仓/关注/规避 合并 + 现金配平）────

def test_merge_actions_ranks_and_sizes():
    """纯合并（决策路径）：两 pass 并成一张按可执行性排序的清单；加仓附 sizing、持有/关注/规避标非行动、来源分明。"""
    from bottleneck_hunter.vip.advisory import _merge_actions
    adv = {"holdings": [{"ticker": "NVDA", "action": "加仓", "reason": "强"},
                        {"ticker": "MU", "action": "减仓", "reason": "超配"},
                        {"ticker": "AAPL", "action": "持有", "reason": "观望"}]}
    rec = {"candidates": [{"ticker": "TSM", "action": "建仓", "suggested_weight": "5%"},
                          {"ticker": "AMD", "action": "关注"},
                          {"ticker": "BABA", "action": "规避"}]}
    sized = {"NVDA": {"ticker": "NVDA", "suggested_shares": 10, "suggested_amount": 3000.0}}
    out = _merge_actions(adv, rec, sized)
    assert [a["ticker"] for a in out] == ["MU", "TSM", "NVDA", "AMD", "AAPL", "BABA"]  # 减仓→建仓→加仓→关注→持有→规避
    nvda = next(a for a in out if a["ticker"] == "NVDA")
    assert nvda["actionable"] is True and nvda["sizing"]["suggested_shares"] == 10   # 加仓附指示性档位
    assert next(a for a in out if a["ticker"] == "AAPL")["actionable"] is False       # 持有＝非行动
    assert next(a for a in out if a["ticker"] == "MU")["source"] == "持仓"
    assert next(a for a in out if a["ticker"] == "TSM")["source"] == "荐新"


def test_build_action_plan_empty_when_no_passes(wl):
    """两 pass 皆未生成 → available False（不硬凑空清单冒充「无行动」）。"""
    from bottleneck_hunter.vip.advisory import build_action_plan
    plan = build_action_plan(wl, "A1")
    assert plan["available"] is False and plan["actions"] == []


# ── P1-5：完整性闸门 FIFO 已实现盈亏（买入历史不覆盖卖出→该标的留 None，绝不反推假数）──────────

def test_realized_pnl_fifo_gates_incomplete_and_skips_pure_buys():
    """AAPL 买 100@100 后卖 60@150 → 已配平 realized 3000；MSFT 无买入直接卖 → 队列下溢判 None+incomplete；
    NVDA 纯买入无卖出 → 不入明细（无已实现）。合计仅含已配平标的。"""
    from bottleneck_hunter.vip.portfolio import compute_realized_pnl_fifo
    txns = [
        {"symbol": "AAPL", "txn_type": "buy", "quantity": 100, "net_amount": -10000.0, "trade_date": "2026-05-01"},
        {"symbol": "AAPL", "txn_type": "sell", "quantity": 60, "net_amount": 9000.0, "trade_date": "2026-06-01"},
        {"symbol": "MSFT", "txn_type": "sell", "quantity": 50, "net_amount": 10000.0, "trade_date": "2026-06-02"},
        {"symbol": "NVDA", "txn_type": "buy", "quantity": 10, "net_amount": -3000.0, "trade_date": "2026-05-03"},
    ]
    out = compute_realized_pnl_fifo(txns)
    assert out["available"] is True and out["total"] == 3000.0            # 仅 AAPL：60×(150−100)
    aapl = next(s for s in out["by_symbol"] if s["symbol"] == "AAPL")
    assert aapl["complete"] is True and aapl["realized_pnl"] == 3000.0 and aapl["matched_qty"] == 60.0
    msft = next(s for s in out["by_symbol"] if s["symbol"] == "MSFT")
    assert msft["complete"] is False and msft["realized_pnl"] is None      # 下溢 → 诚实留空
    assert out["incomplete_symbols"] == ["MSFT"]
    assert "NVDA" not in {s["symbol"] for s in out["by_symbol"]}           # 纯买入不入明细


def test_realized_pnl_fifo_multi_lot_and_price_fallback():
    """多批次 FIFO 按序冲抵 + net_amount 缺失时退 price 口径。买 50@10、买 50@20，卖 60 → 冲 50@10 + 10@20。"""
    from bottleneck_hunter.vip.portfolio import compute_realized_pnl_fifo
    txns = [
        {"symbol": "T", "txn_type": "buy", "quantity": 50, "price": 10.0, "trade_date": "2026-01-01"},
        {"symbol": "T", "txn_type": "buy", "quantity": 50, "price": 20.0, "trade_date": "2026-02-01"},
        {"symbol": "T", "txn_type": "sell", "quantity": 60, "price": 30.0, "trade_date": "2026-03-01"},
    ]
    out = compute_realized_pnl_fifo(txns)
    # 60 股冲抵：50×(30−10)=1000 + 10×(30−20)=100 = 1100
    assert out["total"] == 1100.0 and out["by_symbol"][0]["matched_qty"] == 60.0


def test_realized_pnl_fifo_gates_non_usd_and_splits_by_currency():
    """审查修复①（币种闸门）：港币标的 realized 不并入美元 total、原币种分列 by_currency 且进 foreign_values；
    美元标的照常合计。杜绝把 HKD 净额当美元汇总（HKD ~7.8× 虚高，与 _derivative_notional_usd 同规矩）。"""
    from bottleneck_hunter.vip.portfolio import compute_realized_pnl_fifo
    txns = [
        {"symbol": "0700.HK", "txn_type": "buy", "quantity": 100, "net_amount": -30000.0,
         "currency": "HKD", "trade_date": "2026-05-01"},
        {"symbol": "0700.HK", "txn_type": "sell", "quantity": 100, "net_amount": 40000.0,
         "currency": "HKD", "trade_date": "2026-06-01"},
        {"symbol": "AAPL", "txn_type": "buy", "quantity": 10, "net_amount": -1000.0,
         "currency": "USD", "trade_date": "2026-05-02"},
        {"symbol": "AAPL", "txn_type": "sell", "quantity": 10, "net_amount": 1500.0,
         "currency": "USD", "trade_date": "2026-06-02"},
    ]
    out = compute_realized_pnl_fifo(txns)
    assert out["available"] is True and out["total"] == 500.0          # 仅 AAPL 美元腿计入美元合计
    assert out["by_currency"] == {"HKD": 10000.0}                       # 港币原币种分列，绝不并入美元
    assert 10000.0 in out["foreign_values"]                            # 喂 number_guard 排除 $ 误核
    hk = next(s for s in out["by_symbol"] if s["symbol"] == "0700.HK")
    assert hk["currency"] == "HKD" and hk["realized_pnl"] == 10000.0 and hk["complete"] is True
    # 纯港币账户（无美元腿）→ available False、total None，但 by_currency/明细仍诚实呈现
    only_hk = compute_realized_pnl_fifo(txns[:2])
    assert only_hk["available"] is False and only_hk["total"] is None and only_hk["by_currency"] == {"HKD": 10000.0}


def test_sector_rotation_no_double_bucket_on_substring_collision():
    """审查修复②：'能源'⊂'新能源' 不得令持仓同时进弱/强桶（自相矛盾+虚增走弱敞口）。
    新能源 唯一归走强、走弱敞口为 0；反向 能源 唯一归走弱、不误进走强。"""
    rot = reconcile_sector_rotation(
        [{"ticker": "CATL", "weight_pct": 30.0, "sector": "新能源"}],
        {"strengthening": ["新能源"], "weakening": ["能源"]})
    assert rot["available"] is True
    assert [r["sector"] for r in rot["in_strengthening"]] == ["新能源"]
    assert rot["in_weakening"] == [] and rot["weakening_weight_pct"] == 0.0
    rot2 = reconcile_sector_rotation(
        [{"ticker": "XOM", "weight_pct": 20.0, "sector": "能源"}],
        {"strengthening": ["新能源"], "weakening": ["能源"]})
    assert [r["sector"] for r in rot2["in_weakening"]] == ["能源"]
    assert rot2["in_strengthening"] == []                              # 能源⊂新能源 不误进走强


def test_enforce_hard_sector_concentration_downgrades_by_sector():
    """审查修复③：板块集中度 violation 的 items 是板块名（非 ticker）→ 该板块内所有加仓下调持有；
    草案未回带 sector 时靠 sector_by_ticker（dossier 权威口径）反查；他板块不动。"""
    holdings = [{"ticker": "NVDA", "action": "加仓", "reason": "强", "sector": "半导体"},
                {"ticker": "MU", "action": "加仓", "reason": "稳"},          # 草案未回带 sector
                {"ticker": "KO", "action": "加仓", "reason": "防御"}]
    compliance = {"violations": [{"key": "sector_concentration", "label": "板块集中度", "items": ["半导体"]}]}
    blocked = enforce_mandate_hard(holdings, compliance, sector_by_ticker={"MU": "半导体", "KO": "必需消费"})
    assert set(blocked) == {"NVDA", "MU"}                              # 两只半导体加仓均下调
    assert all(h["action"] == "持有" for h in holdings if h["ticker"] in ("NVDA", "MU"))
    assert next(h for h in holdings if h["ticker"] == "KO")["action"] == "加仓"   # 他板块不动



