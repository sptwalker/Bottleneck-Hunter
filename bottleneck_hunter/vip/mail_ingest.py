"""转发银行邮件自动解读入库（管理员专用薄适配层）。

需求：VIP 用户把银行交易/结单邮件转发到系统收件箱 → 系统定期拉取 → 正文 LLM 解读 + 附件结构化解析 → 入库。

**本模块不碰解析**，只做"把邮件喂进既有管线"：
- 附件（PDF/docx/xlsx/csv）→ dispatch_import()（已实现的月结单/条款单/交易单解析，含哈希幂等、自动归户、加密落库）。
- 正文 → interpret_body() 用 LLM 抽明确写出的交易 → 落**待确认队列**（vip_mail_confirm_pending），管理员确认后才写账户。

## 归属分离（核心正确性约束）
同一封邮件处理内有两种用户归属，切勿混淆：
- **调 LLM**（正文解读）→ set_current_user(admin_sub)，用管理员 key。
- **落库**（附件入库 / 待确认队列）→ wl_store.for_user(vip_sub)，归收件 VIP 用户。

## 幂等三把锁
- 邮件级：Message-Id 在 mail_ingest_log UNIQUE（重复投递跳过）。
- 附件级：dispatch_import 已按文件哈希幂等。
- 正文交易：external_id = msgid + 序号（确认时合成 stmt 走 normalize_statement 幂等 upsert）。

## 安全闸
发件人邮箱匹配已注册 VIP 邮箱才认回用户；认不出一律拦截不入库（防伪造污染他人账户）。
# ponytail: 邮箱可伪造，起步依赖邮箱服务商收信侧 SPF/DKIM；后续要更严可加显式 DKIM 校验。

自检：python -m bottleneck_hunter.vip.mail_ingest 跑 demo()。
"""
from __future__ import annotations

import logging
from email.header import decode_header, make_header
from email.utils import parseaddr
from pathlib import Path

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).resolve().parents[1] / "chain" / "prompts"

_ATTACH_EXTS = (".pdf", ".docx", ".xls", ".xlsx", ".csv")
_TXN_TYPES = {"buy", "sell", "dividend", "interest", "fee", "tax",
              "deposit", "withdrawal", "transfer_in", "transfer_out"}


def _decode(s: str) -> str:
    """解码可能被 RFC2047 编码的邮件头（主题/文件名）。"""
    if not s:
        return ""
    try:
        return str(make_header(decode_header(s)))
    except Exception:  # noqa: BLE001
        return s


def _body_text(msg) -> str:
    """抽取邮件正文纯文本（优先 text/plain；无则从 text/html 粗剥标签）。"""
    parts_text = []
    parts_html = []
    for part in msg.walk() if msg.is_multipart() else [msg]:
        ctype = part.get_content_type()
        disp = str(part.get("Content-Disposition") or "")
        if "attachment" in disp.lower():
            continue
        if ctype == "text/plain":
            parts_text.append(_part_payload(part))
        elif ctype == "text/html":
            parts_html.append(_part_payload(part))
    if parts_text:
        return "\n".join(t for t in parts_text if t).strip()
    # 兜底：html 去标签
    import re
    html = "\n".join(parts_html)
    return re.sub(r"<[^>]+>", " ", html).strip()


def _part_payload(part) -> str:
    try:
        raw = part.get_payload(decode=True)
        if raw is None:
            return ""
        charset = part.get_content_charset() or "utf-8"
        return raw.decode(charset, errors="replace")
    except Exception:  # noqa: BLE001
        return ""


def _attachments(msg) -> list[tuple[str, bytes]]:
    """抽出附件 (filename, bytes)，仅保留可解析扩展名。"""
    out = []
    for part in msg.walk() if msg.is_multipart() else [msg]:
        disp = str(part.get("Content-Disposition") or "")
        fname = _decode(part.get_filename() or "")
        if not fname:
            continue
        if "attachment" not in disp.lower() and not fname.lower().endswith(_ATTACH_EXTS):
            continue
        if not fname.lower().endswith(_ATTACH_EXTS):
            continue
        try:
            data = part.get_payload(decode=True)
        except Exception:  # noqa: BLE001
            data = None
        if data:
            out.append((fname, data))
    return out


