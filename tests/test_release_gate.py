"""P2-4 发布质量门禁 / 迁移演练 / 回滚预案 / 验收报表 专项测试。"""

from __future__ import annotations

import pytest

from bottleneck_hunter.watchlist.release_gate import (
    GateCheck,
    RollbackPlan,
    RollbackStep,
    acceptance_report,
    build_rollback_plan,
    evaluate_release,
    migration_drill,
    render_checklist,
    validate_rollback_plan,
)

# --------------------------------------------------------------------------- 质量门禁


def test_all_pass_releases():
    d = evaluate_release([GateCheck("t", "pass"), GateCheck("r", "pass")])
    assert d.verdict == "release" and d.released and d.blocking_failures == ()


def test_blocking_failure_blocks():
    d = evaluate_release([GateCheck("full_tests", "fail", True, "3 failed"), GateCheck("r", "pass")])
    assert d.verdict == "block" and not d.released
    assert d.blocking_failures == ("full_tests",)


def test_non_blocking_failure_does_not_block_but_warns():
    d = evaluate_release([GateCheck("tests", "pass"), GateCheck("coverage", "fail", False)])
    assert d.verdict == "release" and d.warnings == ("coverage",)


def test_warn_is_reported_not_blocking():
    d = evaluate_release([GateCheck("tests", "pass"), GateCheck("docs", "warn", True)])
    assert d.released and d.warnings == ("docs",)


def test_skip_is_neither_block_nor_warn():
    d = evaluate_release([GateCheck("tests", "pass"), GateCheck("migration", "skip", True)])
    assert d.released and d.warnings == ()


def test_multiple_blocking_failures_all_listed():
    d = evaluate_release([GateCheck("a", "fail"), GateCheck("b", "fail"), GateCheck("c", "pass")])
    assert d.blocking_failures == ("a", "b") and d.verdict == "block"


def test_empty_checks_releases():
    d = evaluate_release([])
    assert d.verdict == "release" and d.checks == ()


def test_decision_and_check_as_dict():
    d = evaluate_release([GateCheck("t", "pass", True, "ok")])
    dd = d.as_dict()
    assert dd["verdict"] == "release" and dd["checks"][0]["name"] == "t"
    assert GateCheck("x", "pass").as_dict()["status"] == "pass"


# --------------------------------------------------------------------------- 迁移演练


def test_drill_idempotent_create():
    r = migration_drill(["CREATE TABLE IF NOT EXISTS t (id INTEGER PRIMARY KEY);"])
    assert r.ok and r.idempotent and r.total == 1 and r.applied == 1
    assert r.as_check().status == "pass" and not r.errors


def test_drill_alter_add_column_not_idempotent():
    r = migration_drill(
        ["ALTER TABLE base ADD COLUMN c TEXT;"],
        setup=["CREATE TABLE base (id INTEGER);"],
    )
    assert r.ok and not r.idempotent
    c = r.as_check()
    assert c.status == "warn" and "非幂等" in c.detail


def test_drill_bad_sql_fails_and_blocks():
    r = migration_drill(["CREATE TABLE ("])
    assert not r.ok and r.applied == 0
    c = r.as_check()
    assert c.status == "fail" and c.blocking


def test_drill_empty_is_skip():
    r = migration_drill([])
    assert r.total == 0 and r.as_check().status == "skip"


def test_drill_ignores_blank_statements():
    r = migration_drill(["", "   ", "CREATE TABLE IF NOT EXISTS a (id INTEGER);"])
    assert r.total == 1 and r.ok


def test_drill_multiple_statements_all_applied():
    r = migration_drill([
        "CREATE TABLE IF NOT EXISTS a (id INTEGER);",
        "CREATE TABLE IF NOT EXISTS b (id INTEGER);",
        "CREATE INDEX IF NOT EXISTS ix_b ON b(id);",
    ])
    assert r.applied == 3 and r.ok and r.idempotent


def test_drill_uses_only_memory_db(tmp_path):
    # 演练绝不落地任何文件：跑一次后临时目录仍为空
    migration_drill(["CREATE TABLE IF NOT EXISTS t (id INTEGER);"])
    assert list(tmp_path.iterdir()) == []


# --------------------------------------------------------------------------- 回滚预案


def test_rollback_commit_mode_has_checkout_rebuild_health_verify():
    plan = build_rollback_plan(from_rev="aaa", to_rev="bbb", reason="healthz fail")
    cmds = " ".join(plan.commands())
    assert "git checkout bbb" in cmds
    assert "--build" in cmds
    assert "/healthz" in cmds
    assert any("证真" in s.action for s in plan.steps)
    assert plan.to_rev == "bbb"


