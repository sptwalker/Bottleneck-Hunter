"""AES-256-GCM 加密/解密 — 用于用户 API KEY 的安全存储。"""

from __future__ import annotations

import base64
import os
import secrets

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_ENV_KEY = "BH_ENCRYPTION_KEY"
_KEY_BYTES = 32  # AES-256
_NONCE_BYTES = 12


def _get_key() -> bytes:
    """获取加密密钥：环境变量 > 持久化文件 > 首次生成并写盘。

    关键：无环境变量时必须**读回** data/.encryption_key，否则每次进程重启都会
    生成新密钥，导致此前加密的数据（用户 API Key / SMTP 密码等）无法解密。
    """
    from pathlib import Path

    raw = os.environ.get(_ENV_KEY, "")
    if raw:
        return base64.urlsafe_b64decode(raw)

    env_path = Path("data/.encryption_key")
    if env_path.exists():
        encoded = env_path.read_text(encoding="utf-8").strip()
        if encoded:
            os.environ[_ENV_KEY] = encoded  # 本进程内缓存
            return base64.urlsafe_b64decode(encoded)

    # 首次：生成并持久化
    key = AESGCM.generate_key(bit_length=256)
    encoded = base64.urlsafe_b64encode(key).decode()
    os.environ[_ENV_KEY] = encoded
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text(encoded, encoding="utf-8")
    return key


def encrypt(plaintext: str) -> str:
    """加密明文，返回 base64 编码的 nonce+ciphertext。"""
    key = _get_key()
    aesgcm = AESGCM(key)
    nonce = secrets.token_bytes(_NONCE_BYTES)
    ct = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
    return base64.urlsafe_b64encode(nonce + ct).decode()


def decrypt(token: str) -> str:
    """解密 base64 编码的 nonce+ciphertext，返回明文。"""
    key = _get_key()
    raw = base64.urlsafe_b64decode(token)
    nonce, ct = raw[:_NONCE_BYTES], raw[_NONCE_BYTES:]
    aesgcm = AESGCM(key)
    return aesgcm.decrypt(nonce, ct, None).decode("utf-8")


def make_hint(api_key: str) -> str:
    """生成 KEY 的脱敏提示，如 sk-...xxxx。"""
    if not api_key:
        return ""
    if len(api_key) <= 8:
        return "***" + api_key[-2:]
    prefix = api_key[:3] if api_key[:3].isalpha() or api_key[:2] == "sk" else ""
    suffix = api_key[-4:]
    return f"{prefix}...{suffix}" if prefix else f"***{suffix}"
