"""VIP 私人财务顾问 API — 挂载于 /api/vip（见 docs/VIP_ADVISOR_TECH_SPEC.md §4/§5）。

M1 端点：上传月结单(PDF)→摄取+规范化+物化 / 列文档 / 生成报告 / 列报告。
全部经 require_vip 门禁 + _user_store 隔离；PII 只在后端处理，响应不含明文金额密文。
"""
from __future__ import annotations

import logging
from collections import Counter

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
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

# VIP 锁屏：解锁态按用户 sub 存进程内存，服务重启即重新上锁（隐私友好，无需持久化）。
# ponytail: 单进程内存集合；serve 为单 uvicorn 进程足够。若将来多 worker 部署需改共享存储（redis/db）。
_unlocked_subs: set[str] = set()


def set_store(store: WatchlistStore) -> None:
    global _store
    _store = store


def require_vip_unlocked(user: dict = Depends(require_vip)) -> dict:
    """在 require_vip 之上再要求「已解锁」：财务 PII 只做前端锁屏会被 F12 直接调 API 绕过，
    故所有 VIP 数据路由都过这道后端门禁；未解锁返回 423，前端据此渲染「开发中」锁屏。"""
    if user["sub"] not in _unlocked_subs:
        raise HTTPException(status_code=423, detail="VIP 已锁定，请先在管理员菜单输入登录密码解锁")
    return user


class _VipUnlockReq(BaseModel):
    password: str


@router.get("/lock-status")
async def vip_lock_status(user: dict = Depends(require_vip)):
    """当前用户 VIP 是否已解锁（不需已解锁即可查，供前端决定显示锁屏还是内容）。"""
    return {"unlocked": user["sub"] in _unlocked_subs}


@router.post("/unlock")
async def vip_unlock(body: _VipUnlockReq, user: dict = Depends(require_vip)):
    """重新输入本人登录密码解锁 VIP。校验通过则本会话（进程存活期）解锁。"""
    from bottleneck_hunter.auth.store import AuthStore
    store = AuthStore()
    u = store.get_user_by_id(user["sub"])
    if not u or not store.verify_password(u, body.password):
        raise HTTPException(status_code=401, detail="密码错误")
    _unlocked_subs.add(user["sub"])
    return {"unlocked": True}


@router.post("/lock")
async def vip_lock(user: dict = Depends(require_vip)):
    """主动重新上锁（离开前隐藏内容）。"""
    _unlocked_subs.discard(user["sub"])
    return {"unlocked": False}


def _wl(user: dict, market: str = "us_stock") -> WatchlistStore:
    if _store is None:
        raise HTTPException(status_code=500, detail="Store 未初始化")
    return _store.for_user(user["sub"]).for_market(market)


def _resolve_ref(wl: WatchlistStore, account_ref: str) -> str:
    """把空 account_ref 解析为具体子账户(单账户自动/多/无账户报 400)。
    VIP 端点绝不放空 ref 下沉——空 ref 会经 get_sim_account("") 懒建决策中心自有模拟盘(预置本金)
    并把幻影组合当真实持仓喂给档案/总览/LLM。见 memory dc_sim_account_decoupled。"""
    try:
        return wl.resolve_vip_account_ref(account_ref)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/statements/upload")
