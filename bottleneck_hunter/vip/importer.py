"""通用导入分发器：任意文件上传 → 自动判类型/内容 → 路由入库 → 统一 ImportResult + 留痕。

单一入口 dispatch_import()：
- PDF → 先 classify_pdf 判是否衍生品条款；命中则抽条款落 vip_derivative_terms，
  否则走 ingest_and_store（月结单 / 交易确认）+ normalize/materialize 投影到 positions/sim_*。
- CSV/Excel → parse_tabular 通用列映射 → StatementTransaction → 复用 normalize_statement。
- 重复(按文件哈希)/无法识别券商/加密/无法解读 → 结构化结果，不抛到 HTTP 层。
每次成功/拒绝/无法解读都写一条 vip_imports 历史（金额脱敏，不存明文 PII）。
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import asdict, dataclass, field


@dataclass
class ImportResult:
    status: str                 # imported | duplicate | rejected | unparseable | needs_account_confirmation
    file_name: str
    file_type: str              # pdf | csv | excel | unknown
    detected_kind: str          # monthly_statement | trade_confirm | accumulator | mli | unknown ...
    summary: str = ""
    key_metrics: dict = field(default_factory=dict)
    reason: str = ""
    resolved_account_ref: str = ""
    account_candidates: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


def dispatch_import(raw: bytes, filename: str, *, user_id: str, wl_store,
                    market: str = "us_stock", account_ref: str = "",
                    password: str = "") -> ImportResult:
    filename = filename or "upload"
    file_hash = hashlib.sha256(raw).hexdigest()
    file_type = _detect_file_type(raw, filename)

    resolved_account_ref = (account_ref or "").strip()
    account_candidates: list[dict] = []
    auto_reason = ""
    if resolved_account_ref:
        auto_reason = "explicit_account"
    else:
        accounts = _list_real_accounts(wl_store)
        if not accounts:
            return ImportResult(
                status="rejected",
                file_name=filename,
                file_type=file_type,
                detected_kind="unknown",
                summary="请先创建账户，或在导入时明确选择账户",
                reason="no_real_accounts",
            )
        resolved_account_ref, account_candidates, auto_reason = _resolve_account_hint(
            raw, filename, file_type, wl_store, password=password
        )
        if not resolved_account_ref:
            return ImportResult(
                status="needs_account_confirmation",
                file_name=filename,
                file_type=file_type,
                detected_kind="unknown",
                summary="未能唯一识别对应账户，请确认后重试",
                reason="account_confirmation_required",
                account_candidates=account_candidates,
            )

    # 去重：同文件已在导入历史。不直接短路——normalize/materialize 皆按 doc_id 幂等（持仓快照覆盖、
    # 交易 upsert、贷款账户级回填），重跑不重复计数，却能回填代码升级后新增的字段（如贷款）。
    # 仍标记为已回填（复用重导），不新增历史行。
    is_redo = _find_import(wl_store, file_hash, account_ref=resolved_account_ref) is not None

    if file_type == "pdf":
        result = _import_pdf(raw, filename, user_id, wl_store, market, resolved_account_ref, password)
    elif file_type in ("csv", "excel"):
        result = _import_tabular(raw, filename, file_type, wl_store, resolved_account_ref)
    else:
        result = ImportResult("unparseable", filename, "unknown", "unknown",
                              summary="无法识别的文件类型", reason="仅支持 PDF / CSV / Excel")

    if auto_reason and result.status == "imported":
        result.summary = f"{result.summary}（已自动归户）" if result.summary else "已自动归户"
    result.resolved_account_ref = resolved_account_ref
    result.account_candidates = account_candidates
    if is_redo:
        # 复用重导：回填已完成，提示用户这是重跑而非首次导入；不新增历史行（UNIQUE 已保证唯一）
        if result.status == "imported":
            result.summary = f"{result.summary}（重复导入，已回填最新字段）" if result.summary else "已回填最新字段"
        return result
    wl_store.create_vip_import(
        file_name=filename, file_hash=file_hash, file_type=result.file_type,
        detected_kind=result.detected_kind, status=result.status,
        summary=result.summary, key_metrics=result.key_metrics, reason=result.reason,
        account_ref=resolved_account_ref)
    return result


# ── 文件类型判别 ──────────────────────────────────────────────────────────

def _detect_file_type(raw: bytes, filename: str) -> str:
    if raw[:5] == b"%PDF-":
        return "pdf"
    if raw[:2] == b"PK":                 # xlsx 是 zip 容器
        return "excel"
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    if ext in ("csv", "tsv", "txt"):
        return "csv"
    for enc in ("utf-8", "gbk"):
        try:
            raw[:4096].decode(enc)
            return "csv"
        except UnicodeDecodeError:
            continue
    return "unknown"


def _find_import(wl_store, file_hash: str, account_ref: str = "") -> dict | None:
    # ponytail: 线性扫描导入历史（单用户量小）；量大再加按 hash 的索引查询方法
    # 只有"真正入库"的历史才算重复拦重试；rejected/unparseable/needs_* 是失败态，
    # 修好解析后必须允许重导（否则一次失败会用一行 vip_imports 永久毒化该文件的重试）。
    for row in wl_store.list_vip_imports(limit=2000, account_ref=account_ref):
        if row.get("file_hash") == file_hash and row.get("status") in ("imported", "duplicate"):
            return row
    return None


def _list_real_accounts(wl_store) -> list[dict]:
    return wl_store.list_vip_accounts(include_hidden_default=False)


def _candidate_payload(row: dict, score: int, reason: str) -> dict:
    return {
        "account_ref": row.get("account_ref", ""),
        "display_name": row.get("display_name", ""),
        "institution_name": row.get("institution_name", ""),
        "account_kind": row.get("account_kind", "broker"),
        "score": score,
        "reason": reason,
    }


def _match_account_by_ref(accounts: list[dict], ref: str) -> dict | None:
    needle = (ref or "").strip().lower()
    if not needle:
        return None
    for row in accounts:
        account_ref = (row.get("account_ref") or "").strip().lower()
        display_name = (row.get("display_name") or "").strip().lower()
        if needle == account_ref or needle == display_name:
            return row
    return None


# broker 探测出的是英文规范名，但账户机构名常填中文/别名 → 用别名表跨语言匹配，
# 否则中文机构名（如"野村"）永远匹配不上英文 broker（"nomura"），多账户自动归户失效。
_BROKER_ALIASES = {
    "nomura": ("nomura", "nsl", "野村"),
    "citi": ("citi", "citibank", "citigroup", "花旗", "花旗环球"),
    "cmbi": ("cmbi", "cmbis", "招银", "招银国际"),
}


def _match_accounts_by_broker(accounts: list[dict], broker: str) -> list[dict]:
    name = (broker or "").strip().lower()
    if not name or name == "unknown":
        return []
    aliases = _BROKER_ALIASES.get(name, (name,))
    out = []
    for row in accounts:
        inst = (row.get("institution_name") or "").strip().lower()
        ref = (row.get("account_ref") or "").strip().lower()
        if any(a in inst for a in aliases) or name == ref:
            out.append(row)
    return out


def _resolve_account_hint(raw: bytes, filename: str, file_type: str, wl_store, password: str = "") -> tuple[str, list[dict], str]:
    accounts = _list_real_accounts(wl_store)
    if not accounts:
        return "", [], ""
    if len(accounts) == 1:
        only = accounts[0]
        return (only.get("account_ref") or "", [_candidate_payload(only, 100, "only_real_account")], "single_account")

    candidates: list[dict] = []
    if file_type == "pdf":
        from bottleneck_hunter.vip import ingest
        try:
            pages = ingest._extract_pages(raw, pdf_password=password)
        except Exception:  # noqa: BLE001
            pages = []
        broker = ingest.detect_broker(pages, filename) if pages else "unknown"
        try:
            stmt = ingest.ingest_pdf(raw, filename=filename, broker_hint=(broker if broker != "unknown" else ""), pdf_password=password)
        except Exception:  # noqa: BLE001
            stmt = None
        if stmt is not None:
            refs = []
            for txn in getattr(stmt, "transactions", []) or []:
                ref = (getattr(txn, "account_ref", "") or "").strip()
                if ref and ref not in refs:
                    refs.append(ref)
            for ref in refs:
                row = _match_account_by_ref(accounts, ref)
                if row:
                    candidates.append(_candidate_payload(row, 100, f"statement_account_ref:{ref}"))
            acct_summary = getattr(stmt, "account_summary", {}) or {}
            for key in ("account_ref", "account_no", "account_number"):
                row = _match_account_by_ref(accounts, acct_summary.get(key, ""))
                if row:
                    candidates.append(_candidate_payload(row, 90, f"account_summary:{key}"))
        broker_matches = _match_accounts_by_broker(accounts, broker)
        if len(broker_matches) == 1:
            candidates.append(_candidate_payload(broker_matches[0], 70, f"broker:{broker}"))
        elif len(broker_matches) > 1:
            candidates.extend(_candidate_payload(row, 60, f"broker:{broker}") for row in broker_matches)

    dedup: dict[str, dict] = {}
    for item in candidates:
        ref = item.get("account_ref", "")
        if not ref:
            continue
        prev = dedup.get(ref)
        if prev is None or item.get("score", 0) > prev.get("score", 0):
            dedup[ref] = item
    ordered = sorted(dedup.values(), key=lambda x: (-x.get("score", 0), x.get("account_ref", "")))
    if len(ordered) == 1:
        return ordered[0]["account_ref"], ordered, ordered[0].get("reason", "")
    if ordered and ordered[0].get("score", 0) >= 90 and ordered[1].get("score", 0) < ordered[0].get("score", 0):
        return ordered[0]["account_ref"], ordered, ordered[0].get("reason", "")
    return "", ordered, ""



def _pwd_result(filename: str) -> ImportResult:
    """PDF 需要密码或密码错误：拒绝入库并提示用户重新上传时输入正确密码。"""
    return ImportResult("rejected", filename, "pdf", "unknown",
                        summary="PDF 已加密，需要密码",
                        reason="该 PDF 需要密码或密码不正确，请重新上传并在弹窗中输入正确密码")


def _import_pdf(raw, filename, user_id, wl_store, market, account_ref, password) -> ImportResult:
    from bottleneck_hunter.vip import derivatives as drv
    try:
        kind = drv.classify_pdf(raw, pdf_password=password)
    except ValueError as e:
        if "password" in str(e):
            return _pwd_result(filename)
        kind = "other"
    except Exception:  # noqa: BLE001 — 损坏/非法 PDF：交由 statement 路径统一报错
        kind = "other"
    if kind in ("accumulator", "decumulator", "mli"):
        return _import_derivative(raw, filename, kind, wl_store, password, account_ref=account_ref)
    return _import_statement(raw, filename, user_id, wl_store, market, account_ref, password)


def _import_derivative(raw, filename, kind, wl_store, password, account_ref: str = "") -> ImportResult:
    from bottleneck_hunter.vip import derivatives as drv
    from bottleneck_hunter.vip import ingest
    broker = ""
    try:
        pages = ingest._extract_pages(raw, pdf_password=password)
        broker = ingest.detect_broker(pages, filename)
    except Exception:  # noqa: BLE001 — broker 仅作展示，探测失败可空
        pass
    if broker == "unknown":
        broker = ""
    try:
        term = (drv.extract_mli_terms if kind == "mli" else drv.extract_accumulator_terms)(
            raw, pdf_password=password)
    except ValueError as e:
        if "password" in str(e):
            return _pwd_result(filename)
        return ImportResult("unparseable", filename, "pdf", kind,
                            summary="衍生品条款抽取失败", reason=str(e))
    drv.save_derivative_term(wl_store, term, source_file_name=filename,
                             source_file_hash=hashlib.sha256(raw).hexdigest(), broker=broker,
                             account_ref=account_ref)
    return ImportResult("imported", filename, "pdf", term.product_family,
                        summary=f"衍生品条款：{term.underlying_symbol} · {term.product_family}",
                        key_metrics={"underlying": term.underlying_symbol,
                                     "family": term.product_family, "currency": term.currency})


def _statement_from_doc(user_id: str, doc_id: str):
    from bottleneck_hunter.auth.store import AuthStore
    from bottleneck_hunter.vip.ingest import BrokerStatement
    d = AuthStore().get_financial_doc(user_id, doc_id, decrypt_parsed=True)
    if not d or not d.get("parsed_json"):
        return None
    try:
        return BrokerStatement.model_validate_json(d["parsed_json"])
    except Exception:  # noqa: BLE001
        return None


def _guarded_result(filename, kind, stmt, as_of_date, reason) -> ImportResult:
    """materialize 触发误覆盖护栏（陈旧单/骤降误判）：数据已入库，但 sim live 快照保持不动。
    状态仍为 imported（文件已落库/规范层留痕），summary 明确提示为防误覆盖已保留原快照。"""
    return ImportResult("imported", filename, "pdf", kind,
                        summary="已入库，但检测到数据异常（快照较旧或总值/持仓骤降），为防误覆盖已保留原账户快照，请人工核对",
                        reason=reason,
                        key_metrics={"period_end": stmt.period_end, "broker": stmt.broker,
                                     "as_of_date": as_of_date})


def _import_statement(raw, filename, user_id, wl_store, market, account_ref, password) -> ImportResult:
    from bottleneck_hunter.vip import ingest, portfolio
    try:
        # broker="" → 让 detect_broker 从正文/文件名识别（不强制 citi）
        res = ingest.ingest_and_store(raw, filename, user_id=user_id, market=market,
                                      broker="", pdf_password=password)
    except ValueError as e:
        msg = str(e)
        if "password" in msg:
            return _pwd_result(filename)
        if "unsupported" in msg:
            return ImportResult("rejected", filename, "pdf", "unknown",
                                summary="无法识别的对账单格式", reason=f"暂不支持的券商/格式：{msg}")
        return ImportResult("unparseable", filename, "pdf", "unknown", summary="解析失败", reason=msg)
    except Exception as e:  # noqa: BLE001
        return ImportResult("unparseable", filename, "pdf", "unknown", summary="解析失败", reason=str(e))

    doc_id = res["doc_id"]
    doc_type = res.get("doc_type", "")
    stmt = _statement_from_doc(user_id, doc_id)
    if stmt is None:
        return ImportResult("unparseable", filename, "pdf", doc_type or "unknown",
                            summary="解析结果无效", reason="parsed_json 缺失")

    if doc_type == "trade_confirm":
        norm = portfolio.normalize_statement(wl_store, stmt, source_doc_id=doc_id, account_ref=account_ref)
        n = norm.get("n_transactions", 0)
        return ImportResult("imported", filename, "pdf", "trade_confirm",
                            summary=f"交易导出：导入 {n} 笔流水",
                            key_metrics={"n_transactions": n, "period_end": stmt.period_end,
                                         "broker": stmt.broker})

    if doc_type == "position_report":
        if res["status"] != "parsed_ok":
            return ImportResult("imported", filename, "pdf", "position_report",
                                summary="当前持仓导出已入库，待人工复核", reason=res["status"],
                                key_metrics={"broker": stmt.broker, "period_end": stmt.period_end})
        norm = portfolio.normalize_statement(wl_store, stmt, source_doc_id=doc_id, account_ref=account_ref)
        mat = portfolio.materialize_portfolio(wl_store, as_of_date=norm["as_of_date"],
                                              account_ref=account_ref,
                                              cash_total_usd=stmt.total_cash_usd)
        if mat.get("guard_skipped"):
            return _guarded_result(filename, "position_report", stmt, norm["as_of_date"], mat["guard_skipped"])
        return ImportResult("imported", filename, "pdf", "position_report",
                            summary=f"当前持仓导出：{mat['n_positions']} 只持仓，期末 {stmt.period_end or '—'}",
                            key_metrics={"n_positions": mat["n_positions"], "period_end": stmt.period_end,
                                         "broker": stmt.broker, "as_of_date": norm["as_of_date"]})

    if res["status"] != "parsed_ok":
        return ImportResult("imported", filename, "pdf", "monthly_statement",
                            summary="月结单已入库，待人工复核", reason=res["status"],
                            key_metrics={"broker": stmt.broker, "period_end": stmt.period_end})
    norm = portfolio.normalize_statement(wl_store, stmt, source_doc_id=doc_id, account_ref=account_ref)
    if not norm.get("snapshot_applied", True):
        return ImportResult("imported", filename, "pdf", "monthly_statement",
                            summary="月结单已入库，当前持仓保持更高优先级快照",
                            key_metrics={"period_end": stmt.period_end, "broker": stmt.broker,
                                         "as_of_date": norm["as_of_date"]})
    nav = None
    broker_id = getattr(stmt, "broker", "")
    summary = getattr(stmt, "account_summary", {}) or {}
    if broker_id == "nomura":
        nav = summary.get("net_asset_value_usd")
        # 野村摘要抽取(_parse_nomura_summary 的固定列偏移)对当前版式不可靠：会把负债当 NAV(负值)、
        # 权益合计抽成 0。非正 NAV 不可信 → 回落 None，让 materialize 用"持仓+现金"算总权益。
        # ponytail: 拿到可复现的野村版式样本后，应改 _parse_nomura_summary 按列锚点定位以取回真实 NAV。
        if not nav or nav <= 0:
            nav = None
    elif broker_id == "cmbi":
        # 招银 TOTAL VALUE = 现金 + 待结 + 组合市值，是结单权威总权益锚。日结单结算中途现金仍含
        # 待结的 FCN 付款(待结列为负)，直接"持仓+现金"会重复计其名义 → 必须用 TOTAL VALUE 覆盖。
        nav = summary.get("total_value_usd")
        if not nav or nav <= 0:
            nav = None
    mat = portfolio.materialize_portfolio(wl_store, as_of_date=norm["as_of_date"],
                                          account_ref=account_ref,
                                          cash_total_usd=stmt.total_cash_usd, account_total_usd=nav,
                                          loan_total_usd=summary.get("loan_outstanding_usd"))
    if mat.get("guard_skipped"):
        return _guarded_result(filename, "monthly_statement", stmt, norm["as_of_date"], mat["guard_skipped"])
    return ImportResult("imported", filename, "pdf", "monthly_statement",
                        summary=f"月结单：{mat['n_positions']} 只持仓，期末 {stmt.period_end or '—'}",
                        key_metrics={"n_positions": mat["n_positions"], "period_end": stmt.period_end,
                                     "broker": stmt.broker, "as_of_date": norm["as_of_date"]})



# ── CSV / Excel 路由：通用列映射 ─────────────────────────────────────────

_SYNONYMS: dict[str, set[str]] = {
    "date": {"date", "tradedate", "trade_date", "交易日期", "日期", "成交日期", "业务日期"},
    "ticker": {"ticker", "symbol", "代码", "股票代码", "证券代码", "股票"},
    "side": {"side", "type", "txntype", "txn_type", "direction", "方向", "交易类型", "买卖方向", "业务名称"},
    "qty": {"qty", "quantity", "shares", "volume", "数量", "成交数量", "股数"},
    "price": {"price", "成交价", "价格", "成交价格"},
    "amount": {"amount", "netamount", "net_amount", "金额", "成交金额", "发生金额", "净额"},
    "currency": {"currency", "ccy", "币种", "货币"},
    "description": {"description", "desc", "memo", "note", "摘要", "备注", "说明"},
}

_SIDE_MAP = {
    "buy": "buy", "b": "buy", "买入": "buy", "买": "buy", "purchase": "buy",
    "sell": "sell", "s": "sell", "卖出": "sell", "卖": "sell", "sale": "sell",
    "dividend": "dividend", "分红": "dividend", "股息": "dividend",
    "fee": "fee", "费用": "fee", "手续费": "fee", "commission": "fee",
    "interest": "interest", "利息": "interest",
    "deposit": "deposit", "入金": "deposit", "转入": "deposit",
    "withdraw": "withdraw", "withdrawal": "withdraw", "出金": "withdraw", "转出": "withdraw",
}


def _norm_header(h) -> str:
    return str(h).strip().lower().replace(" ", "")


def parse_tabular(raw: bytes, file_type: str):
    """读 CSV/Excel 为 DataFrame（pandas 已是主依赖，统一处理两种格式 + 编码兜底）。"""
    import io

    import pandas as pd
    if file_type == "excel":
        return pd.read_excel(io.BytesIO(raw))          # openpyxl 后端
    try:
        return pd.read_csv(io.BytesIO(raw))
    except UnicodeDecodeError:
        return pd.read_csv(io.BytesIO(raw), encoding="gbk")


def _map_columns(cols) -> dict:
    syn = {canon: {_norm_header(s) for s in names} for canon, names in _SYNONYMS.items()}
    out: dict[str, object] = {}
    for c in cols:
        key = _norm_header(c)
        for canon, names in syn.items():
            if canon not in out and key in names:
                out[canon] = c
                break
    return out


def _num(v) -> float:
    try:
        f = float(str(v).replace(",", "").replace("$", "").strip())
        return 0.0 if math.isnan(f) else f
    except (ValueError, TypeError):
        return 0.0


def _iso_date(s: str) -> str:
    import pandas as pd
    try:
        return pd.to_datetime(s).strftime("%Y-%m-%d")
    except Exception:  # noqa: BLE001
        return str(s)


def _row_to_txn(row, colmap):
    from bottleneck_hunter.vip.ingest import StatementTransaction

    def g(canon):
        c = colmap.get(canon)
        return row[c] if c is not None and c in row else None

    date = str(g("date") or "").strip()
    if not date or date.lower() in ("nan", "nat"):
        return None
    raw_side = str(g("side") or "").strip().lower()
    txn_type = _SIDE_MAP.get(raw_side, raw_side or "other")
    amount = _num(g("amount"))
    return StatementTransaction(
        ticker=str(g("ticker") or "").strip(),
        txn_type=txn_type, trade_date=_iso_date(date),
        quantity=_num(g("qty")), price=_num(g("price")),
        gross_amount=amount, net_amount=amount,
        currency=(str(g("currency") or "USD").strip() or "USD"),
        description=str(g("description") or "").strip())


def _import_tabular(raw, filename, file_type, wl_store, account_ref) -> ImportResult:
    from bottleneck_hunter.vip import portfolio
    from bottleneck_hunter.vip.ingest import BrokerStatement, ReconResult
    try:
        df = parse_tabular(raw, file_type)
    except Exception as e:  # noqa: BLE001
        return ImportResult("unparseable", filename, file_type, "unknown",
                            summary="表格读取失败", reason=str(e))
    colmap = _map_columns(list(df.columns))
    if "date" not in colmap or ("amount" not in colmap and "qty" not in colmap):
        have = ", ".join(str(c) for c in df.columns)
        return ImportResult("unparseable", filename, file_type, "tabular",
                            summary="无法识别为交易流水表",
                            reason=f"缺少必要列(日期 + 金额或数量)。检测到表头：{have}")
    txns = [t for t in (_row_to_txn(r, colmap) for _, r in df.iterrows()) if t]
    if not txns:
        return ImportResult("unparseable", filename, file_type, "tabular",
                            summary="表内无有效数据行", reason="所有行解析为空")
    fh = hashlib.sha256(raw).hexdigest()
    stmt = BrokerStatement(
        broker="generic", content_hash=fh, transactions=txns,
        recon=ReconResult(holdings_count=0, holdings_total_usd=0.0,
                          statement_equities_total_usd=None, delta_usd=None,
                          status="no_statement_total"))
    norm = portfolio.normalize_statement(wl_store, stmt, source_doc_id="tab:" + fh[:12],
                                         account_ref=account_ref)
    n = norm.get("n_transactions", 0)
    return ImportResult("imported", filename, file_type, "trade_confirm",
                        summary=f"表格流水：导入 {n} 笔交易", key_metrics={"n_transactions": n})
