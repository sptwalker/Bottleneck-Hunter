"""P2 规范化 + P5 决策适配/报告：BrokerStatement → 规范表 → sim_* → 决策引擎 → 投资分析报告。

数据流（见 docs/VIP_ADVISOR_TECH_SPEC.md §5）：
  normalize_statement:   BrokerStatement → instruments + positions（规范真值层，多币种统一基币 USD）
  materialize_portfolio: 规范 positions → sim_account + sim_positions（决策投影层，先冻 import_snapshot）
  generate_vip_report:   sim_* → 复用组合摘要 → LLM 叙事（过 number_guard）→ 挂免责 → 落 vip_reports + 审计

M1 范围：EQUITIES（股票/ETF），单券商单账户。衍生品/固收留 M3。
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from bottleneck_hunter.vip import compliance, number_guard
from bottleneck_hunter.vip.ingest import BrokerStatement, StatementTransaction
from bottleneck_hunter.watchlist.store_base import normalize_ticker


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ETF ISIN → 可交易代码映射（M1 手工小表；P2 正式版接 OpenFIGI/券商主数据）。
# ingest 对 ETF 用 ISIN 作 symbol，此处映射到可取行情的 ticker。
_ISIN_TO_TICKER = {
    "US4642875235": "SOXX",   # iShares Semiconductor ETF
}

_DOC_PRIORITY = {
    "position_report": 3,
    "monthly_statement": 2,
    "trade_confirm": 1,
}


def _doc_rank(meta: dict | None) -> tuple[int, str, str]:
    meta = meta or {}
    return (
        _DOC_PRIORITY.get(meta.get("doc_type", ""), 0),
        meta.get("created_at", "") or "",
        meta.get("doc_id", "") or "",
    )


def _get_doc_meta(user_id: str, doc_id: str) -> dict:
    if not user_id or not doc_id:
        return {"doc_id": doc_id, "doc_type": "", "created_at": ""}
    from bottleneck_hunter.auth.store import AuthStore

    row = AuthStore().get_financial_doc(user_id, doc_id) or {}
    return {
        "doc_id": doc_id,
        "doc_type": row.get("doc_type", "") or "",
        "created_at": row.get("created_at", "") or "",
    }


def _list_snapshot_doc_ids(wl_store, as_of_date: str, account_ref: str) -> list[str]:
    conn = wl_store._connect()
    try:
        where = ["as_of_date = ?"]
        params: list = [as_of_date]
        if account_ref:
            where.append("account_ref = ?")
            params.append(account_ref)
        q, p = wl_store._filtered(
            f"SELECT DISTINCT source_doc_id FROM positions WHERE {' AND '.join(where)}",
            tuple(params),
        )
        rows = conn.execute(q, p).fetchall()
        return [row["source_doc_id"] for row in rows if row["source_doc_id"]]
    finally:
        conn.close()


def _winning_snapshot_meta(wl_store, as_of_date: str, account_ref: str) -> dict:
    doc_ids = _list_snapshot_doc_ids(wl_store, as_of_date, account_ref)
    if not doc_ids:
        return {"doc_id": "", "doc_type": "", "created_at": ""}
    metas = [_get_doc_meta(getattr(wl_store, "_user_id", ""), doc_id) for doc_id in doc_ids]
    return max(metas, key=_doc_rank)


def _clear_snapshot_positions(wl_store, as_of_date: str, account_ref: str) -> None:
    with wl_store._write_conn() as conn:
        where = ["as_of_date = ?"]
        params: list = [as_of_date]
        if account_ref:
            where.append("account_ref = ?")
            params.append(account_ref)
        q, p = wl_store._filtered(
            f"DELETE FROM positions WHERE {' AND '.join(where)}",
            tuple(params),
        )
        conn.execute(q, p)


def _prepare_snapshot_write(wl_store, as_of_date: str, account_ref: str, source_doc_id: str) -> dict:
    incoming = _get_doc_meta(getattr(wl_store, "_user_id", ""), source_doc_id)
    existing = _winning_snapshot_meta(wl_store, as_of_date, account_ref)
    if existing.get("doc_id") and _doc_rank(existing) > _doc_rank(incoming):
        return {
            "apply": False,
            "selected_doc_id": existing.get("doc_id", ""),
            "selected_doc_type": existing.get("doc_type", ""),
        }
    _clear_snapshot_positions(wl_store, as_of_date, account_ref)
    return {
        "apply": True,
        "selected_doc_id": incoming.get("doc_id", source_doc_id),
        "selected_doc_type": incoming.get("doc_type", ""),
    }


def _map_symbol(symbol: str) -> tuple[str, str]:
    """返回 (可交易代码, instrument_type)。ISIN 形态→查表映射为 ETF ticker。"""
    if len(symbol) >= 11 and symbol[:2].isalpha() and symbol[2:].isalnum():
        return _ISIN_TO_TICKER.get(symbol, symbol), "etf"
    return symbol, "stock"


# ── P2: 规范化 —— BrokerStatement → instruments + positions ──────────────

def normalize_statement(wl_store, stmt: BrokerStatement,
                        source_doc_id: str = "", account_ref: str = "") -> dict:
    """把已解析的 BrokerStatement 写入规范层 instruments + positions + transactions（幂等 upsert）。

    多币种：market_value_usd 已是统一美元口径（ingest 取 Total Value USD 列），
    直接作 market_value_base；组合占比一律用 base 口径。返回 {n_instruments, n_positions}。
    """
    as_of = stmt.period_end or _now_iso()[:10]
    n_inst = n_pos = n_txn = 0
    snapshot = {"apply": True, "selected_doc_id": source_doc_id, "selected_doc_type": ""}
    if stmt.holdings and source_doc_id:
        snapshot = _prepare_snapshot_write(wl_store, as_of, account_ref, source_doc_id)
    instrument_cache: dict[tuple[str, str], str] = {}
    if snapshot["apply"]:
        for h in stmt.holdings:
            symbol, itype = _map_symbol(h.ticker)
            inst_id = _upsert_instrument(wl_store, symbol, itype, h.company,
                                         h.nominal_ccy, source_doc_id)
            instrument_cache[(symbol, itype)] = inst_id
            mv_base = h.market_value_usd                 # 统一美元基币
            _upsert_position(wl_store, inst_id, account_ref, as_of,
                             quantity=h.quantity, market_value_base=mv_base,
                             currency=h.nominal_ccy, source_doc_id=source_doc_id,
                             avg_cost=h.avg_cost, cost_basis=h.cost_basis_usd,
                             unrealized_pnl=h.unrealized_pnl_usd)
            n_inst += 1
            n_pos += 1
    for t in stmt.transactions:
        n_txn += _upsert_transaction(wl_store, t, source_doc_id=source_doc_id,
                                     default_account_ref=account_ref,
                                     instrument_cache=instrument_cache)
    return {
        "n_instruments": n_inst,
        "n_positions": n_pos,
        "n_transactions": n_txn,
        "as_of_date": as_of,
        "snapshot_applied": snapshot["apply"],
        "selected_doc_id": snapshot.get("selected_doc_id", ""),
        "selected_doc_type": snapshot.get("selected_doc_type", ""),
    }


def _upsert_instrument(wl_store, symbol, itype, name, currency, source_doc_id) -> str:
    conn = wl_store._connect()
    try:
        q, p = wl_store._filtered(
            "SELECT id FROM instruments WHERE symbol = ? AND instrument_type = ?",
            (symbol, itype))
        row = conn.execute(q, p).fetchone()
        if row:
            return row["id"]
    finally:
        conn.close()
    iid = uuid.uuid4().hex[:12]
    with wl_store._write_conn() as conn:
        conn.execute(
            f"""INSERT INTO instruments
               (id, symbol, instrument_type, name, currency, source_doc_id,
                created_at{wl_store._user_insert_cols()}{wl_store._market_insert_cols()})
               VALUES (?,?,?,?,?,?,?{wl_store._user_insert_vals()}{wl_store._market_insert_vals()})""",
            (iid, symbol, itype, name, currency, source_doc_id, _now_iso())
            + wl_store._user_insert_params() + wl_store._market_insert_params(),
        )
    return iid


def _upsert_position(wl_store, instrument_id, account_ref, as_of_date, *,
                     quantity, market_value_base, currency, source_doc_id,
                     avg_cost=None, cost_basis=None, unrealized_pnl=None) -> None:
    with wl_store._write_conn() as conn:
        # 幂等：同 (account_ref, instrument_id, as_of_date) 已存在则更新
        q, p = wl_store._filtered(
            "SELECT id FROM positions WHERE account_ref = ? AND instrument_id = ? AND as_of_date = ?",
            (account_ref, instrument_id, as_of_date))
        row = conn.execute(q, p).fetchone()
        if row:
            q2, p2 = wl_store._filtered(
                "UPDATE positions SET quantity=?, market_value_base=?, market_value=?, currency=?, "
                "avg_cost=?, cost_basis=?, unrealized_pnl=?, source_doc_id=? WHERE id=?",
                (quantity, market_value_base, market_value_base, currency,
                 avg_cost or 0, cost_basis or 0, unrealized_pnl or 0, source_doc_id, row["id"]))
            conn.execute(q2, p2)
            return
        pid = uuid.uuid4().hex[:12]
        conn.execute(
            f"""INSERT INTO positions
               (id, instrument_id, account_ref, as_of_date, quantity, currency,
                avg_cost, cost_basis, unrealized_pnl,
                market_value, market_value_base, source_doc_id, created_at{wl_store._user_insert_cols()}{wl_store._market_insert_cols()})
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?{wl_store._user_insert_vals()}{wl_store._market_insert_vals()})""",
            (pid, instrument_id, account_ref, as_of_date, quantity, currency,
             avg_cost or 0, cost_basis or 0, unrealized_pnl or 0,
             market_value_base, market_value_base, source_doc_id, _now_iso())
            + wl_store._user_insert_params() + wl_store._market_insert_params(),
        )


def _map_txn_symbol(txn: StatementTransaction) -> tuple[str, str, str]:
    raw_symbol = txn.ticker or txn.isin or txn.cusip or ""
    if raw_symbol:
        symbol, itype = _map_symbol(normalize_ticker(raw_symbol))
        return symbol, itype, raw_symbol
    company = (txn.company or "").strip()
    if company:
        guess = normalize_ticker(company.split()[0])
        return guess, "stock", company
    return "", "", ""


def _ensure_transaction_instrument(wl_store, txn: StatementTransaction, source_doc_id: str,
                                   instrument_cache: dict[tuple[str, str], str]) -> str:
    symbol, itype, _ = _map_txn_symbol(txn)
    if not symbol or not itype:
        return ""
    key = (symbol, itype)
    if key in instrument_cache:
        return instrument_cache[key]
    inst_id = _upsert_instrument(wl_store, symbol, itype, txn.company or symbol,
                                 txn.currency or "USD", source_doc_id)
    instrument_cache[key] = inst_id
    return inst_id


def _fallback_external_id(txn: StatementTransaction, account_ref: str) -> str:
    parts = [txn.trade_date, account_ref, txn.txn_type, txn.currency,
             f"{txn.net_amount:.2f}", normalize_ticker(txn.ticker or txn.isin or txn.cusip or txn.company)]
    return "txn-" + "-".join(str(p) for p in parts if p)


def _upsert_transaction(wl_store, txn: StatementTransaction, *, source_doc_id: str,
                        default_account_ref: str,
                        instrument_cache: dict[tuple[str, str], str]) -> int:
    account_ref = default_account_ref or txn.account_ref
    external_id = txn.external_id or _fallback_external_id(txn, account_ref)
    instrument_id = _ensure_transaction_instrument(wl_store, txn, source_doc_id, instrument_cache)
    with wl_store._write_conn() as conn:
        q, p = wl_store._filtered(
            "SELECT id FROM transactions WHERE account_ref = ? AND external_id = ?",
            (account_ref, external_id))
        row = conn.execute(q, p).fetchone()
        if row:
            q2, p2 = wl_store._filtered(
                """UPDATE transactions
                   SET instrument_id=?, txn_type=?, trade_date=?, settle_date=?, quantity=?, price=?,
                       gross_amount=?, fee=?, tax=?, net_amount=?, currency=?, fx_rate=?, description=?, source_doc_id=?
                   WHERE id=?""",
                (instrument_id, txn.txn_type, txn.trade_date, txn.settle_date, txn.quantity, txn.price,
                 txn.gross_amount, txn.fee, txn.tax, txn.net_amount, txn.currency, txn.fx_rate,
                 txn.description, source_doc_id, row["id"]))
            conn.execute(q2, p2)
            return 0
        tid = uuid.uuid4().hex[:12]
        conn.execute(
            f"""INSERT INTO transactions
               (id, instrument_id, account_ref, txn_type, trade_date, settle_date, quantity, price,
                gross_amount, fee, tax, net_amount, currency, fx_rate, external_id, description,
                source_doc_id, created_at{wl_store._user_insert_cols()}{wl_store._market_insert_cols()})
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?{wl_store._user_insert_vals()}{wl_store._market_insert_vals()})""",
            (tid, instrument_id, account_ref, txn.txn_type, txn.trade_date, txn.settle_date,
             txn.quantity, txn.price, txn.gross_amount, txn.fee, txn.tax, txn.net_amount,
             txn.currency, txn.fx_rate, external_id, txn.description, source_doc_id, _now_iso())
            + wl_store._user_insert_params() + wl_store._market_insert_params(),
        )
        return 1


# ── P5: 物化 —— 规范 positions → sim_account + sim_positions ──────────────

def _overwrite_guard(wl_store, account, acct_id, as_of_date, incoming_total, incoming_n, account_ref) -> str:
    """返回非空原因串 = "本次快照不可覆盖 sim 现有账户"。防误读文件用错误数据覆盖好数据的两类：
    1. 陈旧单：账户已有更新日期(period_end 更晚)的快照 → 不拿旧数据回填 live(旧持仓仍存规范层作历史)。
    2. 骤降误判：账户已有实质权益且有持仓，本次总值<既有 10% 且持仓数也更少 → 疑似误判券商/部分解析。
    ponytail: 阈值 0.1、且需"值降+数降"同时满足，避免误伤真实大额提取；拿到更多误判样本再校准。
    (全空快照已由上游 not rows 分支处理，此处不重复；空账户 existing_n=0，两条判定天然不触发，首次导入放行。)
    """
    if as_of_date:  # 陈旧：存在比本次更晚日期的快照
        conn = wl_store._connect()
        try:
            where, params = ["as_of_date > ?"], [as_of_date]
            if account_ref:
                where.append("account_ref = ?")
                params.append(account_ref)
            q, p = wl_store._filtered(
                f"SELECT 1 FROM positions WHERE {' AND '.join(where)} LIMIT 1",
                tuple(params), table="positions")
            if conn.execute(q, p).fetchone():
                return f"stale_snapshot:{as_of_date}"
        finally:
            conn.close()
    existing_equity = account.get("total_equity") or 0.0
    existing_n = len(wl_store.get_sim_positions(acct_id))
    if (existing_equity > 0 and existing_n > 0
            and incoming_total < existing_equity * 0.1 and incoming_n < existing_n):
        return f"suspected_misparse:total={incoming_total:.0f}<10%_of_{existing_equity:.0f}"
    return ""


def materialize_portfolio(wl_store, as_of_date: str = "", account_ref: str = "",
                          cash_total_usd: float = 0.0,
                          account_total_usd: float | None = None) -> dict:
    """把某快照日的规范 positions 投影到 sim_*，供决策引擎消费。

    先把旧 sim 快照冻结进 vip_reports(kind='import_snapshot')作溯源锚（M2），再清零重建。
    market_value_base(统一美元)→ sim_positions.market_value；
    - 默认：总权益 = Σ持仓 + 现金(cash_total_usd)
    - 若账户层有更权威锚（如 Nomura NAV），可显式传 account_total_usd 覆盖总权益口径
    返回 {account_id, n_positions, total_equity, cash_balance, snapshot_report_id}。
    """
    account = wl_store.get_sim_account(account_ref=account_ref)
    acct_id = account["id"]

    # 取规范层最新快照
    rows, selected = _latest_positions(wl_store, as_of_date, account_ref)

    # 空快照护栏：待物化日无任何持仓、也没有现金/账户总值等权威口径 → 保持既有 sim 快照不动。
    # 否则一份 0 持仓的月结单（披露页 / 无持仓页 / 解析失败 / period_end 缺失落到今天）会把好快照清成 0，页面变空白。
    # ponytail: 账户真的清仓，请用带 account_total_usd 或含持仓的快照显式表达
    if not rows and account_total_usd is None and not cash_total_usd:
        return {"account_id": acct_id, "n_positions": 0,
                "total_equity": round(account.get("total_equity", 0) or 0, 2),
                "cash_balance": round(account.get("cash_balance", 0) or 0, 2),
                "snapshot_report_id": "", "skipped_empty": True,
                "selected_doc_id": selected.get("doc_id", ""),
                "selected_doc_type": selected.get("doc_type", "")}

    # 误覆盖护栏：陈旧单 / 骤降误判 → 保持既有 sim 快照不动（数据已入库到规范层作历史/待核）。
    total_positions = sum(r["market_value_base"] for r in rows)
    incoming_total = account_total_usd if account_total_usd is not None else total_positions + (cash_total_usd or 0.0)
    guard = _overwrite_guard(wl_store, account, acct_id, as_of_date, incoming_total, len(rows), account_ref)
    if guard:
        return {"account_id": acct_id, "n_positions": 0,
                "total_equity": round(account.get("total_equity", 0) or 0, 2),
                "cash_balance": round(account.get("cash_balance", 0) or 0, 2),
                "snapshot_report_id": "", "guard_skipped": guard,
                "selected_doc_id": selected.get("doc_id", ""),
                "selected_doc_type": selected.get("doc_type", "")}

    # 冻结旧快照（溯源锚）
    old_positions = wl_store.get_sim_positions(acct_id)
    snap_id = ""
    if old_positions:
        snap_id = _freeze_snapshot(wl_store, account, old_positions, account_ref=account_ref)

    # 清零旧 sim 持仓
    for op in old_positions:
        wl_store.update_sim_position(op["id"], shares=0, market_value=0,
                                     unrealized_pnl=0, weight_pct=0)

    total_positions = sum(r["market_value_base"] for r in rows)
    computed_total = total_positions + (cash_total_usd or 0.0)
    total_equity = account_total_usd if account_total_usd is not None else computed_total
    n = 0
    for r in rows:
        symbol = r["symbol"]
        mv = r["market_value_base"] or 0.0
        qty = r["quantity"] or 0.0
        avg = (mv / qty) if qty else 0.0
        pid = wl_store.create_sim_position(acct_id, symbol, int(qty), avg)
        wl_store.update_sim_position(
            pid, current_price=avg, market_value=mv, unrealized_pnl=0.0,
            weight_pct=round(mv / total_equity * 100, 2) if total_equity else 0.0)
        n += 1

    wl_store.update_sim_account(account_ref=account_ref,
                                total_equity=round(total_equity, 2),
                                current_capital=round(total_equity, 2),
                                cash_balance=round(cash_total_usd or 0.0, 2))
    # P3 校准闭环：用刚入库的真值 positions 校准待校准推算（独立 try，绝不带崩导入）
    try:
        from bottleneck_hunter.vip import projection
        projection.calibrate_projections(wl_store, account_ref, rows,
                                          doc_id=selected.get("doc_id", ""), as_of=as_of_date)
    except Exception:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning("结算单校准失败 (acct=%s)", account_ref, exc_info=True)
    return {"account_id": acct_id, "n_positions": n,
            "total_equity": round(total_equity, 2),
            "cash_balance": round(cash_total_usd or 0.0, 2),
            "snapshot_report_id": snap_id,
            "selected_doc_id": selected.get("doc_id", ""),
            "selected_doc_type": selected.get("doc_type", "")}


def _latest_positions(wl_store, as_of_date, account_ref) -> tuple[list[dict], dict]:
    """取规范层持仓 + 工具符号；同日只采用优先级最高的 source_doc_id。"""
    conn = wl_store._connect()
    try:
        if not as_of_date:
            q, p = wl_store._filtered("SELECT MAX(as_of_date) AS d FROM positions", table="positions")
            row = conn.execute(q, p).fetchone()
            as_of_date = row["d"] if row and row["d"] else ""
        selected = _winning_snapshot_meta(wl_store, as_of_date, account_ref) if as_of_date else {"doc_id": "", "doc_type": "", "created_at": ""}
        where = ["p.as_of_date = ?", "p.quantity != 0"]
        params: list = [as_of_date]
        if account_ref:
            where.append("p.account_ref = ?")
            params.append(account_ref)
        if selected.get("doc_id"):
            where.append("p.source_doc_id = ?")
            params.append(selected["doc_id"])
        q, p = wl_store._filtered(
            f"""SELECT p.quantity, p.market_value_base, i.symbol, i.instrument_type, i.name
               FROM positions p JOIN instruments i ON i.id = p.instrument_id
               WHERE {' AND '.join(where)}""",
            tuple(params), table="p")
        return [dict(r) for r in conn.execute(q, p).fetchall()], selected
    finally:
        conn.close()


def _freeze_snapshot(wl_store, account, positions, account_ref: str = "") -> str:
    rid = uuid.uuid4().hex[:12]
    payload = {"account": {k: account.get(k) for k in ("total_equity", "cash_balance")},
               "positions": [{"ticker": p["ticker"], "shares": p["shares"],
                              "market_value": p["market_value"]} for p in positions]}
    with wl_store._write_conn() as conn:
        conn.execute(
            f"""INSERT INTO vip_reports (id, kind, period, payload_json, account_ref, created_at{wl_store._user_insert_cols()}{wl_store._market_insert_cols()})
               VALUES (?,?,?,?,?,?{wl_store._user_insert_vals()}{wl_store._market_insert_vals()})""",
            (rid, "import_snapshot", _now_iso()[:10],
             json.dumps(payload, ensure_ascii=False, default=str), account_ref, _now_iso())
            + wl_store._user_insert_params() + wl_store._market_insert_params(),
        )
    return rid


def list_transactions(wl_store, *, account_ref: str = "", ticker: str = "", txn_type: str = "",
                      start_date: str = "", end_date: str = "", limit: int = 100, offset: int = 0) -> list[dict]:
    conn = wl_store._connect()
    try:
        sql = (
            "SELECT t.*, i.symbol, i.name, i.instrument_type "
            "FROM transactions t LEFT JOIN instruments i ON i.id = t.instrument_id WHERE 1=1"
        )
        params: list = []
        if account_ref:
            sql += " AND t.account_ref = ?"
            params.append(account_ref)
        if ticker:
            sql += " AND i.symbol = ?"
            params.append(normalize_ticker(ticker))
        if txn_type:
            sql += " AND t.txn_type = ?"
            params.append(txn_type)
        if start_date:
            sql += " AND t.trade_date >= ?"
            params.append(start_date)
        if end_date:
            sql += " AND t.trade_date <= ?"
            params.append(end_date)
        sql += " ORDER BY t.trade_date DESC, t.created_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        q, p = wl_store._filtered(sql, tuple(params), table="t")
        return [dict(r) for r in conn.execute(q, p).fetchall()]
    finally:
        conn.close()


def _overview_totals(rows: list[dict]) -> dict:
    totals = {
        "transaction_count": len(rows),
        "buy_amount": 0.0,
        "sell_amount": 0.0,
        "dividend_income": 0.0,
        "interest_income": 0.0,
        "fee_total": 0.0,
        "net_inflow": 0.0,
        "net_outflow": 0.0,
    }
    for row in rows:
        amt = float(row.get("net_amount") or 0.0)
        kind = row.get("txn_type") or ""
        if kind == "buy":
            totals["buy_amount"] += abs(amt)
        elif kind == "sell":
            totals["sell_amount"] += abs(amt)
        elif kind == "dividend":
            totals["dividend_income"] += amt
        elif kind == "interest":
            totals["interest_income"] += amt
        elif kind == "fee":
            totals["fee_total"] += abs(amt)
        if amt >= 0:
            totals["net_inflow"] += amt
        else:
            totals["net_outflow"] += abs(amt)
    return {k: round(v, 2) if isinstance(v, float) else v for k, v in totals.items()}



def build_account_overview(wl_store, *, account_ref: str = "") -> dict:
    summary = build_portfolio_summary(wl_store, account_ref=account_ref)
    rows = list_transactions(wl_store, account_ref=account_ref, limit=10000)
    return {
        **summary,
        **_overview_totals(rows),
        "realized_pnl": None,
        "realized_pnl_available": False,
    }


def _canonical_cost_map(wl_store, account_ref: str) -> dict[str, dict]:
    """规范层最新快照的逐标的成本/盈亏（Phase A：结算单直接解析所得）。
    返回 {symbol: {avg_cost, cost_basis, unrealized_pnl, as_of_date}}。无成本(结单未含)则值为 0/None。
    """
    rows, selected = _latest_positions(wl_store, "", account_ref)  # 复用"胜出快照"选择逻辑
    as_of = selected.get("created_at", "") or ""
    conn = wl_store._connect()
    try:
        where = ["p.quantity != 0"]
        params: list = []
        if account_ref:
            where.append("p.account_ref = ?")
            params.append(account_ref)
        if selected.get("doc_id"):
            where.append("p.source_doc_id = ?")
            params.append(selected["doc_id"])
        q, p = wl_store._filtered(
            f"""SELECT i.symbol, p.as_of_date, p.avg_cost, p.cost_basis, p.unrealized_pnl, p.market_value_base
               FROM positions p JOIN instruments i ON i.id = p.instrument_id
               WHERE {' AND '.join(where)}""",
            tuple(params), table="p")
        out: dict[str, dict] = {}
        for r in conn.execute(q, p).fetchall():
            cb = r["cost_basis"] or 0.0
            mv = r["market_value_base"] or 0.0
            out[r["symbol"]] = {
                "avg_cost": round(r["avg_cost"] or 0.0, 4) or None,
                "cost_basis": round(cb, 2) or None,
                "unrealized_pnl": round(r["unrealized_pnl"] or 0.0, 2) if cb else None,
                "unrealized_pnl_pct": round((mv - cb) / cb * 100, 2) if cb else None,
                "as_of_date": r["as_of_date"],
            }
        return out
    finally:
        conn.close()


def _price_coverage(wl_store, holdings: list, derivative_exposure: list) -> dict:
    """代码判定每个持仓/衍生品标的是否有活跃价源（market_snapshots 有收盘价）。

    无快照 = 无 yfinance 映射（港股/ISIN）或美股未回填 → 市值为结算单结转价、非最新行情。
    顾问层据此对这些标的的判断须谨慎；由代码给出未覆盖清单，LLM 只解释含义、不自行臆测。
    """
    syms = [h.get("ticker", "") for h in holdings] + [d.get("underlying", "") for d in derivative_exposure]
    get = getattr(wl_store, "get_latest_snapshot", None)
    covered, uncovered = {}, []
    for s in dict.fromkeys(x.strip() for x in syms if x and x.strip()):
        snap = get(s) if get else None
        if snap and snap.get("close") is not None:
            covered[s] = snap.get("date", "")
        else:
            uncovered.append(s)
    return {"uncovered": uncovered, "n_covered": len(covered),
            "n_total": len(covered) + len(uncovered), "as_of": covered}


def build_account_dossier(wl_store, *, account_ref: str = "") -> dict:
    """Phase A · 账户完整档案层——LLM 单一事实源。聚合此前碎在 7+ 调用里的账户全貌。

    口径原则（用户拍板）：
    - 头条"真实价值" = 结算单事实（股票 sim 权益 + 现金），**不含衍生品模型估值**。
    - 衍生品单列 `derivative_exposure`（敞口 + 条款），路径依赖/敲出风险由决策层单独消费。
    - 成本/已实现盈亏来自结算单直接解析（无则诚实留 None，不猜、不 FIFO 反推）。
    返回结构见函数末 return。
    """
    account_ref = (account_ref or "").strip()
    summary = build_portfolio_summary(wl_store, account_ref=account_ref)      # sim：真实权益/现金/持仓
    cost_map = _canonical_cost_map(wl_store, account_ref)                     # 规范层：成本/盈亏

    # 逐仓富化成本/盈亏（以 sim 持仓为准，成本从规范层按 symbol 贴合）
    holdings = []
    unrealized_total = 0.0
    cost_covered = 0
    for h in summary.get("holdings", []):
        c = cost_map.get(h["ticker"], {})
        upnl = c.get("unrealized_pnl")
        if upnl is not None:
            unrealized_total += upnl
            cost_covered += 1
        holdings.append({**h,
                         "avg_cost": c.get("avg_cost"),
                         "cost_basis": c.get("cost_basis"),
                         "unrealized_pnl": upnl,
                         "unrealized_pnl_pct": c.get("unrealized_pnl_pct")})

    # 交易流水聚合（净流入/买卖/分红/费用）
    txns = list_transactions(wl_store, account_ref=account_ref, limit=10000)
    totals = _overview_totals(txns)

    # 衍生品敞口（单列，不并入权益）
    try:
        from bottleneck_hunter.vip import derivatives as drv
        deriv = drv.list_derivative_terms(wl_store, account_ref=account_ref)
        derivative_exposure = [{
            "underlying": t.underlying_symbol, "family": t.product_family,
            "currency": t.currency, "tenor_days": t.tenor_days,
            "trade_date": t.terms.get("trade_date", ""), "expiry_date": t.terms.get("expiry_date", ""),
            "afp": t.terms.get("afp"), "knock_out_price": t.terms.get("knock_out_price"),
            "strike_pct_initial": t.terms.get("strike_pct_initial"),
        } for t in deriv]
    except Exception:  # noqa: BLE001 - 衍生品缺失绝不带崩档案
        derivative_exposure = []

    # 数据新鲜度（复用推算层）
    last_proj_date = wl_store.latest_projection_date(account_ref) if hasattr(wl_store, "latest_projection_date") else ""

    return {
        "account_ref": account_ref,
        # ── 头条：真实价值（结算单事实口径，不含衍生品估值）──
        "total_equity": summary.get("total_equity", 0.0),
        "cash_balance": summary.get("cash_balance", 0.0),
        "n_holdings": summary.get("n_holdings", 0),
        "top5_concentration_pct": summary.get("top5_concentration_pct", 0.0),
        # ── 逐仓（含成本/未实现盈亏，来自结算单直接解析）──
        "holdings": holdings,
        "unrealized_pnl_total": round(unrealized_total, 2) if cost_covered else None,
        "cost_coverage": {"covered": cost_covered, "total": len(holdings)},
        # ── 流水聚合 + 已实现盈亏（暂不可得，诚实标注）──
        "flows": totals,
        "realized_pnl": None,
        "realized_pnl_available": False,
        # ── 衍生品敞口（单列，路径依赖风险由决策层消费）──
        "derivative_exposure": derivative_exposure,
        # ── 价源覆盖（代码判定：无快照=无活跃价源，市值走结算单结转，判断须谨慎）──
        "price_coverage": _price_coverage(wl_store, holdings, derivative_exposure),
        # ── 价值曲线 + 新鲜度 ──
        "value_series": value_series(wl_store, account_ref=account_ref),
        "as_of_hint": {"latest_projection_date": last_proj_date},
    }




def _has_account_activity(wl_store, account_ref: str) -> bool:
    ref = (account_ref or "").strip()
    conn = wl_store._connect()
    try:
        for table in ("positions", "transactions", "vip_imports", "vip_derivative_terms"):
            q, p = wl_store._filtered(f"SELECT 1 FROM {table} WHERE account_ref = ? LIMIT 1", (ref,), table=table)
            if conn.execute(q, p).fetchone():
                return True
        return False
    finally:
        conn.close()



def _visible_account_refs(wl_store) -> list[str]:
    refs: list[str] = []
    for row in wl_store.list_vip_accounts(include_hidden_default=False):
        ref = (row.get("account_ref") or "").strip()
        if ref and ref not in refs:
            refs.append(ref)
    return refs



def build_total_overview(wl_store) -> dict:
    account_rows = wl_store.list_vip_accounts(include_hidden_default=False)
    account_meta = {((row.get("account_ref") or "").strip()): row for row in account_rows}
    refs = _visible_account_refs(wl_store)

    accounts: list[dict] = []
    holdings: list[dict] = []
    total_equity = cash_balance = top5 = 0.0
    n_holdings = 0
    for ref in refs:
        summary = build_portfolio_summary(wl_store, account_ref=ref)
        meta = account_meta.get(ref, {})
        account_total = summary.get("total_equity", 0.0) or 0.0
        account_cash = summary.get("cash_balance", 0.0) or 0.0
        account_n_holdings = summary.get("n_holdings", 0) or 0
        total_equity += account_total
        cash_balance += account_cash
        n_holdings += account_n_holdings
        for item in summary.get("holdings", []):
            holdings.append({**item, "account_ref": ref})
        accounts.append({
            "account_ref": ref,
            "display_name": meta.get("display_name") or ref,
            "institution_name": meta.get("institution_name", ""),
            "account_kind": meta.get("account_kind", "broker"),
            "total_equity": round(account_total, 2),
            "cash_balance": round(account_cash, 2),
            "n_holdings": account_n_holdings,
        })
    holdings.sort(key=lambda x: x.get("market_value", 0), reverse=True)
    if total_equity > 0:
        for item in holdings:
            item["weight_pct"] = round((item.get("market_value", 0) or 0.0) / total_equity * 100, 2)
        for row in accounts:
            row["weight_pct"] = round((row.get("total_equity", 0.0) or 0.0) / total_equity * 100, 2)
        top5 = sum(item.get("weight_pct", 0.0) for item in holdings[:5])
    rows = list_transactions(wl_store, limit=10000)
    return {
        "total_equity": round(total_equity, 2),
        "cash_balance": round(cash_balance, 2),
        "total_loan_limit": None,
        "n_accounts": len(accounts),
        "n_holdings": n_holdings,
        "accounts": accounts,
        "holdings": holdings,
        "top5_concentration_pct": round(top5, 1),
        "account_ref": "all",
        **_overview_totals(rows),
        "realized_pnl": None,
        "realized_pnl_available": False,
    }


def _forward_filled_series(rows) -> list[dict]:
    """多账户价值曲线：按 as_of_date 并集，各账户以「该日或之前最近一次快照」前向填充再求和。

    不同账户的月结单/持仓报告快照日期通常不一致；若直接按当日 SUM，缺当日快照的账户会被当成
    0，导致总额虚假跳变（例：A 账户只在 4 月有快照、B 账户只在 7 月有快照，7 月总额会漏掉 A）。
    前向填充后：账户自首个快照起持续计入其最近已知市值，尚无任何快照的账户在该日计 0。
    rows: [{a: account_ref, d: as_of_date, eq: Σmarket_value_base}]，按 d 升序。
    """
    by_acct: dict[str, dict[str, float]] = {}
    dates: set[str] = set()
    for r in rows:
        d = r["d"]
        if not d:
            continue
        by_acct.setdefault(r["a"], {})[d] = r["eq"] or 0.0
        dates.add(d)
    out: list[dict] = []
    for d in sorted(dates):
        total = 0.0
        for snaps in by_acct.values():
            prior = [dd for dd in snaps if dd <= d]
            if prior:
                total += snaps[max(prior)]
        out.append({"as_of_date": d, "total_equity": round(total, 2)})
    return out


def _latest_truth_mv_map(wl_store, account_ref: str) -> dict:
    """该账户真值最新快照日的 {symbol: market_value_base}，作为推算点的 carry-forward 底座。"""
    conn = wl_store._connect()
    try:
        q, p = wl_store._filtered(
            "SELECT MAX(as_of_date) AS d FROM positions WHERE account_ref = ?",
            (account_ref,), table="positions")
        row = conn.execute(q, p).fetchone()
        as_of = (row["d"] if row and row["d"] else "") or ""
        if not as_of:
            return {}
        q, p = wl_store._filtered(
            """SELECT i.symbol AS s, p.market_value_base AS mv
               FROM positions p JOIN instruments i ON i.id = p.instrument_id
               WHERE p.account_ref = ? AND p.as_of_date = ? AND p.quantity != 0""",
            (account_ref, as_of), table="p")
        return {r["s"]: (r["mv"] or 0.0) for r in conn.execute(q, p).fetchall()}
    finally:
        conn.close()


def _projection_point(wl_store, account_ref: str = "") -> dict | None:
    """构造「最近一次系统推算」的价值曲线点（整账户口径）。

    单账户：以真值最新快照的各标的市值为底，已重估标的替换为推算值，
            未重估（缺行情源/衍生品）的标的 carry-forward 沿用真值——
            否则会把缺价标的当成 0，与整账户口径的真值点相比造成虚假断崖。
    全账户：对每个可见账户各自合并后相加（各账户推算日可不一致）。
    返回 {as_of_date, total_equity, is_projected: True} 或 None（无推算）。
    """
    refs = [account_ref.strip()] if (account_ref or "").strip() else _visible_account_refs(wl_store)
    total = 0.0
    latest_date = ""
    found = False
    for ref in refs:
        pmap = wl_store.latest_projection_map(ref)
        if not pmap:
            continue
        found = True
        merged = dict(_latest_truth_mv_map(wl_store, ref))
        for sym, row in pmap.items():
            mv = row.get("market_value_base")
            merged[sym] = mv if mv is not None else merged.get(sym, 0.0)
        total += sum(merged.values())
        d = wl_store.latest_projection_date(ref)
        if d > latest_date:
            latest_date = d
    if not found or not latest_date:
        return None
    return {"as_of_date": latest_date, "total_equity": round(total, 2), "is_projected": True}


def _rebase_benchmark(series: list[dict], snaps: list[dict]) -> bool:
    """把指数收盘按每个 as_of_date 对齐(取 ≤ 该日最近收盘)并 rebase 到起始权益，
    就地给 series 点加 benchmark_value。无有效收盘→不改、返回 False。"""
    closes = sorted(((s["date"][:10], s["close"]) for s in snaps
                     if s.get("close") and s.get("date")), key=lambda x: x[0])
    if not series or not closes:
        return False

    def _on_or_before(d: str):
        hit = None
        for cd, cv in closes:
            if cd <= d:
                hit = cv
            else:
                break
        return hit

    base = _on_or_before(series[0]["as_of_date"])
    if not base:
        return False
    base_eq = series[0]["total_equity"]
    for pt in series:
        c = _on_or_before(pt["as_of_date"])
        if c:
            pt["benchmark_value"] = round(base_eq * c / base, 2)
    return True


def value_series(wl_store, *, account_ref: str = "") -> dict:
    """按 positions.as_of_date 聚合 Σmarket_value_base → 价值曲线；派生逐期收益率。

    无按日真实净值，用月结单期末快照拼点（导入越多期越密）。曲线为持仓市值口径（不含现金，
    因现金未按日留存）。多账户聚合按账户前向填充，避免快照日期不齐造成虚假跳变。
    末尾若存在比最新真值快照更新的系统推算，则叠加一个 is_projected 点（虚线展示，待校准）。
    返回 {series:[{as_of_date,total_equity,is_projected?}], returns:[{period,pct}]}。
    """
    conn = wl_store._connect()
    try:
        if account_ref:
            sql = ("SELECT as_of_date AS d, SUM(market_value_base) AS eq FROM positions "
                   "WHERE quantity != 0 AND account_ref = ? GROUP BY as_of_date ORDER BY as_of_date")
            q, p = wl_store._filtered(sql, (account_ref,), table="positions")
            series = [{"as_of_date": r["d"], "total_equity": round(r["eq"] or 0.0, 2)}
                      for r in conn.execute(q, p).fetchall() if r["d"]]
        else:
            refs = _visible_account_refs(wl_store)
            if not refs:
                series = []
            else:
                placeholders = ",".join("?" for _ in refs)
                sql = ("SELECT account_ref AS a, as_of_date AS d, SUM(market_value_base) AS eq "
                       f"FROM positions WHERE quantity != 0 AND account_ref IN ({placeholders}) "
                       "GROUP BY account_ref, as_of_date ORDER BY as_of_date")
                q, p = wl_store._filtered(sql, tuple(refs), table="positions")
                series = _forward_filled_series(conn.execute(q, p).fetchall())
    finally:
        conn.close()
    # 叠加系统推算点：仅当推算日严格晚于最新真值快照日（否则真值优先，不覆盖）
    proj = _projection_point(wl_store, account_ref=account_ref)
    if proj and (not series or proj["as_of_date"] > series[-1]["as_of_date"]):
        series = series + [proj]
    returns = []
    for prev, cur in zip(series, series[1:]):
        base = prev["total_equity"]
        pct = ((cur["total_equity"] - base) / base * 100) if base else 0.0
        returns.append({"period": cur["as_of_date"], "pct": round(pct, 2),
                        "is_projected": bool(cur.get("is_projected"))})
    # A: 叠加大盘基准对照（rebase 到起始权益，同轴可比）。指数无历史→不加键，诚实缺省。
    benchmark_meta = None
    if series:
        from bottleneck_hunter.watchlist.macro_data import default_benchmark_ticker
        bench_code, bench_label = default_benchmark_ticker(getattr(wl_store, "_market", "") or "us_stock")
        try:
            snaps = wl_store.get_snapshots(bench_code, days=1000)
        except Exception:
            snaps = []
        if _rebase_benchmark(series, snaps):
            benchmark_meta = {"ticker": bench_code, "label": bench_label}
    return {"series": series, "returns": returns, "benchmark": benchmark_meta}


def missing_data_report(wl_store, *, account_ref: str = "") -> list[dict]:
    """数据体检：列出还缺哪些数据、如何补充。返回 [{code,label,hint,severity}]。"""
    conn = wl_store._connect()
    try:
        def cnt(sql: str, params: tuple, table: str) -> int:
            q, p = wl_store._filtered(sql, params, table=table)
            return conn.execute(q, p).fetchone()[0]
        account_ref = (account_ref or "").strip()
        params = (account_ref,) if account_ref else ()
        pos_where = " AND account_ref = ?" if account_ref else ""
        txn_where = " WHERE account_ref = ?" if account_ref else ""
        deriv_where = " WHERE account_ref = ?" if account_ref else ""
        n_pos = cnt(f"SELECT COUNT(*) FROM positions WHERE quantity != 0{pos_where}", params, "positions")
        n_txn = cnt(f"SELECT COUNT(*) FROM transactions{txn_where}", params, "transactions")
        n_dates = cnt(f"SELECT COUNT(DISTINCT as_of_date) FROM positions WHERE quantity != 0{pos_where}", params, "positions")
        n_deriv = cnt(f"SELECT COUNT(*) FROM vip_derivative_terms{deriv_where}", params, "vip_derivative_terms")
    finally:
        conn.close()
    out: list[dict] = []
    if not n_pos:
        out.append({"code": "positions", "label": "持仓数据",
                    "hint": "上传月结单以建立持仓快照", "severity": "high"})
    if not n_txn:
        out.append({"code": "transactions", "label": "交易流水",
                    "hint": "上传交易导出文件以记录买卖/分红/费用", "severity": "medium"})
    if n_dates < 2:
        out.append({"code": "value_series", "label": "价值曲线数据",
                    "hint": "至少导入 2 期月结单，才能绘制价值变化曲线", "severity": "low"})
    if not n_deriv:
        out.append({"code": "derivatives", "label": "衍生品条款",
                    "hint": "如持有累积器/结构化产品，上传条款文件以建模或有敞口", "severity": "low"})
    return out


# ── P5: 报告 —— sim_* → 组合摘要 →（LLM 叙事，M1 可选）→ 落库 ──────────

def build_portfolio_summary(wl_store, *, account_ref: str = "") -> dict:
    """从 sim_* 汇总组合结构（不调 LLM）：总权益、现金、持仓明细、集中度 Top5。供报告与 number_guard facts。"""
    account = wl_store.get_sim_account(account_ref=account_ref)
    positions = sorted(wl_store.get_sim_positions(account["id"]),
                       key=lambda p: p.get("market_value", 0), reverse=True)
    total = account.get("total_equity", 0) or 0
    cash = account.get("cash_balance", 0) or 0
    holdings = [{"ticker": p["ticker"], "shares": p["shares"],
                 "market_value": round(p.get("market_value", 0), 2),
                 "weight_pct": p.get("weight_pct", 0)} for p in positions]
    top5 = sum(p["weight_pct"] for p in holdings[:5])
    return {"total_equity": round(total, 2), "cash_balance": round(cash, 2),
            "n_holdings": len(holdings),
            "holdings": holdings, "top5_concentration_pct": round(top5, 1),
            "account_ref": account.get("account_ref", account_ref or "")}


def render_derivative_summary(terms: list) -> str:
    """把已抽条款的结构化产品压成风险摘要 Markdown（供报告附录/风险提示）。"""
    if not terms:
        return ""
    L = ["## 衍生品 / 结构化产品风险摘要", ""]
    for t in terms:
        if t.product_family in ("equity_accumulator", "equity_decumulator"):
            kind = "累积器" if t.product_family.endswith("accumulator") else "减持器"
            L.append(f"- **{t.underlying_symbol} {kind}**：AFP {t.terms.get('afp')}, KO {t.terms.get('knock_out_price')}, "
                     f"DS {t.terms.get('daily_shares')}, Step-up {t.terms.get('step_up_daily_shares')}。"
                     f"风险在于标的跌破 AFP 时会按 Step-up 股数累积，路径依赖强。")
        elif t.product_family == "equity_mli_booster":
            L.append(f"- **{t.underlying_symbol} MLI Booster**：KI={t.terms.get('knock_in_pct_initial', 0)*100:.2f}% 初始价，"
                     f"Strike={t.terms.get('strike_pct_initial', 0)*100:.0f}% 初始价，上行封顶 {t.terms.get('max_upside_pct', 0)*100:.0f}%。"
                     f"若触发 KI 且到期低于 Strike，将承受与标的下跌类似的损失。")
    L.append("")
    return "\n".join(L)


def render_report_md(summary: dict, narrative: str = "", period: str = "", derivatives_md: str = "") -> str:
    """渲染报告 Markdown（append-lines 风格，仿 chain/report.py）。narrative 已过 number_guard。"""
    L: list[str] = []
    L.append(f"# 持仓分析报告{f'（{period}）' if period else ''}")
    L.append("")
    L.append(f"- 组合总权益：**${summary['total_equity']:,.2f}**（统一美元口径）")
    L.append(f"- 其中可投资现金：**${summary['cash_balance']:,.2f}**")
    L.append(f"- 持仓数：{summary['n_holdings']} 只")
    L.append(f"- 前五大集中度：{summary['top5_concentration_pct']}%")
    L.append("")
    L.append("## 持仓明细")
    L.append("")
    L.append("| 代码 | 数量 | 市值(USD) | 占比 |")
    L.append("|---|---:|---:|---:|")
    for h in summary["holdings"]:
        L.append(f"| {h['ticker']} | {h['shares']:,} | ${h['market_value']:,.2f} | {h['weight_pct']}% |")
    L.append("")
    if narrative:
        L.append("## AI 分析")
        L.append("")
        L.append(narrative)
        L.append("")
    if derivatives_md:
        L.append(derivatives_md)
    return compliance.with_disclaimer("\n".join(L))


_ADVISOR_PROMPT = """你是一支资深私人财务顾问团队，为高净值客户的真实证券组合出具投资分析意见。
下面是客户当前组合快照（统一美元口径，数据真实、请勿臆造任何数字）：

