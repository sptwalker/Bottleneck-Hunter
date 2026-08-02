"""Phase 3 回归守卫：收益率(Modified Dietz) / 风险调整 / 标的贡献 / 确定性叙事。

红线(§8.1/8.2)：稀疏结单期末点、非逐日。
- P3-1：现金流调整收益率——期中注资绝不计入业绩；仅权威净值(含现金)口径呈现，其余口径诚实留 None。
- P3-2：Sharpe/Sortino/Calmar 按实际期均跨度年化，<3 期收益诚实降级(None + 样本不足)。
- P3-3：标的贡献 = 期初权重 × 单价收益(mv/qty 还原单价，剔买卖交割污染)。
- P3-4：报告叙事块全部来自确定性事实(收益率/贡献/仓位事件)，无 LLM 臆造。
"""
import json

import pytest

from bottleneck_hunter.vip import metrics, portfolio
from bottleneck_hunter.vip.ingest import BrokerStatement, EquityHolding, ReconResult
from bottleneck_hunter.watchlist.store import WatchlistStore


@pytest.fixture
def wl(tmp_path, monkeypatch):
    from bottleneck_hunter.auth import store as auth_store_mod
    monkeypatch.setattr(auth_store_mod, "_DEFAULT_DB", tmp_path / "auth.db")
    return WatchlistStore(tmp_path / "wl.db").for_user("u1").for_market("us_stock")


def _imp(wl, iid, pe, te, account_ref, created="2026-07-01T00:00:00+00:00"):
    with wl._write_conn() as conn:
        conn.execute(
            "INSERT INTO vip_imports(id,file_name,file_hash,file_type,detected_kind,status,"
            "key_metrics_json,account_ref,created_at,user_id,market) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (iid, f"{iid}.pdf", iid, "pdf", "monthly_statement", "imported",
             json.dumps({"period_end": pe, "total_equity": te}), account_ref, created, "u1", "us_stock"))


def _snap(wl, account_ref, as_of, holdings, total_equity, ch):
    """写一期：持仓快照(normalize_statement) + 权威净值(vip_imports)。holdings=[(ticker,qty,mv)]。"""
    hs = [EquityHolding(ticker=t, company=t, quantity=q, market_value_usd=mv,
                        nominal_ccy="USD", market_value_nominal=mv) for (t, q, mv) in holdings]
    tot = sum(mv for *_, mv in holdings)
    stmt = BrokerStatement(
        content_hash=ch, period_end=as_of, holdings=hs, cash_balances=[], total_cash_usd=0.0,
        recon=ReconResult(holdings_count=len(hs), holdings_total_usd=tot,
                          statement_equities_total_usd=tot, delta_usd=0.0, status="ok"))
    portfolio.normalize_statement(wl, stmt, source_doc_id=ch, account_ref=account_ref)
    _imp(wl, f"imp_{ch}", as_of, total_equity, account_ref, created=f"2026-07-01T00:00:0{len(ch)%10}+00:00")


def _auth(series):
    return {"series": series, "basis": "authoritative_total_equity"}


# ── P3-1：现金流调整收益率(Modified Dietz) 接入 _perf_summary ──────────────

def test_perf_summary_dietz_excludes_external_flow(wl=None):
    """期初 100k→期末 130k，但期中注资 30k → 简单收益率虚高 +30%，Modified Dietz 真实收益≈0(全靠注资)。"""
    series = [{"as_of_date": "2026-05-31", "total_equity": 100000.0},
              {"as_of_date": "2026-06-30", "total_equity": 130000.0}]
    flows = [{"date": "2026-06-01", "amount": 30000.0}]   # 注资(带正号)
    out = portfolio._perf_summary(_auth(series), {}, 0.0, flows=flows)
    assert out["since_inception_pct"] == 30.0                 # 未剔注资的简单口径(保留参照)
    assert out["dietz_return_pct"] is not None and abs(out["dietz_return_pct"]) < 1.0   # 剔注资后≈0
    assert "已剔外部现金流" in out["dietz_basis"]


def test_perf_summary_dietz_none_when_not_authoritative():
    """非权威净值口径(持仓市值/MTM锚点)分母不含现金 → 不呈现现金流调整收益率，dietz_basis 说明原因。"""
    series = [{"as_of_date": "2026-05-31", "total_equity": 100.0},
              {"as_of_date": "2026-06-30", "total_equity": 110.0}]
    out = portfolio._perf_summary({"series": series, "basis": "positions_market_value"}, {}, 0.0, flows=[])
    assert out["dietz_return_pct"] is None
    assert "先补齐结单权威净值" in out["dietz_basis"]


