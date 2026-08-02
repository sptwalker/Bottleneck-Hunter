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
    # 重解析即权威：本 doc 之前写过的持仓先清掉，再按当前解析重写。否则旧代码/旧版式解析出的
    # 幽灵持仓（如 CMBI 早期把 FCN 当股票落的行）在 holdings 变空后永久粘住，重新导入也删不掉
    # （_upsert_position 只按 (account_ref,instrument_id,as_of) upsert，从不删除已消失的标的）。
    # 导入器对非 parsed_ok 的 position/monthly 会提前返回、根本不进本函数，故解析失败不会误删真仓。
    if source_doc_id:
        with wl_store._write_conn() as conn:
            q, p = wl_store._filtered("DELETE FROM positions WHERE source_doc_id = ?", (source_doc_id,))
            conn.execute(q, p)
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
                             unrealized_pnl=h.unrealized_pnl_usd,
                             market_value_nominal=h.market_value_nominal)  # 原币市值→fx 隐含反算
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
                     avg_cost=None, cost_basis=None, unrealized_pnl=None,
                     market_value_nominal=None, fx_rate=None) -> None:
    # P0 币种敞口：market_value 列历来是 market_value_base 的死镜像（无 SQL 读点，已核）。
    # 现改存"原币市值"，fx_rate/market_price 一并回填 → 币种敞口 + FX 归因可见。
    # 缺原币口径（旧解析器/未重导）时回落 base、fx=1.0，与旧行为字节等价（诚实降级）。
    mv_nominal = market_value_nominal if (market_value_nominal not in (None, 0)) else market_value_base
    fx = fx_rate if (fx_rate not in (None, 0)) else (
        round(market_value_base / mv_nominal, 6) if mv_nominal else 1.0)
    mv_price = round(mv_nominal / quantity, 6) if quantity else 0.0   # 原币单价（供 P1 tooltip）
    with wl_store._write_conn() as conn:
        # 幂等：同 (account_ref, instrument_id, as_of_date) 已存在则更新
        q, p = wl_store._filtered(
            "SELECT id FROM positions WHERE account_ref = ? AND instrument_id = ? AND as_of_date = ?",
            (account_ref, instrument_id, as_of_date))
        row = conn.execute(q, p).fetchone()
        if row:
            q2, p2 = wl_store._filtered(
                "UPDATE positions SET quantity=?, market_value_base=?, market_value=?, market_price=?, "
                "fx_rate=?, currency=?, avg_cost=?, cost_basis=?, unrealized_pnl=?, source_doc_id=? WHERE id=?",
                (quantity, market_value_base, mv_nominal, mv_price, fx, currency,
                 avg_cost or 0, cost_basis or 0, unrealized_pnl or 0, source_doc_id, row["id"]))
            conn.execute(q2, p2)
            return
        pid = uuid.uuid4().hex[:12]
        conn.execute(
            f"""INSERT INTO positions
               (id, instrument_id, account_ref, as_of_date, quantity, currency,
                avg_cost, cost_basis, unrealized_pnl, market_price, fx_rate,
                market_value, market_value_base, source_doc_id, created_at"""
            f"""{wl_store._user_insert_cols()}{wl_store._market_insert_cols()})
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?"""
            f"""{wl_store._user_insert_vals()}{wl_store._market_insert_vals()})""",
            (pid, instrument_id, account_ref, as_of_date, quantity, currency,
             avg_cost or 0, cost_basis or 0, unrealized_pnl or 0, mv_price, fx,
             mv_nominal, market_value_base, source_doc_id, _now_iso())
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


def _backfill_cost(wl_store, acct_id, rows) -> int:
    """把带成本的快照(rows)的每股成本/未实现盈亏按 symbol 回填到 live sim 现有持仓。
    只补成本相关字段，绝不动 shares/current_price/market_value（那是当前快照的权威值）；
    仅当 live 行"无成本"(avg_cost 缺失或恰等于现价即 pnl≈0)且本次有真成本时才覆盖 → 不会拿旧成本
    覆盖已有真成本。返回回填条数。ponytail: 按 ticker 匹配，同标的多持仓行少见，命中首个即可。"""
    live = {p["ticker"]: p for p in wl_store.get_sim_positions(acct_id)}
    n = 0
    for r in rows:
        cost = r["avg_cost"] or 0.0
        qty = r["quantity"] or 0.0
        if cost <= 0 and r["cost_basis"] and qty:
            cost = r["cost_basis"] / qty
        if cost <= 0:
            continue
        symbol, _ = _map_symbol(r["symbol"])
        pos = live.get(symbol) or live.get(r["symbol"])
        if not pos:
            continue
        cur = pos.get("current_price") or 0.0
        had_cost = (pos.get("avg_cost") or 0.0) > 0 and abs((pos.get("avg_cost") or 0.0) - cur) > 1e-6
        if had_cost:  # live 已有真成本(与现价不同)→ 不覆盖
            continue
        shares = pos.get("shares") or 0
        upnl = round((cur - cost) * shares, 2) if cur else 0.0
        wl_store.update_sim_position(pos["id"], avg_cost=round(cost, 6), unrealized_pnl=upnl)
        n += 1
    return n


def materialize_portfolio(wl_store, as_of_date: str = "", account_ref: str = "",
                          cash_total_usd: float = 0.0,
                          account_total_usd: float | None = None,
                          loan_total_usd: float | None = None) -> dict:
    """把某快照日的规范 positions 投影到 sim_*，供决策引擎消费。

    先把旧 sim 快照冻结进 vip_reports(kind='import_snapshot')作溯源锚（M2），再清零重建。
    market_value_base(统一美元)→ sim_positions.market_value；
    - 默认：总权益 = Σ持仓 + 现金(cash_total_usd)
    - 若账户层有更权威锚（如 Nomura NAV），可显式传 account_total_usd 覆盖总权益口径
    返回 {account_id, n_positions, total_equity, cash_balance, snapshot_report_id}。
    """
    account = wl_store.get_sim_account(account_ref=account_ref)
    acct_id = account["id"]

    # 贷款是账户级元数据（结单口径已用融资/负债），与持仓时效无关：只要本次结单带了贷款字段就落库，
    # 不受下面「空快照 / 陈旧覆盖」两道持仓护栏拦截（否则花旗这类"贷款在月结单、最新持仓在另一份导出"
    # 的账户，贷款会因月结单被陈旧护栏拦掉而永远写不进）。position_report 不含该字段 → 传 None → 不覆盖。
    if loan_total_usd is not None:
        wl_store.update_sim_account(account_ref=account_ref, loan_balance=round(loan_total_usd, 2))

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
        # 成本基是持仓自带属性，与快照时效无关（仿贷款/衍生品的处理）：本次快照虽被陈旧护栏挡下不作
        # 实时仓位，但若它带成本、而现有 live 快照(如无成本的仓盘导出)缺成本，就按标的把成本/盈亏回填到
        # live 现有持仓——否则花旗这类"最新持仓在无成本的仓盘导出、成本只在月结单"的账户，成本/颜色永进不来。
        n_back = _backfill_cost(wl_store, acct_id, rows)
        return {"account_id": acct_id, "n_positions": 0, "n_cost_backfilled": n_back,
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
        cur_price = (mv / qty) if qty else 0.0          # 当前每股价（市值/数量）
        # 成本优先用结单抽出的每股成本；缺失(如仓盘导出不含成本)则回落当前价 → pnl=0，前端色显中性
        cost = r["avg_cost"] or 0.0
        if cost <= 0 and r["cost_basis"] and qty:
            cost = r["cost_basis"] / qty
        avg = cost if cost > 0 else cur_price
        # 未实现盈亏优先用结单值；缺失则由 市值−成本 自算（成本回落当前价时自然为 0）
        upnl = r["unrealized_pnl"]
        if upnl is None:
            upnl = round(mv - avg * qty, 2)
        pid = wl_store.create_sim_position(acct_id, symbol, int(qty), avg)
        wl_store.update_sim_position(
            pid, current_price=cur_price, market_value=mv, unrealized_pnl=round(upnl, 2),
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
    # C-4 轻量归因：确定性 diff 旧 sim 持仓 vs 本次导入，写「推断·非确认」备忘（独立 try，绝不带崩导入）
    try:
        from bottleneck_hunter.vip import attribution
        attribution.run_attribution(wl_store, account_ref, old_positions, rows)
    except Exception:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning("归因复盘失败 (acct=%s)", account_ref, exc_info=True)
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
            f"""SELECT p.quantity, p.market_value_base, p.avg_cost, p.cost_basis, p.unrealized_pnl,
                      i.symbol, i.instrument_type, i.name
               FROM positions p JOIN instruments i ON i.id = p.instrument_id
               WHERE {' AND '.join(where)}""",
            tuple(params), table="p")
        return [dict(r) for r in conn.execute(q, p).fetchall()], selected
    finally:
        conn.close()


def _snapshot_dates(wl_store, account_ref: str, limit: int = 2) -> list[str]:
    """账户最近 N 个不同持仓快照日(as_of_date DESC)——供相邻两期贡献归因取「相邻两期」。"""
    conn = wl_store._connect()
    try:
        where = ["quantity != 0"]
        params: list = []
        if account_ref:
            where.append("account_ref = ?")
            params.append(account_ref)
        q, p = wl_store._filtered(
            f"SELECT DISTINCT as_of_date FROM positions WHERE {' AND '.join(where)} "
            "ORDER BY as_of_date DESC LIMIT ?", tuple(params) + (limit,), table="positions")
        return [r["as_of_date"] for r in conn.execute(q, p).fetchall() if r["as_of_date"]]
    finally:
        conn.close()


def _contribution(wl_store, account_ref: str) -> dict:
    """P3-3 · 标的贡献归因：最近相邻两期胜出快照，期初权重 × 单价收益(mv/qty 还原单价，剔买卖污染)。

    权重分母 = 期初股票腿市值和(自洽的股票腿归因，不含现金)。不足两期/无重叠可定价标的 → 空结果 + note。
    数据源与概览/成本层同一「胜出快照」(_latest_positions)，口径一致。
    """
    from bottleneck_hunter.vip import metrics as _m
    dates = _snapshot_dates(wl_store, account_ref, limit=2)
    if len(dates) < 2:
        return {"rows": [], "prev_date": "", "cur_date": "", "coverage": "0/0",
                "note": "不足两期持仓快照，无法做相邻期归因"}
    cur_date, prev_date = dates[0], dates[1]   # DESC：[0] 最新期、[1] 上一期
    prev_rows, _ = _latest_positions(wl_store, prev_date, account_ref)
    cur_rows, _ = _latest_positions(wl_store, cur_date, account_ref)
    prev_h = [{"symbol": r["symbol"], "quantity": r.get("quantity"),
               "market_value_base": r.get("market_value_base")} for r in prev_rows]
    cur_h = [{"symbol": r["symbol"], "quantity": r.get("quantity"),
              "market_value_base": r.get("market_value_base")} for r in cur_rows]
    prev_total = sum(float(h["market_value_base"] or 0.0) for h in prev_h)
    rows = _m.contribution_attribution(prev_h, cur_h, prev_total)
    return {"rows": rows, "prev_date": prev_date, "cur_date": cur_date,
            "coverage": f"{len(rows)}/{len(prev_h)}",
            "note": "" if rows else "相邻两期无重叠可定价标的，归因留空(不臆造)"}


def _nominal_fx_rows(wl_store, as_of_date: str, account_ref: str) -> list[dict]:
    """某快照日的原币市值 + 点位 FX（喂汇率归因）：取胜出快照的 p.market_value(原币) / p.fx_rate。

    与 _latest_positions 同一「胜出快照」选择口径，只是多带 nominal/fx 两列（P0 已落库）。
    """
    selected = _winning_snapshot_meta(wl_store, as_of_date, account_ref) if as_of_date else {}
    conn = wl_store._connect()
    try:
        where = ["p.as_of_date = ?", "p.quantity != 0"]
        params: list = [as_of_date]
        if account_ref:
            where.append("p.account_ref = ?")
            params.append(account_ref)
        if selected.get("doc_id"):
            where.append("p.source_doc_id = ?")
            params.append(selected["doc_id"])
        q, p = wl_store._filtered(
            f"""SELECT i.symbol, p.quantity, p.market_value AS mv_nominal, p.fx_rate AS fx
               FROM positions p JOIN instruments i ON i.id = p.instrument_id
               WHERE {' AND '.join(where)}""",
            tuple(params), table="p")
        return [dict(r) for r in conn.execute(q, p).fetchall()]
    finally:
        conn.close()


def _fx_contribution(wl_store, account_ref: str) -> dict:
    """特性一 P2 · 汇率损益归因：相邻两期期末 vs 期末的本币价收益 r_local + 汇率收益 r_fx（点位口径）。

    逐日 FX 时序是真数据缺口（data_provider 无 FX 适配），本期只做「期末 vs 期末」点位归因（P2 deferred 逐日）。
    数据源与贡献归因同「胜出快照」，缺 nominal/fx 锚的行 fx 腿留空（fx_attribution 内诚实降级）。
    """
    from bottleneck_hunter.vip import metrics as _m
    dates = _snapshot_dates(wl_store, account_ref, limit=2)
    if len(dates) < 2:
        return {"rows": [], "prev_date": "", "cur_date": "", "coverage": "0/0",
                "note": "不足两期持仓快照，无法做相邻期汇率归因"}
    cur_date, prev_date = dates[0], dates[1]
    prev_rows = _nominal_fx_rows(wl_store, prev_date, account_ref)
    cur_rows = _nominal_fx_rows(wl_store, cur_date, account_ref)
    rows = _m.fx_attribution(prev_rows, cur_rows)
    no_fx = sum(1 for r in rows if r.get("r_fx_pct") is None)
    note = ""
    if not rows:
        note = "相邻两期无重叠可定价标的，汇率归因留空(不臆造)"
    elif no_fx:
        note = f"{no_fx}/{len(rows)} 只缺点位汇率锚，其 FX 腿留空(total 退化为本币收益)"
    return {"rows": rows, "prev_date": prev_date, "cur_date": cur_date,
            "coverage": f"{len(rows) - no_fx}/{len(rows)}", "note": note}


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


# 外部现金流分类（注资/提取），TWR/MWR 分母只应剔这些——买卖交割不算。net_amount 已带符号(注入+/提取-)，
# 故 _external_flows 直接取 net_amount 即得 Modified Dietz 所需带符号外部流。
_EXT_IN = {"deposit", "transfer_in"}
_EXT_OUT = {"withdrawal", "transfer_out"}


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
        # P2-2：外部现金流(注资/提取/转入转出)与买卖交割严格分离——TWR/MWR 分母只应剔外部现金流，不应把
        # 买卖交割额当注资(红线 §3.2)。net_inflow/net_outflow 保留旧「按符号的全额」语义不动(向后兼容/测试锁定)。
        # 券商是否逐笔列出转账决定覆盖度：花旗逐笔列(deposit/withdrawal/transfer_in)，招银月结单只出 buy/sell →
        # external_txn_count=0 不等于"无外部现金流"，仅表示该券商未逐笔披露(Phase 3 据此决定能否算精确 TWR/MWR)。
        "external_inflow": 0.0,
        "external_outflow": 0.0,
        "net_external_cashflow": 0.0,
        "external_txn_count": 0,
    }
    _ext_in = _EXT_IN
    _ext_out = _EXT_OUT
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
        elif kind in _ext_in:
            totals["external_inflow"] += abs(amt)
            totals["external_txn_count"] += 1
        elif kind in _ext_out:
            totals["external_outflow"] += abs(amt)
            totals["external_txn_count"] += 1
        if amt >= 0:
            totals["net_inflow"] += amt
        else:
            totals["net_outflow"] += abs(amt)
    totals["net_external_cashflow"] = totals["external_inflow"] - totals["external_outflow"]
    return {k: round(v, 2) if isinstance(v, float) else v for k, v in totals.items()}


def _external_flows(txns: list[dict]) -> list[dict]:
    """P3-1 · 从流水抽外部现金流为 Modified Dietz 输入 [{date, amount(带符号)}]。

    注资(deposit/transfer_in) net_amount>0、提取(withdrawal/transfer_out) net_amount<0——net_amount 本就
    带符号，直接透传即为 Modified Dietz 所需带符号外部流；缺 trade_date 的行跳过(无法归子期)。
    """
    out = []
    for t in txns:
        if (t.get("txn_type") or "") in _EXT_IN or (t.get("txn_type") or "") in _EXT_OUT:
            d = t.get("trade_date")
            if d:
                out.append({"date": str(d)[:10], "amount": float(t.get("net_amount") or 0.0)})
    return out



def _current_derivative_rows(wl_store, account_ref: str) -> list[dict]:
    """账户"当前"结构性产品/衍生品条款：同一 (family, underlying, lot_key) 只取最新一期(MAX created_at)。

    否则多期结单(如招银 05/06/07 三份月结单)会为同一笔 FCN 落三行，概览持仓与曲线 MTM 三倍虚增。
    SQLite 保证：GROUP BY 配合 MAX(created_at) 时，其余裸列取自最大行(3.7.11+)。terms 由调用方解析。
    """
    import json
    conn = wl_store._connect()
    try:
        q, p = wl_store._filtered(
            "SELECT product_family, underlying_symbol, currency, terms_json, MAX(created_at) AS _mx "
            "FROM vip_derivative_terms WHERE account_ref = ? "
            "GROUP BY product_family, underlying_symbol, lot_key ORDER BY _mx DESC",
            (account_ref,), table="vip_derivative_terms")
        rows = [dict(r) for r in conn.execute(q, p).fetchall()]
    finally:
        conn.close()
    for r in rows:
        r["terms"] = json.loads(r.get("terms_json") or "{}")
    return rows


def _derivative_holdings(wl_store, account_ref: str) -> list[dict]:
    """结构性产品/衍生品(vip_derivative_terms)中带当期 MTM 的条款，折成持仓构成条目。

    这些头寸走衍生品栏、不进 sim_positions，但其市值已计入账户总权益(materialize 用结单权威合计
    做锚)。若概览 holdings 只数股票，就会出现"总权益百万级、持仓 0 只、饼图空、集中度 0%"的错位
    (招银账户几乎全是 FCN 即此症)。故把带 MTM 的条款并入概览 holdings。无 MTM 的条款(如仅有条款
    结构的 accumulator)不并入——它们的价值本就不在总权益里，单列于衍生品敞口即可。
    """
    out = []
    for r in _current_derivative_rows(wl_store, account_ref):
        mv = (r.get("terms") or {}).get("market_value_usd")
        if not mv:
            continue
        tag = "结构性" if r["product_family"] in ("equity_fcn", "equity_mli_booster") else "衍生品"
        out.append({"ticker": f"{r['underlying_symbol']}·{tag}", "shares": 0,
                    "market_value": round(mv, 2), "weight_pct": 0.0, "kind": "derivative"})
    return out


def _derivative_mtm_total(wl_store, account_ref: str) -> float:
    """账户结构性产品/衍生品的当期 MTM 合计(仅计带 market_value_usd 的当前条款)。供价值曲线折入。"""
    return round(sum((r.get("terms") or {}).get("market_value_usd") or 0.0
                     for r in _current_derivative_rows(wl_store, account_ref)), 2)


# ── P4-1/P4-2：组合级压力测试 + 净 Greeks（接线 vip/stress 纯引擎，注入真实现价）──
# 默认情景：市场 ±10%/±20%（对称，覆盖股灾与反弹）。衍生品重放 payoff、股票线性 delta。
_STRESS_SCENARIOS = [
    {"name": "市场 +20%", "market_shock": 0.20},
    {"name": "市场 +10%", "market_shock": 0.10},
    {"name": "市场 -10%", "market_shock": -0.10},
    {"name": "市场 -20%", "market_shock": -0.20},
]


def _stress_and_greeks(wl_store, account_ref: str, stock_mv_total: float) -> dict:
    """组装压力测试 + 净 Greeks 输入（真实现价从 get_latest_snapshot 注入），调 vip.stress 纯引擎。

    衍生品由 _current_derivative_rows 重建 DerivativeTerm（同去重口径，一笔 FCN 不重复计）；缺现价/缺条款
    参数的标的由纯引擎诚实剔出并披露覆盖率。股票总市值走线性 delta。返回 {stress, greeks} 或空 dict（无衍生品且无股票）。
    """
    from bottleneck_hunter.vip.derivatives import DerivativeTerm
    from bottleneck_hunter.vip.stress import net_greeks, stress_test

    derivs: list[dict] = []
    for r in _current_derivative_rows(wl_store, account_ref):
        t = r.get("terms") or {}
        sym = (r.get("underlying_symbol") or "").strip()
        term = DerivativeTerm(product_family=r.get("product_family", ""), underlying_symbol=sym,
                              currency=r.get("currency", ""), tenor_days=int(t.get("tenor_days", 0) or 0),
                              terms=t)
        snap = wl_store.get_latest_snapshot(sym) if sym and hasattr(wl_store, "get_latest_snapshot") else None
        spot = (snap or {}).get("close") or 0.0
        derivs.append({"term": term, "spot": float(spot or 0.0)})

    if not derivs and stock_mv_total <= 0:
        return {}
    return {"stress": stress_test(stock_mv_total, derivs, _STRESS_SCENARIOS),
            "greeks": net_greeks(derivs)}


def _import_total_series(wl_store, account_ref: str) -> list[dict]:
    """该账户各期结单权威净值序列(vip_imports.key_metrics_json 的 period_end + total_equity)。

    供纯结构性产品/衍生品账户(positions 表为空)建价值曲线：每个 period_end 取最新一次导入的 total_equity。
    仅历史导入已带 total_equity 的期才有点(旧导入无此键 → 需重导回填)。返回按期升序 [{as_of_date,total_equity}]。
    """
    conn = wl_store._connect()
    try:
        q, p = wl_store._filtered(
            "SELECT json_extract(key_metrics_json,'$.period_end') AS pe, "
            "       json_extract(key_metrics_json,'$.total_equity') AS te, MAX(created_at) AS mx "
            "FROM vip_imports WHERE account_ref = ? "
            "AND pe IS NOT NULL AND pe != '' AND te IS NOT NULL "
            "GROUP BY pe ORDER BY pe",
            (account_ref,), table="vip_imports")
        return [{"as_of_date": r["pe"], "total_equity": round(r["te"], 2)}
                for r in conn.execute(q, p).fetchall()]
    finally:
        conn.close()


def _latest_import_period(wl_store, account_ref: str) -> str:
    """该账户最新导入的结单期末日(vip_imports.key_metrics_json.period_end)，作全衍生品账户曲线锚点。"""
    conn = wl_store._connect()
    try:
        q, p = wl_store._filtered(
            "SELECT json_extract(key_metrics_json,'$.period_end') AS pe FROM vip_imports "
            "WHERE account_ref = ? AND pe IS NOT NULL ORDER BY pe DESC LIMIT 1",
            (account_ref,), table="vip_imports")
        row = conn.execute(q, p).fetchone()
        return (row["pe"] if row else "") or ""
    finally:
        conn.close()


def _holdings_as_of(wl_store, account_ref: str) -> str:
    """0-1：持仓「数据截至」日 = 最新非零持仓快照的 as_of_date（即结算单期末），
    ★区别于市值重估日(latest_projection_date)——前者是持仓事实的锚点(可能 40 天前)，后者只是按最新收盘重估的时刻。
    三面板(顾问/荐新/报告)据此标「数据截至 X 日」，让用户区分"今天生成的意见"与"底层持仓可能是上月的"。
    纯衍生品账户(positions 空)→ 退回结单期末 _latest_import_period；全无→空串（诚实留白，不编造今天）。"""
    conn = wl_store._connect()
    try:
        where = ["quantity != 0"]
        params: list = []
        if account_ref:
            where.append("account_ref = ?")
            params.append(account_ref)
        q, p = wl_store._filtered(
            f"SELECT MAX(as_of_date) AS d FROM positions WHERE {' AND '.join(where)}",
            tuple(params), table="positions")
        row = conn.execute(q, p).fetchone()
        d = (row["d"] if row and row["d"] else "") or ""
    finally:
        conn.close()
    return d or _latest_import_period(wl_store, account_ref)


def build_account_overview(wl_store, *, account_ref: str = "") -> dict:
    summary = build_account_summary(wl_store, account_ref=account_ref)
    rows = list_transactions(wl_store, account_ref=account_ref, limit=10000)
    ov = {
        **summary,
        **_overview_totals(rows),
        "realized_pnl": None,
        "realized_pnl_available": False,
    }
    extra = _derivative_holdings(wl_store, account_ref)
    if extra:
        holdings = list(ov.get("holdings", [])) + extra
        # 权重分母取 max(权威总权益, 各持仓市值之和)：total_equity 已锚定结单口径时用它（差额=现金），
        # 若解析退化未含衍生品致其偏小，则回落到持仓市值和，保证 Σ权重 ≤ 100%，绝不溢出。
        hold_mv = sum((h.get("market_value", 0) or 0) for h in holdings)
        denom = max(ov.get("total_equity", 0) or 0, hold_mv)
        for h in holdings:
            h["weight_pct"] = round((h.get("market_value", 0) or 0) / denom * 100, 1) if denom else 0.0
        holdings.sort(key=lambda h: h.get("market_value", 0), reverse=True)
        ov["holdings"] = holdings
        ov["n_holdings"] = len(holdings)
        ov["top5_concentration_pct"] = round(sum(h["weight_pct"] for h in holdings[:5]), 1)
    # P1 币种敞口：前端账户视图渲染币种敞口饼（含原币金额/隐含汇率），复用已有分桶算法
    ov["exposure_breakdown"] = _exposure_breakdown(wl_store, account_ref)
    return ov


def _canonical_cost_map(wl_store, account_ref: str) -> dict[str, dict]:
    """规范层最新快照的逐标的成本/盈亏（Phase A：结算单直接解析所得）。
    返回 {symbol: {avg_cost, cost_basis, unrealized_pnl, as_of_date}}。无成本(结单未含)则值为 0/None。

    ★成本结转：胜出快照若是「仓盘导出(position_report)」这类无成本列的薄快照，其 cost_basis=0 → upnl 恒 0。
    此时按 symbol 回退到该标的**最近一期带成本的历史快照**取成本基，未实现盈亏用**当前市值**重算
    (upnl = 当前 MV − 历史 cost_basis)。仅当历史股数==当前股数(同一持仓、未买卖)才结转，否则诚实留 None
    (股数变动后旧成本基已失真，不臆造)。—— 修「最新导入的仓盘无成本列致全部持仓未实现收益为 0」。
    """
    rows, selected = _latest_positions(wl_store, "", account_ref)  # 复用"胜出快照"选择逻辑
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
            f"""SELECT i.symbol, p.quantity, p.as_of_date, p.avg_cost, p.cost_basis,
                      p.unrealized_pnl, p.market_value_base
               FROM positions p JOIN instruments i ON i.id = p.instrument_id
               WHERE {' AND '.join(where)}""",
            tuple(params), table="p")
        out: dict[str, dict] = {}
        for r in conn.execute(q, p).fetchall():
            cb = r["cost_basis"] or 0.0
            ac = r["avg_cost"] or 0.0
            mv = r["market_value_base"] or 0.0
            upnl = r["unrealized_pnl"] or 0.0
            carried = False
            if cb <= 0:   # 本期快照无成本 → 结转历史同标的成本基
                prior = _prior_cost_for_symbol(
                    conn, wl_store, account_ref, r["symbol"], r["as_of_date"], r["quantity"])
                if prior:
                    cb, ac = prior["cost_basis"], prior["avg_cost"]
                    upnl = round(mv - cb, 2)   # 成本历史、市值当前：诚实的当前未实现盈亏
                    carried = True
            out[r["symbol"]] = {
                "avg_cost": round(ac, 4) or None,
                "cost_basis": round(cb, 2) or None,
                "unrealized_pnl": round(upnl, 2) if cb else None,
                "unrealized_pnl_pct": round((mv - cb) / cb * 100, 2) if cb else None,
                "as_of_date": r["as_of_date"],
                "cost_carried_from": prior["as_of_date"] if carried else None,  # 披露口径
            }
        return out
    finally:
        conn.close()