def interpret_body(subject: str, body: str, user_id: str) -> dict:
    """用管理员主模型抽取正文里明确写出的交易。返回 {summary, transactions[], events[]}。

    调用方须已 set_current_user(admin_sub)，使 LLM 层解析到管理员 key。
    LLM 不可用/解析失败 → 返回空结构（正文抽取为可选增强，绝不阻断附件入库主链路）。
    """
    from bottleneck_hunter.chain.json_utils import extract_json_object
    from bottleneck_hunter.llm_clients.factory import get_models_for_role

    text = (body or "").strip()
    if not text and not (subject or "").strip():
        return {"summary": "", "transactions": [], "events": []}
    # 复用 vip_statement_extract 角色（已在角色注册表/容量门内）；prefer_primary 让管理员主模型直用。
    models = get_models_for_role("vip_statement_extract", user_id=user_id,
                                 with_fallback=True, prefer_primary=True)
    if not models:
        logger.warning("正文解读无可用 LLM（管理员未配主模型？），跳过正文抽取")
        return {"summary": "", "transactions": [], "events": []}
    llm = models[0][0]
    tmpl = (PROMPTS_DIR / "mail_body_extract.md").read_text(encoding="utf-8")
    prompt = f"{tmpl}\n\n## 邮件主题\n{subject or '(无主题)'}\n\n## 邮件正文\n{text[:6000]}"
    try:
        resp = llm.invoke(prompt)
        data = extract_json_object(getattr(resp, "content", "") or "")
    except Exception as e:  # noqa: BLE001
        logger.warning("正文 LLM 解读失败: %s", e)
        return {"summary": "", "transactions": [], "events": []}
    txns = [t for t in (data.get("transactions") or [])
            if isinstance(t, dict) and (t.get("txn_type") or "").strip().lower() in _TXN_TYPES]
    return {
        "summary": (data.get("summary") or "").strip(),
        "transactions": txns,
        "events": [str(e) for e in (data.get("events") or [])],
    }


def _pending_txn_record(txn: dict, msgid: str, seq: int) -> dict:
    """把正文 LLM 抽的一笔交易规整成 StatementTransaction 形状（确认时直接喂 normalize_statement）。

    external_id = msgid#seq 保证跨轮幂等；trade_date 缺失留空由确认阶段人工补。
    """
    return {
        "ticker": (txn.get("ticker") or "").strip().upper(),
        "txn_type": (txn.get("txn_type") or "").strip().lower(),
        "quantity": float(txn.get("quantity") or 0),
        "price": float(txn.get("price") or 0),
        "trade_date": (txn.get("trade_date") or "").strip(),
        "currency": (txn.get("currency") or "USD").strip().upper(),
        "description": (txn.get("description") or "").strip(),
        "external_id": f"{msgid}#{seq}",
    }


def poll_inbox(wl_store, auth_store) -> dict:
    """拉取收件箱未读邮件，逐封解读入库。返回 {processed, rejected, errors} 计数。

    imaplib 阻塞 IO —— 调用方（scheduler / admin 手动触发）须用 asyncio.to_thread 包裹。
    """
    from bottleneck_hunter.auth.current_user import reset_current_user, set_current_user
    from bottleneck_hunter.auth.email_sender import fetch_unseen, imap_configured, resolve_imap_config
    from bottleneck_hunter.vip.importer import dispatch_import

    cfg = resolve_imap_config(auth_store)
    if not imap_configured(cfg):
        logger.info("IMAP 未配置，跳过邮件轮询")
        return {"processed": 0, "rejected": 0, "errors": 0}

    admin_sub = _find_admin_sub(auth_store)
    if not admin_sub:
        logger.warning("未找到管理员用户，无法用主模型解读正文；本轮跳过")
        return {"processed": 0, "rejected": 0, "errors": 0}

    pdf_password = cfg.get("pdf_password", "")
    counts = {"processed": 0, "rejected": 0, "errors": 0}
    for msg in fetch_unseen(cfg):
        try:
            _process_one(msg, wl_store, auth_store, admin_sub, pdf_password, counts,
                         dispatch_import, set_current_user, reset_current_user)
        except Exception as e:  # noqa: BLE001 - 单封失败不拖垮整轮
            counts["errors"] += 1
            logger.exception("处理邮件失败: %s", e)
    logger.info("邮件轮询完成：processed=%d rejected=%d errors=%d",
                counts["processed"], counts["rejected"], counts["errors"])
    return counts


