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
from bottleneck_hunter.vip.ingest import BrokerStatement


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ETF ISIN → 可交易代码映射（M1 手工小表；P2 正式版接 OpenFIGI/券商主数据）。
# ingest 对 ETF 用 ISIN 作 symbol，此处映射到可取行情的 ticker。
_ISIN_TO_TICKER = {
    "US4642875235": "SOXX",   # iShares Semiconductor ETF
}


def _map_symbol(symbol: str) -> tuple[str, str]:
    """返回 (可交易代码, instrument_type)。ISIN 形态→查表映射为 ETF ticker。"""
    if len(symbol) >= 11 and symbol[:2].isalpha() and symbol[2:].isalnum():
        return _ISIN_TO_TICKER.get(symbol, symbol), "etf"
    return symbol, "stock"


# ── P2: 规范化 —— BrokerStatement → instruments + positions ──────────────

def normalize_statement(wl_store, stmt: BrokerStatement,
                        source_doc_id: str = "", account_ref: str = "") -> dict:
    """把已解析的 BrokerStatement 写入规范层 instruments + positions（幂等 upsert）。

    多币种：market_value_usd 已是统一美元口径（ingest 取 Total Value USD 列），
    直接作 market_value_base；组合占比一律用 base 口径。返回 {n_instruments, n_positions}。
    """
    as_of = stmt.period_end or _now_iso()[:10]
    n_inst = n_pos = 0
    for h in stmt.holdings:
        symbol, itype = _map_symbol(h.ticker)
        inst_id = _upsert_instrument(wl_store, symbol, itype, h.company,
                                     h.nominal_ccy, source_doc_id)
        mv_base = h.market_value_usd                 # 统一美元基币
        _upsert_position(wl_store, inst_id, account_ref, as_of,
                         quantity=h.quantity, market_value_base=mv_base,
                         currency=h.nominal_ccy, source_doc_id=source_doc_id)
        n_inst += 1
        n_pos += 1
    return {"n_instruments": n_inst, "n_positions": n_pos, "as_of_date": as_of}


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
                     quantity, market_value_base, currency, source_doc_id) -> None:
    with wl_store._write_conn() as conn:
        # 幂等：同 (account_ref, instrument_id, as_of_date) 已存在则更新
        q, p = wl_store._filtered(
            "SELECT id FROM positions WHERE account_ref = ? AND instrument_id = ? AND as_of_date = ?",
            (account_ref, instrument_id, as_of_date))
        row = conn.execute(q, p).fetchone()
        if row:
            q2, p2 = wl_store._filtered(
                "UPDATE positions SET quantity=?, market_value_base=?, market_value=?, currency=? WHERE id=?",
                (quantity, market_value_base, market_value_base, currency, row["id"]))
            conn.execute(q2, p2)
            return
        pid = uuid.uuid4().hex[:12]
        conn.execute(
            f"""INSERT INTO positions
               (id, instrument_id, account_ref, as_of_date, quantity, currency,
                market_value, market_value_base, source_doc_id, created_at{wl_store._user_insert_cols()}{wl_store._market_insert_cols()})
               VALUES (?,?,?,?,?,?,?,?,?,?{wl_store._user_insert_vals()}{wl_store._market_insert_vals()})""",
            (pid, instrument_id, account_ref, as_of_date, quantity, currency,
             market_value_base, market_value_base, source_doc_id, _now_iso())
            + wl_store._user_insert_params() + wl_store._market_insert_params(),
        )


# ── P5: 物化 —— 规范 positions → sim_account + sim_positions ──────────────

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
    account = wl_store.get_sim_account()
    acct_id = account["id"]

    # 冻结旧快照（溯源锚）
    old_positions = wl_store.get_sim_positions(acct_id)
    snap_id = ""
    if old_positions:
        snap_id = _freeze_snapshot(wl_store, account, old_positions)

    # 取规范层最新快照
    rows = _latest_positions(wl_store, as_of_date, account_ref)
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

    wl_store.update_sim_account(total_equity=round(total_equity, 2),
                                current_capital=round(total_equity, 2),
                                cash_balance=round(cash_total_usd or 0.0, 2))
    return {"account_id": acct_id, "n_positions": n,
            "total_equity": round(total_equity, 2),
            "cash_balance": round(cash_total_usd or 0.0, 2),
            "snapshot_report_id": snap_id}


