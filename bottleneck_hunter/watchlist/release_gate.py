"""发布质量门禁、迁移演练、回滚预案与验收报表（P2-4）。

把「一次上线」的四件事收敛成可判定、可复现的纯函数，直接回答 P2-4 验收
「门禁失败阻止发布，回滚步骤经演练，使用上一稳定镜像/提交」：

- **质量门禁**：`evaluate_release` 汇总各门禁项（全量测试/Ruff/迁移演练/覆盖率…），
  任一**阻断项**失败即裁决 `block`，绝不放行；非阻断失败与告警项照实呈现，不静默吞掉。
- **迁移演练**：`migration_drill` 把迁移 SQL 在**内存 SQLite** 上真跑两遍——首遍验能否干净应用，
  次遍验可否幂等重跑（`ALTER ADD COLUMN` 这类非幂等语句会被抓出），不碰任何生产库。
- **回滚预案**：`build_rollback_plan` 按 `deploy.sh` 的真实机制（备份→检出/换镜像→重建→healthz）
  生成确定的有序步骤，落实「使用上一稳定镜像/提交」；`validate_rollback_plan` 演练该预案的完备性
  （每步可验证、含健康检查、含容器内证真——防「git reset/restart 不上线代码」的老坑）。
- **验收报表**：`acceptance_report` / `render_checklist` 把门禁裁决 + 迁移演练 + 回滚预案 +
  （调用方传入的）`llm_audit.summarize` 审计汇总拼成结构化报表与人读验收清单。

与既有能力的分工（互补，不替代）：
- `deploy.sh` 是**实际执行**的一键部署脚本；本模块是**发布前的判定与预案**，产出「是否放行」「如何回滚」，
  不执行部署、不 SSH、不碰生产库。
- `quality_gate.py` 是**运行期决策质量**门禁（数据新鲜度/持仓一致性，SSE），面向单次决策；
  本模块是**发布/部署期**门禁，面向一次上线——不同域，不重叠。

纯 stdlib（迁移演练用内建 sqlite3 内存库），确定可复现（给定输入必得同一裁决/预案，逻辑不读 wall-clock）；
**不接线进生产部署链**（回退=不调用，`deploy.sh` 与现有流程完全不变）。默认端口/compose 命令是
校准旋钮，随部署拓扑调整。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import asdict, dataclass

# ---------------------------------------------------------------------------
# 质量门禁：门禁项聚合与放行/阻断裁决
# ---------------------------------------------------------------------------

# 门禁项状态：pass 通过 / fail 失败 / warn 告警(不阻断) / skip 未运行(不阻断)
_ICON = {"pass": "✅", "fail": "❌", "warn": "⚠️", "skip": "⬜"}


@dataclass(frozen=True)
class GateCheck:
    """单个门禁项结论。blocking=True 时该项 fail 直接阻断发布。"""

    name: str
    status: str            # pass | fail | warn | skip
    blocking: bool = True
    detail: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class ReleaseDecision:
    """整次发布的门禁裁决。"""

    verdict: str                          # release | block
    checks: tuple[GateCheck, ...]
    blocking_failures: tuple[str, ...]    # 导致阻断的门禁项名
    warnings: tuple[str, ...]             # 告警 + 非阻断失败项名

    @property
    def released(self) -> bool:
        return self.verdict == "release"

    def as_dict(self) -> dict:
        return asdict(self)


def evaluate_release(checks: Sequence[GateCheck]) -> ReleaseDecision:
    """汇总门禁项：任一阻断项失败即 block；非阻断失败与告警照实上报但不阻断。"""
    checks = tuple(checks)
    blocking_failures = tuple(c.name for c in checks if c.status == "fail" and c.blocking)
    warnings = tuple(
        c.name for c in checks
        if c.status == "warn" or (c.status == "fail" and not c.blocking)
    )
    verdict = "block" if blocking_failures else "release"
    return ReleaseDecision(verdict, checks, blocking_failures, warnings)


# ---------------------------------------------------------------------------
# 迁移演练：内存 SQLite 真跑两遍，验应用 + 幂等
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MigrationDrillResult:
    """迁移演练结果。ok=首遍全部干净应用；idempotent=次遍可安全重跑。"""

    ok: bool
    idempotent: bool
    total: int
    applied: int
    errors: tuple[str, ...]

    def as_dict(self) -> dict:
        return asdict(self)

    def as_check(self) -> GateCheck:
        """转成一个门禁项：应用失败=阻断 fail；能应用但非幂等=warn；无语句=skip。"""
        if self.total == 0:
            return GateCheck("migration_drill", "skip", True, "无迁移语句，跳过演练")
        if not self.ok:
            return GateCheck("migration_drill", "fail", True, f"迁移演练失败：{'；'.join(self.errors)}")
        if not self.idempotent:
            return GateCheck("migration_drill", "warn", True, f"迁移非幂等（重跑将报错）：{'；'.join(self.errors)}")
        return GateCheck("migration_drill", "pass", True, f"迁移演练通过：{self.applied} 条应用且可幂等重跑")


def _first_line(sql: str) -> str:
    for line in sql.splitlines():
        s = line.strip()
        if s and not s.startswith("--"):
            return s[:80]
    return sql.strip()[:80]


def migration_drill(
    statements: Sequence[str],
    *,
    setup: Sequence[str] = (),
) -> MigrationDrillResult:
    """在内存 SQLite 上把迁移 SQL 跑两遍，验能否干净应用且可否幂等重跑。

    setup：前置语句（如 CREATE_TABLES），仅先应用一次以建立前置表，不计入幂等判定。
    statements：待演练的迁移语句，逐条 executescript；首遍验应用，次遍验幂等（重跑不报错）。
    绝不连接任何文件库——只用 `:memory:`，与生产数据完全隔离。
    """
    stmts = [s for s in statements if s and s.strip()]
    errors: list[str] = []
    applied = 0
    idempotent = True
    conn = sqlite3.connect(":memory:")
    try:
        for s in setup:
            if s and s.strip():
                conn.executescript(s)
        for s in stmts:                          # 首遍：能否干净应用
            try:
                conn.executescript(s)
                applied += 1
            except sqlite3.Error as e:
                errors.append(f"首次应用失败: {_first_line(s)} → {e}")
        first_ok = applied == len(stmts)
        if first_ok:
            for s in stmts:                      # 次遍：能否幂等重跑
                try:
                    conn.executescript(s)
                except sqlite3.Error as e:
                    idempotent = False
                    errors.append(f"重复应用非幂等: {_first_line(s)} → {e}")
    finally:
        conn.close()
    return MigrationDrillResult(
        ok=(applied == len(stmts)),
        idempotent=idempotent,
        total=len(stmts),
        applied=applied,
        errors=tuple(errors),
    )


# ---------------------------------------------------------------------------
# 回滚预案：按 deploy.sh 机制生成确定步骤 + 演练完备性
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RollbackStep:
    """回滚预案中的一步：动作 + 可执行命令 + 验证点。"""

    order: int
    action: str
    command: str
    verify: str

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class RollbackPlan:
    """一次回滚的完整预案（使用上一稳定提交或镜像）。"""

    reason: str
    from_rev: str
    to_rev: str
    steps: tuple[RollbackStep, ...]

    def as_dict(self) -> dict:
        return asdict(self)

    def commands(self) -> tuple[str, ...]:
        return tuple(s.command for s in self.steps)


def build_rollback_plan(
    *,
    from_rev: str,
    to_rev: str = "",
    image_tag: str = "",
    reason: str = "",
    compose: str = "docker compose -f docker-compose.yml -f docker-compose.observability.yml",
    app: str = "bottleneck-hunter",
    host_port: int = 8089,
    health_path: str = "/healthz",
    data_dir: str = "data",
    verify_grep: str = "",
) -> RollbackPlan:
    """生成确定的有序回滚步骤，落实「使用上一稳定镜像/提交」。

    必须给出 `to_rev`（上一稳定提交）或 `image_tag`（上一稳定镜像）之一，否则拒绝——绝不生成空目标预案。
    步骤对齐 `deploy.sh`：备份数据→检出提交并重建 / 或切回稳定镜像→健康检查→**容器内证真**。
    容器内证真步骤落实教训「容器烘焙源码，git reset/restart 不上线代码，必须重建 + 容器内核对版本」。
    命令仅为可执行文本，本函数不执行任何部署动作。
    """
    if not (to_rev or image_tag):
        raise ValueError("回滚必须指定上一稳定提交 to_rev 或稳定镜像 image_tag（使用上一稳定镜像/提交）")

    raw: list[tuple[str, str, str]] = [
        ("记录当前版本以备二次回滚", f"git rev-parse HEAD  # 期望 {from_rev}", "输出与 from_rev 一致"),
        ("备份当前数据快照", f"cp -r {data_dir} backups/rollback_$(date +%Y%m%d_%H%M%S)", "备份目录已生成"),
    ]
    if image_tag:
        raw.append((
            "切回上一稳定镜像（镜像已烘焙代码）",
            f"HOST_PORT={host_port} {compose} up -d {app}  # 使用镜像 {image_tag}",
            "容器以稳定镜像启动",
        ))
    else:
        raw.append(("检出上一稳定提交", f"git checkout {to_rev}", f"HEAD 指向 {to_rev}"))
        raw.append((
            "重建并启动容器（源码烘焙进镜像，禁止仅 restart）",
            f"HOST_PORT={host_port} {compose} up -d --build {app}",
            "镜像重建且容器运行",
        ))
    raw.append(("健康检查", f"curl -fsS http://127.0.0.1:{host_port}{health_path}", "HTTP 200"))
    raw.append((
        "容器内证真回滚版本（git reset/restart 不上线代码）",
        verify_grep or f"docker exec {app} git rev-parse HEAD",
        f"容器内版本为 {to_rev or image_tag}",
    ))
    steps = tuple(RollbackStep(i, a, c, v) for i, (a, c, v) in enumerate(raw, 1))
    return RollbackPlan(reason=reason, from_rev=from_rev, to_rev=to_rev or image_tag, steps=steps)


def validate_rollback_plan(plan: RollbackPlan) -> GateCheck:
    """演练回滚预案的完备性（不实际执行），把它变成一个可判定门禁项。

    校验：目标非空且不同于当前、步骤序号连续、每步都有验证点、含健康检查、含容器内证真。
    """
    problems: list[str] = []
    if not plan.to_rev:
        problems.append("未指定回滚目标（上一稳定提交/镜像）")
    if plan.to_rev and plan.to_rev == plan.from_rev:
        problems.append("回滚目标与当前版本相同")
    if not plan.steps:
        problems.append("回滚步骤为空")
    orders = [s.order for s in plan.steps]
    if orders != list(range(1, len(orders) + 1)):
        problems.append("回滚步骤序号不连续")
    if any(not s.verify.strip() for s in plan.steps):
        problems.append("存在无验证点的步骤（回滚每步须可验证）")
    if not any("healthz" in s.command.lower() or "健康" in s.action for s in plan.steps):
        problems.append("缺少健康检查步骤")
    if not any(
        "证真" in s.action or "grep" in s.command.lower() or "rev-parse" in s.command.lower()
        for s in plan.steps
    ):
        problems.append("缺少容器内证真步骤（防 git reset/restart 不上线代码）")
    ok = not problems
    return GateCheck(
        "rollback_drill", "pass" if ok else "fail", True,
        "回滚预案演练通过" if ok else "；".join(problems),
    )


# ---------------------------------------------------------------------------
# 验收报表 / 验收清单
# ---------------------------------------------------------------------------


def acceptance_report(
    decision: ReleaseDecision,
    *,
    rollback: RollbackPlan | None = None,
    drill: MigrationDrillResult | None = None,
    audit_summary: dict | None = None,
    meta: dict | None = None,
) -> dict:
    """把门禁裁决 + 迁移演练 + 回滚预案 + 审计汇总拼成结构化验收报表。

    audit_summary：调用方传入的 `llm_audit.summarize(...)` 输出（本模块不 import，保持解耦）。
    meta：调用方补充的上下文（版本、提交、时间戳等），本函数不自造时间戳以保持确定可复现。
    """
    return {
        "verdict": decision.verdict,
        "released": decision.released,
        "checks": [c.as_dict() for c in decision.checks],
        "blocking_failures": list(decision.blocking_failures),
        "warnings": list(decision.warnings),
        "migration_drill": drill.as_dict() if drill else None,
        "rollback": rollback.as_dict() if rollback else None,
        "audit_summary": audit_summary,
        "meta": dict(meta or {}),
    }


def render_checklist(decision: ReleaseDecision, *, rollback: RollbackPlan | None = None) -> str:
    """把裁决渲染成人读验收清单（✅/❌/⚠️/⬜ 逐项）。"""
    head = "放行" if decision.released else "阻断"
    lines = [f"发布验收清单（裁决：{head}）"]
    for c in decision.checks:
        tag = "" if c.blocking else "（非阻断）"
        lines.append(f"{_ICON.get(c.status, '?')} {c.name}{tag}：{c.detail or c.status}")
    if decision.blocking_failures:
        lines.append(f"阻断项：{', '.join(decision.blocking_failures)}")
    if rollback:
        lines.append(f"↩ 回滚预案：{rollback.from_rev} → {rollback.to_rev}（{len(rollback.steps)} 步，经演练）")
    return "\n".join(lines)


if __name__ == "__main__":
    # ponytail 自检：门禁裁决 + 迁移演练(应用/幂等/失败/空) + 回滚预案(提交/镜像/演练) + 报表 + 确定性
    from dataclasses import FrozenInstanceError

    # 门禁：全通过 → 放行
    d_ok = evaluate_release([
        GateCheck("full_tests", "pass", True, "1999 passed"),
        GateCheck("ruff", "pass", True, "All checks passed"),
    ])
    assert d_ok.verdict == "release" and d_ok.released and not d_ok.blocking_failures

    # 门禁：阻断项失败 → 阻断发布
    d_block = evaluate_release([
        GateCheck("full_tests", "fail", True, "3 failed"),
        GateCheck("ruff", "pass", True),
    ])
    assert d_block.verdict == "block" and d_block.blocking_failures == ("full_tests",)

    # 门禁：非阻断失败 + 告警 → 仍放行但上报
    d_warn = evaluate_release([
        GateCheck("full_tests", "pass", True),
        GateCheck("coverage", "fail", False, "覆盖率略降"),
        GateCheck("lint_docs", "warn", True, "文档待补"),
    ])
    assert d_warn.verdict == "release" and set(d_warn.warnings) == {"coverage", "lint_docs"}

    # 迁移演练：幂等建表跑两遍干净 → ok + 幂等 + pass
    idem = ["CREATE TABLE IF NOT EXISTS t1 (id INTEGER PRIMARY KEY, v TEXT);"]
    r_idem = migration_drill(idem)
    assert r_idem.ok and r_idem.idempotent and r_idem.applied == 1
    assert r_idem.as_check().status == "pass"

    # 迁移演练：ALTER ADD COLUMN 首遍成功、次遍重复列报错 → ok 但非幂等 → warn
    r_alter = migration_drill(
        ["ALTER TABLE base ADD COLUMN c1 TEXT;"],
        setup=["CREATE TABLE base (id INTEGER);"],
    )
    assert r_alter.ok and not r_alter.idempotent
    assert r_alter.as_check().status == "warn" and r_alter.errors

    # 迁移演练：坏 SQL 首遍即失败 → not ok → 阻断 fail
    r_bad = migration_drill(["CREATE TABLE ("])
    assert not r_bad.ok and r_bad.as_check().status == "fail" and r_bad.as_check().blocking

    # 迁移演练：空语句 → skip
    assert migration_drill([]).as_check().status == "skip"

    # 回滚预案（提交模式）：含检出 + 重建 + 健康检查 + 容器内证真
    plan = build_rollback_plan(from_rev="a7d7cca", to_rev="e4c708b", reason="健康检查失败")
    assert plan.to_rev == "e4c708b" and plan.from_rev != plan.to_rev
    cmds = " ".join(plan.commands())
    assert "git checkout e4c708b" in cmds and "--build" in cmds and "/healthz" in cmds
    assert any("证真" in s.action for s in plan.steps)
    assert [s.order for s in plan.steps] == list(range(1, len(plan.steps) + 1))
    assert validate_rollback_plan(plan).status == "pass"

    # 回滚预案（镜像模式）：切回稳定镜像，不 --build，仍有健康检查与证真
    plan_img = build_rollback_plan(from_rev="deadbeef", image_tag="bottleneck-hunter:stable")
    cmds_img = " ".join(plan_img.commands())
    assert "bottleneck-hunter:stable" in cmds_img and "--build" not in cmds_img and "/healthz" in cmds_img
    assert plan_img.to_rev == "bottleneck-hunter:stable"
    assert validate_rollback_plan(plan_img).status == "pass"

    # 回滚预案：无目标直接拒绝，绝不生成空目标预案
    try:
        build_rollback_plan(from_rev="x")
        raise AssertionError("缺回滚目标应抛错")
    except ValueError:
        pass

    # 回滚演练：残缺预案（无健康检查/无证真/无验证点）被判 fail
    broken = RollbackPlan("r", "a", "b", (RollbackStep(1, "只做一步", "echo hi", ""),))
    vc = validate_rollback_plan(broken)
    assert vc.status == "fail" and "健康检查" in vc.detail and "证真" in vc.detail

    # 端到端验收报表：门禁 + 迁移演练 + 回滚 + 审计汇总（复用 llm_audit.summarize 的输出形状）
    final = evaluate_release([
        GateCheck("full_tests", "pass", True, "1999 passed, 4 skipped"),
        GateCheck("ruff", "pass", True, "All checks passed"),
        r_idem.as_check(),
        validate_rollback_plan(plan),
    ])
    rep = acceptance_report(
        final, rollback=plan, drill=r_idem,
        audit_summary={"total": {"calls": 3, "cost_usd": 0.12}},
        meta={"release": "P2-4", "commit": "a7d7cca"},
    )
    assert rep["released"] and rep["verdict"] == "release"
    assert rep["rollback"]["to_rev"] == "e4c708b" and rep["migration_drill"]["ok"]
    assert rep["audit_summary"]["total"]["calls"] == 3 and rep["meta"]["release"] == "P2-4"

    text = render_checklist(final, rollback=plan)
    assert "发布验收清单（裁决：放行）" in text and "✅ full_tests" in text and "↩ 回滚预案" in text
    assert "❌" in render_checklist(d_block)

    # 不可变
    try:
        d_ok.checks[0].status = "fail"  # type: ignore[misc]
        raise AssertionError("frozen 门禁项不可改")
    except FrozenInstanceError:
        pass

    # 确定性：同输入必得同预案/同报表
    p1 = build_rollback_plan(from_rev="a", to_rev="b")
    p2 = build_rollback_plan(from_rev="a", to_rev="b")
    assert p1.as_dict() == p2.as_dict()
    assert migration_drill(idem).as_dict() == migration_drill(idem).as_dict()

    print("release_gate 自检通过：质量门禁 + 迁移演练 + 回滚预案(提交/镜像) + 回滚演练 + 验收报表 + 确定性")
