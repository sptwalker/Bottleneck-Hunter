"""P1-F（N-12）与 P1-G（N-14）的行为护栏 —— 补于 2026-09-26（第三批 C 节核验时发现两项已上线但零测试）。

两项都是"功能对、护栏无"的形态：代码在 `602926a` 已进仓并在生产生效，但报告给它们各自写的
验证条款都没落地，全量 2374 条里**没有一条**会因它们被改坏而变红（变异实测：P1-F 复原 0 击杀 /
P1-G 删块 0 击杀）。本文件补上这两道断言。

各自钉死的契约：
- P1-F：板块漂移必须进逃逸候选集 —— 权益/现金都贴着目标、只有板块结构被打乱时也要能逃逸。
- P1-G：`_clamp_target_allocation` 的类型畸变**不得静默** —— 可救的（"55%"）要修回来，
  不可救的要留 warning，且不得让下游偏离判据静默失效。
"""

import pytest

from bottleneck_hunter.watchlist.store import WatchlistStore


@pytest.fixture
def store(tmp_path):
    return WatchlistStore(db_path=tmp_path / "t.db").for_user("test").for_market("us_stock")


# ── P1-F（N-12）：板块漂移必须能单独触发逃逸 ──────────────────────────

# 权益/现金都**精确贴目标**（60/40），只有板块结构被打乱 —— 这正是 N-12 描述的"策略已失效"
# 形态：`max_abs_drift_pct` 早就把它算进去了，但逃逸判据只读权益/现金两项时整体啥也不做。
# 板块目标刻意用两种写法（`{"target_pct": N}` 明细 / 裸数字），钉住 `_compute_deviation_drift`
# 的取值口径（`tgt.get("target_pct") if isinstance(tgt, dict) else tgt`）。
#
# **为什么是三个板块、不是两个**：逃逸原因由 `max(..., key=abs)` 选出，并列时退化成"取列表首个"，
# 而候选列表来自 `set(...)` 迭代 —— 跨进程随 hash seed 变序。两个板块时这**必然**并列：板块权重
# 互补（a + b = 权益）、目标也互补，两边偏离恒等值反号（semi −30 / soft +30），于是断言会随机红。
# 三板块且偏离互不相同，最大者唯一，用例才是确定性的。
SECTOR_TARGET = {
    "target_allocation": {"equity_pct": 60, "cash_pct": 40},
    "sector_targets": {"半导体": {"target_pct": 40}, "软件": 10, "硬件": {"target_pct": 10}},
}

# (ticker, 名称, 板块)：权重由调用方给，合计即权益仓位
_HOLDINGS = (("NVDA", "NVIDIA", "半导体"), ("MSFT", "Microsoft", "软件"), ("AVGO", "Broadcom", "硬件"))


def _setup_sector_account(store, semi_pct: float, soft_pct: float, hard_pct: float):
    """10 万账户：按给定板块权重建三票持仓，余下为现金 —— 使权益/现金恰好贴 60/40。"""
    s = store.for_market("us_stock")
    acct = s.get_sim_account()
    total = 100_000.0
    for i, (tk, name, sec) in enumerate(_HOLDINGS):
        eid = s.add({"ticker": tk, "company_name": name, "tier": "focus", "market": "us_stock", "sector": sec})
        value = total * (semi_pct, soft_pct, hard_pct)[i] / 100
        pid = s.create_sim_position(acct["id"], tk, shares=int(value / 100), avg_cost=100.0, entry_id=eid)
        s.update_sim_position(pid, current_price=100.0, market_value=value)
    pos_value = total * (semi_pct + soft_pct + hard_pct) / 100
    s.update_sim_account(cash_balance=total - pos_value, total_equity=total, current_capital=total)
    return s


def test_板块漂移单独触发逃逸(store):
    """权益/现金贴目标，半导体 20% vs 目标 40%（偏离 −20pct）→ 必须逃逸，且原因是半导体。

    另两个板块各偏离 +10，**都在 15 的带内**，故半导体是唯一的越线者、也是唯一的最大者。
    """
    from bottleneck_hunter.watchlist.decision_engine import _reuse_escape_reason

    s = _setup_sector_account(store, semi_pct=20.0, soft_pct=20.0, hard_pct=20.0)  # 权益 60 / 现金 40
    esc = _reuse_escape_reason(s, SECTOR_TARGET, "us_stock")
    assert esc, "板块结构被打乱而权益/现金贴目标时必须逃逸（N-12：能识别却逃逸不了）"
    assert "半导体" in esc["reason"], esc["reason"]
    assert esc["drift_pct"] == pytest.approx(-20.0, abs=0.2)
    # 反证判据确实来自板块而非权益/现金：这两项本身必须在阈值内
    assert abs(esc["drift"]["equity_drift_pct"]) <= 0.2
    assert abs(esc["drift"]["cash_drift_pct"]) <= 0.2