async def upload_statement(file: UploadFile = File(...),
                           market: str = "us_stock",
                           broker: str = "citi",
                           account_ref: str = "",
                           pdf_password: str = "",
                           user: dict = Depends(require_vip_unlocked)):
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
        try:
            target_ref = wl.resolve_vip_account_ref(account_ref)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        auth_doc = _statement_from_doc(uid, res["doc_id"])
        if auth_doc:
            norm = portfolio.normalize_statement(wl, auth_doc, source_doc_id=res["doc_id"],
                                                 account_ref=target_ref)
            nav = None
            if getattr(auth_doc, "broker", "") == "nomura":
                nav = (getattr(auth_doc, "account_summary", {}) or {}).get("net_asset_value_usd")
            mat = portfolio.materialize_portfolio(wl, as_of_date=norm["as_of_date"],
                                                  account_ref=target_ref,
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


def _normalize_trade_export(user: dict, market: str, doc_id: str, account_ref: str = "") -> tuple[dict, list[dict]]:
    from bottleneck_hunter.vip import portfolio

    wl = _wl(user, market)
    try:
        target_ref = wl.resolve_vip_account_ref(account_ref)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    auth_doc = _statement_from_doc(user["sub"], doc_id)
    if not auth_doc:
        raise HTTPException(status_code=422, detail="导出解析结果无效")
    norm = portfolio.normalize_statement(wl, auth_doc, source_doc_id=doc_id, account_ref=target_ref)
    txns = portfolio.list_transactions(wl, account_ref=target_ref, limit=max(norm.get("n_transactions", 0), 1))
    return norm, txns


@router.post("/exports/upload")
async def upload_trade_export(file: UploadFile = File(...),
                              market: str = "us_stock",
                              broker: str = "citi",
                              account_ref: str = "",
                              pdf_password: str = "",
                              user: dict = Depends(require_vip_unlocked)):
    """上传花旗导出 PDF → 摄取(加密入库) → trade_confirm 则规范化到 transactions。"""
    raw = await file.read()
    if not raw or raw[:5] != _PDF_MAGIC:
        raise HTTPException(status_code=400, detail="仅支持 PDF 文件")
    if len(raw) > _MAX_PDF_BYTES:
        raise HTTPException(status_code=400, detail="文件超过 20MB 上限")

    from bottleneck_hunter.vip import ingest
    from bottleneck_hunter.web.oplog import record_operation

    uid = user["sub"]
    try:
        res = ingest.ingest_and_store(raw, file.filename or "export.pdf",
                                      user_id=uid, market=market, broker=broker,
                                      pdf_password=pdf_password)
    except Exception as e:  # noqa: BLE001
        logger.exception("VIP 导出摄取失败")
        raise HTTPException(status_code=422, detail=f"导出文件解析失败: {e}") from e

    if res.get("doc_type") != "trade_confirm":
        raise HTTPException(status_code=400, detail="当前仅支持交易导出文件")

    record_operation(uid, "上传交易导出", category="vip_financial",
                     detail=f"doc={res['doc_id'][:8]} status={res['status']}")

    out = {"doc_id": res["doc_id"], "status": res["status"],
           "duplicate": res.get("duplicate", False), "doc_type": res.get("doc_type", "")}
    auth_doc = _statement_from_doc(uid, res["doc_id"])
    if not auth_doc:
        raise HTTPException(status_code=422, detail="导出解析结果无效")

    txns = list(getattr(auth_doc, "transactions", []) or [])
    out.update({
        "date_range": {
            "start": min((t.trade_date for t in txns if t.trade_date), default=""),
            "end": max((t.trade_date for t in txns if t.trade_date), default=""),
        },
        "txn_type_counts": dict(Counter(t.txn_type for t in txns if t.txn_type)),
    })

    if res["status"] == "parsed_ok" and not res.get("duplicate"):
        norm, imported = _normalize_trade_export(user, market, res["doc_id"], account_ref=account_ref)
        out.update({
            "normalized": norm,
            "imported_count": norm.get("n_transactions", 0),
            "skipped_count": max(len(txns) - norm.get("n_transactions", 0), 0),
            "transactions": imported,
        })
    else:
        out.update({
            "imported_count": 0,
            "skipped_count": len(txns),
            "transactions": [],
        })
    return out


@router.get("/accounts")
async def list_accounts(market: str = "us_stock", user: dict = Depends(require_vip_unlocked)):
    wl = _wl(user, market)
    accounts = wl.list_vip_accounts(include_hidden_default=False)
    import_counts = wl.count_vip_imports_by_account()
    for a in accounts:
        c = import_counts.get(a.get("account_ref") or "", {"total": 0, "by_type": {}})
        a["import_count"] = c["total"]
        a["import_by_type"] = c["by_type"]
    default_account = next((a for a in accounts if a.get("is_default")), None)
    preferred_account = default_account or (accounts[0] if accounts else None)
    return {
        "accounts": accounts,
        "default_account": preferred_account,
    }


class CreateAccountReq(BaseModel):
    account_ref: str
    display_name: str = ""
    institution_name: str = ""
    account_kind: str = "broker"
    is_default: bool = False


@router.post("/accounts")
async def create_account(req: CreateAccountReq, market: str = "us_stock", user: dict = Depends(require_vip_unlocked)):
    wl = _wl(user, market)
    try:
        account = wl.create_vip_account(
            account_ref=req.account_ref,
            display_name=req.display_name,
            institution_name=req.institution_name,
            account_kind=req.account_kind,
            is_default=req.is_default,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if req.is_default:
        account = wl.set_default_vip_account(req.account_ref)
    return {"account": account}


class ReorderAccountsReq(BaseModel):
    account_refs: list[str]


@router.patch("/accounts/order")
async def reorder_accounts(req: ReorderAccountsReq, market: str = "us_stock", user: dict = Depends(require_vip_unlocked)):
    wl = _wl(user, market)
    try:
        accounts = wl.set_vip_account_order(req.account_refs)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"accounts": accounts}


class UpdateAccountReq(BaseModel):
    display_name: str | None = None
    institution_name: str | None = None
    account_kind: str | None = None
    is_default: bool = False


def _update_account_payload(wl, account_ref: str, req: UpdateAccountReq) -> dict:
    try:
        account = wl.update_vip_account(
            account_ref=account_ref,
            display_name=req.display_name,
            institution_name=req.institution_name,
            account_kind=req.account_kind,
            is_default=req.is_default,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"account": account}


@router.patch("/accounts/{account_ref}")
async def update_account(account_ref: str, req: UpdateAccountReq,
                         market: str = "us_stock", user: dict = Depends(require_vip_unlocked)):
    return _update_account_payload(_wl(user, market), account_ref, req)


class ClearAccountDataReq(BaseModel):
    account_ref: str


@router.post("/accounts/clear-data")
async def clear_account_data(req: ClearAccountDataReq,
                             market: str = "us_stock",
                             user: dict = Depends(require_vip_unlocked)):
    wl = _wl(user, market)
    try:
        result = wl.delete_vip_account_data(account_ref=req.account_ref)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return result


@router.delete("/accounts/{account_ref}")
async def delete_account(account_ref: str, market: str = "us_stock", user: dict = Depends(require_vip_unlocked)):
    wl = _wl(user, market)
    try:
        result = wl.delete_vip_account(account_ref=account_ref)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return result


@router.get("/account/overview")
async def get_account_overview(market: str = "us_stock",
                               account_ref: str = "",
                               scope: str = "account",
                               user: dict = Depends(require_vip_unlocked)):
    from bottleneck_hunter.vip import portfolio

    wl = _wl(user, market)
    if scope == "all":
        overview = portfolio.build_total_overview(wl)
    else:
        overview = portfolio.build_account_overview(wl, account_ref=_resolve_ref(wl, account_ref))
    return {"overview": overview}


@router.get("/account/transactions")
async def get_account_transactions(market: str = "us_stock",
                                   account_ref: str = "",
                                   ticker: str = "",
                                   txn_type: str = "",
                                   start_date: str = "",
                                   end_date: str = "",
                                   limit: int = 50,
                                   offset: int = 0,
                                   user: dict = Depends(require_vip_unlocked)):
    from bottleneck_hunter.vip import portfolio

    wl = _wl(user, market)
    rows = portfolio.list_transactions(wl, account_ref=account_ref, ticker=ticker,
                                       txn_type=txn_type, start_date=start_date,
                                       end_date=end_date, limit=limit, offset=offset)
    return {"transactions": rows, "limit": limit, "offset": offset}


@router.get("/account/positions")
async def get_account_positions(market: str = "us_stock",
                                account_ref: str = "",
                                user: dict = Depends(require_vip_unlocked)):
    wl = _wl(user, market)
    acct = wl.get_sim_account(account_ref=_resolve_ref(wl, account_ref))
    positions = sorted(wl.get_sim_positions(acct["id"]),
                       key=lambda p: p.get("market_value", 0), reverse=True)
    return {"positions": positions}


@router.post("/import")
async def import_file(file: UploadFile = File(...),
                     market: str = "us_stock",
                     account_ref: str = "",
                     pdf_password: str = "",
                     user: dict = Depends(require_vip_unlocked)):
    """通用导入入口：任意文件 → 自动判类型/内容 → 路由入库 → 统一 ImportResult。

    替代 /statements/upload、/exports/upload、/derivatives/upload（旧路由保留兼容）。
    """
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="空文件")
    if len(raw) > _MAX_PDF_BYTES:
        raise HTTPException(status_code=400, detail="文件超过 20MB 上限")

    from bottleneck_hunter.vip import importer
    from bottleneck_hunter.web.oplog import record_operation

    uid = user["sub"]
    wl = _wl(user, market)
    try:
        result = importer.dispatch_import(raw, file.filename or "upload",
                                          user_id=uid, wl_store=wl, market=market,
                                          account_ref=account_ref, password=pdf_password)
    except Exception as e:  # noqa: BLE001
        logger.exception("VIP 通用导入失败")
        raise HTTPException(status_code=422, detail=f"导入解析失败: {e}") from e
    record_operation(uid, "通用导入", category="vip_financial",
                     detail=f"{result.status}/{result.detected_kind} src={file.filename or ''}")
    return result.as_dict()


