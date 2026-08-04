"""VIP 每日系统推算引擎（P1：股票按最新收盘价重估）。

职责：读取账户在规范层(positions)的最新持仓快照，用行情层(market_snapshots)的最新收盘价
逐标的重估市值/浮盈，写入推算层(vip_projections, status='pending')，并逐条记入账户日志。

严格边界：
- 只写推算层与日志，绝不触碰真值层 positions，也不进入决策/sim_*。
- 全程走 wl_store 的 _filtered / _shared_filter 助手，保持用户 + 市场隔离。
- 收盘价缺失的标的跳过（保留上一次推算/真值），并在日志中说明。
"""

from __future__ import annotations

import re

from bottleneck_hunter.watchlist.store_base import _today

# 美股 ticker 形态：字母打头、全字母/点/连字符、≤6 字符（GOOGL/BRK.B/ARM…）。
# 港股数字码(700)、欧洲 ISIN(IE000CLB8RT6) 天然被排除——前者无字母、后者含数字超长。
_US_TICKER_RE = re.compile(r"^[A-Z][A-Z.\-]{0,5}$")


def collect_priceable_symbols(wl_store, market: str = "us_stock") -> set[str]:
    """汇总该用户 VIP 持仓 + 衍生品标的中「观察池没有」的美股 ticker，供补价用。

    只收 US 形态 ticker（港股/ISIN 无稳定 yfinance 映射，留给 carry-forward）。
    已在观察池的票由 job_price_update 负责，这里不重复。
    """
    if market != "us_stock":
        return set()
    pool = set(wl_store.get_tickers_by_market().get(market, []))
    syms: set[str] = set()
    for acct in wl_store.list_vip_accounts(include_hidden_default=False):
        ref = (acct.get("account_ref") or "").strip()
        if not ref:
            continue
        rows, _ = _latest_snapshot_positions(wl_store, ref)
        for r in rows:
            if (r.get("instrument_type") or "").lower() in ("", "stock", "equity", "etf"):
                syms.add((r.get("symbol") or "").strip())
    from bottleneck_hunter.vip.derivatives import list_derivative_terms_all_accounts
    for it in list_derivative_terms_all_accounts(wl_store, limit=500):
        syms.add((it.get("underlying_symbol") or "").strip())
    return {s for s in syms if s and _US_TICKER_RE.match(s) and s not in pool}


def _latest_snapshot_positions(wl_store, account_ref: str) -> tuple[list[dict], str]:
    """取该账户规范层最新一日、优先级最高来源的持仓（含 symbol/fx/成本）。

    返回 (rows, as_of_date)。rows 每项含 symbol, quantity, market_value_base,
    cost_basis, fx_rate, avg_cost, currency。
    """
    conn = wl_store._connect()
    try:
        # 最新快照日
        if account_ref:
            q, p = wl_store._filtered(
                "SELECT MAX(as_of_date) AS d FROM positions WHERE account_ref = ?",
                (account_ref,), table="positions",
            )
        else:
            q, p = wl_store._filtered("SELECT MAX(as_of_date) AS d FROM positions", table="positions")
        row = conn.execute(q, p).fetchone()
        as_of = (row["d"] if row and row["d"] else "") or ""
        if not as_of:
            return [], ""
        where = ["p.as_of_date = ?", "p.quantity != 0"]
        params: list = [as_of]
        if account_ref:
            where.append("p.account_ref = ?")
            params.append(account_ref)
        q, p = wl_store._filtered(
            f"""SELECT p.quantity, p.market_value_base, p.cost_basis, p.fx_rate,
                       p.avg_cost, p.currency, i.symbol, i.instrument_type
                FROM positions p JOIN instruments i ON i.id = p.instrument_id
                WHERE {' AND '.join(where)}""",
            tuple(params), table="p",
        )
        return [dict(r) for r in conn.execute(q, p).fetchall()], as_of
    finally:
        conn.close()


