"""P0-4 PIT 可见性门禁、泄漏检测与缺失/降级审计测试。"""

from datetime import datetime, timedelta, timezone

import pytest

from bottleneck_hunter.watchlist.pit_gate import (
    PointInTimeViolation,
    assert_observations_visible,
    assert_snapshot_visible,
    audit_snapshot,
    gate_snapshot,
    migrate,
    migration_bypass_active,
)
from bottleneck_hunter.watchlist.research_contracts import (
    ResearchSnapshot,
    SourceObservation,
    StageInputCapture,
)
from bottleneck_hunter.watchlist.stage_snapshot import save_stage_snapshot
from bottleneck_hunter.watchlist.store import WatchlistStore

UTC = timezone.utc
# 固定基准时点，避免测试依赖当前时间。
BASE = datetime(2026, 2, 10, 0, 0, tzinfo=UTC)


def _observation(observation_id="o1", *, metric="close", value=100.0, revision=1,
                 visible_delta=timedelta(0), quality="normal", quality_notes="", ticker="AAPL"):
    return SourceObservation.model_validate({
        "observation_id": observation_id,
        "metric": metric,
        "ticker": ticker,
        "market": "us_stock",
        "value": value,
        "time": {
            "period_start": (BASE - timedelta(days=30)).isoformat(),
            "period_end": (BASE - timedelta(days=1)).isoformat(),
            "effective_at": (BASE - timedelta(days=1)).isoformat(),
            "visible_at": (BASE + visible_delta).isoformat(),
            "collected_at": (BASE + visible_delta).isoformat(),
        },
        "provenance": {
            "source": "vendor", "unit": "USD", "currency": "USD",
            "quality": quality, "quality_notes": quality_notes, "revision": revision,
        },
    })


def _snapshot(observations=(), captures=(), *, snapshot_id="s1", as_of=BASE,
              created_at=None, market="us_stock"):
    return ResearchSnapshot.model_validate({
        "snapshot_id": snapshot_id,
        "market": market,
        "strategy_version": "decision-p0-3-v1",
        "as_of": as_of.isoformat(),
        "created_at": (created_at if created_at is not None else as_of).isoformat(),
        "observations": list(observations),
        "captures": list(captures),
    })


# —— 可见性：未来数据泄漏 ——

def test_future_observation_is_rejected():
    future = _observation(visible_delta=timedelta(days=3))
    with pytest.raises(PointInTimeViolation) as exc:
        assert_observations_visible([future], BASE)
    assert exc.value.code == "future_data_leak"


def test_observation_visible_exactly_at_decision_point_is_allowed():
    assert_observations_visible([_observation()], BASE)


def test_decision_point_before_snapshot_creation_is_rejected():
    """决策时点落在快照创建之前 → 该快照当时还不存在，属于未来数据。"""
    snapshot = _snapshot(created_at=BASE + timedelta(days=1))
    with pytest.raises(PointInTimeViolation) as exc:
        assert_snapshot_visible(snapshot, decision_at=datetime(2026, 2, 10, 12, tzinfo=UTC))
    assert exc.value.code == "snapshot_not_yet_created"


def test_decision_point_at_or_after_creation_is_allowed():
    snapshot = _snapshot(created_at=BASE + timedelta(days=1))
    assert_snapshot_visible(snapshot, decision_at=BASE + timedelta(days=1))
    assert_snapshot_visible(snapshot, decision_at=BASE + timedelta(days=2))


def test_naive_decision_point_is_rejected():
    with pytest.raises(PointInTimeViolation) as exc:
        assert_snapshot_visible(_snapshot(), decision_at=datetime(2026, 2, 10))
    assert exc.value.code == "naive_time"


def test_snapshot_without_decision_point_uses_as_of():
    snapshot = _snapshot([_observation(visible_delta=timedelta(hours=1))])
    # 快照 as_of 早于观测可见时间 → 泄漏
    with pytest.raises(PointInTimeViolation, match="future_data_leak"):
        assert_snapshot_visible(snapshot)


# —— 泄漏：时间语义自相矛盾与修订倒流 ——