@router.get("/imports")
async def list_imports(market: str = "us_stock", limit: int = 100,
                       account_ref: str = "",
                       scope: str = "account",
                       user: dict = Depends(require_vip_unlocked)):
    """导入历史：文件名/时间/类型/状态/摘要/主要数据（金额已脱敏）。"""
    return {"imports": _wl(user, market).list_vip_imports(limit=limit, account_ref=account_ref, scope=scope)}


@router.get("/account/value-series")
async def get_value_series(market: str = "us_stock", account_ref: str = "", scope: str = "account",
                           user: dict = Depends(require_vip_unlocked)):
    """价值变化曲线 + 逐期收益率（按 positions.as_of_date 聚合派生）。"""
    from bottleneck_hunter.vip import portfolio
    if scope == "all":
        return portfolio.value_series(_wl(user, market))
    wl = _wl(user, market)
    return portfolio.value_series(wl, account_ref=_resolve_ref(wl, account_ref))


@router.get("/account/missing")
async def get_missing(market: str = "us_stock", account_ref: str = "", user: dict = Depends(require_vip_unlocked)):
    """数据体检：还缺哪些数据、如何补充。"""
    from bottleneck_hunter.vip import portfolio
    wl = _wl(user, market)
    return {"missing": portfolio.missing_data_report(wl, account_ref=_resolve_ref(wl, account_ref))}


