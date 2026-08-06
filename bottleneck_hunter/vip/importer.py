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
    elif file_type == "docx":
        result = _import_termsheet_docx(raw, filename, wl_store, resolved_account_ref)
    else:
        result = ImportResult("unparseable", filename, "unknown", "unknown",
                              summary="无法识别的文件类型", reason="仅支持 PDF / CSV / Excel / Word 条款单")

    if auto_reason and result.status == "imported":
        result.summary = f"{result.summary}（已自动归户）" if result.summary else "已自动归户"
    result.resolved_account_ref = resolved_account_ref
    result.account_candidates = account_candidates
    if is_redo and result.status == "imported":
        # 复用重导：提示用户这是重跑而非首次导入
        result.summary = f"{result.summary}（重复导入，已回填最新字段）" if result.summary else "已回填最新字段"
    # 无论首导还是重导都写 create_vip_import——它按 (account_ref, file_hash) upsert（见 store_simtrading.py），
    # 重导只刷新既有行而不新增。之前 is_redo 直接 return 会跳过这次 upsert，导致代码升级后新增的 key_metrics
    # 字段（如 total_equity 逐期净值锚点）永远写不进旧行——价值曲线因此建不出多点。
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
    if raw[:2] == b"PK":                 # zip 容器：docx 与 xlsx 同魔数，靠扩展名区分
        ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
        return "docx" if ext == "docx" else "excel"
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
    if kind in ("accumulator", "decumulator", "mli", "fcn"):
        return _import_derivative(raw, filename, kind, wl_store, password, account_ref=account_ref)
    if kind == "product_intro":
        # 产品介绍/推介材料（indicative term sheet）：无成交/持仓，明确拒绝且不计入持仓
        return ImportResult("rejected", filename, "pdf", "product_intro",
                            summary="产品介绍/推介材料，不含持仓信息，未计入持仓",
                            reason="该文件为产品介绍（indicative term sheet），非成交/持仓凭证")
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
        _extractor = {"mli": drv.extract_mli_terms, "fcn": drv.extract_fcn_terms}.get(
            kind, drv.extract_accumulator_terms)
        term = _extractor(raw, pdf_password=password)
    except ValueError as e:
        if "password" in str(e):
            return _pwd_result(filename)
        return ImportResult("unparseable", filename, "pdf", kind,
                            summary="衍生品条款抽取失败", reason=str(e))
    bad = drv.validate_derivative_term(term)
    if bad:
        return ImportResult("unparseable", filename, "pdf", term.product_family or kind,
                            summary="衍生品条款数值异常，疑似解析残缺，未入库，请人工核对",
                            reason=f"sanity_guard:{bad}")
    drv.save_derivative_term(wl_store, term, source_file_name=filename,
                             source_file_hash=hashlib.sha256(raw).hexdigest(), broker=broker,
                             account_ref=account_ref, lot_key=getattr(term, "lot_key", ""))
    return ImportResult("imported", filename, "pdf", term.product_family,
                        summary=f"衍生品条款：{term.underlying_symbol} · {term.product_family}",
                        key_metrics={"underlying": term.underlying_symbol,
                                     "family": term.product_family, "currency": term.currency})


