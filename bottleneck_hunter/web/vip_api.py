"""VIP 私人财务顾问 API — 挂载于 /api/vip（见 docs/VIP_ADVISOR_TECH_SPEC.md §4/§5）。

M1 端点：上传月结单(PDF)→摄取+规范化+物化 / 列文档 / 生成报告 / 列报告。
全部经 require_vip 门禁 + _user_store 隔离；PII 只在后端处理，响应不含明文金额密文。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, Request
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse
from bottleneck_hunter.auth.dependencies import require_vip
from bottleneck_hunter.watchlist.budget import BudgetTracker
from bottleneck_hunter.watchlist.store import WatchlistStore

logger = logging.getLogger(__name__)

router = APIRouter(tags=["vip"])

_MAX_PDF_BYTES = 20 * 1024 * 1024   # 20MB 上限
_PDF_MAGIC = b"%PDF-"

_store: WatchlistStore | None = None


def set_store(store: WatchlistStore) -> None:
    global _store
    _store = store


def _wl(user: dict, market: str = "us_stock") -> WatchlistStore:
    if _store is None:
        raise HTTPException(status_code=500, detail="Store 未初始化")
    return _store.for_user(user["sub"]).for_market(market)


@router.post("/statements/upload")
async def upload_statement(file: UploadFile = File(...),
                           market: str = "us_stock",
                           broker: str = "citi",
                           account_ref: str = "",
                           pdf_password: str = "",
                           user: dict = Depends(require_vip)):
    """上传月结单 PDF → 摄取(加密入库) → parsed_ok 则规范化 + 物化到组合。

    返回 {doc_id, status, recon, n_positions, total_equity}。
    """
    raw = await file.read()
    if not raw or raw[:5] != _PDF_MAGIC:
        raise HTTPException(status_code=400, detail="仅支持 PDF 文件")
    if len(raw) > _MAX_PDF_BYTES:
        raise HTTPException(status_code=400, detail="文件超过 20MB 上限")

    from bottleneck_hunter.vip import ingest, portfolio
    from bottleneck_hunter.web.oplog import record_operation

    uid = user["sub"]
    # 摄取 + 加密入库（幂等去重）
    try:
        res = ingest.ingest_and_store(raw, file.filename or "statement.pdf",
                                      user_id=uid, market=market, broker=broker,
                                      pdf_password=pdf_password)
    except Exception as e:  # noqa: BLE001
        logger.exception("VIP 摄取失败")
        raise HTTPException(status_code=422, detail=f"月结单解析失败: {e}") from e

    record_operation(uid, "上传月结单", category="vip_financial",
                     detail=f"doc={res['doc_id'][:8]} status={res['status']}")

    out = {"doc_id": res["doc_id"], "status": res["status"],
           "recon": res.get("recon"), "duplicate": res.get("duplicate", False)}

    # parsed_ok 触发规范化 + 物化（M1：needs_review 不自动物化，待用户复核）
    if res["status"] == "parsed_ok" and not res.get("duplicate"):
        wl = _wl(user, market)
        auth_doc = _statement_from_doc(uid, res["doc_id"])
        if auth_doc:
            norm = portfolio.normalize_statement(wl, auth_doc, source_doc_id=res["doc_id"],
                                                 account_ref=account_ref)
            nav = None
            if getattr(auth_doc, "broker", "") == "nomura":
                nav = (getattr(auth_doc, "account_summary", {}) or {}).get("net_asset_value_usd")
            mat = portfolio.materialize_portfolio(wl, as_of_date=norm["as_of_date"],
                                                  account_ref=account_ref,
                                                  cash_total_usd=auth_doc.total_cash_usd,
                                                  account_total_usd=nav)
            out.update({"normalized": norm, "n_positions": mat["n_positions"],
                        "total_equity": mat["total_equity"],
                        "cash_balance": mat["cash_balance"]})
    return out


def _statement_from_doc(uid: str, doc_id: str):
    """从 auth.db 取回已加密的解析结果，重建 BrokerStatement（供规范化）。"""
    from bottleneck_hunter.auth.store import AuthStore
    from bottleneck_hunter.vip.ingest import BrokerStatement
    d = AuthStore().get_financial_doc(uid, doc_id, decrypt_parsed=True)
    if not d or not d.get("parsed_json"):
        return None
    try:
        return BrokerStatement.model_validate_json(d["parsed_json"])
    except Exception:  # noqa: BLE001
        return None


@router.get("/statements")
async def list_statements(market: str = "us_stock", user: dict = Depends(require_vip)):
    """列出该用户的月结单（元数据，无 PII 金额）。"""
    from bottleneck_hunter.auth.store import AuthStore
    return {"documents": AuthStore().list_financial_docs(user["sub"])}


@router.post("/derivatives/upload")
async def upload_derivative_file(file: UploadFile = File(...),
                                 market: str = "us_stock",
                                 broker: str = "nomura",
                                 pdf_password: str = "",
                                 user: dict = Depends(require_vip)):
    """上传日常衍生品/结构票据文件 → 分类 → 条款抽取 → 落 vip_derivative_terms。"""
    raw = await file.read()
    if not raw or raw[:5] != _PDF_MAGIC:
        raise HTTPException(status_code=400, detail="仅支持 PDF 文件")
    if len(raw) > _MAX_PDF_BYTES:
        raise HTTPException(status_code=400, detail="文件超过 20MB 上限")

    from hashlib import sha256
    from bottleneck_hunter.vip import derivatives as drv
    from bottleneck_hunter.web.oplog import record_operation
    uid = user["sub"]
    wl = _wl(user, market)
    kind = drv.classify_pdf(raw, pdf_password=pdf_password)
    if kind not in ("accumulator", "decumulator", "mli"):
        raise HTTPException(status_code=400, detail=f"该文件类型当前不建模：{kind}")
    try:
        if kind in ("accumulator", "decumulator"):
            term = drv.extract_accumulator_terms(raw, pdf_password=pdf_password)
        else:
            term = drv.extract_mli_terms(raw, pdf_password=pdf_password)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=f"条款抽取失败: {e}") from e
    did = drv.save_derivative_term(wl, term, source_file_name=file.filename or "term.pdf",
                                   source_file_hash=sha256(raw).hexdigest(), broker=broker)
    record_operation(uid, "上传衍生品文件", category="vip_financial",
                     detail=f"deriv={did[:8]} kind={kind} src={file.filename or ''}")
    return {"id": did, "kind": kind, "term": {"family": term.product_family, "underlying": term.underlying_symbol}}


@router.get("/derivatives")
async def list_derivatives(market: str = "us_stock", user: dict = Depends(require_vip)):
    from bottleneck_hunter.vip import derivatives as drv
    terms = drv.list_derivative_terms(_wl(user, market))
    return {"items": [{"product_family": t.product_family, "underlying_symbol": t.underlying_symbol,
                        "currency": t.currency, "source_file": t.source_file} for t in terms]}


@router.post("/reports/generate")
async def generate_report(market: str = "us_stock", period: str = "",
                          with_ai: bool = True, user: dict = Depends(require_vip)):
    """基于当前已物化组合生成持仓分析报告（with_ai=True 含顾问团队叙事）。"""
    from bottleneck_hunter.vip import portfolio
    from bottleneck_hunter.web.oplog import record_operation

    wl = _wl(user, market)
    acct = wl.get_sim_account()
    if not wl.get_sim_positions(acct["id"]):
        raise HTTPException(status_code=400, detail="尚无持仓，请先上传月结单")

    uid = user["sub"]
    from bottleneck_hunter.vip import derivatives as drv
    dterms = drv.list_derivative_terms(wl)
    if with_ai:
        out = await portfolio.generate_vip_report_ai(wl, period=period, user_id=uid, derivative_terms=dterms)
    else:
        out = portfolio.generate_vip_report(wl, period=period, derivative_terms=dterms)
    record_operation(uid, "生成投资分析报告", category="vip_financial",
                     detail=f"report={out['report_id'][:8]} period={period}")
    return {"report_id": out["report_id"], "report_md": out["report_md"],
            "unverified": out.get("unverified", [])}


@router.get("/reports")
async def list_reports(market: str = "us_stock", limit: int = 20,
                       user: dict = Depends(require_vip)):
    """列出该用户的报告（periodic/alert，不含 import_snapshot）。"""
    wl = _wl(user, market)
    conn = wl._connect()
    try:
        q, p = wl._filtered(
            "SELECT id, kind, period, created_at FROM vip_reports "
            "WHERE kind != 'import_snapshot' ORDER BY created_at DESC LIMIT ?", (limit,))
        rows = [dict(r) for r in conn.execute(q, p).fetchall()]
    finally:
        conn.close()
    return {"reports": rows}


@router.get("/reports/{report_id}")
async def get_report(report_id: str, market: str = "us_stock",
                     user: dict = Depends(require_vip)):
    wl = _wl(user, market)
    conn = wl._connect()
    try:
        q, p = wl._filtered("SELECT id, kind, period, report_md, created_at FROM vip_reports WHERE id = ?",
                            (report_id,))
        row = conn.execute(q, p).fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="报告不存在")
    return dict(row)


class ChatReq(BaseModel):
    session_id: str = ""
    question: str
    market: str = "us_stock"


@router.get("/chat/sessions")
async def list_chat_sessions(market: str = "us_stock", user: dict = Depends(require_vip)):
    from bottleneck_hunter.vip import chat
    return {"sessions": chat.list_chat_sessions(_wl(user, market))}


@router.get("/chat/sessions/{session_id}")
async def get_chat_messages(session_id: str, market: str = "us_stock", user: dict = Depends(require_vip)):
    from bottleneck_hunter.vip import chat
    return {"messages": chat.get_chat_messages(_wl(user, market), session_id)}


@router.post("/chat")
async def stream_chat(req: ChatReq, request: Request, user: dict = Depends(require_vip)):
    from bottleneck_hunter.vip import chat
    wl = _wl(user, req.market)
    budget = BudgetTracker(wl)

    async def event_generator():
        async for e in chat.stream_vip_chat(wl, user_id=user["sub"], question=req.question,
                                            session_id=req.session_id, budget=budget):
            if await request.is_disconnected():
                break
            yield e
    return EventSourceResponse(event_generator())
