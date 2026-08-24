"""邮箱验证码发送 —— stdlib smtplib，无第三方依赖。

SMTP 配置来源（按优先级）：
1. 管理后台配置（存 AuthStore.system_config，密码 AES 加密）——只要设置了 smtp_host 即整组生效。
2. 环境变量（.env）：SMTP_HOST / SMTP_PORT / SMTP_USER / SMTP_PASSWORD / SMTP_FROM / SMTP_USE_TLS。

两者都未配置时：把验证码打到服务器日志（开发兜底），注册/改邮箱流程仍可走通。
"""

from __future__ import annotations

import logging
import os
import smtplib
from email.message import EmailMessage

from bottleneck_hunter.auth.crypto import decrypt

logger = logging.getLogger(__name__)

_PURPOSE_SUBJECT = {
    "register": "BottleneckHunter 注册验证码",
    "change_email": "BottleneckHunter 邮箱变更验证码",
    "test": "BottleneckHunter SMTP 测试邮件",
}


def resolve_smtp_config(store=None) -> dict:
    """解析生效的 SMTP 配置。store 的 system_config 里设置了 smtp_host 则整组用 DB，否则用环境变量。

    返回 {host, port, user, password, from, use_tls, source}。
    """
    def _tls(v: str) -> bool:
        return str(v).strip().lower() in ("1", "true", "yes")

    if store is not None and (store.get_config("smtp_host", "") or "").strip():
        password = ""
        enc = store.get_config("smtp_password_enc", "")
        if enc:
            try:
                password = decrypt(enc)
            except Exception:
                logger.warning("SMTP 密码解密失败", exc_info=True)
        return {
            "host": store.get_config("smtp_host", "").strip(),
            "port": int(store.get_config("smtp_port", "587") or "587"),
            "user": store.get_config("smtp_user", "").strip(),
            "password": password,
            "from": store.get_config("smtp_from", "").strip(),
            "use_tls": _tls(store.get_config("smtp_use_tls", "true")),
            "source": "db",
        }
    return {
        "host": os.getenv("SMTP_HOST", "").strip(),
        "port": int(os.getenv("SMTP_PORT", "587") or "587"),
        "user": os.getenv("SMTP_USER", "").strip(),
        "password": os.getenv("SMTP_PASSWORD", ""),
        "from": os.getenv("SMTP_FROM", "").strip(),
        "use_tls": _tls(os.getenv("SMTP_USE_TLS", "true")),
        "source": "env",
    }


def smtp_configured(config: dict | None = None) -> bool:
    cfg = config if config is not None else resolve_smtp_config()
    return bool(cfg.get("host"))


def _build_message(to_email: str, body: str, subject: str, sender: str) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to_email
    msg.set_content(body)
    return msg


def _send_smtp(msg: EmailMessage, config: dict) -> None:
    host = config["host"]
    port = int(config.get("port") or 587)
    user = config.get("user", "")
    password = config.get("password", "")
    use_tls = config.get("use_tls", True)

    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=15) as s:
            if user:
                s.login(user, password)
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=15) as s:
            if use_tls:
                s.starttls()
            if user:
                s.login(user, password)
            s.send_message(msg)


def _sender_addr(config: dict) -> str:
    return config.get("from") or config.get("user") or "no-reply@bottleneck-hunter.local"


def send_verification_email(to_email: str, code: str, purpose: str, config: dict | None = None) -> bool:
    """发送验证码邮件。config 省略时按环境变量解析。

    未配置 SMTP → 打日志兜底，返回 True。已配置但发送失败 → 返回 False。
    """
    cfg = config if config is not None else resolve_smtp_config()
    if not smtp_configured(cfg):
        # SMTP 未配置：默认绝不把验证码写日志（会经 stdout→Loki 存 14 天，任何能读日志者可接管账号）。
        # 仅当显式设 BH_DEV_LOG_CODES=1（本地开发）才打码；生产永不设该变量。
        import os
        if os.getenv("BH_DEV_LOG_CODES") == "1":
            logger.warning(
                "[邮件未配置-开发兜底] 发往 %s 的%s验证码：%s",
                to_email, _PURPOSE_SUBJECT.get(purpose, ""), code,
            )
        else:
            logger.warning(
                "[邮件未配置] 已为 %s 生成%s验证码但未发送（SMTP 未配置）；"
                "如需本地调试可设 BH_DEV_LOG_CODES=1 打印验证码",
                to_email, _PURPOSE_SUBJECT.get(purpose, ""),
            )
        return True
    body = (
        f"您的验证码是：{code}\n\n"
        f"该验证码 10 分钟内有效，请勿泄露给他人。\n"
        f"如果这不是您本人的操作，请忽略此邮件。\n\n"
        f"— BottleneckHunter"
    )
    try:
        _send_smtp(_build_message(to_email, body, _PURPOSE_SUBJECT.get(purpose, "验证码"), _sender_addr(cfg)), cfg)
        logger.info("验证码邮件已发送至 %s (%s)", to_email, purpose)
        return True
    except Exception:
        logger.exception("验证码邮件发送失败: %s", to_email)
        return False