def _prior_cost_for_symbol(conn, wl_store, account_ref, symbol, before_date, cur_qty):
    """该标的 before_date 之前**最近一期带成本**的快照 → {avg_cost, cost_basis, as_of_date}；无则 None。
    仅当历史股数≈当前股数(同一持仓未买卖)才返回，否则 None(股数变动旧成本失真、不结转)。
    """
    where = ["i.symbol = ?", "p.cost_basis > 0", "p.as_of_date < ?", "p.quantity != 0"]
    params: list = [symbol, before_date]
    if account_ref:
        where.append("p.account_ref = ?")
        params.append(account_ref)
    q, p = wl_store._filtered(
        f"""SELECT p.avg_cost, p.cost_basis, p.quantity, p.as_of_date
           FROM positions p JOIN instruments i ON i.id = p.instrument_id
           WHERE {' AND '.join(where)} ORDER BY p.as_of_date DESC LIMIT 1""",
        tuple(params), table="p")
    row = conn.execute(q, p).fetchone()
    if not row:
        return None
    # 股数变动 → 旧总成本基失真，不结转（相对容差 1%，容许结单四舍五入/拆股微差）
    pq, cq = row["quantity"] or 0.0, cur_qty or 0.0
    if cq and abs(pq - cq) / cq > 0.01:
        return None
    return {"avg_cost": row["avg_cost"] or 0.0, "cost_basis": row["cost_basis"] or 0.0,
            "as_of_date": row["as_of_date"]}


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