{facts}

{mandate}

请用简体中文、分三层给出专业意见，每层 2-4 句，务实不空泛，**只依据上面给出的数字**，
不要编造快照里没有的价格/收益/占比：

## 一、宏观研判
（当前宏观与所处行业周期对该组合的影响判断）

## 二、组合配置诊断
（集中度、行业/单票暴露、结构性风险；点名占比过高的持仓）

## 三、操作建议
（给出方向性建议：加/减/持/对冲，说明理由；不承诺收益；**须贴合上面「本账户投资纲领」的风险偏好/回撤上限/聚焦方向/排除清单**）

要求：直接输出上述三段 Markdown，不要额外前言/结语/免责（系统会另加免责声明）。"""


async def generate_advisor_narrative(summary: dict, *, user_id: str = "",
                                     budget=None, mandate_text: str = "") -> dict:
    """调 vip_advisor 角色生成分层叙事（宏观/配置/操作）。返回 {narrative, provider, model}。

    facts=组合摘要；mandate_text=本账户投资纲领块（可空）；叙事回来后由 generate_vip_report 过 number_guard。
    预算不足或无可用模型 → narrative 空（报告降级为纯数据报告，不阻断）。
    """
    from bottleneck_hunter.llm_clients.factory import get_models_for_role

    if budget is not None and not budget.can_spend():
        return {"narrative": "", "provider": "", "model": "", "skipped": "budget"}
    try:
        results = get_models_for_role("vip_advisor", user_id=user_id, with_fallback=True)
    except Exception:  # noqa: BLE001
        results = []
    if not results:
        return {"narrative": "", "provider": "", "model": "", "skipped": "no_model"}
    llm, provider, model = results[0]
    facts = json.dumps(summary, ensure_ascii=False, default=str)
    prompt = _ADVISOR_PROMPT.format(facts=facts, mandate=mandate_text or "## 本账户投资纲领\n用户尚未设定，按中性稳健处理即可。")
    try:
        resp = await llm.ainvoke(prompt)
        text = getattr(resp, "content", resp)
        text = text if isinstance(text, str) else str(text)
    except Exception as e:  # noqa: BLE001
        return {"narrative": "", "provider": provider, "model": model, "error": str(e)[:200]}
    if budget is not None:
        try:
            budget.record(0)   # token 计费由 LLM 层记；此处仅占位，避免重复计
        except Exception:  # noqa: BLE001
            pass
    return {"narrative": text.strip(), "provider": provider, "model": model}


async def generate_vip_report_ai(wl_store, *, period: str = "",
                                 source_doc_ids: list | None = None,
                                 user_id: str = "", budget=None,
                                 derivative_terms: list | None = None,
                                 account_ref: str = "") -> dict:
    """异步：组合摘要 → vip_advisor 分层叙事 → number_guard → 落库。M1 报告的 AI 增强入口。"""
    from bottleneck_hunter.vip import mandate as _mandate
    summary = build_portfolio_summary(wl_store, account_ref=account_ref)
    mandate_text = _mandate.format_mandate_for_prompt(wl_store, account_ref=account_ref)
    nar = await generate_advisor_narrative(summary, user_id=user_id, budget=budget, mandate_text=mandate_text)
    return generate_vip_report(
        wl_store, period=period, narrative=nar.get("narrative", ""),
        source_doc_ids=source_doc_ids,
        model_provider=nar.get("provider", ""), model_name=nar.get("model", ""),
        derivative_terms=derivative_terms, account_ref=account_ref)


def generate_vip_report(wl_store, *, period: str = "", narrative: str = "",
                        source_doc_ids: list | None = None,
                        model_provider: str = "", model_name: str = "",
                        derivative_terms: list | None = None,
                        account_ref: str = "") -> dict:
    """生成并落库一份持仓分析报告。narrative 为 LLM 叙事段（可空=纯数据报告）。

    narrative 渲染前**强制过 number_guard**（facts=组合摘要），未核到的金额/占比标"⚠未核到"。
    落 vip_reports(kind='periodic') + advice_audit_trail。返回 {report_id, report_md, unverified}。
    """
    import hashlib

    summary = build_portfolio_summary(wl_store, account_ref=account_ref)
    facts = json.dumps(summary, ensure_ascii=False, default=str)

    unverified = []
    if narrative:
        checks = number_guard.verify_numbers(narrative, facts)
        unverified = [c["token"] for c in checks if c["status"] == "unverified"]
        narrative = number_guard.annotate_unverified(narrative, facts)

    report_md = render_report_md(summary, narrative, period,
                                 derivatives_md=render_derivative_summary(derivative_terms or []))

    rid = uuid.uuid4().hex[:12]
    with wl_store._write_conn() as conn:
        conn.execute(
            f"""INSERT INTO vip_reports (id, kind, period, report_md, payload_json, account_ref, created_at{wl_store._user_insert_cols()}{wl_store._market_insert_cols()})
               VALUES (?,?,?,?,?,?,?{wl_store._user_insert_vals()}{wl_store._market_insert_vals()})""",
            (rid, "periodic", period, report_md,
             json.dumps(summary, ensure_ascii=False, default=str), account_ref, _now_iso())
            + wl_store._user_insert_params() + wl_store._market_insert_params(),
        )

    # 审计（auth.db）
    try:
        from bottleneck_hunter.auth.store import AuthStore
        uid = getattr(wl_store, "_user_id", "") or ""
        if uid:
            AuthStore().create_advice_audit(
                uid, advice_type="report", advice_ref=rid,
                source_doc_ids=source_doc_ids or [],
                source_data_ref={"report_snapshot_id": rid, "tickers": [h["ticker"] for h in summary["holdings"]]},
                model_provider=model_provider, model_name=model_name,
                disclaimer_version=compliance.DISCLAIMER_VERSION,
                content_hash=hashlib.sha256(report_md.encode()).hexdigest(),
                market=getattr(wl_store, "_market", "") or "us_stock")
    except Exception:  # noqa: BLE001
        pass

    return {"report_id": rid, "report_md": report_md, "unverified": unverified}


if __name__ == "__main__":
    # ponytail 自检：_rebase_benchmark 纯逻辑（净值基准对齐 + rebase，money path）——DB 路径不入自检
    # 1) 同起点、指数 +10% → 基准点 rebase 到起始权益后同比例 +10%
    s = [{"as_of_date": "2026-01-01", "total_equity": 1000.0},
         {"as_of_date": "2026-02-01", "total_equity": 900.0}]
    assert _rebase_benchmark(s, [{"date": "2026-01-01", "close": 100.0},
                                 {"date": "2026-02-01", "close": 110.0}]) is True
    assert s[0]["benchmark_value"] == 1000.0 and s[1]["benchmark_value"] == 1100.0, s

    # 2) 日期空洞：as_of_date 无恰好收盘 → 取 ≤ 该日最近收盘（不外推未来交易日）
    s2 = [{"as_of_date": "2026-01-01", "total_equity": 1000.0},
          {"as_of_date": "2026-02-15", "total_equity": 950.0}]
    assert _rebase_benchmark(s2, [{"date": "2026-01-01", "close": 100.0},
                                  {"date": "2026-02-10", "close": 120.0},
                                  {"date": "2026-03-01", "close": 130.0}]) is True
    assert s2[1]["benchmark_value"] == 1200.0, s2  # 取 02-10 收盘、不外推 03-01

    # 3) 空 snaps → False、不写键（诚实缺省，不画假平线）
    s3 = [{"as_of_date": "2026-01-01", "total_equity": 1000.0}]
    assert _rebase_benchmark(s3, []) is False and "benchmark_value" not in s3[0]

    # 4) 起始日早于最早收盘 → base 取不到 → False（账户历史超出已抓指数历史时的诚实缺省）
    s4 = [{"as_of_date": "2025-06-01", "total_equity": 1000.0}]
    assert _rebase_benchmark(s4, [{"date": "2026-01-01", "close": 100.0}]) is False
    assert "benchmark_value" not in s4[0]

    print("portfolio self-check OK")