@router.get("/account/dossier")
async def get_account_dossier(market: str = "us_stock", account_ref: str = "",
                              user: dict = Depends(require_vip_unlocked)):
    """Phase A · 账户完整档案：LLM 单一事实源。头条真实价值(结算单口径,不含衍生品估值)+
    逐仓成本/未实现盈亏 + 流水聚合 + 衍生品敞口(单列) + 价值曲线 + 数据新鲜度。"""
    from bottleneck_hunter.vip import portfolio
    wl = _wl(user, market)
    return portfolio.build_account_dossier(wl, account_ref=_resolve_ref(wl, account_ref))


@router.get("/account/mandate")
async def get_account_mandate(market: str = "us_stock", account_ref: str = "",
                             user: dict = Depends(require_vip_unlocked)):
    """读取本账户投资纲领（用户设定的投资设想与目标，供 LLM 决策依据）。未设定返回默认档。"""
    from bottleneck_hunter.vip import mandate
    return {"mandate": mandate.load_mandate(_wl(user, market), account_ref=account_ref)}


@router.put("/account/mandate")
async def put_account_mandate(payload: dict, market: str = "us_stock", account_ref: str = "",
                             user: dict = Depends(require_vip_unlocked)):
    """保存本账户投资纲领（做范围 clamp + 枚举校验），返回规范化后的 dict。"""
    from bottleneck_hunter.vip import mandate
    saved = mandate.save_mandate(_wl(user, market), payload or {}, account_ref=account_ref)
    return {"mandate": saved}