def _date_span_days(d0: str, d1: str) -> int:
    from datetime import date
    try:
        return (date.fromisoformat(str(d1)[:10]) - date.fromisoformat(str(d0)[:10])).days
    except (ValueError, TypeError):
        return 0


def _perf_summary(vseries: dict, totals: dict, pos_mv: float, flows: list[dict] | None = None) -> dict:
    """Phase 0-3 · 绩效摘要 KPI（私行季报首排数字），全部来自 value_series 期末点 + 交易流水聚合。

    诚实边界（红线 §8.1/8.2）：这些是「基于 N 期结单期末点」的**指示性/近似**口径——非逐日。
    - since_inception/annualized：简单价格收益率（**未剔注资**），分母口径由 value_series 决定，保留为原始参照。
    - dietz_*/mwr（P3-1）：Modified Dietz·链接≈TWR，**已剔外部现金流**，是真实业绩收益率；需 flows 且权威口径。
    - sharpe/sortino/calmar（P3-2）：稀疏期收益上的风险调整，按实际期均跨度年化，<3 期收益诚实降级 None。
    调用方必须带 `basis` 标注展示，绝不伪装精确。推算点(is_projected)不计入真实绩效。
    """
    series = [s for s in (vseries.get("series") or []) if not s.get("is_projected")]
    # basis 随曲线口径如实变化(P2-1)：权威净值含现金·净融资 vs 持仓市值不含现金——两者绩效分母不同，不可混淆。
    _caliber = {"authoritative_total_equity": "结单权威净值(含现金·净融资)",
                "derivative_mtm_anchor": "衍生品当期MTM锚点"}.get(vseries.get("basis"), "持仓市值(不含现金)")
    out = {
        "since_inception_pct": None, "annualized_pct": None,
        "income_yield_pct": None, "excess_vs_benchmark_pct": None,
        "max_drawdown_pct": None, "n_points": len(series),
        "basis": f"基于 N 期{_caliber}期末点·非逐日·未剔注资，指示性口径",
        # P3-1/P3-2：现金流调整收益 + 风险调整（默认 None；下方按数据可得性填充，各带诚实口径标注）
        "dietz_return_pct": None, "dietz_annualized_pct": None, "mwr_pct": None, "dietz_basis": "",
        "sharpe": None, "sortino": None, "calmar": None, "risk_note": "",
    }
    if len(series) < 2:
        return out
    first, last = series[0]["total_equity"], series[-1]["total_equity"]
    if first > 0:
        out["since_inception_pct"] = round((last / first - 1) * 100, 2)
        days = _date_span_days(series[0]["as_of_date"], series[-1]["as_of_date"])
        if days >= 30 and last > 0:   # 跨度不足 1 月不年化：短样本年化会爆表失真
            out["annualized_pct"] = round(((last / first) ** (365.0 / days) - 1) * 100, 2)
    # 累计 income yield（股息+利息 / 当前持仓市值）——累计口径，非年化
    income = (totals.get("dividend_income") or 0.0) + (totals.get("interest_income") or 0.0)
    if pos_mv > 0 and income:
        out["income_yield_pct"] = round(income / pos_mv * 100, 2)
    # vs 基准超额：同一 value_series 的 benchmark_value 首末差（_rebase_benchmark 已对齐同轴，单一权威源）
    bf, bl = series[0].get("benchmark_value"), series[-1].get("benchmark_value")
    if bf and bl and bf > 0 and out["since_inception_pct"] is not None:
        out["excess_vs_benchmark_pct"] = round(out["since_inception_pct"] - (bl / bf - 1) * 100, 2)
    # 近似最大回撤（稀疏期末点 peak-to-trough，非逐日）。
    # 权威净值口径下 equity 含外部现金流(注资抬高/提取压低)——须先剔累计外部净流再做峰谷，否则注资掩盖真实回撤、
    # 提取伪造回撤台阶，令 Calmar(=年化/|MDD|) 方向性失真(与 dietz 剔流口径不一致)。其余口径不含现金，直接峰谷。
    _flows = flows or []
    if vseries.get("basis") == "authoritative_total_equity" and _flows:
        def _adj_eq(s):  # 该点权益 − 截至该点(含)的累计外部净流 = 剔注资/提取后的市场口径权益
            cum = sum(f["amount"] for f in _flows if str(f.get("date"))[:10] <= s["as_of_date"])
            return s["total_equity"] - cum
        eqs = [_adj_eq(s) for s in series]
    else:
        eqs = [s["total_equity"] for s in series]
    peak, mdd = eqs[0], 0.0
    for eq in eqs:
        if eq > peak:
            peak = eq
        elif peak > 0:
            mdd = min(mdd, (eq - peak) / peak)
    out["max_drawdown_pct"] = round(mdd * 100, 2)

    # ── P3-1/P3-2：Modified Dietz(已剔外部现金流·链接≈TWR) + 稀疏期收益风险调整 ──
    # 只在权威净值口径下呈现精确业绩收益率——持仓市值/MTM锚点口径分母不含现金、剔流无意义，仅保留 dietz_basis 说明。
    from bottleneck_hunter.vip import metrics as _m
    if vseries.get("basis") == "authoritative_total_equity":
        dietz = _m.linked_modified_dietz(series, flows or [])
        out["dietz_return_pct"] = dietz["cumulative_pct"]
        out["dietz_annualized_pct"] = dietz["annualized_pct"]
        out["mwr_pct"] = dietz["mwr_pct"]
        out["dietz_basis"] = (f"Modified Dietz·链接≈TWR·基于 {dietz['n_periods']} 期结单"
                              f"·已剔外部现金流·非逐日 (有效期覆盖 {dietz['coverage']})")
        rets = [p["pct"] / 100.0 for p in dietz["period_returns"]]
        spans = [p["span_days"] for p in dietz["period_returns"]]
        ra = _m.risk_adjusted(rets, spans, out["max_drawdown_pct"])
        out["sharpe"], out["sortino"], out["calmar"] = ra["sharpe"], ra["sortino"], ra["calmar"]
        out["risk_note"] = (ra["note"] or
                            f"基于 {ra['n_returns']} 期收益·按实际期均跨度年化·样本极少属指示性趋势")
    else:
        out["dietz_basis"] = "非权威净值口径(缺含现金 NAV)，不呈现现金流调整收益率——先补齐结单权威净值"
    return out