def _import_termsheet_docx(raw, filename, wl_store, account_ref: str = "") -> ImportResult:
    """Word(.docx) 结构性产品条款单(招银国际 Worst-of Autocall FCN) → equity_fcn 记录。
    与 PDF 衍生品同落 vip_derivative_terms（幂等，lot_key=ISIN:到期日），不进 sim_positions。"""
    from bottleneck_hunter.vip import derivatives as drv
    try:
        term = drv.extract_cmbi_fcn_docx(raw)
    except ImportError:
        return ImportResult("rejected", filename, "docx", "unknown",
                            summary="服务器缺少 python-docx，无法解析 Word 条款单", reason="python-docx 未安装")
    except Exception as e:  # noqa: BLE001
        return ImportResult("unparseable", filename, "docx", "unknown",
                            summary="Word 条款单解析失败", reason=str(e))
    if not term.terms.get("underlyings"):
        return ImportResult("unparseable", filename, "docx", "unknown",
                            summary="未能从条款单识别标的篮子", reason="no_basket_parsed")
    bad = drv.validate_derivative_term(term)
    if bad:
        return ImportResult("unparseable", filename, "docx", term.product_family or "unknown",
                            summary="条款单数值异常，疑似解析残缺，未入库，请人工核对",
                            reason=f"sanity_guard:{bad}")
    drv.save_derivative_term(wl_store, term, source_file_name=filename,
                             source_file_hash=hashlib.sha256(raw).hexdigest(), broker="cmbi",
                             account_ref=account_ref, lot_key=term.lot_key)
    unds = "/".join(b["symbol"] for b in term.terms["underlyings"])
    wo = "（Worst-of）" if term.terms.get("worst_of") else ""
    mat = term.terms.get("maturity") or "—"
    return ImportResult("imported", filename, "docx", term.product_family,
                        summary=f"结构性产品条款单：{unds} · {term.product_family}{wo}，到期 {mat}",
                        key_metrics={"underlying": term.underlying_symbol, "family": term.product_family,
                                     "currency": term.currency, "notional": term.terms.get("notional"),
                                     "maturity": term.terms.get("maturity"), "isin": term.terms.get("isin")})


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


def _persist_derivative_terms(wl_store, stmt, filename, account_ref) -> int:
    """把结单抽出的结构性产品/衍生品薄记录落 vip_derivative_terms（幂等，lot_key 判别同标的多笔）。
    与 sim_positions 股票分栏——这些头寸不进模拟持仓。返回落库/命中条数。"""
    from bottleneck_hunter.vip.derivatives import DerivativeTerm, save_derivative_term
    rows = getattr(stmt, "derivative_terms", None) or []
    if not rows:
        return 0
    from bottleneck_hunter.vip.derivatives import validate_derivative_term
    broker = getattr(stmt, "broker", "") or ""
    file_hash = getattr(stmt, "content_hash", "") or ""
    n = 0
    for r in rows:
        try:
            term = DerivativeTerm(
                product_family=r.get("product_family", ""),
                underlying_symbol=r.get("underlying_symbol", ""),
                currency=r.get("currency", "USD"),
                tenor_days=int(r.get("tenor_days", 0) or 0),
                terms=r.get("terms", {}) or {},
                source_file=filename,
            )
            # 单条薄记录也过合理性护栏：坏条(数值错位/空壳)跳过不入库，不拖垮整单其余好条。
            # ponytail: 跳过计数暂不透出到 UI(月结单薄记录本就稀疏、坏条罕见)；真出现批量误跳再加账户日志。
            if validate_derivative_term(term):
                continue
            save_derivative_term(wl_store, term, source_file_name=filename, source_file_hash=file_hash,
                                 broker=broker, account_ref=account_ref, lot_key=r.get("lot_key", ""))
            n += 1
        except Exception:  # noqa: BLE001 — 单条薄记录抽取残缺不该拖垮整单导入
            continue
    return n