@router.get("/account/log")
async def get_account_log(market: str = "us_stock", account_ref: str = "",
                          event_type: str = "", limit: int = 200,
                          user: dict = Depends(require_vip_unlocked)):
    """账户日志：逐条自动推算 / 校准 / 异常 / 结算记录（供账户日志窗口渲染）。"""
    wl = _wl(user, market)
    return {"log": wl.list_account_log(account_ref=account_ref, event_type=event_type, limit=limit)}


@router.get("/account/staleness")
async def get_account_staleness(market: str = "us_stock", account_ref: str = "",
                                user: dict = Depends(require_vip_unlocked)):
    """校准新鲜度：距上一份结算单校准已过多少天、最近一次推算日、待校准推算条数。"""
    from datetime import datetime, timezone
    wl = _wl(user, market)
    ref = (account_ref or "").strip()

    # 最近一次真值校准日 = 该账户 positions 的最新 as_of_date
    conn = wl._connect()
    try:
        if ref:
            q, p = wl._filtered("SELECT MAX(as_of_date) AS d FROM positions WHERE account_ref=?",
                                (ref,), table="positions")
        else:
            q, p = wl._filtered("SELECT MAX(as_of_date) AS d FROM positions", table="positions")
        row = conn.execute(q, p).fetchone()
        last_calib = (row["d"] if row and row["d"] else "") or ""
    finally:
        conn.close()

    days_since = None
    if last_calib:
        try:
            d0 = datetime.strptime(last_calib[:10], "%Y-%m-%d").date()
            days_since = (datetime.now(timezone.utc).date() - d0).days
        except ValueError:
            days_since = None

    pending = wl.list_projections(account_ref=ref, status="pending")
    return {
        "last_calibrated_date": last_calib,
        "days_since_calibration": days_since,
        "latest_projection_date": wl.latest_projection_date(ref),
        "pending_projection_count": len(pending),
    }


@router.post("/account/project-now")
async def project_now(market: str = "us_stock", account_ref: str = "",
                      user: dict = Depends(require_vip_unlocked)):
    """手动触发一次每日股票重估推算（与定时任务同一逻辑，便于即时刷新/排障）。"""
    from bottleneck_hunter.vip import projection
    wl = _wl(user, market)
    ref = (account_ref or "").strip()
    if not ref:
        # 单账户可自动解析；多账户要求显式指定
        try:
            ref = wl.resolve_vip_account_ref("")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
    res = projection.project_stock_mtm(wl, ref)
    return {"result": res}


@router.get("/statements")
async def list_statements(market: str = "us_stock", user: dict = Depends(require_vip_unlocked)):
    """列出该用户的月结单（元数据，无 PII 金额）。"""
    from bottleneck_hunter.auth.store import AuthStore
    return {"documents": AuthStore().list_financial_docs(user["sub"], market=market)}


@router.post("/derivatives/upload")
async def upload_derivative_file(file: UploadFile = File(...),
                                 market: str = "us_stock",
                                 broker: str = "nomura",
                                 account_ref: str = "",
                                 pdf_password: str = "",
                                 user: dict = Depends(require_vip_unlocked)):
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
                                   source_file_hash=sha256(raw).hexdigest(), broker=broker,
                                   account_ref=account_ref)
    record_operation(uid, "上传衍生品文件", category="vip_financial",
                     detail=f"deriv={did[:8]} kind={kind} src={file.filename or ''}")
    return {"id": did, "kind": kind, "term": {"family": term.product_family, "underlying": term.underlying_symbol}}