def _derivative_notional(t: dict) -> float | None:
    """Phase 0-7 · 单笔衍生品名义敞口（USD 口径，缺参数诚实 None，绝不臆造）。

    FCN：条款直接带 `notional`（花旗 issue_size / 巴克莱 aggregate nominal）→ 直接用。
    累购/累沽：`max_nominal_shares × afp`（成交远期价）= 客户最大购入义务金额（真实隐含杠杆的分子）。
    MLI booster：无本金参数（project_derivative_accrual 亦跳过）→ None。
    """
    if t.get("notional"):
        return round(float(t["notional"]), 2)
    mn = t.get("max_nominal_shares") or 0.0
    px = t.get("afp") or t.get("strike") or 0.0
    if mn and px:
        return round(float(mn) * float(px), 2)
    return None


def _derivative_notional_usd(t: dict, currency: str) -> tuple[float | None, float | None]:
    """Phase 0-7 修正 · 单笔名义敞口 (usd, native)：先算原币种名义 native，仅当条款币种为美元(或未知)
    时才同时作 USD 口径回传；非美元(HKD/JPY FCN、港币累购)名义 usd=None——绝不冒充美元汇总/除美元权益
    (否则 HKD FCN 杠杆虚高 ~7.8×、JPY ~150×)。native 仍保留供呈现「原币种名义」。"""
    from bottleneck_hunter.vip.number_guard import _USD_CCY
    native = _derivative_notional(t)
    if native is None:
        return None, None
    is_usd = (currency or "").strip().lower() in _USD_CCY
    return (native if is_usd else None), native


