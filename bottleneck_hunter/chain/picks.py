"""最终推荐名单（top_picks）的唯一口径。

背景：同一批入围公司在三处被三条互不相同的代码路径各排了一次名——
Phase 3 评选表用「勾选子集 + quality^wq × alpha^wa」，落库的 top_picks 用
「supplier_scorecards[:5] + overall_score ≥ 6」，Phase 4 交叉验证表用「全量 + final_score」。
三者结果天然不同，用户看到的就是「交叉验证的公司列表和上一阶段筛选出来的不一样」。

这里统一为一条规则，所有落库/呈现路径都调它：

1. 过滤掉事实核查硬门 REJECT 的公司；
2. 排序键取 final_score（推荐分），缺失时退化为 overall_score——与 Phase 3/4 表里
   显示的那一列完全一致，用户看到的顺序就是落库的顺序；
3. 门槛 min_score（默认 5.0）；
4. 取前 top_n（默认 5，可被 scoring_config.top_n 覆盖）；
5. 去重保序（同一家公司可能被多条通路带进来）。

排序键刻意**不**用 min(final_score, overall_score)：那会把「预期差高但质量一般」的名字
压回去，等于用筛选口径覆盖评选口径，Phase 3 显示的名单又会和 Phase 4 不一致——
正是本次要修的 bug。质量兜底由 overall_score 在 Phase 2 筛选阶段负责。

自检：`python -m bottleneck_hunter.chain.picks`
"""

from __future__ import annotations

from typing import Any

DEFAULT_TOP_N = 5
DEFAULT_MIN_SCORE = 5.0


def _get(sc: Any, key: str, default: Any = None) -> Any:
    """dict / Pydantic 模型双形态取值。"""
    if isinstance(sc, dict):
        return sc.get(key, default)
    return getattr(sc, key, default)


def _ticker(sc: Any) -> str:
    supplier = _get(sc, "supplier")
    if supplier is not None:
        t = _get(supplier, "ticker", "")
        if t:
            return t
    return _get(sc, "ticker", "") or ""


def _is_reject(sc: Any) -> bool:
    return (_get(sc, "fact_check_recommendation") or "").upper() == "REJECT"


def ticker_of(sc: Any) -> str:
    """取计分卡的 ticker（dict / Pydantic 双形态）。"""
    return _ticker(sc)


def sort_key(sc: Any) -> float:
    """统一排序键：final_score（推荐分），缺失则用 overall_score。"""
    return _score_key(sc)


def _score_key(sc: Any) -> float:
    final = _get(sc, "final")
    if final is not None:
        fs = _get(final, "final_score", None)
        if fs is not None:
            return float(fs)
    return float(_get(sc, "overall_score", 0) or 0)


def canonical_picks(
    scorecards: Any,
    *,
    top_n: int = DEFAULT_TOP_N,
    min_score: float = DEFAULT_MIN_SCORE,
) -> list[str]:
    """按统一口径从计分卡里选出最终推荐名单（ticker 列表，去重保序）。"""
    if not scorecards:
        return []

    passed = [sc for sc in scorecards if not _is_reject(sc)]
    passed.sort(key=_score_key, reverse=True)

    picks: list[str] = []
    for sc in passed[:max(0, top_n)]:
        if _score_key(sc) < min_score:
            continue
        t = _ticker(sc)
        if t and t not in picks:
            picks.append(t)
    return picks


def passed_top(
    scorecards: Any,
    *,
    top_n: int = DEFAULT_TOP_N,
) -> list[Any]:
    """按统一口径过滤 REJECT + 排序后取前 top_n 张计分卡（对象原样返回，不做门槛裁剪）。

    Phase 4 交叉验证表与 top_picks 必须来自同一份名单，否则用户会看到
    「交叉验证的公司和上一阶段筛选的不一样」。
    """
    if not scorecards:
        return []
    passed = [sc for sc in scorecards if not _is_reject(sc)]
    passed.sort(key=_score_key, reverse=True)
    return passed[: max(0, top_n)]


def picks_top_n_from_config(scoring_config: dict | None) -> int:
    """从 scoring_config 读 top_n，读不到用默认值。"""
    if not isinstance(scoring_config, dict):
        return DEFAULT_TOP_N
    try:
        n = int(scoring_config.get("top_n", DEFAULT_TOP_N))
    except (TypeError, ValueError):
        return DEFAULT_TOP_N
    return n if n > 0 else DEFAULT_TOP_N


def _selfcheck() -> None:  # pragma: no cover - 手动自检入口
    """可跑自检：验证 REJECT 拦截、final_score 排序、门槛、去重、双形态一致。"""
    def card(ticker: str, overall: float, final: float | None, fc: str = "PASS") -> dict:
        d: dict[str, Any] = {
            "supplier": {"ticker": ticker},
            "overall_score": overall,
            "fact_check_recommendation": fc,
        }
        d["final"] = None if final is None else {"final_score": final}
        return d

    cards = [
        card("A", 9.0, 6.0),            # final 6.0
        card("B", 8.0, 8.5),            # final 8.5 → 第一
        card("C", 7.0, 4.0),            # final 4.0 → 低于门槛 5.0 被剔除
        card("D", 9.5, 9.5, "REJECT"),  # REJECT 被拦截
        card("E", 6.0, None),           # final 缺失 → 用 overall 6.0
        card("B", 8.0, 8.5),            # 重复项 → 去重
        card("F", 4.0, 4.0),            # 低于门槛
    ]
    got = canonical_picks(cards, top_n=10, min_score=5.0)
    assert got == ["B", "A", "E"], f"picks 口径不符: {got}"

    # top_n 截断
    assert canonical_picks(cards, top_n=1) == ["B"], "top_n 截断失效"
    # REJECT 即使分最高也不出现（D 的 9.5 不得越过任何一道门）
    assert "D" not in canonical_picks(cards, top_n=10, min_score=0.0), "REJECT 硬门失效"
    # 空输入
    assert canonical_picks([]) == []
    # config 读取
    assert picks_top_n_from_config({"top_n": 8}) == 8
    assert picks_top_n_from_config({"top_n": 0}) == DEFAULT_TOP_N
    assert picks_top_n_from_config(None) == DEFAULT_TOP_N

    # 对象形态与 dict 形态结果一致
    class _F:
        def __init__(self, v: float) -> None:
            self.final_score = v

    class _S:
        def __init__(self, ticker: str, v: float) -> None:
            self.ticker = ticker

    class _C:
        def __init__(self, ticker: str, overall: float, final: float | None, fc: str = "PASS") -> None:
            self.supplier = _S(ticker, 0)
            self.overall_score = overall
            self.final = None if final is None else _F(final)
            self.fact_check_recommendation = fc

    objs = [_C("A", 9.0, 6.0), _C("B", 8.0, 8.5), _C("C", 7.0, 4.0), _C("E", 6.0, None)]
    assert canonical_picks(objs, top_n=10) == got, "对象形态与 dict 形态结果不一致"

    print("picks selfcheck OK:", got, objs and canonical_picks(objs, top_n=10))


if __name__ == "__main__":  # pragma: no cover
    _selfcheck()
