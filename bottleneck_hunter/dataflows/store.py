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
    language         TEXT DEFAULT 'zh',
    created_at       TEXT NOT NULL,
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

# 列表查询不返回 result_json（体积大），只返回摘要
_LIST_COLS = [
    "id", "sector", "end_product", "provider", "model", "market",
    "max_depth", "top_n", "language", "created_at", "top_picks",
    "bottleneck_count", "supplier_count", "report_path",
]


class AnalysisStore:
    """轻量级 SQLite 存储，管理分析历史。"""

    def __init__(self, db_path: str | Path | None = None):
        self.db_path = Path(db_path) if db_path else DEFAULT_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

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
            conn.commit()
        logger.info(f"分析数据库已就绪: {self.db_path}")

    # ── 公共 API ──────────────────────────────────────

    def save(self, config: Any, result_dict: dict, report_path: str = "") -> str:
        """保存分析结果。

        Args:
            config: ScreenRequest 或类似对象（需有 sector, end_product 等属性）
            result_dict: ScreeningResult.model_dump() 的输出字典
            report_path: Markdown 报告路径

        Returns:
            新记录的 id (UUID)
        """
        analysis_id = str(uuid.uuid4())
        now = datetime.now().isoformat(timespec="seconds")

        top_picks = result_dict.get("top_picks", [])
        bottleneck_count = len(result_dict.get("bottleneck_reports", []))
        supplier_count = len(result_dict.get("supplier_scorecards", []))

        with self._connect() as conn:
            conn.execute(
                """INSERT INTO analyses
                   (id, sector, end_product, provider, model, market,
                    max_depth, top_n, language, created_at, top_picks,
                    bottleneck_count, supplier_count, result_json, report_path)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    analysis_id,
                    getattr(config, "sector", ""),
                    getattr(config, "end_product", ""),
                    getattr(config, "provider", ""),
                    getattr(config, "model", ""),
                    getattr(config, "market", ""),
                    getattr(config, "max_depth", 3),
                    getattr(config, "top_n", 5),
                    getattr(config, "language", "zh"),
                    now,
                    json.dumps(top_picks, ensure_ascii=False),
                    bottleneck_count,
                    supplier_count,
                    json.dumps(result_dict, ensure_ascii=False, default=str),
                    report_path,
                ),
            )
            conn.commit()

        logger.info(f"分析已保存: {analysis_id} ({getattr(config, 'sector', '')})")
        return analysis_id

    def list_all(self) -> list[dict]:
        """返回所有记录的摘要列表（按时间倒序，不含 result_json）。"""
        cols = ", ".join(_LIST_COLS)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT {cols} FROM analyses ORDER BY created_at DESC"
            ).fetchall()

        results = []
        for row in rows:
            d = dict(row)
            # 将 top_picks 从 JSON 字符串解析为列表
            try:
                d["top_picks"] = json.loads(d["top_picks"] or "[]")
            except (json.JSONDecodeError, TypeError):
                d["top_picks"] = []
            results.append(d)
        return results

    def get(self, analysis_id: str) -> dict | None:
        """返回完整记录（含 result_json）。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM analyses WHERE id = ?", (analysis_id,)
            ).fetchone()

        if not row:
            return None

        d = dict(row)
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
            cur = conn.execute("DELETE FROM analyses WHERE id = ?", (analysis_id,))
            conn.commit()
            deleted = cur.rowcount > 0

        if deleted:
            logger.info(f"分析已删除: {analysis_id}")
        return deleted

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
            conn.execute(
                """UPDATE analyses
                   SET result_json = ?, top_picks = ?
                   WHERE id = ?""",
                (
                    json.dumps(result, ensure_ascii=False, default=str),
                    json.dumps(top_picks, ensure_ascii=False),
                    analysis_id,
                ),
            )
            conn.commit()

        logger.info(f"交叉验证已更新: {analysis_id}")
        return True