def test_板块在带内不逃逸(store):
    """板块与权益/现金全部贴目标 → 不逃逸（不得因"有板块明细"就无脑逃逸，那会每轮烧一次 L2）。"""
    from bottleneck_hunter.watchlist.decision_engine import _reuse_escape_reason

    s = _setup_sector_account(store, semi_pct=40.0, soft_pct=10.0, hard_pct=10.0)
    assert _reuse_escape_reason(s, SECTOR_TARGET, "us_stock") == {}


def test_板块未进候选集则逃逸不了(store):
    """变异复现：候选集退回修复前的 (权益, 现金) 两项 → 本用例必须红（证明上条不是恒真）。"""
    from bottleneck_hunter.watchlist import decision_engine as de

    s = _setup_sector_account(store, semi_pct=20.0, soft_pct=20.0, hard_pct=20.0)
    real = de._compute_deviation_drift

    def _strip_sector_drift(*a, **k):
        d = real(*a, **k)
        d = dict(d)
        d["sector_drift"] = []  # 模拟"板块漂移没接进判据"
        return d

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(de, "_compute_deviation_drift", _strip_sector_drift)
        assert de._reuse_escape_reason(s, SECTOR_TARGET, "us_stock") == {}, (
            "把板块漂移摘掉后必须逃逸不了 —— 否则上一条断言与板块无关，是恒真夹具")


# ── P1-G（N-14）：类型畸变不得静默 ────────────────────────────────────

def _bounds():
    from bottleneck_hunter.watchlist.regime_mapper import get_allocation_bounds

    return get_allocation_bounds("sideways", "balanced", 5)


def test_字符串百分号被修回来且有留痕():
    """LLM 常见畸变 `"55%"` → 修成 55.0，且必须有 warning（修复前既不钳也不告警、静默跳过）。"""
    from bottleneck_hunter.watchlist.decision_engine import _clamp_target_allocation

    b = _bounds()
    r = {"target_allocation": {"equity_pct": "55%"}}
    warns = _clamp_target_allocation(r, b)
    assert r["target_allocation"]["equity_pct"] == pytest.approx(55.0)
    assert any("类型修正" in w for w in warns), warns


def test_不可救的类型不静默_必留warning():
    """不可救（如 list）→ 必须有 warning；且 equity_pct 要**填回推荐值**，不留 None。

    N-14 的病根不是"少钳一次"，而是 `equity_pct` 变 None 后 `_compute_deviation_drift` 的
    `has_target` 塌掉 → `rebalance_suggested` 可能变 None → `_reuse_escape_reason` 直接 return {}
    → **整个逃逸机制永久沉默**且不留痕迹。故这里同时钉"有痕"与"有出路"。
    """
    from bottleneck_hunter.watchlist.decision_engine import _clamp_target_allocation

    b = _bounds()
    r = {"target_allocation": {"equity_pct": ["55"]}}
    warns = _clamp_target_allocation(r, b)
    assert any("类型不可用" in w for w in warns), warns
    assert r["target_allocation"]["equity_pct"] == pytest.approx(b["recommended_equity"]), (
        "不可救时必须填 L1 推荐值，不能留 None（否则下游逃逸判据静默失效）")


def test_类型污染不熄灭逃逸机制(store):
    """端到端：L2 目标里的 `equity_pct` 被写成字符串时，逃逸判据仍必须能工作（修复前的静默路径）。"""
    from bottleneck_hunter.watchlist.decision_engine import _clamp_target_allocation, _reuse_escape_reason

    s = store.for_market("us_stock")
    acct = s.get_sim_account()
    total = 100_000.0
    eid = s.add({"ticker": "NVDA", "company_name": "NVIDIA", "tier": "focus", "market": "us_stock"})
    pid = s.create_sim_position(acct["id"], "NVDA", shares=100, avg_cost=100.0, entry_id=eid)
    s.update_sim_position(pid, current_price=100.0, market_value=10_000.0)  # 10% 权益
    s.update_sim_account(cash_balance=90_000.0, total_equity=total, current_capital=total)

    plan_rj = {"target_allocation": {"equity_pct": "60%"}}  # 字符串畸变
    warns = _clamp_target_allocation(plan_rj, _bounds())
    assert warns, "字符串畸变必须留痕"
    esc = _reuse_escape_reason(s, plan_rj, "us_stock")
    assert esc, "钳制修回 60% 后，实际 10% 权益 → 偏离 50pct，必须逃逸（不得静默失效）"
