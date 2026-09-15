"""P0-4 Point-in-Time 可见性门禁、未来数据泄漏检测与缺失/降级审计。

三条边界：
1. 可见性：决策时点不可见的观测一律拒绝，绝不回退到采集时间推断历史可见性。
2. 泄漏：修订号倒流、可见时间晚于研究时点、采集时间早于所属期结束均视为泄漏。
3. 缺失：缺失值必须显式标记来源，降级必须带质量标签；两者都不静默填充。
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from bottleneck_hunter.watchlist.research_contracts import ResearchSnapshot, SourceObservation

# 缺失值允许的显式标记；默认值填充不在此列，因为"默认"本身不是来源。
MISSING_SOURCES = ("not_available", "not_disclosed", "provider_gap")
# 需要随行记录来源的降级质量标签；normal 为唯一非降级状态。
DEGRADED_QUALITIES = ("degraded", "stale", "estimated", "fallback", "partial")


class PointInTimeViolation(ValueError):
    """PIT 门禁拒绝；code 用于测试与审计报表的稳定断言。"""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"[{code}] {message}")


@dataclass(frozen=True)
class MissingField:
    """缺失或降级的单个字段；path 定位到快照内的具体位置。"""

    path: str
    kind: Literal["missing", "degraded"]
    detail: str


@dataclass(frozen=True)
class AuditReport:
    """一次缺失/降级审计的结果。"""

    snapshot_id: str
    as_of: datetime
    findings: tuple[MissingField, ...] = ()
    checked_observations: int = 0
    checked_captures: int = 0

    @property
    def missing(self) -> tuple[MissingField, ...]:
        return tuple(item for item in self.findings if item.kind == "missing")

    @property
    def degraded(self) -> tuple[MissingField, ...]:
        return tuple(item for item in self.findings if item.kind == "degraded")

    @property
    def ok(self) -> bool:
        """无任何缺失或降级。降级不阻断决策，但必须让调用方看见。"""
        return not self.findings

    @property
    def usable(self) -> bool:
        """可否继续决策：无缺失即可；降级允许带标记放行。"""
        return not self.missing

    def summary(self) -> str:
        if self.ok:
            return f"快照 {self.snapshot_id} 在 {self.as_of.isoformat()} 无缺失或降级"
        parts = []
        if self.missing:
            parts.append(f"缺失 {len(self.missing)} 项")
        if self.degraded:
            parts.append(f"降级 {len(self.degraded)} 项")
        return f"快照 {self.snapshot_id}：" + "，".join(parts)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise PointInTimeViolation("naive_time", "决策时点必须带时区，禁止用本地时间解释历史可见性")
    return value.astimezone(timezone.utc)


def _coerce_decision_point(snapshot: ResearchSnapshot, decision_at: datetime | None) -> datetime:
    """未显式给出决策时点时，以快照 as_of 作为研究时点。"""
    return _as_utc(decision_at) if decision_at is not None else _as_utc(snapshot.as_of)


def assert_observations_visible(observations: Iterable[SourceObservation], decision_at: datetime,
                                *, snapshot_id: str = "") -> None:
    """拒绝决策时点尚不可见的观测；这是防未来数据泄漏的唯一入口。

    可见性返回后顺序必须保持：修订单调性检查依赖调用方传入的原始顺序（ordinal）。
    """
    cutoff = _as_utc(decision_at)
    ordered = list(observations)
    for observation in ordered:
        visible_at = _as_utc(observation.time.visible_at)
        if visible_at > cutoff:
            raise PointInTimeViolation(
                "future_data_leak",
                f"观测 {observation.observation_id} 在 {cutoff.isoformat()} 尚不可见"
                f"（visible_at={visible_at.isoformat()}），禁止用于该时点的决策或回测",
            )
        collected_at = _as_utc(observation.time.collected_at)
        if collected_at < _as_utc(observation.time.period_end):
            raise PointInTimeViolation(
                "collection_before_period_end",
                f"观测 {observation.observation_id} 在所属期结束前即被采集，时间语义自相矛盾",
            )
        if collected_at < visible_at:
            raise PointInTimeViolation(
                "collected_before_visible",
                f"观测 {observation.observation_id} 采集时间早于可见时间，无法证明可见性来源",
            )
    _assert_revisions_monotonic(ordered)


def assert_snapshot_visible(snapshot: ResearchSnapshot, *, decision_at: datetime | None = None) -> None:
    """快照整体门禁：决策时点必须落在快照存在之后，来源观测必须在决策时点可见。"""
    cutoff = _coerce_decision_point(snapshot, decision_at)
    created_at = _as_utc(snapshot.created_at)
    if cutoff < created_at:
        raise PointInTimeViolation(
            "snapshot_not_yet_created",
            f"决策时点 {cutoff.isoformat()} 早于快照创建时间 {created_at.isoformat()}，"
            "当时该快照尚不存在，用它就是拿未来的数据做过去的决策",
        )
    assert_observations_visible(snapshot.observations, cutoff, snapshot_id=snapshot.snapshot_id)


def _assert_revisions_monotonic(observations: Iterable[SourceObservation]) -> None:
    """同一 ticker+指标的修订必须随可见时间单调递增；倒流说明历史被事后改写。

    逐条与"已接受的最新修订"比较，而不是全集合取极值，否则后出现的低修订号会被掩盖。
    """
    latest: dict[tuple[str, str], tuple[int, datetime, str]] = {}
    for observation in observations:
        key = (observation.ticker, observation.metric)
        revision = observation.provenance.revision
        visible_at = _as_utc(observation.time.visible_at)
        previous = latest.get(key)
        if previous is None:
            latest[key] = (revision, visible_at, observation.observation_id)
            continue
        prev_revision, prev_visible, prev_id = previous
        if revision < prev_revision:
            raise PointInTimeViolation(
                "revision_regression",
                f"{observation.ticker}/{observation.metric} 修订号 {revision} 低于 "
                f"{prev_id} 的 {prev_revision}，历史被事后改写",
            )
        if revision == prev_revision:
            if visible_at != prev_visible:
                raise PointInTimeViolation(
                    "revision_visible_conflict",
                    f"{observation.ticker}/{observation.metric} 同修订号 {revision} 出现不同可见时间，"
                    "修订必须使用新 ID",
                )
            continue
        if visible_at < prev_visible:
            raise PointInTimeViolation(
                "revision_time_regression",
                f"{observation.ticker}/{observation.metric} 修订 {revision} 的可见时间早于 "
                f"修订 {prev_revision}，时间线非单调",
            )
        latest[key] = (revision, visible_at, observation.observation_id)


def _collect_observation_findings(observations: Iterable[SourceObservation]) -> list[MissingField]:
    findings: list[MissingField] = []
    for observation in observations:
        base = f"observations[{observation.observation_id}]"
        if observation.value is None:
            note = observation.provenance.quality_notes.strip()
            if not note:
                findings.append(MissingField(
                    path=f"{base}.value", kind="missing",
                    detail=f"{observation.metric} 缺失且未标记来源，禁止用默认值填充",
                ))
            elif not any(marker in note for marker in MISSING_SOURCES):
                findings.append(MissingField(
                    path=f"{base}.value", kind="missing",
                    detail=f"{observation.metric} 缺失标记 {note!r} 不是已知来源 {MISSING_SOURCES}",
                ))
        quality = observation.provenance.quality
        if quality in DEGRADED_QUALITIES:
            findings.append(MissingField(
                path=f"{base}.provenance.quality", kind="degraded",
                detail=f"{observation.metric} 数据为 {quality}（来源 {observation.provenance.source}）",
            ))
        elif quality != "normal":
            findings.append(MissingField(
                path=f"{base}.provenance.quality", kind="degraded",
                detail=f"质量标签 {quality!r} 不在受支持集合内，按降级处理",
            ))
    return findings


def _collect_capture_findings(snapshot: ResearchSnapshot) -> list[MissingField]:
    """阶段捕获审计。

    空 payload 记为降级而非缺失：阶段输入为空是采集期的上下文缺口，历史快照本就如此，
    阻断它只会让所有旧记录无法审计。结构损坏（非 JSON / 非对象）记为缺失：那说明写入
    边界被绕过，必须拦住。
    """
    findings: list[MissingField] = []
    for index, capture in enumerate(snapshot.captures):
        base = f"captures[{index}].{capture.stage}"
        try:
            payload = json.loads(capture.payload_json)
        except json.JSONDecodeError:
            findings.append(MissingField(path=f"{base}.payload_json", kind="missing",
                                         detail=f"{capture.stage} 阶段输入不是合法 JSON，输入封存被破坏"))
            continue
        if not isinstance(payload, dict):
            findings.append(MissingField(path=f"{base}.payload_json", kind="missing",
                                         detail=f"{capture.stage} 阶段输入不是 JSON 对象，输入封存被破坏"))
            continue
        if not payload:
            findings.append(MissingField(path=f"{base}.payload_json", kind="degraded",
                                         detail=f"{capture.stage} 阶段输入为空，决策上下文不完整"))
    return findings


def audit_snapshot(snapshot: ResearchSnapshot, *, decision_at: datetime | None = None,
                   require_complete: bool = True) -> AuditReport:
    """审计缺失与降级；require_complete 为真时缺失即拒绝，降级始终只记录不阻断。"""
    cutoff = _coerce_decision_point(snapshot, decision_at)
    findings = _collect_observation_findings(snapshot.observations) + _collect_capture_findings(snapshot)
    report = AuditReport(
        snapshot_id=snapshot.snapshot_id,
        as_of=cutoff,
        findings=tuple(findings),
        checked_observations=len(snapshot.observations),
        checked_captures=len(snapshot.captures),
    )
    if require_complete and report.missing:
        detail = "；".join(item.detail for item in report.missing)
        raise PointInTimeViolation("missing_data", f"决策输入存在未标记缺失：{detail}")
    return report


def gate_snapshot(snapshot: ResearchSnapshot, *, decision_at: datetime | None = None,
                  require_complete: bool = True) -> AuditReport:
    """P0-4 统一入口：先过可见性与泄漏门禁，再做缺失/降级审计。"""
    assert_snapshot_visible(snapshot, decision_at=decision_at)
    return audit_snapshot(snapshot, decision_at=decision_at, require_complete=require_complete)


class _MigrationBypass:
    """离线迁移专用旁路；必须显式进入上下文，且不提供生产常开开关。"""

    def __init__(self) -> None:
        self._reason: str | None = None

    def open(self, reason: str) -> None:
        if not isinstance(reason, str) or not reason.strip():
            raise PointInTimeViolation("bypass_without_reason", "关闭 PIT 门禁必须写明离线迁移原因")
        self._reason = reason

    def close(self) -> None:
        self._reason = None

    @property
    def active(self) -> bool:
        return self._reason is not None

    @property
    def reason(self) -> str | None:
        return self._reason


_MIGRATION = _MigrationBypass()


class migrate:
    """`with migrate("回填 2026-08 旧快照"):` — 仅限离线迁移，生产链路不得进入。"""

    def __init__(self, reason: str):
        self.reason = reason

    def __enter__(self) -> None:
        _MIGRATION.open(self.reason)
        return None

    def __exit__(self, exc_type, exc, tb) -> Literal[False]:
        _MIGRATION.close()
        return False


def migration_bypass_active() -> bool:
    return _MIGRATION.active