@router.get("/derivatives")
async def list_derivatives(market: str = "us_stock", account_ref: str = "", scope: str = "account",
                           user: dict = Depends(require_vip_unlocked)):
    from bottleneck_hunter.vip import derivatives as drv

    wl = _wl(user, market)
    if scope == "all":
        items = drv.list_derivative_terms_all_accounts(wl)
        return {"items": [{
            "product_family": t["product_family"],
            "underlying_symbol": t["underlying_symbol"],
            "currency": t["currency"],
            "source_file": t["source_file"],
            "account_ref": t["account_ref"],
        } for t in items]}
    terms = drv.list_derivative_terms(wl, account_ref=account_ref)
    # 结构性产品分栏需展示当期 MTM/名义/到期 → 暴露 terms 里这几项（结单薄记录权威字段）
    # 双击展开需完整 terms（主要指标 + 合约说明），一并透出 terms 原始 dict。
    return {"items": [{"id": t.id, "product_family": t.product_family, "underlying_symbol": t.underlying_symbol,
                        "currency": t.currency, "tenor_days": t.tenor_days, "source_file": t.source_file,
                        "market_value_usd": (t.terms or {}).get("market_value_usd"),
                        "notional": (t.terms or {}).get("notional"),
                        "maturity": (t.terms or {}).get("maturity") or (t.terms or {}).get("expiry_date"),
                        "terms": t.terms or {}}
                       for t in terms]}


@router.post("/derivatives/{did}/reextract")
async def reextract_derivative(did: str, file: UploadFile = File(...),
                               market: str = "us_stock",
                               pdf_password: str = "",
                               user: dict = Depends(require_vip_unlocked)):
    """重传原始结算单 → 重新抽取条款 → 按 id 覆盖 terms（回填 trade_date 等旧数据缺失字段）。

    仅更新条款内容，不改归属账户；id 命中用户/市场隔离由 update_derivative_term 保证。
    """
    raw = await file.read()
    if not raw or raw[:5] != _PDF_MAGIC:
        raise HTTPException(status_code=400, detail="仅支持 PDF 文件")
    if len(raw) > _MAX_PDF_BYTES:
        raise HTTPException(status_code=400, detail="文件超过 20MB 上限")

    from bottleneck_hunter.vip import derivatives as drv
    from bottleneck_hunter.web.oplog import record_operation
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
    if not drv.update_derivative_term(wl, did, term):
        raise HTTPException(status_code=404, detail="未找到该条款记录（或不属当前用户）")
    record_operation(user["sub"], "重抽衍生品条款", category="vip_financial",
                     detail=f"deriv={did[:8]} kind={kind} src={file.filename or ''}")
    trade_date = term.terms.get("trade_date", "")
    return {"id": did, "kind": kind, "trade_date": trade_date,
            "term": {"family": term.product_family, "underlying": term.underlying_symbol}}


@router.post("/reports/generate")
async def generate_report(market: str = "us_stock", period: str = "",
                          account_ref: str = "",
                          with_ai: bool = True, user: dict = Depends(require_vip_unlocked)):
    """基于当前已物化组合生成持仓分析报告（with_ai=True 含顾问团队叙事）。"""
    from bottleneck_hunter.vip import portfolio
    from bottleneck_hunter.web.oplog import record_operation

    wl = _wl(user, market)
    account_ref = _resolve_ref(wl, account_ref)
    acct = wl.get_sim_account(account_ref=account_ref)
    if not wl.get_sim_positions(acct["id"]):
        raise HTTPException(status_code=400, detail="尚无持仓，请先上传月结单")

    uid = user["sub"]
    from bottleneck_hunter.vip import derivatives as drv
    dterms = drv.list_derivative_terms(wl, account_ref=account_ref)
    if with_ai:
        out = await portfolio.generate_vip_report_ai(wl, period=period, user_id=uid, derivative_terms=dterms, account_ref=account_ref)
    else:
        out = portfolio.generate_vip_report(wl, period=period, derivative_terms=dterms, account_ref=account_ref)
    record_operation(uid, "生成投资分析报告", category="vip_financial",
                     detail=f"report={out['report_id'][:8]} period={period}")
    return {"report_id": out["report_id"], "report_md": out["report_md"],
            "unverified": out.get("unverified", [])}


