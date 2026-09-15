"""P0-3 快照绑定校验；不伪造快照，也不代表生产链路已接入。"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bottleneck_hunter.watchlist.research_contracts import ResearchSnapshot


def bind_snapshot(*, snapshot_id: str | None, strategy_version: str | None,
                  strict: bool = True,
                  get_snapshot: Callable[[str], ResearchSnapshot | None] | None = None,
                  ) -> tuple[str | None, str | None]:
    """旧调用允许双 NULL；显式绑定必须指向当前用户、市场下的真实快照。"""
    if snapshot_id is None and strategy_version is None and not strict:
        return None, None
    if any(not isinstance(value, str) or not value.strip() for value in (snapshot_id, strategy_version)):
        raise ValueError("新决策记录必须同时绑定非空 snapshot_id 与 strategy_version")
    if get_snapshot is None:
        raise ValueError("快照绑定必须提供当前用户和市场的查询入口")
    snapshot = get_snapshot(snapshot_id)
    if snapshot is None or snapshot.strategy_version != strategy_version:
        raise ValueError("当前用户和市场下快照不存在或 strategy_version 不匹配")
    return snapshot_id, strategy_version


def snapshot_columns(*, snapshot_id: str | None, strategy_version: str | None,
                     strict: bool = True,
                     get_snapshot: Callable[[str], ResearchSnapshot | None] | None = None,
                     ) -> tuple[str, str, tuple[str | None, str | None]]:
    """INSERT 字段、占位符与参数保持同序；旧调用显式写 NULL。"""
    values = bind_snapshot(snapshot_id=snapshot_id, strategy_version=strategy_version,
                           strict=strict, get_snapshot=get_snapshot)
    return ", snapshot_id, strategy_version", ", ?, ?", values