def project_stock_mtm(wl_store, account_ref: str, as_of: str = "") -> dict:
    """按最新收盘价重估账户内股票市值，写入推算层 + 账户日志。

    as_of：推算目标日（展示口径），默认取行情最新日或今天(北京)；仅作标注，不影响取价逻辑。
    返回 {account_ref, n, n_priced, n_skipped, total_mv_base, total_pnl, as_of}。
    """
    account_ref = (account_ref or "").strip()
    rows, snap_date = _latest_snapshot_positions(wl_store, account_ref)
    as_of = (as_of or "").strip() or _today()

    if not rows:
        return {"account_ref": account_ref, "n": 0, "n_priced": 0, "n_skipped": 0,
                "total_mv_base": 0.0, "total_pnl": 0.0, "as_of": as_of, "snap_date": snap_date}

    # B1：每次重估先把规范层结转成本(含历史结转)回填到存量 sim 真值层，根治「成本只在导入时结转、
    #     改码不回写已物化旧行」的复发。B2：本次推算的成本基也优先取结转值(_cost_map)，回退本快照原值
    #     → 薄快照(仓盘导出)cost_basis=0 时不再恒 pnl=0。均以历史真值快照为源，非臆造。
    from bottleneck_hunter.vip import portfolio  # 局部导入避免 portfolio→projection 循环
    try:
        portfolio.backfill_account_cost(wl_store, account_ref)
    except Exception:  # noqa: BLE001 —— 成本回填失败不拖垮重估主流程
        import logging
        logging.getLogger(__name__).warning("sim 成本回填失败 (acct=%s)", account_ref, exc_info=True)
    _cost_map = portfolio._canonical_cost_map(wl_store, account_ref)

    n_priced = n_skipped = 0
    total_mv = total_pnl = 0.0
    skipped_syms: list[str] = []
    for r in rows:
        symbol = r["symbol"]
        # 仅重估普通股票；衍生品在 P2 用条款逐日重放，这里跳过
        if (r.get("instrument_type") or "").lower() not in ("", "stock", "equity", "etf"):
            continue
        # positions.fx_rate 现未回填(恒 1.0)，故只对美元基准币持仓做逐日重估；非美元持仓沿用结算单
        # 权威 USD 市值，避免用本币收盘价×1.0 写出约 FX 倍高估。FX 逐日重估待 P2/P3。ponytail: 币种守卫。
        ccy = (r.get("currency") or "USD").strip().upper()
        if ccy and ccy != "USD":
            n_skipped += 1
            skipped_syms.append(symbol)
            wl_store.log_account_event(
                account_ref=account_ref, event_type="projection",
                title=f"{symbol} 非美元({ccy})持仓，跳过逐日重估",
                detail="每日重估暂仅支持美元基准币持仓；本币持仓沿用结算单权威美元市值，待 FX 逐日重估(P2)。",
                severity="info",
                payload={"ticker": symbol, "reason": "non_usd", "currency": ccy},
            )
            continue
        snap = wl_store.get_latest_snapshot(symbol)
        close = (snap or {}).get("close")
        qty = r["quantity"] or 0.0
        fx = r["fx_rate"] or 1.0
        # 成本基优先取规范层结转(含历史结转)，回退本快照原值 → 薄快照(仓盘导出)也有成本、pnl 不再恒 0
        cost_basis = (_cost_map.get(symbol, {}).get("cost_basis")) or r["cost_basis"] or 0.0
        if not close:
            # 无收盘价：保留真值，记日志说明
            n_skipped += 1
            skipped_syms.append(symbol)
            wl_store.log_account_event(
                account_ref=account_ref, event_type="projection",
                title=f"{symbol} 缺当日收盘价，跳过重估",
                detail="行情层无最新收盘价，本日沿用上一份结算单/推算数值。",
                severity="warn",
                payload={"ticker": symbol, "reason": "no_close"},
            )
            continue
        new_mv_base = round(qty * float(close) * fx, 2)
        new_pnl = round(new_mv_base - cost_basis, 2) if cost_basis else 0.0
        total_mv += new_mv_base
        total_pnl += new_pnl
        n_priced += 1
        wl_store.upsert_projection(
            account_ref=account_ref, as_of_date=as_of, kind="stock_mtm", ticker=symbol,
            quantity=qty, market_value_base=new_mv_base, unrealized_pnl=new_pnl,
            basis={"close": float(close), "fx_rate": fx, "snap_date": (snap or {}).get("date", ""),
                   "src_snapshot_date": snap_date, "cost_basis": cost_basis},
            status="pending", confidence=0.7,
        )
        wl_store.log_account_event(
            account_ref=account_ref, event_type="projection",
            title=f"{symbol} 按 {(snap or {}).get('date', as_of)} 收盘价重估市值",
            detail=f"{qty:g} 股 × {float(close):.4g} × 汇率{fx:g} = {new_mv_base:,.2f}（待结算单校准）",
            severity="info",
            payload={"ticker": symbol, "close": float(close), "qty": qty,
                     "market_value_base": new_mv_base, "unrealized_pnl": new_pnl},
        )

    # 汇总日志
    wl_store.log_account_event(
        account_ref=account_ref, event_type="projection",
        title=f"每日股票重估完成：{n_priced} 只已重估" + (f"，{n_skipped} 只缺价跳过" if n_skipped else ""),
        detail=f"推算总市值 {total_mv:,.2f}（基于 {snap_date} 持仓快照 + 最新收盘价，标注为待校准）。"
               + (f" 缺价：{', '.join(skipped_syms)}" if skipped_syms else ""),
        severity="warn" if n_skipped else "info",
        payload={"n_priced": n_priced, "n_skipped": n_skipped,
                 "total_mv_base": round(total_mv, 2), "total_pnl": round(total_pnl, 2)},
    )
    return {"account_ref": account_ref, "n": len(rows), "n_priced": n_priced, "n_skipped": n_skipped,
            "total_mv_base": round(total_mv, 2), "total_pnl": round(total_pnl, 2),
            "as_of": as_of, "snap_date": snap_date}


