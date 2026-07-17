"""产业链版本管理 — SQLite 持久化存储。

每次产业链拆解完成后自动保存版本，支持按产品名查询历史、
获取最新版本、以及对比两个版本之间的节点增删变化。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 默认数据库路径：项目根目录 data/chains.db
_DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "chains.db"


class ChainStore:
    """产业链版本存储（SQLite）。"""

    def __init__(self, db_path: str | Path | None = None):
        self._db_path = Path(db_path) if db_path else _DEFAULT_DB_PATH
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    # ── 初始化 ─────────────────────────────────────────────

    def _init_db(self) -> None:
        """创建表结构（如不存在）。"""
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS chain_versions (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    product_name TEXT NOT NULL,
                    version     INTEGER NOT NULL,
                    model_used  TEXT NOT NULL DEFAULT '',
                    chain_json  TEXT NOT NULL,
                    created_at  TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_chain_product
                ON chain_versions(product_name)
            """)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        return conn

    # ── 写入 ───────────────────────────────────────────────

    def save_chain(
        self,
        product_name: str,
        chain_data: dict[str, Any],
        model_used: str = "",
    ) -> int:
        """保存一个产业链版本，自动递增版本号。

        Args:
            product_name: 终端产品名称
            chain_data: ChainGraph.model_dump() 的结果
            model_used: 使用的 LLM 模型名称

        Returns:
            新版本的 id
        """
        with self._connect() as conn:
            # 获取当前最大版本号
            row = conn.execute(
                "SELECT MAX(version) AS max_ver FROM chain_versions WHERE product_name = ?",
                (product_name,),
            ).fetchone()
            next_version = (row["max_ver"] or 0) + 1

            now = datetime.now(timezone.utc).isoformat()
            chain_json = json.dumps(chain_data, ensure_ascii=False)

            cursor = conn.execute(
                """INSERT INTO chain_versions (product_name, version, model_used, chain_json, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (product_name, next_version, model_used, chain_json, now),
            )
            new_id = cursor.lastrowid
            logger.info(f"产业链已保存: {product_name} v{next_version} (id={new_id}, model={model_used})")
            return new_id

    # ── 查询 ───────────────────────────────────────────────

    def get_chains(self, product_name: str) -> list[dict[str, Any]]:
        """获取某个产品的全部版本（按版本号降序）。

        Returns:
            列表，每项包含 id, product_name, version, model_used, chain_json(已解析), created_at
        """
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT id, product_name, version, model_used, chain_json, created_at
                   FROM chain_versions
                   WHERE product_name = ?
                   ORDER BY version DESC""",
                (product_name,),
            ).fetchall()

        return [self._row_to_dict(r) for r in rows]

    def get_latest_chain(self, product_name: str) -> dict[str, Any] | None:
        """获取某个产品的最新版本。"""
        with self._connect() as conn:
            row = conn.execute(
                """SELECT id, product_name, version, model_used, chain_json, created_at
                   FROM chain_versions
                   WHERE product_name = ?
                   ORDER BY version DESC
                   LIMIT 1""",
                (product_name,),
            ).fetchone()

        return self._row_to_dict(row) if row else None

    def get_chain_by_id(self, chain_id: int) -> dict[str, Any] | None:
        """按 ID 获取单个版本。"""
        with self._connect() as conn:
            row = conn.execute(
                """SELECT id, product_name, version, model_used, chain_json, created_at
                   FROM chain_versions WHERE id = ?""",
                (chain_id,),
            ).fetchone()

        return self._row_to_dict(row) if row else None

    def get_fresh_chain(self, product_name: str, max_age_days: int = 14,
                        min_depth: int = 0, sector: str = "") -> dict[str, Any] | None:
        """返回可复用的产业链版本，否则 None。省 70~360 次 LLM 拆解调用。

        逐版本(新→旧)扫描，返回第一个同时满足所有安全门的版本——**不只看最新版**，
        这样一次失败/退化的保存(root-only)不会把更早的好版本"遮蔽"掉。
        安全门(缺一不可复用)：
        - 够新：created_at 在 max_age_days 内。
        - 赛道匹配：同 end_product 不同 sector(如"轴承"在汽车 vs 风电)拆出的链完全不同。
        - 非部分结果：拆解超时会 metadata.partial=True，其深层不完整。
        - **实际达到深度** ≥ 请求深度：用节点的真实最大 layer 判定，而非存储的 max_depth
          (max_depth 记的是「请求深度」，LLM 全失败时会得到 root-only 但 max_depth 仍等于请求值)。
        """
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        try:
            versions = self.get_chains(product_name)  # 新→旧
        except Exception:
            return None
        for v in versions:
            cj = v.get("chain_json") or {}
            if not isinstance(cj, dict):
                continue
            # 够新
            try:
                created = datetime.fromisoformat(v.get("created_at", ""))
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                continue
            if (now - created).total_seconds() / 86400 > max_age_days:
                continue  # 更旧的版本只会更旧，但为简单起见继续扫(数量少)
            # 赛道匹配(sector 为空则不校验，兼容旧调用)
            if sector and (cj.get("sector") or "") != sector:
                continue
            # 非部分结果
            if (cj.get("metadata") or {}).get("partial"):
                continue
            # 实际达到深度 ≥ 请求深度(用真实节点 layer，而非存储 max_depth)
            nodes = cj.get("nodes") or []
            achieved = max((n.get("layer", 0) or 0 for n in nodes), default=0)
            if min_depth and achieved < min_depth:
                continue
            return v
        return None

    # ── 版本对比 ───────────────────────────────────────────

    def compare_chains(self, v1_id: int, v2_id: int) -> dict[str, Any]:
        """对比两个版本的节点增删变化。

        Args:
            v1_id: 旧版本 ID
            v2_id: 新版本 ID

        Returns:
            {
                "v1": {"id", "version", "model_used", "created_at"},
                "v2": {"id", "version", "model_used", "created_at"},
                "added_nodes": [节点名列表],    # v2 新增的节点
                "removed_nodes": [节点名列表],  # v2 删除的节点
                "common_nodes": [节点名列表],   # 共有的节点
                "v1_node_count": int,
                "v2_node_count": int,
            }
        """
        c1 = self.get_chain_by_id(v1_id)
        c2 = self.get_chain_by_id(v2_id)

        if not c1 or not c2:
            missing = []
            if not c1:
                missing.append(str(v1_id))
            if not c2:
                missing.append(str(v2_id))
            raise ValueError(f"未找到版本: {', '.join(missing)}")

        nodes1 = {n["name"] for n in c1["chain_json"].get("nodes", [])}
        nodes2 = {n["name"] for n in c2["chain_json"].get("nodes", [])}

        return {
            "v1": {
                "id": c1["id"],
                "version": c1["version"],
                "model_used": c1["model_used"],
                "created_at": c1["created_at"],
            },
            "v2": {
                "id": c2["id"],
                "version": c2["version"],
                "model_used": c2["model_used"],
                "created_at": c2["created_at"],
            },
            "added_nodes": sorted(nodes2 - nodes1),
            "removed_nodes": sorted(nodes1 - nodes2),
            "common_nodes": sorted(nodes1 & nodes2),
            "v1_node_count": len(nodes1),
            "v2_node_count": len(nodes2),
        }

    # ── 内部工具 ───────────────────────────────────────────

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        """将 sqlite3.Row 转为普通 dict，chain_json 自动解析。"""
        d = dict(row)
        if "chain_json" in d and isinstance(d["chain_json"], str):
            try:
                d["chain_json"] = json.loads(d["chain_json"])
            except json.JSONDecodeError:
                pass
        return d
