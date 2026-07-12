"""操作日志存储 mixin —— 按用户记录/查询/修剪「实时操作日志」。

三类：auto_update(系统自动更新) / user_action(用户操作) / error(错误失败)。用白话标题+详情。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

VALID_CATEGORIES = {"auto_update", "user_action", "error"}
VALID_RESULTS = {"success", "partial", "fail"}


def _oplog_ts() -> str:
    """微秒级时间戳：避免同秒多条在 `ts <` 分页时被整秒吃掉（丢历史）。"""
    return datetime.now(timezone.utc).isoformat()


class _OpLogMixin:
    def record_operation(self, user_id: str, title: str, *, category: str = "user_action",
                         detail: str = "", result: str = "success", market: str = "",
                         meta: dict | None = None) -> dict:
        """写一条操作日志，返回该记录 dict（供实时广播复用）。"""
        cat = category if category in VALID_CATEGORIES else "user_action"
        res = result if result in VALID_RESULTS else "success"
        rec = {
            "id": uuid.uuid4().hex[:16],
            "user_id": user_id or "",
            "ts": _oplog_ts(),
            "category": cat,
            "title": (title or "")[:200],
            "detail": (detail or "")[:1000],
            "result": res,
            "market": market or "",
            "meta_json": json.dumps(meta, ensure_ascii=False) if meta else "",
        }
        with self._write_conn() as conn:
            conn.execute(
                """INSERT INTO operation_log
                   (id, user_id, ts, category, title, detail, result, market, meta_json)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (rec["id"], rec["user_id"], rec["ts"], rec["category"], rec["title"],
                 rec["detail"], rec["result"], rec["market"], rec["meta_json"]),
            )
        return rec

    def get_operations(self, user_id: str, *, limit: int = 200, category: str = "",
                       before_ts: str = "") -> list[dict]:
        """按用户取操作日志（时间倒序）。category 过滤可选；before_ts 用于翻页。"""
        q = "SELECT * FROM operation_log WHERE user_id = ?"
        params: list = [user_id or ""]
        if category in VALID_CATEGORIES:
            q += " AND category = ?"
            params.append(category)
        if before_ts:
            q += " AND ts < ?"
            params.append(before_ts)
        q += " ORDER BY ts DESC LIMIT ?"
        params.append(max(1, min(int(limit or 200), 1000)))
        conn = self._connect()
        try:
            rows = conn.execute(q, tuple(params)).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def prune_operations(self, keep_days: int = 30) -> int:
        """删除超过 keep_days 天的操作日志，返回删除条数。"""
        from datetime import datetime, timedelta, timezone
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max(1, keep_days))).isoformat(timespec="seconds")
        with self._write_conn() as conn:
            cur = conn.execute("DELETE FROM operation_log WHERE ts < ?", (cutoff,))
            return cur.rowcount