# ── 衍生品逐日累计推算（P2 Step B） ─────────────────────────────────────────

_DATE_FMTS = ("%b %d, %Y", "%d %B %Y", "%B %d, %Y", "%d %b %Y", "%Y-%m-%d")


def _to_iso(s: str) -> str:
    """把结算单里的多种日期格式（Jul 22, 2026 / 22 July 2026 / 2026-07-22）归一为 ISO。失败返回空。"""
    from datetime import datetime
    s = (s or "").strip()
    for f in _DATE_FMTS:
        try:
            return datetime.strptime(s, f).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return ""


def project_derivative_accrual(wl_store, account_ref: str, as_of: str = "") -> dict:
    """按起始交易日 + 逐日累计，用 payoff 引擎对累购/累沽做浮盈推算，写推算层 + 账户日志。

    仅累购(accumulator)/累沽(decumulator)：需 terms.trade_date 才能算 days_observed。
    MLI booster 无本金参数无法折算基准市值，暂跳过（记 info 日志）。
    标的价来自 Step A 补齐的 market_snapshots；KO 判定扫起始日以来的收盘价。
    """
    import numpy as np

    from bottleneck_hunter.vip.derivatives import list_derivative_terms, payoff_accumulator

    account_ref = (account_ref or "").strip()
    as_of = (as_of or "").strip() or _today()
    terms = list_derivative_terms(wl_store, account_ref=account_ref, limit=200)
    n_priced = n_skipped = 0
    for term in terms:
        sym = (term.underlying_symbol or "").strip()
        t = term.terms
        fam = term.product_family
        if fam == "equity_mli_booster":
            wl_store.log_account_event(
                account_ref=account_ref, event_type="projection",
                title=f"{sym} MLI 结构未逐日推算", detail="MLI Booster 为到期兑付结构、缺本金参数，暂不折算基准市值。",
                severity="info", payload={"ticker": sym, "family": fam})
            continue
        trade_iso = _to_iso(t.get("trade_date", ""))
        if not trade_iso:
            n_skipped += 1
            wl_store.log_account_event(
                account_ref=account_ref, event_type="projection",
                title=f"{sym} 衍生品缺起始交易日，无法逐日推算",
                detail="terms 无 trade_date（旧数据），请对该条款「重新抽取条款」回填后再推算。",
                severity="warn", payload={"ticker": sym, "family": fam, "reason": "no_trade_date"})
            continue
        snap = wl_store.get_latest_snapshot(sym)
        close = (snap or {}).get("close")
        if not close:
            n_skipped += 1
            wl_store.log_account_event(
                account_ref=account_ref, event_type="projection",
                title=f"{sym} 衍生品标的缺当日收盘价，跳过",
                detail="行情层无最新收盘价，本日沿用上一次推算。", severity="warn",
                payload={"ticker": sym, "family": fam, "reason": "no_close"})
            continue
        final_price = float(close)
        afp_v = t.get("afp") or 0.0
        if afp_v <= 0:  # 缺成交远期价(AFP)无法算成本/浮盈，跳过等回填（否则会把全部市值当利润）
            n_skipped += 1
            wl_store.log_account_event(
                account_ref=account_ref, event_type="projection",
                title=f"{sym} 衍生品缺 AFP（成交远期价），跳过推算",
                detail="terms 无 afp（抽取不全），请对该条款「重新抽取条款」回填后再推算。", severity="warn",
                payload={"ticker": sym, "family": fam, "reason": "no_afp"})
            continue

        expiry_iso = _to_iso(t.get("expiry_date", ""))
        end_iso = min(as_of, expiry_iso) if expiry_iso else as_of
        settled = bool(expiry_iso) and as_of >= expiry_iso

        # 起始日以来的收盘价序列，一次取用于「实际交易日计数 + KO 判定」（避免两次查库）
        snaps = wl_store.get_snapshots(sym, days=400)
        obs_dates = [d for d in ((row.get("date") or "") for row in snaps) if trade_iso <= d <= end_iso]
        # days_observed：优先用真实交易日数（行情层节假日天然缺行、且不分美股/港股日历）；
        # 仅当行情覆盖到窗口末端(最大日≥end)才可信，否则回落 busday_count（去周末、但含节假日略高估）。ponytail: PROJ6。
        max_snap = max((row.get("date") or "") for row in snaps) if snaps else ""
        if obs_dates and max_snap >= end_iso:
            days_observed = len(obs_dates)
        else:
            try:
                days_observed = max(0, int(np.busday_count(trade_iso, end_iso)))
            except (ValueError, TypeError):
                days_observed = 0

        # KO 判定：起始日以来收盘价是否触碰敲出线
        ko = t.get("knock_out_price") or 0.0
        ko_dir = t.get("knock_out_direction", "up_and_out")
        knock_out = False
        if ko:
            for row in snaps:
                if (row.get("date") or "") < trade_iso:
                    continue
                c = row.get("close")
                if c is None:
                    continue
                if ko_dir == "down_and_out" and float(c) <= ko:
                    knock_out = True; break
                if ko_dir != "down_and_out" and float(c) >= ko:
                    knock_out = True; break

        r = payoff_accumulator(term, final_price, knock_out_happened=knock_out, days_observed=days_observed)
        max_nom = t.get("max_nominal_shares") or 0.0
        if fam == "equity_decumulator":
            shares = r.get("shares_decumulated", 0.0)
        else:
            shares = r.get("shares_acquired", 0.0)
        if max_nom and shares > max_nom:  # 引擎不自带封顶，累计股数不得超过名义上限
            shares = max_nom
        mtm = round(shares * final_price, 2)
        # pnl 用封顶后的 shares 重算，与 quantity/market_value 自洽（payoff 的 pnl 基于未封顶股数）
        pnl = round((shares * afp_v - mtm) if fam == "equity_decumulator" else (mtm - shares * afp_v), 2)

        kind = "deriv_settle" if settled else "deriv_accum"
        wl_store.upsert_projection(
            account_ref=account_ref, as_of_date=as_of, kind=kind, ticker=sym, lot_key=term.lot_key,
            quantity=shares, market_value_base=mtm, unrealized_pnl=pnl,
            basis={"family": fam, "trade_date": trade_iso, "expiry_date": expiry_iso,
                   "days_observed": days_observed, "knock_out": knock_out, "final_price": final_price,
                   "afp": t.get("afp"), "daily_shares": t.get("daily_shares"),
                   "step_up_daily_shares": t.get("step_up_daily_shares"), "max_nominal_shares": max_nom},
            status="pending", confidence=0.5,
        )
        n_priced += 1
        verb = "减持" if fam == "equity_decumulator" else "累计"
        wl_store.log_account_event(
            account_ref=account_ref, event_type="projection",
            title=f"{sym} {'到期结算' if settled else '逐日'}{verb} {shares:g} 股"
                  + ("（已敲出）" if knock_out else ""),
            detail=f"起始 {trade_iso} 起 {days_observed} 个交易日，终值 {final_price:.4g}，"
                   f"推算浮盈 {pnl:,.2f}（静态近似，待结算单校准）。",
            severity="info",
            payload={"ticker": sym, "family": fam, "shares": shares, "market_value_base": mtm,
                     "unrealized_pnl": pnl, "days_observed": days_observed, "knock_out": knock_out})

    return {"account_ref": account_ref, "n": len(terms), "n_priced": n_priced,
            "n_skipped": n_skipped, "as_of": as_of}


