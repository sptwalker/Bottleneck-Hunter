"""L4 待确认队列只收可成交指令：hold / 漏填股数(shares=0)不落库，杜绝 UI '--股'不可执行指令。

根因：constraint_validator.validate_execution_plan 缺股数即 fail-open 返 valid，
零股计划会绕过校验落进 pending，确认时被 execute_trade 挡下('缺少关键字段')。
"""
from bottleneck_hunter.watchlist.decision_engine import _is_executable_plan


def test_zero_or_missing_shares_not_executable():
    assert _is_executable_plan({"action": "buy", "shares": 0}) is False
    assert _is_executable_plan({"action": "buy"}) is False           # 漏填 → 缺省 0
    assert _is_executable_plan({"action": "sell", "shares": None}) is False


def test_hold_and_alias_actions_not_executable():
    assert _is_executable_plan({"action": "hold", "shares": 100}) is False
    # execute_trade 不认这些别名，视为不可成交
    assert _is_executable_plan({"action": "accumulate", "shares": 100}) is False


def test_real_trade_with_positive_shares_executable():
    for act in ("buy", "add", "sell", "reduce"):
        assert _is_executable_plan({"action": act, "shares": 100}) is True
    # LLM 常把股数发成字符串
    assert _is_executable_plan({"action": "buy", "shares": "50"}) is True
    # 脏数据不崩，判不可执行
    assert _is_executable_plan({"action": "buy", "shares": "abc"}) is False