def test_perf_summary_dietz_none_when_denom_degenerate():
    """红线守卫：期中大额提取使分母翻负(平均投入本金<0)→ Modified Dietz 诚实降级 None，
    绝不返回符号翻转伪收益并链接进 TWR。期初 100k、期中转出 130k(权重≈1)→ denom<0。"""
    series = [{"as_of_date": "2026-05-31", "total_equity": 100000.0},
              {"as_of_date": "2026-06-30", "total_equity": 8000.0}]
    flows = [{"date": "2026-06-01", "amount": -130000.0}]   # 转出(带负号)，加权后 denom 翻负
    out = portfolio._perf_summary(_auth(series), {}, 0.0, flows=flows)
    assert out["dietz_return_pct"] is None                    # 退化子期被跳过、无有效期 → cumulative None
    assert "0/1" in out["dietz_basis"]                        # coverage：1 个子期全退化，有效 0


def test_perf_summary_mdd_excludes_external_flow():
    """红线守卫：MDD 须剔外部现金流(与 dietz 同口径)。市场真跌 30% 但同期注资掩盖了裸 equity 回撤——
    剔流后仍应看到真实回撤，Calmar 分母不被注资虚高。期初 100k、市场跌到 70k、注资 40k → 裸 equity 110k 无回撤。"""
    series = [{"as_of_date": "2026-04-30", "total_equity": 100000.0},
              {"as_of_date": "2026-05-31", "total_equity": 110000.0},   # 市场 70k + 注资 40k = 裸值不破前高
              {"as_of_date": "2026-06-30", "total_equity": 115000.0}]
    flows = [{"date": "2026-05-15", "amount": 40000.0}]
    out = portfolio._perf_summary(_auth(series), {}, 0.0, flows=flows)
    # 剔累计注资后：100k → (110k-40k)=70k → (115k-40k)=75k，真实回撤 (70-100)/100 = -30%
    assert out["max_drawdown_pct"] == -30.0
    # 对照：不传 flows 时裸 equity 单调不破前高，MDD=0（证明剔流确实改变了结果、非巧合）
    bare = portfolio._perf_summary(_auth(series), {}, 0.0, flows=[])
    assert bare["max_drawdown_pct"] == 0.0


# ── P3-2：风险调整(Sharpe/Sortino/Calmar) 样本闸 ──────────────────────────

def test_perf_summary_risk_adjusted_needs_min_samples():
    """<3 期收益(仅 2 个期末点=1 期收益) → 风险调整全 None + 样本不足；不拿 1 个点冒充统计量。"""
    series = [{"as_of_date": "2026-05-31", "total_equity": 100000.0},
              {"as_of_date": "2026-06-30", "total_equity": 105000.0}]
    out = portfolio._perf_summary(_auth(series), {}, 0.0, flows=[])
    assert out["sharpe"] is None and out["sortino"] is None and out["calmar"] is None
    assert "样本不足" in out["risk_note"]


def test_perf_summary_risk_adjusted_computes_with_enough_samples():
    """5 个期末点=4 期收益(≥3) → Sharpe 出数；按实际期均跨度年化(非 252)。"""
    series = [{"as_of_date": "2026-02-28", "total_equity": 100000.0},
              {"as_of_date": "2026-03-31", "total_equity": 102000.0},
              {"as_of_date": "2026-04-30", "total_equity": 101000.0},
              {"as_of_date": "2026-05-31", "total_equity": 104000.0},
              {"as_of_date": "2026-06-30", "total_equity": 105500.0}]
    out = portfolio._perf_summary(_auth(series), {}, 0.0, flows=[])
    assert out["dietz_return_pct"] is not None
    assert out["sharpe"] is not None                         # 4 期收益足够出 Sharpe
    assert "样本不足" not in (out["risk_note"] or "")


# ── P3-3：标的贡献归因(相邻两期×权重，剔买卖污染) via DB ───────────────────

def test_contribution_attribution_via_store(wl):
    """两期快照：AAPL 单价 +20%、MSFT 单价 -10%，等权 → AAPL 贡献 +10pct 排首、MSFT -5pct。"""
    _snap(wl, "ACC", "2026-05-31", [("AAPL", 10, 1000.0), ("MSFT", 10, 1000.0)], 2000.0, "s1")
    _snap(wl, "ACC", "2026-06-30", [("AAPL", 10, 1200.0), ("MSFT", 10, 900.0)], 2100.0, "s2")

    c = portfolio._contribution(wl, "ACC")
    assert c["prev_date"] == "2026-05-31" and c["cur_date"] == "2026-06-30"
    by = {r["symbol"]: r for r in c["rows"]}
    assert abs(by["AAPL"]["contribution_pct"] - 10.0) < 1e-6
    assert abs(by["MSFT"]["contribution_pct"] - (-5.0)) < 1e-6
    assert c["rows"][0]["symbol"] == "AAPL"                  # 按 |贡献| 降序，AAPL 在前


def test_contribution_excludes_buy_pollution(wl):
    """期末对 MSFT 加仓翻倍(qty 10→20, mv 1800)但单价其实 -10% → 贡献按单价算，不受加仓抬高市值污染。"""
    _snap(wl, "ACC2", "2026-05-31", [("MSFT", 10, 1000.0)], 1000.0, "p1")
    _snap(wl, "ACC2", "2026-06-30", [("MSFT", 20, 1800.0)], 1900.0, "p2")   # 单价 1800/20=90 vs 100 → -10%

    c = portfolio._contribution(wl, "ACC2")
    msft = {r["symbol"]: r for r in c["rows"]}["MSFT"]
    assert abs(msft["price_return_pct"] - (-10.0)) < 1e-6    # 剔买卖：看单价不看市值增量