def test_collection_before_period_end_is_rejected():
    observation = _observation()
    payload = observation.model_dump()
    payload["time"]["collected_at"] = (BASE - timedelta(days=5)).isoformat()
    payload["time"]["visible_at"] = (BASE - timedelta(days=5)).isoformat()
    contradictory = SourceObservation.model_validate(payload)
    with pytest.raises(PointInTimeViolation) as exc:
        assert_observations_visible([contradictory], BASE)
    assert exc.value.code == "collection_before_period_end"


def test_revision_regression_is_rejected():
    observations = [
        _observation("o2", revision=2, visible_delta=timedelta(days=1)),
        _observation("o1", revision=1),
    ]
    with pytest.raises(PointInTimeViolation) as exc:
        assert_observations_visible(observations, BASE + timedelta(days=5))
    assert exc.value.code == "revision_regression"


def test_revision_time_regression_is_rejected():
    observations = [
        _observation("o1", revision=1, visible_delta=timedelta(days=2)),
        _observation("o2", revision=2, visible_delta=timedelta(days=1)),
    ]
    with pytest.raises(PointInTimeViolation) as exc:
        assert_observations_visible(observations, BASE + timedelta(days=5))
    assert exc.value.code == "revision_time_regression"


def test_monotonic_revisions_are_allowed():
    observations = [
        _observation("o1", revision=1),
        _observation("o2", revision=2, visible_delta=timedelta(days=1)),
    ]
    assert_observations_visible(observations, BASE + timedelta(days=2))


# —— 缺失：不静默填充 ——

def test_unlabelled_missing_value_is_rejected():
    snapshot = _snapshot([_observation(value=None)])
    with pytest.raises(PointInTimeViolation) as exc:
        audit_snapshot(snapshot)
    assert exc.value.code == "missing_data"


def test_labelled_missing_value_is_recorded_not_filled():
    snapshot = _snapshot([_observation(value=None, quality_notes="not_disclosed")])
    report = audit_snapshot(snapshot)
    assert report.ok
    assert report.checked_observations == 1
    # 缺失仍以 None 保留，未被任何默认值替换
    assert snapshot.observations[0].value is None


def test_unknown_missing_marker_is_rejected():
    snapshot = _snapshot([_observation(value=None, quality_notes="猜的")])
    with pytest.raises(PointInTimeViolation) as exc:
        audit_snapshot(snapshot)
    assert exc.value.code == "missing_data"
    assert "不是已知来源" in str(exc.value)


def test_missing_can_be_reported_without_raising():
    snapshot = _snapshot([_observation(value=None)])
    report = audit_snapshot(snapshot, require_complete=False)
    assert not report.ok
    assert len(report.missing) == 1
    assert report.missing[0].path == "observations[o1].value"


# —— 降级：只记录不阻断，但绝不隐藏 ——

@pytest.mark.parametrize("quality", ["degraded", "stale", "estimated", "fallback", "partial"])
def test_degraded_quality_is_reported_but_not_blocking(quality):
    snapshot = _snapshot([_observation(quality=quality)])
    report = audit_snapshot(snapshot)
    # 降级必须出现在报告里，但不阻断决策
    assert not report.ok
    assert report.usable
    assert len(report.degraded) == 1
    assert report.degraded[0].kind == "degraded"
    assert "降级" in report.summary()


def test_unsupported_quality_label_is_treated_as_degraded():
    snapshot = _snapshot([_observation(quality="something_new")])
    report = audit_snapshot(snapshot)
    assert len(report.degraded) == 1
    assert "不在受支持集合内" in report.degraded[0].detail


def test_empty_stage_capture_is_reported_as_degraded():
    capture = {
        "stage": "L1", "captured_at": BASE.isoformat(), "run_id": "r1",
        "payload_json": "{}",
    }
    snapshot = _snapshot(captures=[capture])
    report = audit_snapshot(snapshot)
    assert report.usable
    assert [item.kind for item in report.findings] == ["degraded"]
    assert report.findings[0].path.startswith("captures[0].L1")