def send_test_email(to_email: str, config: dict) -> tuple[bool, str]:
    """发送一封测试邮件，返回 (成功?, 错误信息)。供管理后台验证 SMTP 连通性。"""
    if not smtp_configured(config):
        return False, "SMTP 未配置（缺少服务器地址）"
    body = "这是一封来自 BottleneckHunter 的 SMTP 测试邮件。收到即表示邮件发送配置正常。"
    try:
        _send_smtp(_build_message(to_email, body, _PURPOSE_SUBJECT["test"], _sender_addr(config)), config)
        return True, ""
    except Exception as e:
        logger.warning("SMTP 测试邮件发送失败: %s", e)
        return False, str(e)


# ─────────────────────────── IMAP 收信（管理员专用银行邮件转发管道） ───────────────────────────
# 镜像上面 SMTP 三件套：配置 DB 优先→env 兜底、connectivity 测试、拉 UNSEEN。stdlib imaplib，零新依赖。

def resolve_imap_config(store=None) -> dict:
    """解析生效的 IMAP 配置。store 的 system_config 设了 imap_host 则整组用 DB，否则用环境变量。

    返回 {host, port, user, password, use_ssl, pdf_password, source}。
    """
    def _ssl(v: str) -> bool:
        return str(v).strip().lower() in ("1", "true", "yes")

    if store is not None and (store.get_config("imap_host", "") or "").strip():
        password = ""
        enc = store.get_config("imap_password_enc", "")
        if enc:
            try:
                password = decrypt(enc)
            except Exception:
                logger.warning("IMAP 密码解密失败", exc_info=True)
        pdf_pwd = ""
        pdf_enc = store.get_config("imap_pdf_password_enc", "")
        if pdf_enc:
            try:
                pdf_pwd = decrypt(pdf_enc)
            except Exception:
                logger.warning("默认 PDF 密码解密失败", exc_info=True)
        return {
            "host": store.get_config("imap_host", "").strip(),
            "port": int(store.get_config("imap_port", "993") or "993"),
            "user": store.get_config("imap_user", "").strip(),
            "password": password,
            "use_ssl": _ssl(store.get_config("imap_use_ssl", "true")),
            "pdf_password": pdf_pwd,
            "source": "db",
        }
    return {
        "host": os.getenv("IMAP_HOST", "").strip(),
        "port": int(os.getenv("IMAP_PORT", "993") or "993"),
        "user": os.getenv("IMAP_USER", "").strip(),
        "password": os.getenv("IMAP_PASSWORD", ""),
        "use_ssl": _ssl(os.getenv("IMAP_USE_SSL", "true")),
        "pdf_password": os.getenv("IMAP_PDF_PASSWORD", ""),
        "source": "env",
    }


def imap_configured(config: dict | None = None) -> bool:
    cfg = config if config is not None else resolve_imap_config()
    return bool(cfg.get("host"))


def _imap_connect(config: dict):
    """建连并 login，返回 imaplib 连接对象。调用方负责 logout。"""
    import imaplib

    host = config["host"]
    port = int(config.get("port") or 993)
    if config.get("use_ssl", True):
        conn = imaplib.IMAP4_SSL(host, port)
    else:
        conn = imaplib.IMAP4(host, port)
    user = config.get("user", "")
    if user:
        conn.login(user, config.get("password", ""))
    return conn


def test_imap_connection(config: dict) -> tuple[bool, str]:
    """连接+login+select INBOX 试探，返回 (成功?, 错误信息)。供后台"测试连接"。"""
    if not imap_configured(config):
        return False, "IMAP 未配置（缺少服务器地址）"
    conn = None
    try:
        conn = _imap_connect(config)
        typ, _ = conn.select("INBOX", readonly=True)
        if typ != "OK":
            return False, "无法选择 INBOX 文件夹"
        return True, ""
    except Exception as e:
        logger.warning("IMAP 测试连接失败: %s", e)
        return False, str(e)
    finally:
        if conn is not None:
            try:
                conn.logout()
            except Exception:
                pass


def fetch_unseen(config: dict, mark_seen: bool = True) -> list:
    """拉 INBOX 中 UNSEEN 邮件，返回解析后的 email.message.Message 列表。

    mark_seen=True 时处理后按 UID 打 \\Seen（幂等由上层 msgid 去重兜底，这里只避免重复拉取）。
    imaplib 是阻塞 IO，调用方需 asyncio.to_thread 包裹。
    """
    import email as email_mod

    conn = None
    out: list = []
    try:
        conn = _imap_connect(config)
        conn.select("INBOX")
        typ, data = conn.search(None, "UNSEEN")
        if typ != "OK" or not data or not data[0]:
            return out
        uids = data[0].split()
        for uid in uids:
            typ, msg_data = conn.fetch(uid, "(RFC822)")
            if typ != "OK" or not msg_data or not msg_data[0]:
                continue
            raw = msg_data[0][1]
            msg = email_mod.message_from_bytes(raw)
            out.append(msg)
            if mark_seen:
                try:
                    conn.store(uid, "+FLAGS", "\\Seen")
                except Exception:
                    logger.warning("标记邮件 %s 已读失败", uid, exc_info=True)
        return out
    finally:
        if conn is not None:
            try:
                conn.close()
                conn.logout()
            except Exception:
                pass