# ── KO/KI 状态面板（P1-4：抽 project_derivative_accrual 的 KO 扫描为只读查询）─────────

def _days_between_iso(a_iso: str, b_iso: str) -> int | None:
    """两个 ISO 日期相差天数（b−a）；任一非法返回 None（诚实「未知」，不回落 365）。"""
    from datetime import date
    try:
        a = date(*map(int, a_iso.split("-")))
        b = date(*map(int, b_iso.split("-")))
        return (b - a).days
    except (ValueError, TypeError):
        return None


def _scan_barriers(t: dict, close: float, snaps: list[dict], trade_iso: str, end_iso: str) -> dict:
    """纯函数：条款 + 当日收盘价 + 起始日以来快照 → 距障碍缓冲% + 历史是否触障。

    缓冲%>0 = 距障碍的安全垫，≤0 = 已触/穿越。方向语义：
    - KO up_and_out（累购/FCN autocall）：还需**上涨** (ko−close)/close 才触敲出（利润封顶）。
    - KO down_and_out（累沽）：还需**下跌** (close−ko)/close 才触敲出。
    - KI down_and_in（MLI/FCN）：还需**下跌** (close−ki)/close 才触敲入（本金风险激活）。
    历史触障：起始日~end_iso 的收盘价任一在障碍方向越线即 True（复用 accrual 的 KO 扫描）。
    """
    ko = t.get("knock_out_price") or 0.0
    ko_dir = t.get("knock_out_direction", "up_and_out") or "up_and_out"
    ki = t.get("knock_in_price") or 0.0
    ki_dir = t.get("knock_in_direction", "down_and_in") or "down_and_in"
    out = {"ko_buffer_pct": None, "ki_buffer_pct": None, "knock_out": None, "knock_in": None}
    c = float(close)
    if ko:
        out["ko_buffer_pct"] = round(((c - ko) if ko_dir == "down_and_out" else (ko - c)) / c * 100, 2)
    if ki:
        out["ki_buffer_pct"] = round(((ki - c) if ki_dir == "up_and_in" else (c - ki)) / c * 100, 2)
    ko_hit = ki_hit = False
    for row in snaps:
        d = row.get("date") or ""
        if (trade_iso and d < trade_iso) or (end_iso and d > end_iso):
            continue
        cc = row.get("close")
        if cc is None:
            continue
        cc = float(cc)
        if ko and ((ko_dir == "down_and_out" and cc <= ko) or (ko_dir != "down_and_out" and cc >= ko)):
            ko_hit = True
        if ki and ((ki_dir == "up_and_in" and cc >= ki) or (ki_dir != "up_and_in" and cc <= ki)):
            ki_hit = True
    if ko:
        out["knock_out"] = ko_hit
    if ki:
        out["knock_in"] = ki_hit
    return out


