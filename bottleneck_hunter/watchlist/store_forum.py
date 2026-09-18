"""WatchlistStore mixin：互动留言系统（每用户一个私有留言板）。

板/帖/回帖/AI 身份 override/每日配额/板设置，全部按 user_id 严格隔离。
forum 表只有 user_id、无 market（留言板跨市场共用），故读用 _user_filter（不用 _filtered），
写入显式带 user_id（无全局行）。未绑定用户时：读被 _user_filter 的 forum 护栏拦、
写被 _forum_uid 拦——两道都抛，绝不静默跨板。身份/配额/设置的合并默认值语义见各 get_*。

数据层只做存取，不含业务判定：去重/内容规则/配额闸门是 forum_moderation(F3) 的纯函数，
默认人设表与合并是 forum_identity(F2)——本 mixin 只提供它们所需的 override 行读写。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from bottleneck_hunter.watchlist.store_base import _now_iso, _today

# 用户可编辑的身份字段（banned 走 set_forum_role_banned；user_id/role_key 绑定注册表不可改）
_FORUM_IDENTITY_COLS = ("display_name", "gender", "age", "persona_identity", "personality", "bio")

# forum_settings 缺行时的回退（opt-in：AI 自主发帖默认关；daily_cap＝近 6 小时全板发言上限）
_FORUM_DEFAULT_SETTINGS = {"ai_enabled": 0, "daily_cap": 20}


class _ForumMixin:
    # ---------- 通用 ----------
    def _forum_uid(self) -> str:
        """取当前绑定用户；未绑定即拒（forum 无全局行，写入必须有板主）。"""
        if not self._user_id:
            raise ValueError("forum 操作需先 .for_user(sub) 绑定用户")
        return self._user_id

    # ---------- 帖子 ----------
    def create_forum_post(
        self, author_type: str, body: str, *,
        author_role_key: str = "", title: str = "", ticker: str = "", content_hash: str = "",
    ) -> int:
        uid = self._forum_uid()
        with self._write_conn() as conn:
            cur = conn.execute(
                """INSERT INTO forum_posts
                   (user_id, author_type, author_role_key, title, body, ticker, content_hash, created_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (uid, author_type, author_role_key, title, body, ticker, content_hash, _now_iso()),
            )
            return int(cur.lastrowid)

    def list_forum_posts(
        self, *, limit: int = 50, offset: int = 0, include_deleted: bool = False, role_key: str = "",
    ) -> list[dict]:
        conds, params = [], ()
        if not include_deleted:
            conds.append("p.deleted = 0")
        if role_key:
            conds.append("p.author_role_key = ?")
            params = params + (role_key,)
        # reply_count：LEFT JOIN 聚合未软删回帖数，让前端「💬 回帖」直接显示条数（否则回帖埋在展开层里看不见）。
        # 用 JOIN+GROUP BY 而非相关子查询：子查询自带的 WHERE 会骗过 _user_filter 的 " WHERE " 检测。
        base = ("SELECT p.*, COUNT(r.id) AS reply_count FROM forum_posts p "
                "LEFT JOIN forum_replies r ON r.post_id = p.id AND r.deleted = 0")
        if conds:
            base += " WHERE " + " AND ".join(conds)
        base += " GROUP BY p.id ORDER BY p.created_at DESC, p.id DESC LIMIT ? OFFSET ?"
        params = params + (int(limit), int(offset))
        q, p = self._user_filter(base, params, table="p")  # table 别名→p.user_id 过滤（插到 GROUP BY 前）
        conn = self._connect()
        try:
            return [dict(r) for r in conn.execute(q, p).fetchall()]
        finally:
            conn.close()

    def get_forum_post(self, post_id: int) -> dict | None:
        q, p = self._user_filter("SELECT * FROM forum_posts WHERE id = ?", (int(post_id),))
        conn = self._connect()
        try:
            row = conn.execute(q, p).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def soft_delete_forum_post(self, post_id: int) -> bool:
        q, p = self._user_filter("UPDATE forum_posts SET deleted = 1 WHERE id = ?", (int(post_id),))
        with self._write_conn() as conn:
            return conn.execute(q, p).rowcount > 0

    def set_forum_comments_closed(self, post_id: int, closed: bool) -> bool:
        q, p = self._user_filter(
            "UPDATE forum_posts SET comments_closed = ? WHERE id = ?", (1 if closed else 0, int(post_id))
        )
        with self._write_conn() as conn:
            return conn.execute(q, p).rowcount > 0

    # ---------- 回帖 ----------
    def create_forum_reply(
        self, post_id: int, author_type: str, body: str, *,
        author_role_key: str = "", content_hash: str = "",
    ) -> int:
        uid = self._forum_uid()
        with self._write_conn() as conn:
            cur = conn.execute(
                """INSERT INTO forum_replies
                   (user_id, post_id, author_type, author_role_key, body, content_hash, created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (uid, int(post_id), author_type, author_role_key, body, content_hash, _now_iso()),
            )
            return int(cur.lastrowid)

    def list_forum_replies(self, post_id: int, *, include_deleted: bool = False) -> list[dict]:
        base = "SELECT * FROM forum_replies WHERE post_id = ?"
        if not include_deleted:
            base += " AND deleted = 0"
        base += " ORDER BY created_at ASC, id ASC"
        q, p = self._user_filter(base, (int(post_id),))
        conn = self._connect()
        try:
            return [dict(r) for r in conn.execute(q, p).fetchall()]
        finally:
            conn.close()

    def soft_delete_forum_reply(self, reply_id: int) -> bool:
        q, p = self._user_filter("UPDATE forum_replies SET deleted = 1 WHERE id = ?", (int(reply_id),))
        with self._write_conn() as conn:
            return conn.execute(q, p).rowcount > 0

    # ---------- AI 身份 override ----------
    def get_forum_identity_override(self, role_key: str) -> dict | None:
        q, p = self._user_filter("SELECT * FROM forum_ai_identities WHERE role_key = ?", (role_key,))
        conn = self._connect()
        try:
            row = conn.execute(q, p).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def list_forum_identity_overrides(self) -> dict[str, dict]:
        q, p = self._user_filter("SELECT * FROM forum_ai_identities", ())
        conn = self._connect()
        try:
            return {r["role_key"]: dict(r) for r in conn.execute(q, p).fetchall()}
        finally:
            conn.close()

    def set_forum_identity(self, role_key: str, **fields) -> None:
        """写身份 override 行；只更新提供的已知列（未提供列保留旧值，故用 UPSERT 而非 REPLACE）。"""
        uid = self._forum_uid()
        cols = [c for c in _FORUM_IDENTITY_COLS if c in fields]
        if not cols:
            return
        insert_cols = ["user_id", "role_key", *cols, "updated_at"]
        placeholders = ",".join("?" for _ in insert_cols)
        set_clause = ",".join(f"{c}=excluded.{c}" for c in cols) + ",updated_at=excluded.updated_at"
        sql = (
            f"INSERT INTO forum_ai_identities({','.join(insert_cols)}) VALUES({placeholders}) "
            f"ON CONFLICT(user_id, role_key) DO UPDATE SET {set_clause}"
        )
        vals = [uid, role_key, *[str(fields[c]) for c in cols], _now_iso()]
        with self._write_conn() as conn:
            conn.execute(sql, vals)

    def set_forum_role_banned(self, role_key: str, banned: bool) -> None:
        uid = self._forum_uid()
        with self._write_conn() as conn:
            conn.execute(
                """INSERT INTO forum_ai_identities(user_id, role_key, banned, updated_at)
                   VALUES(?,?,?,?)
                   ON CONFLICT(user_id, role_key)
                   DO UPDATE SET banned=excluded.banned, updated_at=excluded.updated_at""",
                (uid, role_key, 1 if banned else 0, _now_iso()),
            )

    def is_forum_role_banned(self, role_key: str) -> bool:
        row = self.get_forum_identity_override(role_key)
        return bool(row and row.get("banned"))

    # ---------- 每日配额计数 ----------
    def get_forum_daily_count(self, role_key: str, day: str | None = None) -> int:
        q, p = self._user_filter(
            "SELECT post_count FROM forum_ai_daily WHERE role_key = ? AND day = ?", (role_key, day or _today())
        )
        conn = self._connect()
        try:
            row = conn.execute(q, p).fetchone()
            return int(row["post_count"]) if row else 0
        finally:
            conn.close()

    def incr_forum_daily_count(self, role_key: str, day: str | None = None, n: int = 1) -> int:
        uid = self._forum_uid()
        day = day or _today()
        with self._write_conn() as conn:
            conn.execute(
                """INSERT INTO forum_ai_daily(user_id, role_key, day, post_count)
                   VALUES(?,?,?,?)
                   ON CONFLICT(user_id, role_key, day)
                   DO UPDATE SET post_count = post_count + excluded.post_count""",
                (uid, role_key, day, int(n)),
            )
            q, p = self._user_filter(
                "SELECT post_count FROM forum_ai_daily WHERE role_key = ? AND day = ?", (role_key, day)
            )
            row = conn.execute(q, p).fetchone()
            return int(row["post_count"]) if row else 0

    def get_forum_board_daily_total(self, day: str | None = None) -> int:
        q, p = self._user_filter(
            "SELECT COALESCE(SUM(post_count),0) AS total FROM forum_ai_daily WHERE day = ?", (day or _today(),)
        )
        conn = self._connect()
        try:
            return int(conn.execute(q, p).fetchone()["total"])
        finally:
            conn.close()

    # ---------- 滚动窗口配额（近 N 小时全板发言数）----------
    # 日粒度配额（forum_ai_daily）管不了日内节奏：20 条可能在前 2 小时烧光，之后一整天板内无话。
    # 故另记带时间戳的流水（forum_ai_events），配额改按「近 6 小时全板 ≤ cap」的滚动窗口判：
    # 跨带边界不会漏放（23:59 发满、00:01 仍受限），且天然分时——这是替代日配额的那道闸。
    def get_forum_recent_total(self, hours: int = 6) -> int:
        """近 N 小时全板 AI 发言条数（滚动窗口，含帖与回帖）。"""
        since = (datetime.now(timezone.utc) - timedelta(hours=int(hours))).isoformat(timespec="seconds")
        q, p = self._user_filter(
            "SELECT COUNT(*) AS n FROM forum_ai_events WHERE created_at > ?", (since,))
        conn = self._connect()
        try:
            return int(conn.execute(q, p).fetchone()["n"])
        finally:
            conn.close()

    def incr_forum_event(self, role_key: str, kind: str = "post") -> None:
        """记一次 AI 自主发言（滚动窗口配额的计数来源）。"""
        uid = self._forum_uid()
        with self._write_conn() as conn:
            conn.execute(
                "INSERT INTO forum_ai_events(user_id, role_key, kind, created_at) VALUES(?,?,?,?)",
                (uid, role_key, kind, _now_iso()),
            )
            # 顺手清 30 天前的流水：窗口只看 6h，老行纯积压（无到期任务，读时轻扫即可）
            conn.execute("DELETE FROM forum_ai_events WHERE created_at < ?",
                         ((datetime.now(timezone.utc) - timedelta(days=30)).isoformat(timespec="seconds"),))

    # ---------- 待回应队列（用户发帖 → AI 择机回应，不即时、不计配额）----------
    def add_forum_pending(self, post_id: int) -> None:
        """用户发帖/回帖后入队，等后续 AI 轮次挑一条回应（幂等：同帖只留一行）。"""
        uid = self._forum_uid()
        with self._write_conn() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO forum_ai_pending(post_id, user_id, created_at) VALUES(?,?,?)",
                (int(post_id), uid, _now_iso()),
            )

    def list_forum_pending(self, *, min_age_min: int = 0, limit: int = 5) -> list[dict]:
        """待回应的帖（最老优先），并可要求「入队已满 min_age_min 分钟」以错开秒回。

        min_age_min=0 表示不过滤（不是「早于本秒」——时间戳是秒精度，那样会把
        同一秒内刚入队的帖也筛掉）。要求时长时用严格小于 + 多减 1 秒的 floor，
        否则「<= now-min_age」会让同一秒入队的帖蒙混过关。
        """
        if int(min_age_min) > 0:
            floor = (datetime.now(timezone.utc)
                     - timedelta(minutes=int(min_age_min), seconds=1)).isoformat(timespec="seconds")
            q, p = self._user_filter(
                "SELECT post_id, created_at FROM forum_ai_pending WHERE created_at < ? "
                "ORDER BY created_at ASC LIMIT ?", (floor, int(limit)))
        else:
            q, p = self._user_filter(
                "SELECT post_id, created_at FROM forum_ai_pending "
                "ORDER BY created_at ASC LIMIT ?", (int(limit),))
        conn = self._connect()
        try:
            return [dict(r) for r in conn.execute(q, p).fetchall()]
        finally:
            conn.close()

    def drop_forum_pending(self, post_id: int) -> None:
        """回应（或放弃）后出队。"""
        uid = self._forum_uid()
        with self._write_conn() as conn:
            conn.execute("DELETE FROM forum_ai_pending WHERE post_id = ? AND user_id = ?",
                         (int(post_id), uid))

    # ---------- 每用户板设置 ----------
    def get_forum_settings(self) -> dict:
        q, p = self._user_filter("SELECT ai_enabled, daily_cap FROM forum_settings", ())
        conn = self._connect()
        try:
            row = conn.execute(q, p).fetchone()
        finally:
            conn.close()
        if not row:
            return dict(_FORUM_DEFAULT_SETTINGS)
        return {"ai_enabled": int(row["ai_enabled"]), "daily_cap": int(row["daily_cap"])}

    def set_forum_settings(self, *, ai_enabled: bool | None = None, daily_cap: int | None = None) -> None:
        uid = self._forum_uid()
        cur = self.get_forum_settings()
        ai = cur["ai_enabled"] if ai_enabled is None else (1 if ai_enabled else 0)
        cap = cur["daily_cap"] if daily_cap is None else int(daily_cap)
        with self._write_conn() as conn:
            conn.execute(
                """INSERT INTO forum_settings(user_id, ai_enabled, daily_cap, updated_at)
                   VALUES(?,?,?,?)
                   ON CONFLICT(user_id) DO UPDATE SET
                        ai_enabled=excluded.ai_enabled, daily_cap=excluded.daily_cap, updated_at=excluded.updated_at""",
                (uid, ai, cap, _now_iso()),
            )

    def is_forum_ai_enabled(self) -> bool:
        return bool(self.get_forum_settings()["ai_enabled"])

    # ---------- 长期记忆（自述备忘，P3·#6） ----------
    def get_forum_memory(self, role_key: str) -> dict | None:
        """取该角色在本板的长期立场备忘 {stance, updated_at}；未建行返回 None。"""
        q, p = self._user_filter(
            "SELECT stance, updated_at FROM forum_memory WHERE role_key = ?", (role_key,)
        )
        conn = self._connect()
        try:
            row = conn.execute(q, p).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def list_forum_memories(self) -> dict[str, dict]:
        """本板全部角色的立场备忘 role_key → {stance, updated_at}（一轮建一次，供注入自述+同侪立场）。"""
        q, p = self._user_filter("SELECT role_key, stance, updated_at FROM forum_memory", ())
        conn = self._connect()
        try:
            return {r["role_key"]: {"stance": r["stance"], "updated_at": r["updated_at"]}
                    for r in conn.execute(q, p).fetchall()}
        finally:
            conn.close()

    def set_forum_memory(self, role_key: str, stance: str) -> None:
        """UPSERT 该角色的长期立场备忘（覆盖旧值 + 刷新 updated_at，蒸馏节流靠 updated_at 判陈旧）。"""
        uid = self._forum_uid()
        with self._write_conn() as conn:
            conn.execute(
                """INSERT INTO forum_memory(user_id, role_key, stance, updated_at)
                   VALUES(?,?,?,?)
                   ON CONFLICT(user_id, role_key)
                   DO UPDATE SET stance=excluded.stance, updated_at=excluded.updated_at""",
                (uid, role_key, stance, _now_iso()),
            )