def _exposure_breakdown(wl_store, account_ref: str) -> dict:
    """Phase 0-4 · 币种 + 资产类别敞口分桶（Σ market_value_base，USD 口径）。

    数据源同 _canonical_cost_map 的胜出快照 positions；多币种真账户（港币/日元/美元混持）的汇率
    敞口首次可见。currency=结单名义币（p.currency），asset_class=instruments.instrument_type。
    衍生品不在此层（单列 derivative_exposure），此处仅股票/规范层持仓。
    """
    _, selected = _latest_positions(wl_store, "", account_ref)   # 复用"胜出快照"选择逻辑
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
            f"""SELECT p.currency, i.instrument_type, p.market_value_base,
                      p.market_value AS mv_nominal, p.fx_rate
               FROM positions p JOIN instruments i ON i.id = p.instrument_id
               WHERE {' AND '.join(where)}""",
            tuple(params), table="p")
        by_ccy: dict[str, float] = {}
        by_asset: dict[str, float] = {}
        by_ccy_nom: dict[str, float] = {}      # 原币口径累计（P1 币种敞口）
        for r in conn.execute(q, p).fetchall():
            mv = r["market_value_base"] or 0.0
            ccy = ((r["currency"] or "USD").strip().upper()) or "USD"
            asset = ((r["instrument_type"] or "stock").strip().lower()) or "stock"
            by_ccy[ccy] = round(by_ccy.get(ccy, 0.0) + mv, 2)
            by_asset[asset] = round(by_asset.get(asset, 0.0) + mv, 2)
            by_ccy_nom[ccy] = round(by_ccy_nom.get(ccy, 0.0) + (r["mv_nominal"] or mv), 2)
        total_base = round(sum(by_ccy.values()), 2)
        # 每币种明细：美元敞口 + 原币敞口 + 隐含汇率（usd/nominal）+ 占比。美元桶 fx=1。
        by_ccy_detail = [{
            "currency": c,
            "market_value_usd": by_ccy[c],
            "market_value_nominal": by_ccy_nom.get(c, by_ccy[c]),
            "implied_fx": round(by_ccy[c] / by_ccy_nom[c], 6) if by_ccy_nom.get(c) else 1.0,
            "weight_pct": round(by_ccy[c] / total_base * 100, 2) if total_base else 0.0,
        } for c in sorted(by_ccy, key=lambda k: by_ccy[k], reverse=True)]
        return {"by_currency": by_ccy, "by_asset_class": by_asset,
                "by_currency_detail": by_ccy_detail, "total_base": total_base}
    finally:
        conn.close()


