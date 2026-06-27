"""SQLite-based persistence for analysis results.

Stores the full ScreeningResult JSON alongside indexed metadata
for fast listing/filtering. Zero external dependencies — uses
Python's built-in sqlite3.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_DB_DIR = Path("data")
DEFAULT_DB_PATH = DEFAULT_DB_DIR / "analyses.db"

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS analyses (
    id               TEXT PRIMARY KEY,
    sector           TEXT NOT NULL,
    end_product      TEXT NOT NULL,
    provider         TEXT,
    model            TEXT,
    market           TEXT,
    max_depth        INTEGER,
    top_n            INTEGER,
    max_market_cap_yi REAL,
    language         TEXT DEFAULT 'zh',
    created_at       TEXT NOT NULL,
    updated_at       TEXT,
    top_picks        TEXT,
    bottleneck_count INTEGER DEFAULT 0,
    supplier_count   INTEGER DEFAULT 0,
    result_json      TEXT NOT NULL,
    report_path      TEXT
);
"""

_CREATE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_analyses_created ON analyses(created_at DESC);
"""

_MIGRATIONS = [
    "ALTER TABLE analyses ADD COLUMN max_market_cap_yi REAL",
    "ALTER TABLE analyses ADD COLUMN updated_at TEXT",
    "ALTER TABLE analyses ADD COLUMN seq_no INTEGER",
    "ALTER TABLE analyses ADD COLUMN completed_phases INTEGER DEFAULT 0",
    # Phase 16B: 多用户
    "ALTER TABLE analyses ADD COLUMN user_id TEXT DEFAULT ''",
    "CREATE INDEX IF NOT EXISTS idx_analyses_user ON analyses(user_id)",
]

# 列表查询不返回 result_json（体积大），只返回摘要
_LIST_COLS = [
    "id", "seq_no", "completed_phases", "sector", "end_product", "provider", "model", "market",
    "max_depth", "top_n", "max_market_cap_yi", "language", "created_at", "updated_at",
    "top_picks", "bottleneck_count", "supplier_count", "report_path",
]


class AnalysisStore:
    """轻量级 SQLite 存储，管理分析历史。"""

    def __init__(self, db_path: str | Path | None = None, user_id: str = ""):
        self.db_path = Path(db_path) if db_path else DEFAULT_DB_PATH
        self._user_id = user_id
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def for_user(self, user_id: str) -> "AnalysisStore":
        """返回绑定指定用户的 store 克隆。"""
        clone = object.__new__(AnalysisStore)
        clone.db_path = self.db_path
        clone._user_id = user_id
        return clone

    def _user_filter(self, query: str, params: tuple = ()) -> tuple[str, tuple]:
        """为 SQL 查询自动追加 user_id 过滤条件。"""
        if not self._user_id:
            return query, params
        upper = query.upper()
        has_where = " WHERE " in upper
        clause = " AND user_id = ?" if has_where else " WHERE user_id = ?"
        search_start = upper.find(" WHERE ") + 7 if has_where else 0
        insert_pos = len(query)
        for kw in (" ORDER BY ", " GROUP BY ", " LIMIT "):
            idx = upper.find(kw, search_start)
            if idx != -1 and idx < insert_pos:
                insert_pos = idx
        query = query[:insert_pos] + clause + query[insert_pos:]
        return query, params + (self._user_id,)

    # ── 内部方法 ──────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self):
        with self._connect() as conn:
            conn.execute(_CREATE_TABLE)
            conn.execute(_CREATE_INDEX)
            for sql in _MIGRATIONS:
                try:
                    conn.execute(sql)
                except sqlite3.OperationalError:
                    pass
            self._backfill_seq_no(conn)
            self._backfill_completed_phases(conn)
            conn.commit()
        logger.info(f"分析数据库已就绪: {self.db_path}")

    def _backfill_seq_no(self, conn: sqlite3.Connection):
        """为缺少 seq_no 的旧记录补上递增编号。"""
        rows = conn.execute(
            "SELECT id FROM analyses WHERE seq_no IS NULL ORDER BY created_at ASC"
        ).fetchall()
        if not rows:
            return
        max_row = conn.execute("SELECT COALESCE(MAX(seq_no), 0) FROM analyses").fetchone()
        start = (max_row[0] or 0) + 1
        for i, row in enumerate(rows):
            conn.execute("UPDATE analyses SET seq_no = ? WHERE id = ?", (start + i, row[0]))
        logger.info(f"已为 {len(rows)} 条旧记录补上编号 #{start}~#{start + len(rows) - 1}")

    def _backfill_completed_phases(self, conn: sqlite3.Connection):
        """根据 result_json 内容为旧记录推算已完成步数。"""
        rows = conn.execute(
            "SELECT id, result_json, bottleneck_count, supplier_count "
            "FROM analyses WHERE completed_phases IS NULL OR completed_phases = 0"
        ).fetchall()
        if not rows:
            return
        for row in rows:
            try:
                result = json.loads(row["result_json"] or "{}")
            except (json.JSONDecodeError, TypeError):
                result = {}
            phases = 0
            if (row["bottleneck_count"] or 0) > 0:
                phases = 1
            if (row["supplier_count"] or 0) > 0:
                phases = 2
            if result.get("cross_validations"):
                phases = 3
            if result.get("meeting_result"):
                phases = 4
            if phases > 0:
                conn.execute("UPDATE analyses SET completed_phases = ? WHERE id = ?", (phases, row["id"]))
        logger.info(f"已为 {len(rows)} 条旧记录回填进度信息")

    # ── 公共 API ──────────────────────────────────────

    def save(self, config: Any, result_dict: dict, report_path: str = "") -> str:
        """保存分析结果。

        Args:
            config: ScreenRequest 或类似对象（需有 sector, end_product 等属性）
            result_dict: ScreeningResult.model_dump() 的输出字典
            report_path: Markdown 报告路径

        Returns:
            (analysis_id, seq_no) 元组
        """
        analysis_id = str(uuid.uuid4())
        now = datetime.now().isoformat(timespec="seconds")

        top_picks = result_dict.get("top_picks", [])
        bottleneck_count = len(result_dict.get("bottleneck_reports", []))
        supplier_count = len(result_dict.get("supplier_scorecards", []))

        with self._connect() as conn:
            q, p = self._user_filter("SELECT COALESCE(MAX(seq_no), 0) FROM analyses")
            row = conn.execute(q, p).fetchone()
            seq_no = (row[0] or 0) + 1

            uid_col = ", user_id" if self._user_id else ""
            uid_val = ", ?" if self._user_id else ""
            uid_param = (self._user_id,) if self._user_id else ()
            conn.execute(
                f"""INSERT INTO analyses
                   (id, seq_no, completed_phases, sector, end_product, provider, model, market,
                    max_depth, top_n, max_market_cap_yi, language, created_at, updated_at,
                    top_picks, bottleneck_count, supplier_count, result_json, report_path{uid_col})
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?{uid_val})""",
                (
                    analysis_id,
                    seq_no,
                    1,
                    getattr(config, "sector", ""),
                    getattr(config, "end_product", ""),
                    getattr(config, "provider", ""),
                    getattr(config, "model", ""),
                    getattr(config, "market", ""),
                    getattr(config, "max_depth", 3),
                    getattr(config, "top_n", 5),
                    getattr(config, "max_market_cap_yi", None),
                    getattr(config, "language", "zh"),
                    now,
                    now,
                    json.dumps(top_picks, ensure_ascii=False),
                    bottleneck_count,
                    supplier_count,
                    json.dumps(result_dict, ensure_ascii=False, default=str),
                    report_path,
                ) + uid_param,
            )
            conn.commit()

        logger.info(f"分析已保存: #{seq_no} {analysis_id} ({getattr(config, 'sector', '')})")
        return analysis_id, seq_no

    def list_all(self) -> list[dict]:
        """返回所有记录的摘要列表（按时间倒序，不含 result_json）。"""
        cols = ", ".join(_LIST_COLS)
        q, p = self._user_filter(f"SELECT {cols} FROM analyses ORDER BY created_at DESC")
        with self._connect() as conn:
            rows = conn.execute(q, p).fetchall()

        results = []
        # 统计每个 sector+end_product 的累计分析次数
        combo_count: dict[str, int] = {}
        for row in rows:
            key = (row["sector"] or "") + "|" + (row["end_product"] or "")
            combo_count[key] = combo_count.get(key, 0) + 1

        for row in rows:
            d = dict(row)
            # 将 top_picks 从 JSON 字符串解析为列表
            try:
                d["top_picks"] = json.loads(d["top_picks"] or "[]")
            except (json.JSONDecodeError, TypeError):
                d["top_picks"] = []
            # 同一赛道的累计分析次数
            key = (d.get("sector") or "") + "|" + (d.get("end_product") or "")
            d["run_count"] = combo_count.get(key, 1)
            results.append(d)
        return results

    def get(self, analysis_id: str) -> dict | None:
        """返回完整记录（含 result_json）。"""
        with self._connect() as conn:
            q, p = self._user_filter("SELECT * FROM analyses WHERE id = ?", (analysis_id,))
            row = conn.execute(q, p).fetchone()

            if not row:
                return None

            d = dict(row)

            # 同一赛道的累计分析次数
            sector = d.get("sector") or ""
            end_product = d.get("end_product") or ""
            q2, p2 = self._user_filter(
                "SELECT COUNT(*) FROM analyses WHERE COALESCE(sector,'') = ? AND COALESCE(end_product,'') = ?",
                (sector, end_product),
            )
            count_row = conn.execute(q2, p2).fetchone()
            d["run_count"] = count_row[0] if count_row else 1

        try:
            d["top_picks"] = json.loads(d["top_picks"] or "[]")
        except (json.JSONDecodeError, TypeError):
            d["top_picks"] = []
        try:
            d["result_json"] = json.loads(d["result_json"])
        except (json.JSONDecodeError, TypeError):
            d["result_json"] = {}
        return d

    def delete(self, analysis_id: str) -> bool:
        """删除一条记录，返回是否实际删除。"""
        with self._connect() as conn:
            q, p = self._user_filter("DELETE FROM analyses WHERE id = ?", (analysis_id,))
            cur = conn.execute(q, p)
            conn.commit()
            deleted = cur.rowcount > 0

        if deleted:
            logger.info(f"分析已删除: {analysis_id}")
        return deleted

    def count_by_sector(self, sector: str, end_product: str) -> int:
        """返回同一 sector+end_product 的累计分析记录数。"""
        with self._connect() as conn:
            q, p = self._user_filter(
                "SELECT COUNT(*) FROM analyses WHERE COALESCE(sector,'') = ? AND COALESCE(end_product,'') = ?",
                (sector or "", end_product or ""),
            )
            row = conn.execute(q, p).fetchone()
            return row[0] if row else 0

    def update_cross_validations(
        self, analysis_id: str, cross_validations: list[dict]
    ) -> bool:
        """更新指定记录的交叉验证结果，同时重新计算 top_picks。"""
        record = self.get(analysis_id)
        if not record:
            return False

        result = record["result_json"]
        result["cross_validations"] = cross_validations

        # 重新计算 top_picks
        top_picks = []
        scorecards = result.get("supplier_scorecards", [])
        for cv in cross_validations:
            if cv.get("consensus") in ("pass", "concern"):
                top_picks.append(cv.get("ticker", ""))
        if not top_picks:
            for sc in scorecards[:5]:
                score = sc.get("overall_score", 0)
                ticker = sc.get("supplier", {}).get("ticker", sc.get("ticker", ""))
                if score >= 6 and ticker:
                    top_picks.append(ticker)
        result["top_picks"] = top_picks

        with self._connect() as conn:
            q, p = self._user_filter(
                """UPDATE analyses
                   SET result_json = ?, top_picks = ?,
                       completed_phases = MAX(COALESCE(completed_phases, 0), 3),
                       updated_at = ?
                   WHERE id = ?""",
                (
                    json.dumps(result, ensure_ascii=False, default=str),
                    json.dumps(top_picks, ensure_ascii=False),
                    datetime.now().isoformat(timespec="seconds"),
                    analysis_id,
                ),
            )
            conn.execute(q, p)
            conn.commit()

        logger.info(f"交叉验证已更新: {analysis_id}")
        return True

    def update_meeting_result(self, analysis_id: str, meeting_result: dict) -> bool:
        """保存圆桌会议结果到 result_json["meeting_result"]。"""
        record = self.get(analysis_id)
        if not record:
            return False

        result = record["result_json"]
        result["meeting_result"] = meeting_result

        with self._connect() as conn:
            q, p = self._user_filter(
                """UPDATE analyses SET result_json = ?,
                       completed_phases = MAX(COALESCE(completed_phases, 0), 4),
                       updated_at = ? WHERE id = ?""",
                (
                    json.dumps(result, ensure_ascii=False, default=str),
                    datetime.now().isoformat(timespec="seconds"),
                    analysis_id,
                ),
            )
            conn.execute(q, p)
            conn.commit()

        logger.info(f"圆桌会议结果已保存: {analysis_id}")
        return True

    def update_suppliers(
        self, analysis_id: str,
        supplier_scorecards: list[dict],
        cross_validations: list[dict] | None = None,
        max_market_cap_yi: float | None = None,
        scoring_config: dict | None = None,
    ) -> bool:
        """更新指定记录的供应商评估和交叉验证结果。"""
        record = self.get(analysis_id)
        if not record:
            return False

        result = record["result_json"]
        result["supplier_scorecards"] = supplier_scorecards
        if cross_validations is not None:
            result["cross_validations"] = cross_validations
        if scoring_config is not None:
            result["scoring_config"] = scoring_config

        top_picks = []
        cv_list = result.get("cross_validations", [])
        for cv in cv_list:
            if cv.get("consensus") in ("pass", "concern"):
                top_picks.append(cv.get("ticker", ""))
        if not top_picks:
            for sc in supplier_scorecards[:5]:
                score = sc.get("overall_score", 0)
                ticker = sc.get("supplier", {}).get("ticker", sc.get("ticker", ""))
                if score >= 6 and ticker:
                    top_picks.append(ticker)
        result["top_picks"] = top_picks

        supplier_count = len(supplier_scorecards)

        with self._connect() as conn:
            q, p = self._user_filter(
                """UPDATE analyses
                   SET result_json = ?, top_picks = ?, supplier_count = ?,
                       max_market_cap_yi = COALESCE(?, max_market_cap_yi),
                       completed_phases = MAX(COALESCE(completed_phases, 0), 2),
                       updated_at = ?
                   WHERE id = ?""",
                (
                    json.dumps(result, ensure_ascii=False, default=str),
                    json.dumps(top_picks, ensure_ascii=False),
                    supplier_count,
                    max_market_cap_yi,
                    datetime.now().isoformat(timespec="seconds"),
                    analysis_id,
                ),
            )
            conn.execute(q, p)
            conn.commit()

        logger.info(f"供应商数据已更新: {analysis_id}")
        return True

    def update_ai_report(
        self, analysis_id: str, report_key: str, text: str, scoring_config: dict,
        model: str = "", provider: str = "", generated_at: str = "",
    ) -> bool:
        """保存一份 AI 评点到 result_json.ai_reports.{report_key}。"""
        record = self.get(analysis_id)
        if not record:
            return False

        result = record["result_json"]
        if "ai_reports" not in result:
            result["ai_reports"] = {}
        result["ai_reports"][report_key] = {
            "text": text,
            "scoring_config": scoring_config,
            "model": model,
            "provider": provider,
            "generated_at": generated_at,
        }

        with self._connect() as conn:
            q, p = self._user_filter(
                "UPDATE analyses SET result_json = ?, updated_at = ? WHERE id = ?",
                (json.dumps(result, ensure_ascii=False, default=str),
                 datetime.now().isoformat(timespec="seconds"), analysis_id),
            )
            conn.execute(q, p)
            conn.commit()

        logger.info(f"AI 评点已保存: {analysis_id}/{report_key}")
        return True

    def update_phase_status(self, analysis_id: str, phase_status: dict) -> bool:
        """更新信号灯状态到 result_json._phase_status。"""
        record = self.get(analysis_id)
        if not record:
            return False
        result = record["result_json"]
        result["_phase_status"] = phase_status
        now = datetime.now().isoformat(timespec="seconds")
        with self._connect() as conn:
            q, p = self._user_filter(
                "UPDATE analyses SET result_json = ?, updated_at = ? WHERE id = ?",
                (json.dumps(result, ensure_ascii=False, default=str), now, analysis_id),
            )
            conn.execute(q, p)
            conn.commit()
        return True

    def get_ai_reports(self, analysis_id: str) -> dict:
        """读取所有已保存的 AI 评点。"""
        record = self.get(analysis_id)
        if not record:
            return {}
        return record.get("result_json", {}).get("ai_reports", {})