def test_contribution_empty_when_single_period(wl):
    """仅一期快照 → 无法相邻归因，诚实空 + note，不臆造。"""
    _snap(wl, "ACC3", "2026-06-30", [("AAPL", 10, 1000.0)], 1000.0, "o1")
    c = portfolio._contribution(wl, "ACC3")
    assert c["rows"] == [] and "不足两期" in c["note"]


# ── P3-4：确定性叙事块(收益率+贡献+仓位事件，无 LLM) ────────────────────────

def test_render_period_narrative_deterministic(wl):
    """两期快照+平仓事件：叙事块含真实收益率(Modified Dietz)、标的贡献、确定性仓位事件，全来自事实。"""
    _snap(wl, "NAR", "2026-05-31", [("AAPL", 10, 1000.0), ("TSLA", 10, 1000.0)], 2000.0, "n1")
    _snap(wl, "NAR", "2026-06-30", [("AAPL", 10, 1200.0)], 2100.0, "n2")   # TSLA 平仓、AAPL +20%

    md = portfolio.render_period_narrative(wl, "NAR")
    assert "本期业绩（现金流调整）" in md
    assert "标的贡献归因" in md and "AAPL" in md
    assert "本期仓位变动" in md and "平仓" in md and "TSLA" in md
    # 确定性事实必须是真值而非仅标题：AAPL 单价 +20%、等权 50% → 贡献 ≈ +10pct（若收益/贡献算错，标题在但数字错）
    assert "+10.00pct" in md and "+20.0%" in md


def test_linked_modified_dietz_flow_boundary_bucketing():
    """(d0, d1] 半开区间归桶守卫：注资恰在期末日 d1 权重=0(不减业绩)、恰在期初日 d0 排除出本子期。
    锁死边界口径——若把 `<` / `<=` 翻转会双重扣减或漏扣某笔注资，此断言即失败。"""
    series = [{"as_of_date": "2026-01-31", "total_equity": 100.0},
              {"as_of_date": "2026-02-28", "total_equity": 120.0}]
    # 注资恰在期末日 d1：`<= d1` 收进本子期但权重 (span-span)/span=0 → 分子减 20、分母不加 → 期末瞬时注入不算业绩
    at_d1 = metrics.linked_modified_dietz(series, [{"date": "2026-02-28", "amount": 20.0}])
    assert at_d1["cumulative_pct"] == 0.0                     # (120-100-20)/100 = 0
    # 注资恰在期初日 d0：`d0 <` 严格排除 → 不进本子期 → 该 20 未被剔 → 简单收益 20%
    at_d0 = metrics.linked_modified_dietz(series, [{"date": "2026-01-31", "amount": 20.0}])
    assert at_d0["cumulative_pct"] == 20.0                    # (120-100-0)/100 = 20%


def test_render_period_narrative_empty_when_no_history(wl):
    """单期账户：无相邻期收益/贡献/事件 → 叙事块留空(不硬凑段落)。"""
    _snap(wl, "NAR2", "2026-06-30", [("AAPL", 10, 1000.0)], 1000.0, "e1")
    assert portfolio.render_period_narrative(wl, "NAR2").strip() == ""


# ── 集成：dossier 带 contribution + perf_summary 的现金流调整字段 ────────────

def test_dossier_includes_phase3_fields(wl):
    _snap(wl, "DOS", "2026-05-31", [("AAPL", 10, 1000.0), ("MSFT", 10, 1000.0)], 2000.0, "d1")
    _snap(wl, "DOS", "2026-06-30", [("AAPL", 10, 1200.0), ("MSFT", 10, 900.0)], 2100.0, "d2")
    portfolio.materialize_portfolio(wl, account_ref="DOS", cash_total_usd=0.0)

    dossier = portfolio.build_account_dossier(wl, account_ref="DOS")
    assert "contribution" in dossier and dossier["contribution"]["rows"]
    perf = dossier["perf_summary"]
    assert "dietz_return_pct" in perf and "sharpe" in perf and "dietz_basis" in perf


def test_external_flows_signed_and_filtered():
    """_external_flows：外部现金流带符号透传(注资+/提取-)，买卖/分红不计，缺日期跳过。"""
    txns = [{"txn_type": "deposit", "trade_date": "2026-06-01", "net_amount": 500.0},
            {"txn_type": "withdrawal", "trade_date": "2026-06-10", "net_amount": -300.0},
            {"txn_type": "transfer_in", "trade_date": "", "net_amount": 200.0},     # 缺日期 → 跳过
            {"txn_type": "buy", "trade_date": "2026-06-05", "net_amount": -1000.0}]  # 买卖 → 不计
    flows = portfolio._external_flows(txns)
    assert flows == [{"date": "2026-06-01", "amount": 500.0},
                     {"date": "2026-06-10", "amount": -300.0}]