def test_rollback_image_mode_no_rebuild():
    plan = build_rollback_plan(from_rev="aaa", image_tag="bottleneck-hunter:stable")
    cmds = " ".join(plan.commands())
    assert "bottleneck-hunter:stable" in cmds
    assert "--build" not in cmds
    assert "/healthz" in cmds
    assert plan.to_rev == "bottleneck-hunter:stable"


def test_rollback_requires_target():
    with pytest.raises(ValueError):
        build_rollback_plan(from_rev="aaa")


def test_rollback_steps_contiguously_ordered():
    plan = build_rollback_plan(from_rev="aaa", to_rev="bbb")
    assert [s.order for s in plan.steps] == list(range(1, len(plan.steps) + 1))


def test_rollback_custom_port_and_app():
    plan = build_rollback_plan(from_rev="a", to_rev="b", host_port=9000, app="myapp")
    cmds = " ".join(plan.commands())
    assert "HOST_PORT=9000" in cmds and "myapp" in cmds


def test_rollback_plan_deterministic():
    assert build_rollback_plan(from_rev="a", to_rev="b").as_dict() == \
        build_rollback_plan(from_rev="a", to_rev="b").as_dict()


# --------------------------------------------------------------------------- 回滚演练


def test_validate_good_plan_passes():
    plan = build_rollback_plan(from_rev="aaa", to_rev="bbb")
    assert validate_rollback_plan(plan).status == "pass"


def test_validate_same_rev_fails():
    plan = build_rollback_plan(from_rev="same", to_rev="same")
    c = validate_rollback_plan(plan)
    assert c.status == "fail" and "相同" in c.detail


def test_validate_missing_healthz_and_verify_fails():
    broken = RollbackPlan("r", "a", "b", (RollbackStep(1, "只做一步", "echo hi", ""),))
    c = validate_rollback_plan(broken)
    assert c.status == "fail"
    assert "健康检查" in c.detail and "证真" in c.detail and "无验证点" in c.detail


def test_validate_non_contiguous_order_fails():
    steps = (
        RollbackStep(1, "健康检查", "curl /healthz", "200"),
        RollbackStep(3, "证真", "git rev-parse HEAD", "ok"),
    )
    c = validate_rollback_plan(RollbackPlan("r", "a", "b", steps))
    assert c.status == "fail" and "序号不连续" in c.detail


def test_validate_empty_steps_fails():
    c = validate_rollback_plan(RollbackPlan("r", "a", "b", ()))
    assert c.status == "fail" and "步骤为空" in c.detail


# --------------------------------------------------------------------------- 验收报表


def test_acceptance_report_embeds_all_parts():
    plan = build_rollback_plan(from_rev="aaa", to_rev="bbb")
    drill = migration_drill(["CREATE TABLE IF NOT EXISTS t (id INTEGER);"])
    decision = evaluate_release([GateCheck("tests", "pass"), drill.as_check(), validate_rollback_plan(plan)])
    rep = acceptance_report(
        decision, rollback=plan, drill=drill,
        audit_summary={"total": {"calls": 2}}, meta={"release": "P2-4"},
    )
    assert rep["released"] and rep["verdict"] == "release"
    assert rep["rollback"]["to_rev"] == "bbb"
    assert rep["migration_drill"]["ok"] is True
    assert rep["audit_summary"]["total"]["calls"] == 2
    assert rep["meta"]["release"] == "P2-4"


def test_acceptance_report_optional_parts_none():
    rep = acceptance_report(evaluate_release([GateCheck("t", "pass")]))
    assert rep["rollback"] is None and rep["migration_drill"] is None
    assert rep["audit_summary"] is None and rep["meta"] == {}


def test_acceptance_report_block_verdict():
    rep = acceptance_report(evaluate_release([GateCheck("tests", "fail", True)]))
    assert rep["verdict"] == "block" and not rep["released"]
    assert rep["blocking_failures"] == ["tests"]


def test_render_checklist_release():
    plan = build_rollback_plan(from_rev="aaa", to_rev="bbb")
    d = evaluate_release([GateCheck("full_tests", "pass", True, "1999 passed")])
    text = render_checklist(d, rollback=plan)
    assert "裁决：放行" in text and "✅ full_tests" in text and "↩ 回滚预案" in text


def test_render_checklist_block_lists_failures():
    d = evaluate_release([GateCheck("full_tests", "fail", True, "3 failed")])
    text = render_checklist(d)
    assert "裁决：阻断" in text and "❌ full_tests" in text and "阻断项：full_tests" in text


def test_render_checklist_marks_non_blocking():
    d = evaluate_release([GateCheck("coverage", "fail", False, "略降")])
    assert "（非阻断）" in render_checklist(d)