def _process_one(msg, wl_store, auth_store, admin_sub, pdf_password, counts,
                 dispatch_import, set_current_user, reset_current_user) -> None:
    msgid = (msg.get("Message-Id") or msg.get("Message-ID") or "").strip()
    if not msgid:
        # 无 Message-Id 无法幂等去重 → 用 From+Subject+Date 合成一个，避免重复入库
        msgid = f"synth:{parseaddr(msg.get('From',''))[1]}|{msg.get('Subject','')}|{msg.get('Date','')}"
    sender = (parseaddr(msg.get("From", ""))[1] or "").strip()
    subject = _decode(msg.get("Subject", ""))

    # 邮件级幂等（全局 msgid UNIQUE，跨用户查）
    if wl_store.find_mail_log_by_msgid(msgid):
        logger.info("邮件已处理过，跳过: %s", msgid)
        return

    vip = auth_store.get_user_by_email(sender)
    if not vip:
        # 安全闸：认不出发件人一律拦截，不产生任何落库（日志归属管理员桶，供后台审计）
        wl_store.for_user(admin_sub).create_mail_ingest_log(
            msgid=msgid, sender=sender, summary=f"未知发件人，已拦截：{subject}",
            status="rejected", reason="unknown_sender")
        counts["rejected"] += 1
        logger.warning("拦截未注册发件人邮件: %s", sender)
        return

    wl = wl_store.for_user(vip.id).for_market("us_stock")

    # ① 正文解读（LLM，用管理员 key）——套用管理员上下文，出了本块立即还原
    tok = set_current_user(admin_sub)
    try:
        body_result = interpret_body(subject, _body_text(msg), admin_sub)
    finally:
        reset_current_user(tok)

    # 正文交易 → 待确认队列（不入账户）
    n_body_txn = 0
    for i, txn in enumerate(body_result.get("transactions", [])):
        wl.create_pending_txn(msgid=msgid, account_ref="",
                              txn=_pending_txn_record(txn, msgid, i))
        n_body_txn += 1

    # ② 附件解读（结构化，自动入库，归 VIP 用户）
    attach_results = []
    for fname, data in _attachments(msg):
        try:
            r = dispatch_import(data, fname, user_id=vip.id, wl_store=wl,
                                market="us_stock", password=pdf_password)
            attach_results.append({"file_name": fname, "status": r.status,
                                   "detected_kind": r.detected_kind, "summary": r.summary})
        except Exception as e:  # noqa: BLE001 - 单附件失败不拖垮整封
            attach_results.append({"file_name": fname, "status": "error", "reason": str(e)[:200]})
            logger.warning("附件入库失败 %s: %s", fname, e)

    # ③ 解读记录
    summary = body_result.get("summary") or subject or "（无摘要）"
    wl.create_mail_ingest_log(msgid=msgid, sender=sender, summary=summary,
                              n_body_txn=n_body_txn, attachments=attach_results,
                              status="processed")
    counts["processed"] += 1


def _find_admin_sub(auth_store) -> str:
    """取第一个管理员用户 id（用其主模型解读正文）。"""
    try:
        for u in auth_store.list_users():
            if getattr(u, "role", "") == "admin":
                return u.id
    except Exception:  # noqa: BLE001
        pass
    return ""


# ─────────────────────────── 自检 ───────────────────────────
def demo() -> None:
    """合成 .eml 断言：附件字节被正确抽出、陌生发件人被拦截、msgid 去重。无需真实 IMAP / LLM。"""
    from email.message import EmailMessage

    # 1) 附件抽取 + 正文抽取
    m = EmailMessage()
    m["From"] = "Alice <alice@bank.com>"
    m["Subject"] = "=?utf-8?b?5oiQ5Lqk56Gu6K6k?="  # "成交确认"
    m["Message-Id"] = "<abc@bank.com>"
    m.set_content("买入 NVDA 100 股，成交价 120.5 美元。")
    m.add_attachment(b"%PDF-1.4 fake", maintype="application", subtype="pdf", filename="statement.pdf")

    atts = _attachments(m)
    assert len(atts) == 1 and atts[0][0] == "statement.pdf" and atts[0][1].startswith(b"%PDF"), atts
    assert "买入" in _body_text(m), _body_text(m)
    assert _decode(m["Subject"]) == "成交确认", _decode(m["Subject"])

    # 2) 陌生发件人拦截 + msgid 去重（用假 store/auth 断言控制流，不碰真实 DB / LLM）
    class _FakeWl:
        def __init__(self):
            self.logs = {}
            self.pending = []
            self.scope = None
        def for_user(self, uid):
            self.scope = uid
            return self
        def for_market(self, m):
            return self
        def find_mail_log_by_msgid(self, msgid):
            return self.logs.get(msgid)
        def create_mail_ingest_log(self, *, msgid, sender, summary, n_body_txn=0,
                                   attachments=None, status="processed", reason=""):
            self.logs[msgid] = {"status": status, "reason": reason, "user": self.scope}
            return "id"
        def create_pending_txn(self, *, msgid, account_ref, txn):
            self.pending.append(txn)
            return "pid"

    class _FakeAuth:
        def get_user_by_email(self, email):
            return None  # 一律陌生
        def list_users(self):
            class _U:
                id = "admin1"
                role = "admin"
            return [_U()]

    wl = _FakeWl()
    counts = {"processed": 0, "rejected": 0, "errors": 0}
    def noop(*a, **k):
        return None
    _process_one(m, wl, _FakeAuth(), "admin1", "", counts,
                 dispatch_import=noop,
                 set_current_user=lambda x: None, reset_current_user=noop)
    assert counts["rejected"] == 1 and counts["processed"] == 0, counts
    assert wl.logs["<abc@bank.com>"]["reason"] == "unknown_sender"
    assert wl.logs["<abc@bank.com>"]["user"] == "admin1", "拦截日志须归管理员桶"
    assert wl.pending == [], "陌生发件人不得产生待确认交易"

    # 3) 同一 msgid 再来一封 → 去重跳过，计数不变
    _process_one(m, wl, _FakeAuth(), "admin1", "", counts,
                 dispatch_import=noop,
                 set_current_user=lambda x: None, reset_current_user=noop)
    assert counts["rejected"] == 1, f"msgid 去重失败: {counts}"

    print("mail_ingest demo OK: attachments / body / reject-unknown / msgid-dedup all pass")


if __name__ == "__main__":
    demo()