def derivative_barrier_status(wl_store, account_ref: str, as_of: str = "") -> list[dict]:
    """只读扫描各衍生品条款的敲出/敲入状态 + 距障碍缓冲% + 剩余名义额度（不写推算层/日志）。

    供 KO/KI 状态面板与投委会风险信号：用户看到「累购距敲出仅 3%（利润将封顶）」「FCN 已敲入（本金风险激活）」。
    剩余名义额度读最近一次逐日推算的累计股数（缺则 None，绝不在此重算 payoff——那是 project_derivative_accrual 的活）。
    每项 available：barrier + 当日收盘价齐备才 True；缺价/缺障碍/缺起始日 → available False + note 说明（诚实降级）。
    """
    from bottleneck_hunter.vip.derivatives import list_derivative_terms

    account_ref = (account_ref or "").strip()
    as_of = (as_of or "").strip() or _today()
    try:
        terms = list_derivative_terms(wl_store, account_ref=account_ref, limit=200)
    except Exception:  # noqa: BLE001 - 衍生品缺失绝不带崩面板
        return []
    # 最近一次逐日推算的累计股数（lot_key → quantity），用于剩余名义额度；缺则该项留 None
    accrued: dict[str, float] = {}
    try:
        pdate = wl_store.latest_projection_date(account_ref) if hasattr(wl_store, "latest_projection_date") else ""
        if pdate:
            for p in wl_store.list_projections(account_ref=account_ref, as_of_date=pdate):
                if (p.get("kind") or "") in ("deriv_accum", "deriv_settle"):
                    accrued[(p.get("lot_key") or "")] = p.get("quantity")
    except Exception:  # noqa: BLE001
        accrued = {}

    out: list[dict] = []
    for term in terms:
        sym = (term.underlying_symbol or "").strip()
        t = term.terms or {}
        ko = t.get("knock_out_price") or 0.0
        ki = t.get("knock_in_price") or 0.0
        trade_iso = _to_iso(t.get("trade_date", ""))
        expiry_iso = _to_iso(t.get("expiry_date", "") or t.get("maturity", ""))
        snap = wl_store.get_latest_snapshot(sym) if hasattr(wl_store, "get_latest_snapshot") else None
        close = (snap or {}).get("close")
        item = {
            "symbol": sym, "family": term.product_family, "lot_key": term.lot_key,
            "ko_price": ko or None, "ko_direction": t.get("knock_out_direction", "") if ko else "",
            "ki_price": ki or None, "ki_direction": t.get("knock_in_direction", "") if ki else "",
            "last_close": float(close) if close else None,
            "maturity": expiry_iso,
            "days_to_maturity": _days_between_iso(as_of, expiry_iso) if expiry_iso else None,
            "max_nominal_shares": t.get("max_nominal_shares") or None,
            "accrued_shares": None, "remaining_nominal_shares": None,
            "ko_buffer_pct": None, "ki_buffer_pct": None, "knock_out": None, "knock_in": None,
            "available": False, "note": "",
        }
        # 剩余名义额度（累购/累沽）：读最近推算累计股数，缺则 None（不重算 payoff）
        max_nom = t.get("max_nominal_shares") or 0.0
        acc = accrued.get(term.lot_key)
        if max_nom and acc is not None:
            item["accrued_shares"] = round(acc, 2)
            item["remaining_nominal_shares"] = round(max(0.0, max_nom - acc), 2)
        if not close or not (ko or ki):
            item["note"] = "缺当日收盘价，无法算距障碍" if not close else "条款无敲出/敲入价"
            out.append(item)
            continue
        try:
            snaps = wl_store.get_snapshots(sym, days=400) if hasattr(wl_store, "get_snapshots") else []
        except Exception:  # noqa: BLE001
            snaps = []
        end_iso = min(as_of, expiry_iso) if expiry_iso else as_of
        item.update(_scan_barriers(t, close, snaps, trade_iso, end_iso))
        item["available"] = True
        if not trade_iso:
            item["note"] = "缺起始交易日，历史触障扫描可能不完整"
        out.append(item)
    return out