@router.get("/reports")
async def list_reports(market: str = "us_stock", limit: int = 20,
                       account_ref: str = "",
                       user: dict = Depends(require_vip_unlocked)):
    """列出该用户的报告（periodic/alert，不含 import_snapshot）。"""
    wl = _wl(user, market)
    conn = wl._connect()
    try:
        q, p = wl._filtered(
            "SELECT id, kind, period, created_at FROM vip_reports "
            "WHERE kind != 'import_snapshot' AND account_ref = ? ORDER BY created_at DESC LIMIT ?", (account_ref, limit))
        rows = [dict(r) for r in conn.execute(q, p).fetchall()]
    finally:
        conn.close()
    return {"reports": rows}


@router.get("/reports/{report_id}")
async def get_report(report_id: str, market: str = "us_stock",
                     account_ref: str = "",
                     user: dict = Depends(require_vip_unlocked)):
    wl = _wl(user, market)
    conn = wl._connect()
    try:
        q, p = wl._filtered("SELECT id, kind, period, report_md, created_at FROM vip_reports WHERE id = ? AND account_ref = ?",
                            (report_id, account_ref))
        row = conn.execute(q, p).fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="报告不存在")
    return dict(row)


@router.get("/account/advisory")
async def get_account_advisory(market: str = "us_stock", account_ref: str = "",
                               user: dict = Depends(require_vip_unlocked)):
    """读该账户最近一份顾问建议（进标签页回显）；无则返回 null。"""
    from bottleneck_hunter.vip import advisory
    return {"advisory": advisory.get_latest_advisory(_wl(user, market), account_ref=account_ref)}


@router.get("/account/advisory/history")
async def get_account_advisory_history(market: str = "us_stock", account_ref: str = "", limit: int = 20,
                                       user: dict = Depends(require_vip_unlocked)):
    """该账户历史顾问建议列表（新→旧，每条含完整 result 供点选回看）。"""
    from bottleneck_hunter.vip import advisory
    return {"history": advisory.list_advisory(_wl(user, market), account_ref=account_ref, limit=limit)}


@router.post("/account/advisory")
async def post_account_advisory(market: str = "us_stock", account_ref: str = "",
                                user: dict = Depends(require_vip_unlocked)):
    """生成账户顾问建议（吃 dossier+纲领+L1宏观+衍生品敞口 → 投委会评审 → 每仓 减/持/加）。只出建议不下单。"""
    from bottleneck_hunter.vip import advisory
    from bottleneck_hunter.web.oplog import record_operation
    uid = user["sub"]
    out = await advisory.generate_account_advisory(_wl(user, market), account_ref=account_ref, user_id=uid)
    if out.get("error"):
        raise HTTPException(status_code=400, detail=out["error"])
    record_operation(uid, "生成顾问决策建议", category="vip_financial",
                     detail=f"advisory={out['advisory_id'][:8]} account={account_ref}")
    return out


@router.get("/account/recommend")
async def get_account_recommend(market: str = "us_stock", account_ref: str = "",
                                user: dict = Depends(require_vip_unlocked)):
    """读该账户最近一份荐新建议（进标签页回显）；无则返回 null。"""
    from bottleneck_hunter.vip import recommend
    return {"recommendation": recommend.get_latest_recommendations(_wl(user, market), account_ref=account_ref)}