def compute_realized_pnl_fifo(txns: list[dict]) -> dict:
    """P1-5 · FIFO 已实现盈亏（完整性闸门 + 币种闸门：残缺/非美元标的绝不冒充美元合计）。

    结算单常只覆盖近几期，期初建仓的买入未必在库；且流水金额是**原币种**净额（港币/日元…，
    fx_rate 未回填）。故双闸门：
      ① 完整性：某笔卖出在库买入存量不足（队列下溢）→ 该标的历史残缺，realized 记 None、剔出合计。
      ② 币种：realized 按原币种撮合；仅「全腿美元(或未知)」的标的计入美元 total；非美元标的原币种
         分列 by_currency + foreign_values（喂 number_guard，$ 令牌不得据此核实），绝不 ÷1 冒充
         美元（HKD ~7.8×/JPY ~150× 虚高），与衍生品 _derivative_notional_usd「非美元不汇总」同规矩。
    每股口径用 |net_amount|/qty（含费税的净额；缺则退 price）。仅纳入「有卖出」的标的（纯买入跳过）。返回见函数末。
    """
    from bottleneck_hunter.vip.number_guard import _USD_CCY  # 币种口径判定复用防伪器同一集合（含 ""=未知→按美元）

    def _unit(r: dict) -> float | None:      # 每股净额（含费税）：优先 net_amount，退回 price；无从定价→None
        qty = abs(float(r.get("quantity") or 0.0))
        if qty <= 0:
            return None
        net = abs(float(r.get("net_amount") or 0.0))
        if net > 0:
            return net / qty
        price = abs(float(r.get("price") or 0.0))
        return price if price > 0 else None

    def _ccy(rows: list[dict]) -> str:       # 标的币种：任一腿为非美元原币种→取该原币种；否则全腿美元/未知→USD
        for r in rows:
            c = str(r.get("currency") or "").strip()
            if c.lower() not in _USD_CCY:
                return c.upper()
        return "USD"

    by: dict[str, list[dict]] = {}
    for t in txns:
        sym = t.get("symbol") or t.get("ticker") or ""
        if sym and (t.get("txn_type") in ("buy", "sell")):
            by.setdefault(sym, []).append(t)

    by_symbol: list[dict] = []
    incomplete: list[str] = []
    by_currency: dict[str, float] = {}                       # 非美元原币种合计（不并入美元 total）
    foreign_values: list[float] = []                        # 非美元 realized：喂 number_guard 排除
    usd_total = 0.0
    any_usd = False
    for sym, rows in by.items():
        rows.sort(key=lambda r: (r.get("trade_date") or "", r.get("created_at") or ""))
        if not any(r.get("txn_type") == "sell" for r in rows):
            continue                                        # 纯买入 → 无已实现，不入明细
        queue: list[list[float]] = []                       # FIFO 批次 [剩余股数, 每股成本]
        realized, matched_qty, sell_count, broken = 0.0, 0.0, 0, False
        for r in rows:
            qty, u = abs(float(r.get("quantity") or 0.0)), _unit(r)
            if qty <= 0 or u is None:
                broken = True
                break                                       # 无法定价 → 判该标的残缺
            if r["txn_type"] == "buy":
                queue.append([qty, u])
                continue
            sell_count += 1
            remaining = qty
            while remaining > 1e-9 and queue:
                lot = queue[0]
                take = min(remaining, lot[0])
                realized += (u - lot[1]) * take
                matched_qty += take
                lot[0] -= take
                remaining -= take
                if lot[0] <= 1e-9:
                    queue.pop(0)
            if remaining > 1e-9:                            # 卖出多于在库买入 → 期初建仓缺失
                broken = True
                break
        if broken:
            by_symbol.append({"symbol": sym, "realized_pnl": None, "sell_count": sell_count,
                              "complete": False, "note": "买入历史不足以覆盖卖出（期初建仓未在库）"})
            incomplete.append(sym)
            continue
        ccy = _ccy(rows)
        realized = round(realized, 2)
        entry = {"symbol": sym, "realized_pnl": realized, "currency": ccy,
                 "matched_qty": round(matched_qty, 4), "sell_count": sell_count, "complete": True}
        if ccy == "USD":
            any_usd = True
            usd_total += realized
        else:                                               # 非美元：原币种分列，绝不并入美元合计
            by_currency[ccy] = round(by_currency.get(ccy, 0.0) + realized, 2)
            foreign_values.append(realized)
            entry["note"] = "原币种口径，未计入美元合计（汇率未回填，不冒充美元）"
        by_symbol.append(entry)
    by_symbol.sort(key=lambda s: (s["complete"], abs(s.get("realized_pnl") or 0.0)), reverse=True)
    return {"available": any_usd,                           # 有可计入美元合计的标的（纯非美元账户→False，total None）
            "total": round(usd_total, 2) if any_usd else None,   # 仅全腿美元标的的美元合计
            "by_symbol": by_symbol,
            "by_currency": by_currency,                     # {原币种: 合计}（诚实分列，不混美元）
            "foreign_values": foreign_values,               # 非美元 realized，供 number_guard 排除 $ 误核
            "incomplete_symbols": incomplete,
            "basis": "FIFO 净额口径（含费税）；仅全腿美元计入 total，非美元原币种分列 by_currency，残缺留空不猜"}