# ── 结算单校准闭环（P3） ────────────────────────────────────────────────────

CALIB_FLAG_THRESHOLD = 0.15  # |推算 vs 真值| 超过此比例 → flagged 待人工核


def calibrate_projections(wl_store, account_ref: str, real_positions: list[dict],
                          doc_id: str = "", as_of: str = "") -> dict:
    """新结算单物化后，用真值 positions 校准最近一日 pending 的 stock_mtm 推算。

    对每个标的：diff=(推算市值-真值市值)/真值；|diff|>15% 标 flagged，否则 calibrated。
    仅校准 stock_mtm——衍生品估值需结算单级逐项数据（暂不可靠抽取），留后续。
    推算里有、结算单里没有的标的（已清仓/本期未披露）保持 pending，记 info 日志。
    """
    account_ref = (account_ref or "").strip()
    # 真值：symbol → 统一美元市值（同标的多行累加）
    real_mv: dict[str, float] = {}
    for r in real_positions:
        sym = (r.get("symbol") or "").strip()
        if sym:
            real_mv[sym] = real_mv.get(sym, 0.0) + (r.get("market_value_base") or 0.0)

    d = wl_store.latest_projection_date(account_ref)
    if not d:
        return {"account_ref": account_ref, "n_calibrated": 0, "n_flagged": 0, "n_unmatched": 0}
    pend = wl_store.list_projections(account_ref=account_ref, as_of_date=d, kind="stock_mtm", status="pending")

    n_cal = n_flag = n_unmatched = 0
    for p in pend:
        sym = p.get("ticker") or ""
        proj_mv = p.get("market_value_base") or 0.0
        real = real_mv.get(sym)
        if not real:  # 结算单无此标的或真值为 0 → 无法比对，保持 pending
            n_unmatched += 1
            wl_store.log_account_event(
                account_ref=account_ref, event_type="calibration",
                title=f"{sym} 本期结算单无对应真值，推算保持待校准",
                detail="该标的未出现在本次结算单（可能已清仓/本期未披露），暂不校准。",
                severity="info", payload={"ticker": sym, "reason": "no_truth"})
            continue
        diff_pct = round((proj_mv - real) / real * 100, 2)
        flagged = abs(diff_pct) > CALIB_FLAG_THRESHOLD * 100
        wl_store.mark_projection_calibrated(p["id"], doc_id=doc_id, diff_pct=diff_pct, flagged=flagged)
        if flagged:
            n_flag += 1
        else:
            n_cal += 1
        wl_store.log_account_event(
            account_ref=account_ref, event_type="anomaly" if flagged else "calibration",
            title=f"{sym} 推算{'偏差超阈值' if flagged else '已校准'}（{diff_pct:+.1f}%）",
            detail=f"推算市值 {proj_mv:,.2f} vs 结算单真值 {real:,.2f}，偏差 {diff_pct:+.1f}%"
                   + ("（超 15%，已标记待人工核查）" if flagged else "（在阈内）"),
            severity="warn" if flagged else "info",
            payload={"ticker": sym, "proj_mv": proj_mv, "real_mv": real,
                     "diff_pct": diff_pct, "doc_id": doc_id})

    wl_store.log_account_event(
        account_ref=account_ref, event_type="calibration",
        title=f"结算单校准完成：{n_cal} 项通过，{n_flag} 项超阈值" + (f"，{n_unmatched} 项无真值" if n_unmatched else ""),
        detail=f"以 {as_of or d} 结算单校准 {d} 的推算（阈值 ±{CALIB_FLAG_THRESHOLD*100:.0f}%）。",
        severity="warn" if n_flag else "info",
        payload={"n_calibrated": n_cal, "n_flagged": n_flag, "n_unmatched": n_unmatched, "doc_id": doc_id})
    return {"account_ref": account_ref, "n_calibrated": n_cal, "n_flagged": n_flag, "n_unmatched": n_unmatched}
