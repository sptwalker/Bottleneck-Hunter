"""AuthStore — 用户、邀请码、系统配置的 SQLite 存储。"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import bcrypt as _bcrypt

from .models import InviteCode, UserInDB

logger = logging.getLogger(__name__)

_DEFAULT_DB = Path("data/auth.db")


class AuthStore:
    """认证数据存储层。线程安全（每次调用建立新连接）。"""

    def __init__(self, db_path: Optional[Path] = None):
        self._db_path = db_path or _DEFAULT_DB
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_tables()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_tables(self):
        with self._conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    username TEXT UNIQUE NOT NULL,
                    display_name TEXT DEFAULT '',
                    email TEXT DEFAULT '',
                    password_hash TEXT NOT NULL,
                    role TEXT DEFAULT 'user',
                    is_active INTEGER DEFAULT 1,
                    watchlist_limit INTEGER DEFAULT 24,
                    created_at TEXT,
                    last_login_at TEXT,
                    settings_json TEXT DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS invite_codes (
                    code TEXT PRIMARY KEY,
                    created_by TEXT DEFAULT '',
                    used_by TEXT,
                    created_at TEXT,
                    used_at TEXT,
                    expires_at TEXT,
                    is_active INTEGER DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS system_config (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );
                CREATE TABLE IF NOT EXISTS user_api_keys (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    encrypted_key TEXT NOT NULL,
                    key_hint TEXT DEFAULT '',
                    created_at TEXT,
                    updated_at TEXT,
                    UNIQUE(user_id, provider)
                );
                CREATE TABLE IF NOT EXISTS custom_providers (
                    id TEXT PRIMARY KEY,
                    provider_id TEXT UNIQUE NOT NULL,
                    display_name TEXT NOT NULL,
                    base_url TEXT NOT NULL,
                    api_key_encrypted TEXT DEFAULT '',
                    api_key_hint TEXT DEFAULT '',
                    default_model TEXT NOT NULL,
                    is_active INTEGER DEFAULT 1,
                    created_at TEXT,
                    updated_at TEXT
                );
                CREATE TABLE IF NOT EXISTS data_source_keys (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    base_url TEXT DEFAULT '',
                    encrypted_key TEXT NOT NULL,
                    key_hint TEXT DEFAULT '',
                    is_active INTEGER DEFAULT 1,
                    created_at TEXT,
                    updated_at TEXT,
                    verified_at TEXT DEFAULT '',
                    UNIQUE(user_id, source_id)
                );
                CREATE TABLE IF NOT EXISTS email_verifications (
                    id TEXT PRIMARY KEY,
                    email TEXT NOT NULL,
                    code TEXT NOT NULL,
                    purpose TEXT NOT NULL,
                    payload_json TEXT DEFAULT '{}',
                    attempts INTEGER DEFAULT 0,
                    expires_at TEXT,
                    created_at TEXT
                );
            """)
        self._migrate()

    def _migrate(self):
        """幂等迁移：为旧库补充新增列。"""
        with self._conn() as conn:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
            if "email" not in cols:
                conn.execute("ALTER TABLE users ADD COLUMN email TEXT DEFAULT ''")
            ds_cols = {r["name"] for r in conn.execute("PRAGMA table_info(data_source_keys)").fetchall()}
            if ds_cols and "verified_at" not in ds_cols:
                conn.execute("ALTER TABLE data_source_keys ADD COLUMN verified_at TEXT DEFAULT ''")

    # ── 系统配置 ──────────────────────────────────────────

    def get_config(self, key: str, default: str = "") -> str:
        with self._conn() as conn:
            row = conn.execute("SELECT value FROM system_config WHERE key = ?", (key,)).fetchone()
            return row["value"] if row else default

    def set_config(self, key: str, value: str):
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO system_config (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def is_registration_open(self) -> bool:
        # 兼容两种存储格式: "1"/"0" 和 "true"/"false"
        val = self.get_config("open_registration", "0").lower()
        return val in ("1", "true")

    # ── 用户 CRUD ─────────────────────────────────────────

    def _row_to_user(self, row: sqlite3.Row) -> UserInDB:
        keys = row.keys()
        return UserInDB(
            id=row["id"],
            username=row["username"],
            display_name=row["display_name"] or "",
            email=(row["email"] if "email" in keys else "") or "",
            password_hash=row["password_hash"],
            role=row["role"] or "user",
            is_active=bool(row["is_active"]),
            watchlist_limit=row["watchlist_limit"] or 24,
            created_at=row["created_at"],
            last_login_at=row["last_login_at"],
            settings_json=row["settings_json"] or "{}",
        )

    def get_user_by_username(self, username: str) -> Optional[UserInDB]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
            return self._row_to_user(row) if row else None

    def get_user_by_id(self, user_id: str) -> Optional[UserInDB]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            return self._row_to_user(row) if row else None

    def list_users(self) -> list[UserInDB]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM users ORDER BY created_at DESC").fetchall()
            return [self._row_to_user(r) for r in rows]

    def count_users(self) -> int:
        with self._conn() as conn:
            row = conn.execute("SELECT COUNT(*) AS cnt FROM users").fetchone()
            return row["cnt"] if row else 0

    def create_user(
        self, username: str, password: str = "", role: str = "user",
        display_name: str = "", watchlist_limit: int = 24,
        email: str = "", password_hash: str = "",
    ) -> UserInDB:
        user_id = uuid.uuid4().hex[:16]
        pw_hash = password_hash or _bcrypt.hashpw(password.encode("utf-8"), _bcrypt.gensalt()).decode("utf-8")
        now = datetime.utcnow().isoformat()
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO users (id, username, display_name, email, password_hash, role, is_active, "
                "watchlist_limit, created_at, settings_json) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, '{}')",
                (user_id, username, display_name, email, pw_hash, role, watchlist_limit, now),
            )
        return self.get_user_by_id(user_id)  # type: ignore[return-value]

    @staticmethod
    def hash_password(password: str) -> str:
        """对外暴露的密码哈希（供两阶段注册在验证前预哈希）。"""
        return _bcrypt.hashpw(password.encode("utf-8"), _bcrypt.gensalt()).decode("utf-8")

    def verify_password(self, user: UserInDB, password: str) -> bool:
        return _bcrypt.checkpw(password.encode("utf-8"), user.password_hash.encode("utf-8"))

    def update_last_login(self, user_id: str):
        now = datetime.utcnow().isoformat()
        with self._conn() as conn:
            conn.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (now, user_id))

    def change_password(self, user_id: str, new_password: str):
        pw_hash = _bcrypt.hashpw(new_password.encode("utf-8"), _bcrypt.gensalt()).decode("utf-8")
        with self._conn() as conn:
            conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (pw_hash, user_id))

    def update_user(self, user_id: str, **fields):
        """更新用户字段（role, is_active, watchlist_limit, display_name）。"""
        allowed = {"role", "is_active", "watchlist_limit", "display_name", "settings_json"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [user_id]
        with self._conn() as conn:
            conn.execute(f"UPDATE users SET {set_clause} WHERE id = ?", values)

    def delete_user(self, user_id: str):
        with self._conn() as conn:
            conn.execute("DELETE FROM users WHERE id = ?", (user_id,))

    def get_user_by_email(self, email: str) -> Optional[UserInDB]:
        if not email:
            return None
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM users WHERE email = ? COLLATE NOCASE", (email,)).fetchone()
            return self._row_to_user(row) if row else None

    def update_email(self, user_id: str, new_email: str):
        with self._conn() as conn:
            conn.execute("UPDATE users SET email = ? WHERE id = ?", (new_email, user_id))

    # ── 邮箱验证码 ────────────────────────────────────────

    def create_verification(
        self, email: str, code: str, purpose: str,
        payload: dict | None = None, ttl_seconds: int = 600,
    ) -> None:
        """写入一条验证码记录（同 email+purpose 先清旧记录，保证只有最新一条有效）。"""
        import json
        from datetime import timedelta
        now = datetime.utcnow()
        expires = (now + timedelta(seconds=ttl_seconds)).isoformat()
        with self._conn() as conn:
            conn.execute("DELETE FROM email_verifications WHERE email = ? AND purpose = ?", (email, purpose))
            conn.execute(
                "INSERT INTO email_verifications (id, email, code, purpose, payload_json, attempts, "
                "expires_at, created_at) VALUES (?, ?, ?, ?, ?, 0, ?, ?)",
                (uuid.uuid4().hex[:16], email, code, purpose, json.dumps(payload or {}), expires, now.isoformat()),
            )

    def get_verification_age_seconds(self, email: str, purpose: str) -> float | None:
        """返回最近一条验证码的存在秒数（用于重发冷却）。无记录返回 None。"""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT created_at FROM email_verifications WHERE email = ? AND purpose = ?",
                (email, purpose),
            ).fetchone()
        if not row or not row["created_at"]:
            return None
        try:
            return (datetime.utcnow() - datetime.fromisoformat(row["created_at"])).total_seconds()
        except (ValueError, TypeError):
            return None

    def verify_code(self, email: str, purpose: str, code: str) -> tuple[bool, str, dict]:
        """校验验证码。返回 (成功?, 错误信息, payload)。

        成功即删除该记录；失败则 attempts+1（达 5 次上限后作废）。
        """
        import json
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM email_verifications WHERE email = ? AND purpose = ?",
                (email, purpose),
            ).fetchone()
            if not row:
                return False, "验证码不存在或已使用，请重新获取", {}
            # 过期
            try:
                if datetime.utcnow() > datetime.fromisoformat(row["expires_at"]):
                    conn.execute("DELETE FROM email_verifications WHERE id = ?", (row["id"],))
                    return False, "验证码已过期，请重新获取", {}
            except (ValueError, TypeError):
                pass
            # 尝试次数上限
            if (row["attempts"] or 0) >= 5:
                conn.execute("DELETE FROM email_verifications WHERE id = ?", (row["id"],))
                return False, "尝试次数过多，请重新获取验证码", {}
            if row["code"] != code:
                conn.execute("UPDATE email_verifications SET attempts = attempts + 1 WHERE id = ?", (row["id"],))
                return False, "验证码错误", {}
            payload = json.loads(row["payload_json"] or "{}")
            conn.execute("DELETE FROM email_verifications WHERE id = ?", (row["id"],))
            return True, "", payload

    def list_active_user_ids(self) -> list[str]:
        """返回所有活跃用户 ID。"""
        with self._conn() as conn:
            rows = conn.execute("SELECT id FROM users WHERE is_active = 1").fetchall()
            return [r["id"] for r in rows]

    # ── 默认管理员 ────────────────────────────────────────

    def ensure_default_admin(self) -> Optional[UserInDB]:
        """如果无用户，创建 admin 并生成随机密码（仅终端打印一次）。"""
        if self.count_users() > 0:
            return None
        import secrets as _secrets
        default_pw = _secrets.token_urlsafe(12)
        admin = self.create_user("admin", default_pw, role="admin", display_name="管理员")
        logger.critical("⚠️  已创建默认管理员 admin，临时密码: %s（仅显示一次，请尽快修改）", default_pw)
        return admin

    # ── 邀请码 ────────────────────────────────────────────

    def create_invite_codes(self, count: int, created_by: str, expires_days: int = 30) -> list[str]:
        codes = []
        now = datetime.utcnow()
        expires = (now + timedelta(days=expires_days)).isoformat() if expires_days > 0 else None
        with self._conn() as conn:
            for _ in range(count):
                code = uuid.uuid4().hex[:8].upper()
                conn.execute(
                    "INSERT INTO invite_codes (code, created_by, created_at, expires_at, is_active) "
                    "VALUES (?, ?, ?, ?, 1)",
                    (code, created_by, now.isoformat(), expires),
                )
                codes.append(code)
        return codes

    def validate_invite_code(self, code: str) -> Optional[InviteCode]:
        """验证邀请码：存在、未使用、未过期、未作废。"""
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM invite_codes WHERE code = ?", (code,)).fetchone()
            if not row:
                return None
            ic = InviteCode(
                code=row["code"], created_by=row["created_by"] or "",
                used_by=row["used_by"], created_at=row["created_at"],
                used_at=row["used_at"], expires_at=row["expires_at"],
                is_active=bool(row["is_active"]),
            )
            if not ic.is_active or ic.used_by:
                return None
            if ic.expires_at and ic.expires_at < datetime.utcnow().isoformat():
                return None
            return ic

    def consume_invite_code(self, code: str, user_id: str):
        now = datetime.utcnow().isoformat()
        with self._conn() as conn:
            conn.execute(
                "UPDATE invite_codes SET used_by = ?, used_at = ?, is_active = 0 WHERE code = ?",
                (user_id, now, code),
            )

    def list_invite_codes(self) -> list[InviteCode]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM invite_codes ORDER BY created_at DESC").fetchall()
            return [
                InviteCode(
                    code=r["code"], created_by=r["created_by"] or "",
                    used_by=r["used_by"], created_at=r["created_at"],
                    used_at=r["used_at"], expires_at=r["expires_at"],
                    is_active=bool(r["is_active"]),
                )
                for r in rows
            ]

    def revoke_invite_code(self, code: str):
        with self._conn() as conn:
            conn.execute("UPDATE invite_codes SET is_active = 0 WHERE code = ?", (code,))

    # ── 用户 API KEY ─────────────────────────────────────

    def save_user_api_key(self, user_id: str, provider: str,
                          encrypted_key: str, key_hint: str) -> str:
        """保存或更新用户的 API KEY（已加密）。返回 record id。"""
        now = datetime.utcnow().isoformat()
        record_id = uuid.uuid4().hex[:16]
        with self._conn() as conn:
            # UPSERT: 同一用户+provider 只保留一条
            existing = conn.execute(
                "SELECT id FROM user_api_keys WHERE user_id = ? AND provider = ?",
                (user_id, provider),
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE user_api_keys SET encrypted_key = ?, key_hint = ?, updated_at = ? "
                    "WHERE user_id = ? AND provider = ?",
                    (encrypted_key, key_hint, now, user_id, provider),
                )
                return existing["id"]
            else:
                conn.execute(
                    "INSERT INTO user_api_keys (id, user_id, provider, encrypted_key, key_hint, "
                    "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (record_id, user_id, provider, encrypted_key, key_hint, now, now),
                )
                return record_id

    def get_user_api_keys(self, user_id: str) -> list[dict]:
        """返回用户所有 API KEY（不含明文，只有 hint）。"""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id, provider, key_hint, created_at, updated_at "
                "FROM user_api_keys WHERE user_id = ? ORDER BY provider",
                (user_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_user_api_key_encrypted(self, user_id: str, provider: str) -> str | None:
        """返回指定 provider 的加密 KEY（用于解密后传给 LLM factory）。"""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT encrypted_key FROM user_api_keys WHERE user_id = ? AND provider = ?",
                (user_id, provider),
            ).fetchone()
            return row["encrypted_key"] if row else None

    def delete_user_api_key(self, user_id: str, provider: str) -> bool:
        """删除用户某 provider 的 KEY。返回是否有删除。"""
        with self._conn() as conn:
            cur = conn.execute(
                "DELETE FROM user_api_keys WHERE user_id = ? AND provider = ?",
                (user_id, provider),
            )
            return cur.rowcount > 0

    def delete_all_user_api_keys(self, user_id: str) -> int:
        """删除用户所有 KEY（用于删除用户时清理）。"""
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM user_api_keys WHERE user_id = ?", (user_id,))
            return cur.rowcount

    # ── 付费数据源 Key（按 user 隔离） ────────────────────

    def save_data_source_key(self, user_id: str, source_id: str, base_url: str,
                             encrypted_key: str, key_hint: str) -> str:
        """保存或更新用户的付费数据源 API KEY（已加密）。base_url 供自定义源。返回 record id。"""
        now = datetime.utcnow().isoformat()
        record_id = uuid.uuid4().hex[:16]
        with self._conn() as conn:
            existing = conn.execute(
                "SELECT id FROM data_source_keys WHERE user_id = ? AND source_id = ?",
                (user_id, source_id),
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE data_source_keys SET base_url = ?, encrypted_key = ?, key_hint = ?, "
                    "updated_at = ?, verified_at = '' WHERE user_id = ? AND source_id = ?",
                    (base_url, encrypted_key, key_hint, now, user_id, source_id),
                )
                return existing["id"]
            conn.execute(
                "INSERT INTO data_source_keys (id, user_id, source_id, base_url, encrypted_key, "
                "key_hint, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (record_id, user_id, source_id, base_url, encrypted_key, key_hint, now, now),
            )
            return record_id

    def get_data_source_keys(self, user_id: str) -> list[dict]:
        """返回用户所有数据源配置（不含明文，只有 hint + base_url + 验证状态）。"""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT source_id, key_hint, base_url, created_at, updated_at, verified_at "
                "FROM data_source_keys WHERE user_id = ? ORDER BY source_id",
                (user_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def set_data_source_verified(self, user_id: str, source_id: str, verified_at: str) -> None:
        """标记/清除某数据源 KEY 的验证状态（verified_at 为空串表示未验证）。"""
        with self._conn() as conn:
            conn.execute(
                "UPDATE data_source_keys SET verified_at = ? WHERE user_id = ? AND source_id = ?",
                (verified_at or "", user_id, source_id),
            )

    def get_data_source_key_encrypted(self, user_id: str, source_id: str) -> str | None:
        """返回指定数据源的加密 KEY（用于解密后传给 fetcher）。"""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT encrypted_key FROM data_source_keys WHERE user_id = ? AND source_id = ?",
                (user_id, source_id),
            ).fetchone()
            return row["encrypted_key"] if row else None

    def get_data_source_base_url(self, user_id: str, source_id: str) -> str:
        """返回指定数据源的 base_url（自定义源用）；无则空串。"""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT base_url FROM data_source_keys WHERE user_id = ? AND source_id = ?",
                (user_id, source_id),
            ).fetchone()
            return (row["base_url"] if row else "") or ""

    def delete_data_source_key(self, user_id: str, source_id: str) -> bool:
        """删除用户某数据源的 KEY。返回是否有删除。"""
        with self._conn() as conn:
            cur = conn.execute(
                "DELETE FROM data_source_keys WHERE user_id = ? AND source_id = ?",
                (user_id, source_id),
            )
            return cur.rowcount > 0

    def any_data_source_key_encrypted(self, source_id: str) -> str | None:
        """已废弃：严格按用户隔离下禁止跨用户借用数据源 KEY，永远返回 None。"""
        # ponytail: 保留方法签名以防旧调用，但行为改为严格拒绝借用
        return None

    # ── 自定义 Provider ───────────────────────────────────

    def save_custom_provider(
        self, provider_id: str, display_name: str, base_url: str,
        encrypted_key: str, key_hint: str, default_model: str,
    ) -> str:
        """保存或更新自定义 OpenAI 兼容 provider。返回 record id。"""
        now = datetime.utcnow().isoformat()
        record_id = uuid.uuid4().hex[:16]
        with self._conn() as conn:
            existing = conn.execute(
                "SELECT id FROM custom_providers WHERE provider_id = ?",
                (provider_id,),
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE custom_providers SET display_name = ?, base_url = ?, "
                    "api_key_encrypted = ?, api_key_hint = ?, default_model = ?, "
                    "updated_at = ? WHERE provider_id = ?",
                    (display_name, base_url, encrypted_key, key_hint,
                     default_model, now, provider_id),
                )
                return existing["id"]
            else:
                conn.execute(
                    "INSERT INTO custom_providers (id, provider_id, display_name, base_url, "
                    "api_key_encrypted, api_key_hint, default_model, is_active, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
                    (record_id, provider_id, display_name, base_url,
                     encrypted_key, key_hint, default_model, now, now),
                )
                return record_id

    def list_custom_providers(self) -> list[dict]:
        """返回所有自定义 provider（不含明文 key）。"""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id, provider_id, display_name, base_url, api_key_hint, "
                "default_model, is_active, created_at, updated_at "
                "FROM custom_providers ORDER BY created_at"
            ).fetchall()
            return [dict(r) for r in rows]

    def get_custom_provider(self, provider_id: str) -> dict | None:
        """返回指定自定义 provider（含 encrypted_key，用于 factory 注册）。"""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM custom_providers WHERE provider_id = ?",
                (provider_id,),
            ).fetchone()
            return dict(row) if row else None

    def delete_custom_provider(self, provider_id: str) -> bool:
        """删除自定义 provider。返回是否有删除。"""
        with self._conn() as conn:
            cur = conn.execute(
                "DELETE FROM custom_providers WHERE provider_id = ?",
                (provider_id,),
            )
            return cur.rowcount > 0