def _guarded_result(filename, kind, stmt, as_of_date, reason, n_deriv: int = 0, n_back: int = 0) -> ImportResult:
    """materialize 触发误覆盖护栏：数据已入库，但 sim live 快照保持不动（状态仍 imported）。
    按护栏原因分流文案——陈旧单是正常时效行为(账户已有更新快照)，不该报"数据异常"吓用户；
    只有骤降误判(suspected_misparse)才真需人工核对是否解析残缺。"""
    if reason.startswith("stale_snapshot"):
        summary = "已入库；账户已有更新日期的持仓快照，本结单作历史留档，未回填实时仓位（正常）"
    else:
        summary = "已入库，但检测到总值/持仓骤降（疑似解析残缺），为防误覆盖已保留原账户快照，请人工核对"
    if n_back:
        summary += f"；已按成本回填 {n_back} 只持仓（现价颜色/未实现盈亏已更新）"
    if n_deriv:
        summary += f"；另已记录 {n_deriv} 笔结构性产品/衍生品"
    return ImportResult("imported", filename, "pdf", kind,
                        summary=summary, reason=reason,
                        key_metrics={"period_end": stmt.period_end, "broker": stmt.broker,
                                     "as_of_date": as_of_date, "n_derivatives": n_deriv,
                                     "n_cost_backfilled": n_back})


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
        # 花旗新版仓盘含结构性产品/期权(→衍生品薄记录)：与月结单分支同口径落 vip_derivative_terms，
        # 否则仓盘里的 MLI/accumulator 永进不了库。幂等按 lot_key 去重，不受股票快照时效护栏影响。
        n_deriv = _persist_derivative_terms(wl_store, stmt, filename, account_ref)
        deriv_hint = f"，另 {n_deriv} 笔结构性产品/衍生品" if n_deriv else ""
        norm = portfolio.normalize_statement(wl_store, stmt, source_doc_id=doc_id, account_ref=account_ref)
        mat = portfolio.materialize_portfolio(wl_store, as_of_date=norm["as_of_date"],
                                              account_ref=account_ref,
                                              cash_total_usd=stmt.total_cash_usd,
                                              loan_total_usd=(stmt.account_summary or {}).get("loan_outstanding_usd"))
        if mat.get("guard_skipped"):
            return _guarded_result(filename, "position_report", stmt, norm["as_of_date"], mat["guard_skipped"],
                                   n_deriv, n_back=mat.get("n_cost_backfilled", 0))
        if mat.get("skipped_empty"):
            return ImportResult("imported", filename, "pdf", "position_report",
                                summary=f"当前持仓导出：未识别到持仓，未更新账户{deriv_hint}",
                                reason="empty_positions",
                                key_metrics={"n_positions": 0, "n_derivatives": n_deriv,
                                             "period_end": stmt.period_end, "broker": stmt.broker})
        return ImportResult("imported", filename, "pdf", "position_report",
                            summary=f"当前持仓导出：{mat['n_positions']} 只持仓{deriv_hint}"
                                    f"，期末 {stmt.period_end or '—'}",
                            key_metrics={"n_positions": mat["n_positions"], "n_derivatives": n_deriv,
                                         "period_end": stmt.period_end,
                                         "broker": stmt.broker, "as_of_date": norm["as_of_date"]})

    if res["status"] != "parsed_ok":
        return ImportResult("imported", filename, "pdf", "monthly_statement",
                            summary="月结单已入库，待人工复核", reason=res["status"],
                            key_metrics={"broker": stmt.broker, "period_end": stmt.period_end})
    # 衍生品/结构性产品是账户级头寸，与股票快照时效无关（仿 materialize 里贷款的处理）：只要本月结单
    # parsed_ok 就落库，绝不被下面「同期更高优先级快照 / 陈旧覆盖」两道 sim_positions 护栏拦掉——
    # 否则花旗这类"最新持仓在另一份导出、衍生品只在月结单"的账户，衍生品/结构性产品永远进不了库。
    # ponytail: 多份不同期月结单会各自留一份期末快照行（按 file_hash 幂等去重）；期级替换待需要时再做。
    n_deriv = _persist_derivative_terms(wl_store, stmt, filename, account_ref)
    deriv_hint = f"，另 {n_deriv} 笔结构性产品/衍生品" if n_deriv else ""
    norm = portfolio.normalize_statement(wl_store, stmt, source_doc_id=doc_id, account_ref=account_ref)
    if not norm.get("snapshot_applied", True):
        return ImportResult("imported", filename, "pdf", "monthly_statement",
                            summary=f"月结单已入库{deriv_hint}，当前持仓保持更高优先级快照",
                            key_metrics={"period_end": stmt.period_end, "broker": stmt.broker,
                                         "n_derivatives": n_deriv, "as_of_date": norm["as_of_date"]})
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
    elif broker_id == "citi":
        # 花旗 Total Assets 是「总资产(gross)」锚，含结构性产品/衍生品市值，比"股票+现金"更完整；
        # 但它未扣融资负债。野村(NAV)/招银(TOTAL VALUE)都是净值口径，故此处须减去融资负债对齐为净权益，
        # 否则有保证金贷款的账户总权益会被高估一整笔贷款(样本约 +46%)。ponytail: Total Assets 为 gross。
        nav = summary.get("total_assets_usd")
        if not nav or nav <= 0:
            nav = None
        else:
            loan = summary.get("loan_outstanding_usd") or 0.0
            if 0 < loan < nav:
                nav = round(nav - loan, 2)
    # 结单本应带权威总额锚(NAV/TOTAL VALUE/Total Assets)，若抽取缺失或非正被回落 None，
    # materialize 将改用「持仓+现金」估算总权益——这会漏掉结构性产品/衍生品市值。发生即记 warn 账户日志，
    # 让顾问知道该期总权益是估算而非结单权威值(不静默)。ponytail: PROJ8 可见化。
    _ANCHOR_KEY = {"nomura": "net_asset_value_usd", "cmbi": "total_value_usd", "citi": "total_assets_usd"}
    if broker_id in _ANCHOR_KEY and nav is None:
        try:
            wl_store.log_account_event(
                account_ref=account_ref, event_type="anomaly",
                title="结单权威总额缺失，总权益改用「持仓+现金」估算",
                detail=f"{broker_id} 结单未抽到有效 {_ANCHOR_KEY[broker_id]}（缺失或非正），"
                       "本期总权益为估算值、可能不含结构性产品/衍生品市值，请以结单原件为准。",
                severity="warn", payload={"broker": broker_id, "anchor_key": _ANCHOR_KEY[broker_id]})
        except Exception:  # noqa: BLE001 — 日志失败不该拖垮导入
            pass
    mat = portfolio.materialize_portfolio(wl_store, as_of_date=norm["as_of_date"],
                                          account_ref=account_ref,
                                          cash_total_usd=stmt.total_cash_usd, account_total_usd=nav,
                                          loan_total_usd=summary.get("loan_outstanding_usd"))
    if mat.get("guard_skipped"):
        return _guarded_result(filename, "monthly_statement", stmt, norm["as_of_date"],
                               mat["guard_skipped"], n_deriv, n_back=mat.get("n_cost_backfilled", 0))
    # 落逐期权威总额：纯结构性产品/衍生品账户(如招银全 FCN)的 positions 表为空，价值曲线无从按持仓建线；
    # 把本期结单权威净值(mat.total_equity=NAV/TOTAL VALUE/Total Assets)按 period_end 留在 key_metrics，
    # value_series 便能对无持仓账户拼出逐期净值曲线。ponytail: 借 vip_imports 已有的 period_end 存点，不新增表。
    return ImportResult("imported", filename, "pdf", "monthly_statement",
                        summary=f"月结单：{mat['n_positions']} 只持仓{deriv_hint}，期末 {stmt.period_end or '—'}",
                        key_metrics={"n_positions": mat["n_positions"], "n_derivatives": n_deriv,
                                     "period_end": stmt.period_end, "total_equity": mat.get("total_equity"),
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
    # 规范值必须命中 transactions.txn_type 的 CHECK 约束(store_schema)——只有 'withdrawal' 合法。
    # 曾误映射为半词 'withdraw'：既撞 CHECK 抛 IntegrityError 令含提取行的 CSV/Excel 整单导入失败，
    # 又与花旗 PDF(ingest 出 'withdrawal')口径分裂、被 _overview_totals 外部现金流聚合漏计。
    "withdraw": "withdrawal", "withdrawal": "withdrawal", "出金": "withdrawal", "转出": "withdrawal",
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
