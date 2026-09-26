"""L1 日检结果的持久化护栏（2026-09-27 补）。

病根（报告 §9.6(b)）：L1 日检的推理结论 `daily_commentary`（"今日市场与策略的一致性"）
只有唯一一路出口 —— 一句瞬时 SSE，**不进 result_json、无列、无前端读**，生成完即丢，
用户在任何界面都看不到。

修法：复用 `minor_tweaks` 已有的那条回写路径写进 `result_json`（**不新增列** —— 本表
`valid_until_trigger` 的 N-19 注释已定调"别为同一份数据接第二个消费方"）。本文件钉死三件事：

1. 落库真的发生（不是只在 SSE 里出现一次）；
2. 空点评**不覆盖**已有内容（LLM 没给点评 ≠ 该把上一条抹掉）；
3. 旧行为没被碰坏（`minor_tweaks` 与本条路径互不干扰）。
"""

import json

import pytest

from bottleneck_hunter.watchlist.store import WatchlistStore


@pytest.fixture
def store(tmp_path):
    return WatchlistStore(db_path=tmp_path / "t.db").for_user("test").for_market("us_stock")


def test_日检点评落进result_json(store):
    """契约：update_macro_status(daily_commentary=...) 必须能从读接口原样取回。"""
    s = store.for_market("us_stock")
    mid = s.create_macro_strategy({"regime": "bull", "market_summary": "原总结"}, strict=False)

    s.update_macro_status(mid, "valid", daily_commentary="今日市场与策略一致，维持现行判断。")

    row = s.get_latest_macro_strategy()
    assert row["result_json"]["daily_commentary"] == "今日市场与策略一致，维持现行判断。", (
        "日检点评必须落进 result_json —— 否则前端 renderMacro 读不到，等于没落库"
    )
    assert row["status"] == "valid"
    # 落库不得顺手破坏同一份 JSON 里的其它字段
    assert row["result_json"]["market_summary"] == "原总结"


def test_空点评不覆盖上一条(store):
    """LLM 没给 daily_commentary 时（空串/None）不得把上一次的点评清成空。

    两种空值都测：契约缺键 → 调用方传 `""`；字段缺失 → 传 `None`。任一都不该抹掉已有内容。
    """
    s = store.for_market("us_stock")
    mid = s.create_macro_strategy({"regime": "bull"}, strict=False)
    s.update_macro_status(mid, "valid", daily_commentary="第一次点评")

    for empty in ("", None):
        s.update_macro_status(mid, "valid", daily_commentary=empty)
        got = s.get_latest_macro_strategy()["result_json"].get("daily_commentary")
        assert got == "第一次点评", f"传入 {empty!r} 不得抹掉上一条点评（实际 {got!r}）"


def test_点评与minor_tweaks互不干扰(store):
    """两者共用 result_json 这一段，任一方写入都不得吃掉另一方（修复前只有 minor_tweaks 一侧）。"""
    s = store.for_market("us_stock")
    mid = s.create_macro_strategy({"regime": "bull"}, strict=False)
    tweaks = [{"aspect": "板块权重", "from": "20%", "to": "15%", "reason": "防御"}]

    s.update_macro_status(mid, "needs_minor_tweak", minor_tweaks=tweaks, daily_commentary="点评甲")
    rj = s.get_latest_macro_strategy()["result_json"]
    assert rj["minor_tweaks"] == tweaks and rj["daily_commentary"] == "点评甲"

    # 只写点评、不带 minor_tweaks：上一次的微调必须还在（None ≠ 清空）
    s.update_macro_status(mid, "valid", daily_commentary="点评乙")
    rj = s.get_latest_macro_strategy()["result_json"]
    assert rj["minor_tweaks"] == tweaks, "不传 minor_tweaks 不得清掉已有微调"
    assert rj["daily_commentary"] == "点评乙"


@pytest.mark.asyncio
async def test_日检流程真的把点评写进库(store):
    """端到端：跑一遍 run_macro_check，库里必须留下它刚生成的那句点评。

    这是本护栏的主用例 —— 上面三条测的是 store 层，这条测的是"日检确实调了它"。
    """
    from unittest.mock import MagicMock, patch

    from bottleneck_hunter.watchlist.decision_engine import run_macro_check

    s = store.for_market("us_stock")
    s.create_macro_strategy({"regime": "bull", "market_summary": "现行策略"}, strict=False)

    llm = MagicMock()
    msg = MagicMock()
    msg.content = json.dumps(
        {"strategy_status": "valid", "daily_commentary": "宏观环境无重大变化"}, ensure_ascii=False
    )
    llm.invoke = MagicMock(return_value=msg)

    with patch(
        "bottleneck_hunter.watchlist.decision_engine.get_llm_for_position",
        return_value=(llm, "deepseek", "deepseek-chat"),
    ), patch(
        "bottleneck_hunter.watchlist.decision_engine._collect_market_context",
        return_value={"markets": [], "summary": "stub"},
    ):
        async for _evt in run_macro_check(s):
            pass

    rj = s.get_latest_macro_strategy()["result_json"]
    assert rj.get("daily_commentary") == "宏观环境无重大变化", (
        "日检跑完库里必须留下点评 —— 这正是修复前缺的那一环（只发 SSE、不落库）"
    )


def test_变异复现_省略落库参数则栏红(store):
    """变异复现：模拟修复前"日检只发 SSE、不传 daily_commentary"→ 本断言必须红。

    否则上面几条测的是 store 自己的能力，与"日检是否真的接上"无关，是恒真夹具。
    """
    s = store.for_market("us_stock")
    mid = s.create_macro_strategy({"regime": "bull"}, strict=False)

    # 修复前 decision_engine 的调用形态：只传 status 与 minor_tweaks
    s.update_macro_status(mid, "valid", minor_tweaks=None)

    assert s.get_latest_macro_strategy()["result_json"].get("daily_commentary") is None, (
        "不传 daily_commentary 就该是没落库 —— 证明上一条端到端用例不是恒真"
    )
