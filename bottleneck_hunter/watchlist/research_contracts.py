"""研究持久化契约；只接收显式时间，不从旧行情推断历史可见性。"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from bottleneck_hunter.watchlist.data_contracts import DataTimeSemantics, PointInTimeObservation

UTCTime = Annotated[datetime, AfterValidator(DataTimeSemantics.require_timezone)]


class SourceObservation(PointInTimeObservation):
    """指标观测的一次来源修订；新修订必须使用新的 observation_id。"""

    observation_id: str = Field(min_length=1)
    metric: str = Field(min_length=1)


class StageInputCapture(BaseModel):
    """当前决策上下文的封存；采集时间不代表历史可见时间。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    stage: Literal[
        "L1", "L2", "L3", "L4", "hard_stop", "committee", "committee_consensus", "macro_consult"
    ]
    captured_at: UTCTime
    run_id: str = Field(min_length=1)
    semantics: Literal["current_context"] = "current_context"
    historical_visibility: Literal["unknown"] = "unknown"
    payload_json: str

    @model_validator(mode="after")
    def normalize_payload(self) -> StageInputCapture:
        def pairs(items):
            result = {}
            for key, value in items:
                if key in result:
                    raise ValueError("阶段输入 JSON 包含重复键")
                result[key] = value
            return result

        def invalid_constant(value):
            raise ValueError("阶段输入必须为有限 JSON 数值")

        parsed = json.loads(self.payload_json, object_pairs_hook=pairs, parse_constant=invalid_constant)
        if not isinstance(parsed, dict):
            raise ValueError("阶段输入必须为 JSON 对象")
        normalized = json.dumps(parsed, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        object.__setattr__(self, "payload_json", normalized)
        return self


class ResearchSnapshot(BaseModel):
    """封存的研究输入；来源顺序也是快照内容，不支持事后增删。"""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True, allow_inf_nan=False)

    snapshot_id: str = Field(min_length=1)
    market: str = Field(min_length=1)
    strategy_version: str = Field(min_length=1)
    as_of: UTCTime
    created_at: UTCTime
    observations: tuple[SourceObservation, ...]
    captures: tuple[StageInputCapture, ...] = ()

    @model_validator(mode="after")
    def validate_members(self) -> ResearchSnapshot:
        ids = [item.observation_id for item in self.observations]
        if len(ids) != len(set(ids)):
            raise ValueError("快照包含重复 observation_id")
        if any(item.market != self.market for item in self.observations):
            raise ValueError("快照与来源观测市场不一致")
        if self.created_at < self.as_of:
            raise ValueError("快照创建时间不能早于研究时点")
        # P0-4 单独执行 PIT 可见性门禁；此处不把采集时间伪装成历史 visible_at。
        return self
