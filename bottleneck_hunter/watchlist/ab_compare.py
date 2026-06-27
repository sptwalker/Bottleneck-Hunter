"""A/B 对比模块 — 参数配置快照 & 差异分析

支持保存当前系统参数快照、列出快照、对比两个快照的差异。
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


class ABCompare:
    """参数快照 & 对比分析。"""

    def __init__(self, store):
        self.store = store

    def snapshot_params(self, label: str, params: dict) -> str:
        """保存参数快照，返回 snapshot_id"""
        snapshot_id = uuid.uuid4().hex[:12]
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        conn = self.store._connect()
        try:
            conn.execute(
                f"""INSERT INTO ab_snapshots
                   (id, label, params_json, created_at{self.store._user_insert_cols()})
                   VALUES (?, ?, ?, ?{self.store._user_insert_vals()})""",
                (snapshot_id, label, json.dumps(params, ensure_ascii=False), now)
                + self.store._user_insert_params(),
            )
            conn.commit()
            return snapshot_id
        finally:
            conn.close()

    def compare(self, snapshot_id_a: str, snapshot_id_b: str) -> dict:
        """对比两个快照的差异"""
        conn = self.store._connect()
        try:
            q_a, p_a = self.store._user_filter(
                "SELECT * FROM ab_snapshots WHERE id = ?", (snapshot_id_a,)
            )
            q_b, p_b = self.store._user_filter(
                "SELECT * FROM ab_snapshots WHERE id = ?", (snapshot_id_b,)
            )
            row_a = conn.execute(q_a, p_a).fetchone()
            row_b = conn.execute(q_b, p_b).fetchone()
            if not row_a or not row_b:
                return {"error": "快照不存在"}

            params_a = json.loads(row_a["params_json"] or "{}")
            params_b = json.loads(row_b["params_json"] or "{}")

            diffs = []
            all_keys = sorted(set(list(params_a.keys()) + list(params_b.keys())))
            for key in all_keys:
                val_a = params_a.get(key)
                val_b = params_b.get(key)
                if val_a != val_b:
                    diffs.append({
                        "parameter": key,
                        "value_a": val_a,
                        "value_b": val_b,
                    })

            return {
                "snapshot_a": {
                    "id": row_a["id"],
                    "label": row_a["label"],
                    "created_at": row_a["created_at"],
                },
                "snapshot_b": {
                    "id": row_b["id"],
                    "label": row_b["label"],
                    "created_at": row_b["created_at"],
                },
                "diffs": diffs,
                "total_params": len(all_keys),
                "changed_params": len(diffs),
            }
        finally:
            conn.close()

    def list_snapshots(self) -> list[dict]:
        """列出所有快照"""
        conn = self.store._connect()
        try:
            q, p = self.store._user_filter(
                "SELECT id, label, created_at FROM ab_snapshots ORDER BY created_at DESC"
            )
            rows = conn.execute(q, p).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_current_params(self) -> dict:
        """聚合当前系统配置参数"""
        params = {}

        # 瓶颈权重（从 user_preferences 读取）
        prefs = self.store.get_preferences(category="bottleneck_weights")
        for p in prefs:
            params[f"weight.{p['key']}"] = p["value"]

        # 约束阈值
        prefs_constraint = self.store.get_preferences(category="constraints")
        for p in prefs_constraint:
            params[f"constraint.{p['key']}"] = p["value"]

        # 仓位参数
        prefs_position = self.store.get_preferences(category="position_sizing")
        for p in prefs_position:
            params[f"position.{p['key']}"] = p["value"]

        # 预算配置
        budget = self.store.get_budget_limits()
        for k, v in budget.items():
            params[f"budget.{k}"] = str(v)

        # tier 限制
        params["tier.focus_limit"] = str(self.store._TIER_LIMITS.get("focus", 6))
        params["tier.normal_limit"] = str(self.store._TIER_LIMITS.get("normal", 6))
        params["tier.track_limit"] = str(self.store._TIER_LIMITS.get("track", 12))

        # 宏观策略相关
        macro = self.store.get_latest_macro_strategy()
        if macro:
            params["macro.regime"] = macro.get("regime", "")
            params["macro.risk_appetite"] = macro.get("risk_appetite", "")
            params["macro.cash_pct"] = str(macro.get("recommended_cash_pct", 25.0))

        return params

    def delete_snapshot(self, snapshot_id: str) -> bool:
        """删除指定快照"""
        conn = self.store._connect()
        try:
            q, p = self.store._user_filter(
                "DELETE FROM ab_snapshots WHERE id = ?", (snapshot_id,)
            )
            cur = conn.execute(q, p)
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()