def _latest_positions(wl_store, as_of_date, account_ref) -> list[dict]:
    """取规范层持仓 + 工具符号（join instruments）。as_of_date 空则取最新日。"""
    conn = wl_store._connect()
    try:
        if not as_of_date:
            q, p = wl_store._filtered("SELECT MAX(as_of_date) AS d FROM positions", table="positions")
            row = conn.execute(q, p).fetchone()
            as_of_date = row["d"] if row and row["d"] else ""
        q, p = wl_store._filtered(
            """SELECT p.quantity, p.market_value_base, i.symbol, i.instrument_type, i.name
               FROM positions p JOIN instruments i ON i.id = p.instrument_id
               WHERE p.as_of_date = ? AND p.quantity != 0""",
            (as_of_date,), table="p")
        return [dict(r) for r in conn.execute(q, p).fetchall()]
    finally:
        conn.close()


def _freeze_snapshot(wl_store, account, positions) -> str:
    rid = uuid.uuid4().hex[:12]
    payload = {"account": {k: account.get(k) for k in ("total_equity", "cash_balance")},
               "positions": [{"ticker": p["ticker"], "shares": p["shares"],
                              "market_value": p["market_value"]} for p in positions]}
    with wl_store._write_conn() as conn:
        conn.execute(
            f"""INSERT INTO vip_reports (id, kind, period, payload_json, created_at{wl_store._user_insert_cols()}{wl_store._market_insert_cols()})
               VALUES (?,?,?,?,?{wl_store._user_insert_vals()}{wl_store._market_insert_vals()})""",
            (rid, "import_snapshot", _now_iso()[:10],
             json.dumps(payload, ensure_ascii=False, default=str), _now_iso())
            + wl_store._user_insert_params() + wl_store._market_insert_params(),
        )
    return rid


# ── P5: 报告 —— sim_* → 组合摘要 →（LLM 叙事，M1 可选）→ 落库 ──────────

def build_portfolio_summary(wl_store) -> dict:
    """从 sim_* 汇总组合结构（不调 LLM）：总权益、现金、持仓明细、集中度 Top5。供报告与 number_guard facts。"""
    account = wl_store.get_sim_account()
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
            "holdings": holdings, "top5_concentration_pct": round(top5, 1)}


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

请用简体中文、分三层给出专业意见，每层 2-4 句，务实不空泛，**只依据上面给出的数字**，
不要编造快照里没有的价格/收益/占比：

## 一、宏观研判
（当前宏观与所处行业周期对该组合的影响判断）

## 二、组合配置诊断
（集中度、行业/单票暴露、结构性风险；点名占比过高的持仓）

## 三、操作建议
（给出方向性建议：加/减/持/对冲，说明理由；不承诺收益）

要求：直接输出上述三段 Markdown，不要额外前言/结语/免责（系统会另加免责声明）。"""


async def generate_advisor_narrative(summary: dict, *, user_id: str = "",
                                     budget=None) -> dict:
    """调 vip_advisor 角色生成分层叙事（宏观/配置/操作）。返回 {narrative, provider, model}。

    facts=组合摘要；叙事回来后由 generate_vip_report 过 number_guard 标未核到数字。
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
    prompt = _ADVISOR_PROMPT.format(facts=facts)
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
                                 user_id: str = "", budget=None) -> dict:
    """异步：组合摘要 → vip_advisor 分层叙事 → number_guard → 落库。M1 报告的 AI 增强入口。"""
    summary = build_portfolio_summary(wl_store)
    nar = await generate_advisor_narrative(summary, user_id=user_id, budget=budget)
    return generate_vip_report(
        wl_store, period=period, narrative=nar.get("narrative", ""),
        source_doc_ids=source_doc_ids,
        model_provider=nar.get("provider", ""), model_name=nar.get("model", ""))


def generate_vip_report(wl_store, *, period: str = "", narrative: str = "",
                        source_doc_ids: list | None = None,
                        model_provider: str = "", model_name: str = "",
                        derivative_terms: list | None = None) -> dict:
    """生成并落库一份持仓分析报告。narrative 为 LLM 叙事段（可空=纯数据报告）。

    narrative 渲染前**强制过 number_guard**（facts=组合摘要），未核到的金额/占比标"⚠未核到"。
    落 vip_reports(kind='periodic') + advice_audit_trail。返回 {report_id, report_md, unverified}。
    """
    import hashlib

    summary = build_portfolio_summary(wl_store)
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
            f"""INSERT INTO vip_reports (id, kind, period, report_md, payload_json, created_at{wl_store._user_insert_cols()}{wl_store._market_insert_cols()})
               VALUES (?,?,?,?,?,?{wl_store._user_insert_vals()}{wl_store._market_insert_vals()})""",
            (rid, "periodic", period, report_md,
             json.dumps(summary, ensure_ascii=False, default=str), _now_iso())
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