def build_account_dossier(wl_store, *, account_ref: str = "") -> dict:
    """Phase A · 账户完整档案层——LLM 单一事实源。聚合此前碎在 7+ 调用里的账户全貌。

    口径原则（用户拍板）：
    - 头条"真实价值" = 结算单事实（股票 sim 权益 + 现金），**不含衍生品模型估值**。
    - 衍生品单列 `derivative_exposure`（敞口 + 条款），路径依赖/敲出风险由决策层单独消费。
    - 成本/已实现盈亏来自结算单直接解析（成本无则留 None）；已实现盈亏走完整性闸门的 FIFO（残缺→None，不硬凑）。
    返回结构见函数末 return。
    """
    account_ref = (account_ref or "").strip()
    summary = build_account_summary(wl_store, account_ref=account_ref)      # sim：真实权益/现金/持仓
    cost_map = _canonical_cost_map(wl_store, account_ref)                     # 规范层：成本/盈亏

    # 逐仓富化成本/盈亏（以 sim 持仓为准，成本从规范层按 symbol 贴合）
    holdings = []
    unrealized_total = 0.0
    cost_covered = 0
    join_covered = 0
    for h in summary.get("holdings", []):
        c = cost_map.get(h["ticker"], {})
        upnl = c.get("unrealized_pnl")
        if upnl is not None:
            unrealized_total += upnl
            cost_covered += 1
        # 0-5: 逐仓反查观察池补 entry_id/sector/bottleneck_node —— 产业前瞻(Phase 4)的地基，
        # 且顺手修「持仓催化剂恒空」bug：advisory 把 h["entry_id"] 喂给 build_ticker_background，
        # 此前 dossier 从不做此 join → entry_id 恒 None → 催化剂段恒"暂无"。非观察池标的诚实降级。
        # ponytail: 逐仓一次 get_by_ticker（单用户周期性，N 小）；量大再批量 IN 查。
        wl = wl_store.get_by_ticker(h["ticker"]) or {}
        if wl.get("id"):
            join_covered += 1
        holdings.append({**h,
                         "avg_cost": c.get("avg_cost"),
                         "cost_basis": c.get("cost_basis"),
                         "unrealized_pnl": upnl,
                         "unrealized_pnl_pct": c.get("unrealized_pnl_pct"),
                         "is_derivative": False,  # 0-8：股票track（计入总权益）；与 derivative_exposure 的 True 对轨
                         "entry_id": wl.get("id"),
                         "sector": wl.get("sector") or h.get("sector") or "",
                         "bottleneck_node": wl.get("bottleneck_node") or ""})

    # 交易流水聚合（净流入/买卖/分红/费用）
    txns = list_transactions(wl_store, account_ref=account_ref, limit=10000)
    totals = _overview_totals(txns)
    realized = compute_realized_pnl_fifo(txns)     # P1-5：复用已取的 txns，完整性闸门 FIFO（残缺标的留 None）

    # 衍生品敞口（单列，不并入权益）——★与 mtm_total_usd 同源用去重后的"当前条款"(_current_derivative_rows)：
    # 否则多期结单(招银 05/06/07)同一笔 FCN 落三行 → 名义/杠杆三倍虚增，且与已去重的 MTM 同字典自相矛盾。
    try:
        rows = _current_derivative_rows(wl_store, account_ref)
        derivative_exposure = []
        for r in rows:
            tm = r.get("terms") or {}
            n_usd, n_native = _derivative_notional_usd(tm, r.get("currency", ""))
            derivative_exposure.append({
                "underlying": r.get("underlying_symbol"), "family": r.get("product_family"),
                "currency": r.get("currency"), "tenor_days": int(tm.get("tenor_days", 0) or 0),
                "is_derivative": True,  # 0-8：衍生品track（名义敞口，**不计入总权益**）——供合并配置视图分色/隔离
                "trade_date": tm.get("trade_date", ""), "expiry_date": tm.get("expiry_date", ""),
                "afp": tm.get("afp"), "knock_out_price": tm.get("knock_out_price"),
                "strike_pct_initial": tm.get("strike_pct_initial"),
                # 0-7：名义敞口——仅美元(或未知)币种给 notional_usd；非美元只留 notional_native+币种(诚实不硬凑美元)
                "notional_usd": n_usd, "notional_native": n_native,
                "mtm_usd": tm.get("market_value_usd"),
            })
    except Exception:  # noqa: BLE001 - 衍生品缺失绝不带崩档案
        derivative_exposure = []

    # 0-7：组合级衍生品名义敞口 + 杠杆比率（名义 / 真实权益，暴露"总权益不含衍生品"下的尾部杠杆）。
    # ★只对"美元口径"腿求和/算杠杆——非美元名义在原币种、与美元权益不可直接相除；非美元腿数单列 coverage。
    head_equity = summary.get("total_equity", 0.0) or 0.0
    notional_vals = [d["notional_usd"] for d in derivative_exposure if d.get("notional_usd")]
    notional_total = round(sum(notional_vals), 2)
    non_usd = sum(1 for d in derivative_exposure
                  if d.get("notional_usd") is None and d.get("notional_native"))
    derivative_summary = {
        "notional_total_usd": notional_total,
        "mtm_total_usd": _derivative_mtm_total(wl_store, account_ref),
        "leverage_ratio": round(notional_total / head_equity, 2) if head_equity > 0 and notional_total else None,
        "notional_coverage": {"computable": len(notional_vals), "non_usd": non_usd,
                              "total": len(derivative_exposure)},
    }

    # 数据新鲜度（复用推算层）
    last_proj_date = wl_store.latest_projection_date(account_ref) if hasattr(wl_store, "latest_projection_date") else ""

    vseries = value_series(wl_store, account_ref=account_ref)
    pos_mv = sum(h.get("market_value") or 0.0 for h in holdings)
    perf_summary = _perf_summary(vseries, totals, pos_mv, flows=_external_flows(txns))

    # P4-1/P4-2：组合级压力测试 + 净 Greeks（衍生品 payoff 重放 + 股票线性 delta）。缺价/未建模诚实剔出并披露。
    try:
        stress_greeks = _stress_and_greeks(wl_store, account_ref, pos_mv)
    except Exception:  # noqa: BLE001 - 压测/Greeks 失败绝不带崩档案
        stress_greeks = {}

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
        "join_coverage": {"covered": join_covered, "total": len(holdings)},
        # ── 币种 + 资产类别敞口（0-4，多币种账户汇率敞口首次可见）──
        "exposure_breakdown": _exposure_breakdown(wl_store, account_ref),
        # ── 流水聚合 + 已实现盈亏（P1-5：完整性闸门 FIFO，残缺标的留 None，不猜）──
        "flows": totals,
        "realized_pnl": realized["total"],
        "realized_pnl_available": realized["available"],
        "realized_pnl_detail": realized,
        # ── 绩效摘要 KPI（0-3，指示性口径，须带 basis 标注展示）──
        "perf_summary": perf_summary,
        # ── 标的贡献归因（P3-3，相邻两期×期初权重，剔买卖污染，须带 coverage/note）──
        "contribution": _contribution(wl_store, account_ref),
        # ── 汇率损益归因（特性一 P2，相邻两期点位 r_local/r_fx 乘性拆解，缺 fx 锚 FX 腿留空）──
        "fx_attribution": _fx_contribution(wl_store, account_ref),
        # ── 衍生品敞口（单列，路径依赖风险由决策层消费）──
        "derivative_exposure": derivative_exposure,
        # ── 衍生品组合级名义敞口 + 杠杆比率（0-7，名义/真实权益）──
        "derivative_summary": derivative_summary,
        # ── 组合级压力测试 + 净 Greeks（P4-1/P4-2，衍生品 payoff 重放 + 股票线性 delta；缺价/未建模剔出披露）──
        "stress_test": stress_greeks.get("stress"),
        "net_greeks": stress_greeks.get("greeks"),
        # ── 价源覆盖（代码判定：无快照=无活跃价源，市值走结算单结转，判断须谨慎）──
        "price_coverage": _price_coverage(wl_store, holdings, derivative_exposure),
        # ── 价值曲线 + 新鲜度 ──
        "value_series": vseries,
        "as_of_hint": {"latest_projection_date": last_proj_date,
                       "data_as_of": _holdings_as_of(wl_store, account_ref)},
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
    total_loan = 0.0
    n_holdings = 0
    for ref in refs:
        summary = build_account_summary(wl_store, account_ref=ref)
        meta = account_meta.get(ref, {})
        account_total = summary.get("total_equity", 0.0) or 0.0
        account_cash = summary.get("cash_balance", 0.0) or 0.0
        account_n_holdings = summary.get("n_holdings", 0) or 0
        account_loan = summary.get("loan_balance", 0.0) or 0.0
        extra_holdings = _derivative_holdings(wl_store, ref)  # 结构性产品/衍生品并入构成，见 _derivative_holdings
        account_n_holdings += len(extra_holdings)
        total_equity += account_total
        cash_balance += account_cash
        n_holdings += account_n_holdings
        total_loan += account_loan
        for item in summary.get("holdings", []):
            holdings.append({**item, "account_ref": ref})
        for item in extra_holdings:
            holdings.append({**item, "account_ref": ref})
        accounts.append({
            "account_ref": ref,
            "display_name": meta.get("display_name") or ref,
            "institution_name": meta.get("institution_name", ""),
            "account_kind": meta.get("account_kind", "broker"),
            "total_equity": round(account_total, 2),
            "cash_balance": round(account_cash, 2),
            "loan_balance": round(account_loan, 2),
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
        "total_loan": round(total_loan, 2),
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
    """结单权威净值(含现金·净融资)按 period_end 逐期成点 → 价值曲线；派生逐期收益率。

    单账户优先用各期结单权威净值(total_equity)建点——与头条 total_equity 同口径(含现金、净融资)，
    消除"曲线不含现金 vs 头条含现金"的同屏分裂(P2-1)；缺权威净值(仅持仓导出/旧导入未落)才回落
    持仓市值口径(不含现金)，并以 basis 如实标注。多账户聚合仍按持仓市值口径前向填充(basis 标明)。
    末尾若存在比最新真值快照更新的系统推算，则叠加一个 is_projected 点（虚线展示，待校准）。
    返回 {series:[{as_of_date,total_equity,is_projected?}], returns:[{period,pct}], basis}。
    """
    basis = "positions_market_value"  # 口径标签：持仓市值(不含现金)；单账户命中权威净值时改写
    series: list[dict] = []
    conn = wl_store._connect()
    try:
        if account_ref:
            sql = ("SELECT as_of_date AS d, SUM(market_value_base) AS eq FROM positions "
                   "WHERE quantity != 0 AND account_ref = ? GROUP BY as_of_date ORDER BY as_of_date")
            q, p = wl_store._filtered(sql, (account_ref,), table="positions")
            pos_series = [{"as_of_date": r["d"], "total_equity": round(r["eq"] or 0.0, 2)}
                          for r in conn.execute(q, p).fetchall() if r["d"]]
        else:
            pos_series = []
            refs = _visible_account_refs(wl_store)
            if refs:
                placeholders = ",".join("?" for _ in refs)
                sql = ("SELECT account_ref AS a, as_of_date AS d, SUM(market_value_base) AS eq "
                       f"FROM positions WHERE quantity != 0 AND account_ref IN ({placeholders}) "
                       "GROUP BY account_ref, as_of_date ORDER BY as_of_date")
                q, p = wl_store._filtered(sql, tuple(refs), table="positions")
                series = _forward_filled_series(conn.execute(q, p).fetchall())
    finally:
        conn.close()
    # P2-1：单账户曲线口径统一为「结单权威净值(含现金·净融资)」——与头条 total_equity 同口径，消同屏分裂。
    # 权威净值(vip_imports.total_equity = NAV / TOTAL VALUE / Total Assets−loan)按 period_end 逐期成点，
    # 有则为准(cash-inclusive)；无(仅持仓导出/旧导入未落 total_equity)才回落持仓市值口径(不含现金)，如实标 basis。
    # 结构性产品/衍生品走 vip_derivative_terms、不进 positions，其当期 MTM 已含在权威净值里；纯衍生品账户既无
    # 股票快照又无权威净值时，退回单点当期 MTM 锚点，曲线不空白。ponytail: 持仓多于结单期的日期点被权威净值取代属预期。
    # ponytail: 反向的半迁移态(最新一期是旧导入未落 total_equity、比已回填的旧期更新)下，末点保持权威净值口径、
    #           不追加更新的持仓点——补持仓点(不含现金)会重新引入 P2-1 消除的同屏口径分裂，得不偿失；重新导入
    #           使该期落 total_equity 即自愈。真正需要时的正解是读该期物化头条(sim_account)补回含现金口径,非补持仓和。
    if account_ref:
        auth = _import_total_series(wl_store, account_ref)
        if auth:
            series, basis = auth, "authoritative_total_equity"
        else:
            series = pos_series
            if not series:
                mtm = _derivative_mtm_total(wl_store, account_ref)
                if mtm:
                    anchor = _latest_import_period(wl_store, account_ref)
                    if anchor:
                        series = [{"as_of_date": anchor, "total_equity": round(mtm, 2)}]
                        basis = "derivative_mtm_anchor"
    # 叠加系统推算点：仅当推算日严格晚于最新真值快照日（否则真值优先，不覆盖）
    proj = _projection_point(wl_store, account_ref=account_ref)
    if proj and (not series or proj["as_of_date"] > series[-1]["as_of_date"]):
        # P2-1 口径对齐：推算点只重估了股票腿(不含现金)。权威净值口径(含现金·净融资)下直接叠加会把末点
        # 打回持仓市值口径、破坏红线不变量「曲线末点==头条 total_equity」、并派生虚假末期收益/回撤。
        # 故只取推算的股票重估「增量」(proj − 真值底座)，叠加到最近权威净值上——现金/衍生品/融资按最新
        # 结单恒定 carry-forward，末点仍是含现金口径且与头条同源。ponytail: 现金等非股票腿假定期间不变;
        # 待逐日现金/衍生品重估(P2 后续)再细化，推算点本就是虚线待校准态。
        if basis == "authoritative_total_equity" and account_ref and series:
            base_stock = sum(_latest_truth_mv_map(wl_store, account_ref).values())
            proj = {**proj, "total_equity": round(series[-1]["total_equity"] + (proj["total_equity"] - base_stock), 2)}
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
    return {"series": series, "returns": returns, "benchmark": benchmark_meta, "basis": basis}


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

def build_account_summary(wl_store, *, account_ref: str = "") -> dict:
    """从 sim_* 汇总组合结构（不调 LLM）：总权益、现金、持仓明细、集中度 Top5。供报告与 number_guard facts。"""
    # 硬守卫：VIP 端永不用空 ref。空 ref 会经 get_sim_account("") 越界读并【懒建】决策中心自有
    # 模拟盘(account_ref='')、预置 10 万/100 万本金 → 把幻影组合当"真实持仓"喂给 dossier/总览/LLM。
    # 见 memory dc_sim_account_decoupled。跨账户聚合走 build_total_overview/value_series(scope=all)，不经此函数。
    if not (account_ref or "").strip():
        raise ValueError("account_ref_required: VIP 账户视图须指定具体子账户，请先选择账户或上传月结单")
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
            "loan_balance": round(account.get("loan_balance", 0) or 0, 2),
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


def render_period_narrative(wl_store, account_ref: str) -> str:
    """P3-4 · 确定性本期叙事块（无 LLM）：现金流调整真实收益率 + 标的贡献 Top/Bottom + 确定性仓位事件。

    全部来自结构化事实——收益率过 Modified Dietz(已剔外部现金流)，贡献相邻两期×期初权重(剔买卖污染)，
    仓位事件走 attribution 确定性 diff。红线：仓位事件是「推断·非确认」备忘录、非因果结论。
    自包含(内部取 txns/vseries/contribution)，供 generate_vip_report 作为 AI 主观分析之前的事实锚。
    """
    from bottleneck_hunter.vip.attribution import _LABEL, detect_position_events
    txns = list_transactions(wl_store, account_ref=account_ref, limit=10000)
    vseries = value_series(wl_store, account_ref=account_ref)
    perf = _perf_summary(vseries, _overview_totals(txns), 0.0, flows=_external_flows(txns))
    contribution = _contribution(wl_store, account_ref)
    L: list[str] = []

    dr = perf.get("dietz_return_pct")
    if dr is not None:
        ann = perf.get("dietz_annualized_pct")
        L += ["## 本期业绩（现金流调整）", "",
              f"- 累计收益率：**{dr:+.2f}%**" + (f"（年化 {ann:+.2f}%）" if ann is not None else ""),
              f"  - 口径：{perf.get('dietz_basis', '')}"]
        if perf.get("sharpe") is not None:
            L.append(f"- 风险调整：Sharpe {perf['sharpe']}｜Sortino {perf.get('sortino')}｜Calmar {perf.get('calmar')}"
                     f"（{perf.get('risk_note', '')}）")
        L.append("")

    rows = contribution.get("rows") or []
    if rows:
        def _fmt(r):
            return (f"{r['symbol']} {r['contribution_pct']:+.2f}pct"
                    f"(单价{r['price_return_pct']:+.1f}%·权重{r['weight_pct']:.1f}%)")
        gainers = [r for r in rows if r["contribution_pct"] > 0][:3]
        losers = [r for r in rows if r["contribution_pct"] < 0][:3]
        L += [f"## 标的贡献归因（{contribution.get('prev_date', '')}→{contribution.get('cur_date', '')}）", ""]
        if gainers:
            L.append("- 贡献最大：" + "；".join(_fmt(r) for r in gainers))
        if losers:
            L.append("- 拖累最大：" + "；".join(_fmt(r) for r in losers))
        L.append(f"  - 覆盖 {contribution.get('coverage', '')}（相邻两期×期初权重，已剔买卖交割污染，非逐日）")
        L.append("")

    dates = _snapshot_dates(wl_store, account_ref, limit=2)
    if len(dates) >= 2:
        prev_rows, _ = _latest_positions(wl_store, dates[1], account_ref)
        cur_rows, _ = _latest_positions(wl_store, dates[0], account_ref)
        old = [{"ticker": r["symbol"], "shares": r.get("quantity"), "market_value": r.get("market_value_base")}
               for r in prev_rows]
        events = detect_position_events(old, cur_rows)
        if events:
            L += ["## 本期仓位变动（确定性·推断非确认）", ""]
            for e in events:
                L.append(f"- {_LABEL.get(e['event'], e['event'])} **{e['ticker']}**："
                         f"数量 {e['old_qty']:g}→{e['new_qty']:g}（{e['chg_pct']:+.1f}%）")
            L.append("")

    return "\n".join(L).strip()


def render_report_md(summary: dict, narrative: str = "", period: str = "", derivatives_md: str = "",
                     data_as_of: str = "", perf_narrative_md: str = "") -> str:
    """渲染报告 Markdown（append-lines 风格，仿 chain/report.py）。narrative 已过 number_guard。"""
    L: list[str] = []
    L.append(f"# 持仓分析报告{f'（{period}）' if period else ''}")
    L.append("")
    if data_as_of:  # 0-1：持仓数据截至日（结算单期末），与"生成于今天"区分开
        L.append(f"> 📅 数据截至 **{data_as_of}**（持仓来自该日结算单；市值或按更晚收盘重估，判断请以此为锚）")
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
    if perf_narrative_md:   # P3-4：确定性本期叙事（真实收益率+贡献+仓位事件），置于 AI 主观分析之前作事实锚
        L.append(perf_narrative_md)
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
    summary = build_account_summary(wl_store, account_ref=account_ref)
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

    summary = build_account_summary(wl_store, account_ref=account_ref)
    facts = json.dumps(summary, ensure_ascii=False, default=str)

    unverified = []
    if narrative:
        checks = number_guard.verify_numbers(narrative, facts)
        unverified = [c["token"] for c in checks if c["status"] == "unverified"]
        narrative = number_guard.annotate_unverified(narrative, facts)

    report_md = render_report_md(summary, narrative, period,
                                 derivatives_md=render_derivative_summary(derivative_terms or []),
                                 data_as_of=_holdings_as_of(wl_store, account_ref),
                                 perf_narrative_md=render_period_narrative(wl_store, account_ref))
    # F1：AI 报告挂"顾问可信度"角标——读回本模型 vip_advisor 历史校准(G5 复盘写入)，透明呈现。
    # 放 number_guard 之后，'0.83x' 不进防伪扫描；纯数据报告(无模型)不挂。
    if model_provider and model_name:
        from bottleneck_hunter.vip.advisory import advisor_calibration
        report_md += f"\n\n> 顾问可信度：{advisor_calibration(wl_store, model_provider, model_name)['note']}\n"

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