@router.get("/account/recommend/history")
async def get_account_recommend_history(market: str = "us_stock", account_ref: str = "", limit: int = 20,
                                        user: dict = Depends(require_vip_unlocked)):
    """该账户历史荐新建议列表（新→旧，每条含完整 result 供点选回看）。"""
    from bottleneck_hunter.vip import recommend
    return {"history": recommend.list_recommendations(_wl(user, market), account_ref=account_ref, limit=limit)}


@router.post("/account/recommend")
async def post_account_recommend(market: str = "us_stock", account_ref: str = "",
                                 user: dict = Depends(require_vip_unlocked)):
    """生成账户荐新建议（吃 dossier+纲领+L1宏观 + 观察池候选 → 投委会评审 → 建仓/关注/规避）。只出建议不下单。"""
    from bottleneck_hunter.vip import recommend
    from bottleneck_hunter.web.oplog import record_operation
    uid = user["sub"]
    out = await recommend.generate_account_recommendations(_wl(user, market), account_ref=account_ref, user_id=uid)
    if out.get("error"):
        raise HTTPException(status_code=400, detail=out["error"])
    record_operation(uid, "生成荐新建议", category="vip_financial",
                     detail=f"recommend={out['recommendation_id'][:8]} account={account_ref}")
    return out


@router.get("/account/budget-reconciliation")
async def get_budget_reconciliation(market: str = "us_stock", account_ref: str = "",
                                    user: dict = Depends(require_vip_unlocked)):
    """B · 现金/仓位预算对照（只读、指示性）：advisory 加仓 + recommend 建仓的量化仓位加总，对照可投资现金给容量 sanity。
    任一 pass 尚未生成则 partial=True（缺的 pass 不计入需求、判断偏乐观）。只提示、不约束生成、不下单。"""
    from bottleneck_hunter.vip import advisory, portfolio, recommend
    ref = (account_ref or "").strip()
    if not ref:  # 空 ref 会经 build_account_dossier→get_sim_account('') 越界读决策中心模拟盘（见 memory:dc_sim_account_decoupled）
        raise HTTPException(status_code=400, detail="请先选择具体子账户")
    wl = _wl(user, market)
    dossier = portfolio.build_account_dossier(wl, account_ref=ref)
    adv = advisory.get_latest_advisory(wl, account_ref=ref)
    rec = recommend.get_latest_recommendations(wl, account_ref=ref)
    result = advisory.summarize_cash_budget(dossier, adv, rec, wl_store=wl)
    result.update({"partial": (adv is None) or (rec is None),
                   "has_advisory": adv is not None, "has_recommend": rec is not None})
    return {"budget": result}


class ChatReq(BaseModel):
    session_id: str = ""
    question: str
    market: str = "us_stock"
    account_ref: str = ""


@router.get("/chat/sessions")
async def list_chat_sessions(market: str = "us_stock", account_ref: str = "", user: dict = Depends(require_vip_unlocked)):
    from bottleneck_hunter.vip import chat
    return {"sessions": chat.list_chat_sessions(_wl(user, market), account_ref=account_ref)}


@router.get("/chat/sessions/{session_id}")
async def get_chat_messages(session_id: str, market: str = "us_stock", account_ref: str = "", user: dict = Depends(require_vip_unlocked)):
    from bottleneck_hunter.vip import chat
    return {"messages": chat.get_chat_messages(_wl(user, market), session_id, account_ref=account_ref)}


@router.post("/chat")
async def stream_chat(req: ChatReq, request: Request, user: dict = Depends(require_vip_unlocked)):
    from bottleneck_hunter.vip import chat
    wl = _wl(user, req.market)
    budget = BudgetTracker(wl)

    async def event_generator():
        async for e in chat.stream_vip_chat(wl, user_id=user["sub"], question=req.question,
                                            session_id=req.session_id, budget=budget,
                                            account_ref=req.account_ref):
            if await request.is_disconnected():
                break
            yield e
    return EventSourceResponse(event_generator())
