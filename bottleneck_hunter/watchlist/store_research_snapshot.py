"""原子封存研究输入；来源可复用，成员由数据库随快照一次性生成。"""

import sqlite3
from contextlib import closing
from datetime import datetime

from bottleneck_hunter.watchlist.research_contracts import ResearchSnapshot, SourceObservation


class _ResearchSnapshotMixin:
    def save_research_snapshot(self, snapshot: ResearchSnapshot) -> str:
        uid = getattr(self, "_user_id", "")
        market = getattr(self, "_market", "")
        if not uid or not uid.strip():
            raise ValueError("研究快照写入必须绑定用户")
        if not market or not market.strip():
            raise ValueError("研究快照写入必须绑定市场")
        # model_copy/model_construct 可绕开 Pydantic 校验，持久化边界必须重新验证。
        snapshot = ResearchSnapshot.model_validate(snapshot.model_dump())
        if snapshot.market != market:
            raise ValueError("研究快照市场与 store 不一致")
        try:
            with self._write_conn() as conn:
                for observation in snapshot.observations:
                    payload = observation.model_dump_json()
                    existing = conn.execute(
                        "SELECT payload_json FROM research_observations "
                        "WHERE user_id=? AND market=? AND observation_id=?",
                        (uid, market, observation.observation_id),
                    ).fetchone()
                    if existing:
                        if existing["payload_json"] != payload:
                            raise ValueError("来源 observation_id 内容冲突；修订必须使用新 ID")
                    else:
                        conn.execute(
                            "INSERT INTO research_observations "
                            "(observation_id,user_id,market,metric,payload_json,revision) VALUES (?,?,?,?,?,?)",
                            (observation.observation_id, uid, market, observation.metric,
                             payload, observation.provenance.revision),
                        )
                conn.execute(
                    "INSERT INTO research_snapshots "
                    "(snapshot_id,user_id,market,strategy_version,as_of,created_at,payload_json) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (snapshot.snapshot_id, uid, market, snapshot.strategy_version,
                     snapshot.as_of.isoformat(), snapshot.created_at.isoformat(), snapshot.model_dump_json()),
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError("研究快照重复写入或来源关联冲突") from exc
        return snapshot.snapshot_id

    def get_research_snapshot(self, snapshot_id: str) -> ResearchSnapshot | None:
        uid = getattr(self, "_user_id", "")
        market = getattr(self, "_market", "")
        if not uid or not market:
            return None
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT payload_json FROM research_snapshots WHERE snapshot_id=? AND user_id=? AND market=?",
                (snapshot_id, uid, market),
            ).fetchone()
        return ResearchSnapshot.model_validate_json(row["payload_json"]) if row else None

    def get_research_observations(self, snapshot_id: str) -> tuple[SourceObservation, ...]:
        uid = getattr(self, "_user_id", "")
        market = getattr(self, "_market", "")
        if not uid or not market:
            return ()
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT o.payload_json FROM research_snapshot_observations m "
                "JOIN research_observations o ON o.user_id=m.user_id AND o.market=m.market "
                "AND o.observation_id=m.observation_id "
                "WHERE m.snapshot_id=? AND m.user_id=? AND m.market=? ORDER BY m.ordinal",
                (snapshot_id, uid, market),
            ).fetchall()
        return tuple(SourceObservation.model_validate_json(row["payload_json"]) for row in rows)

    def get_visible_research_observations(self, snapshot_id: str, *,
                                          decision_at: datetime | None = None) -> tuple[SourceObservation, ...]:
        """P0-4 门禁读取：不可见观测在返回前即被拒绝，调用方拿不到泄漏数据。

        未显式给出 decision_at 时以快照 as_of 为决策时点；快照不存在则空结果（保持
        `get_research_snapshot` 的语义，不伪造）。
        """
        from bottleneck_hunter.watchlist.pit_gate import assert_snapshot_visible

        snapshot = self.get_research_snapshot(snapshot_id)
        if snapshot is None:
            return ()
        assert_snapshot_visible(snapshot, decision_at=decision_at)
        return self.get_research_observations(snapshot_id)