def test_rogue_capture_payload_blocks():
    """契约本身已拒绝非对象 payload；审计层必须独立兜住绕过校验的脏数据。

    用 model_construct 模拟直接落库的历史脏行（绕过 Pydantic 校验）。
    """
    for bad_payload in ("[1,2,3]", "{not json"):
        rogue_capture = StageInputCapture.model_construct(
            stage="L4", captured_at=BASE, run_id="r1", payload_json=bad_payload,
        )
        snapshot = _snapshot([_observation()]).model_copy(update={"captures": (rogue_capture,)})
        with pytest.raises(PointInTimeViolation) as exc:
            audit_snapshot(snapshot)
        assert exc.value.code == "missing_data"


# —— 统一入口 ——

def test_gate_snapshot_combines_visibility_and_audit():
    snapshot = _snapshot([_observation()])
    report = gate_snapshot(snapshot)
    assert report.ok
    assert report.snapshot_id == "s1"
    assert report.checked_observations == 1


def test_gate_snapshot_checks_visibility_before_audit():
    snapshot = _snapshot([_observation(value=None, visible_delta=timedelta(days=3))])
    with pytest.raises(PointInTimeViolation) as exc:
        gate_snapshot(snapshot)
    assert exc.value.code == "future_data_leak"


# —— 迁移旁路：只限离线，不提供生产常开开关 ——

def test_migration_bypass_is_off_by_default():
    assert not migration_bypass_active()


def test_migration_bypass_requires_reason_and_is_scoped():
    with pytest.raises(PointInTimeViolation) as exc, migrate("  "):
        pass
    assert exc.value.code == "bypass_without_reason"
    assert not migration_bypass_active()

    with migrate("回填 2026-08 旧快照"):
        assert migration_bypass_active()
    assert not migration_bypass_active()


def test_migration_bypass_closes_on_exception():
    with pytest.raises(RuntimeError), migrate("演练"):
        raise RuntimeError("boom")
    assert not migration_bypass_active()


# —— 生产读取路径：门禁真的接上了 ——

def test_store_gated_read_rejects_future_observation(tmp_path):
    store = WatchlistStore(tmp_path / "pit.db").for_user("u").for_market("us_stock")
    snapshot = _snapshot([_observation(visible_delta=timedelta(days=3))])
    store.save_research_snapshot(snapshot)
    with pytest.raises(PointInTimeViolation) as exc:
        store.get_visible_research_observations("s1")
    assert exc.value.code == "future_data_leak"


def test_store_gated_read_returns_visible_observations(tmp_path):
    store = WatchlistStore(tmp_path / "pit.db").for_user("u").for_market("us_stock")
    store.save_research_snapshot(_snapshot([_observation()]))
    observations = store.get_visible_research_observations("s1")
    assert [item.observation_id for item in observations] == ["o1"]


def test_store_gated_read_honours_explicit_decision_point(tmp_path):
    """快照建立于 BASE，其中观测比 as_of 晚一天可见：默认时点必须拒绝，显式推后才放行。"""
    as_of = BASE
    created_at = BASE + timedelta(days=1)
    store = WatchlistStore(tmp_path / "pit.db").for_user("u").for_market("us_stock")
    store.save_research_snapshot(_snapshot(
        [_observation(visible_delta=timedelta(days=1))], as_of=as_of, created_at=created_at))
    # 以 as_of 为决策时点 → 决策时点早于快照建立，拒绝
    with pytest.raises(PointInTimeViolation, match="snapshot_not_yet_created"):
        store.get_visible_research_observations("s1")
    # 显式指定快照已存在且观测已可见的时点 → 放行
    later = store.get_visible_research_observations("s1", decision_at=created_at + timedelta(days=1))
    assert len(later) == 1
    assert later[0].observation_id == "o1"


def test_store_gated_read_missing_snapshot_returns_empty(tmp_path):
    store = WatchlistStore(tmp_path / "pit.db").for_user("u").for_market("us_stock")
    assert store.get_visible_research_observations("nope") == ()


def test_saved_stage_snapshot_passes_gate(tmp_path):
    """生产写入路径（save_stage_snapshot）产出的快照必须天然通过 P0-4 门禁。"""
    store = WatchlistStore(tmp_path / "pit.db").for_user("u").for_market("us_stock")
    binding = save_stage_snapshot(store, "L1", {"macro_score": 1.5})
    snapshot = store.get_research_snapshot(binding["snapshot_id"])
    assert snapshot is not None
    report = gate_snapshot(snapshot)
    assert report.ok
    assert report.checked_captures == 1
