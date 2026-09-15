"""统一的 Point-in-Time 数据契约与时间语义。"""

from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator


class DataTimeSemantics(BaseModel):
    """描述观测所属期、可用性和采集时间，所有时间必须带时区。"""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True, allow_inf_nan=False)

    period_start: datetime = Field(description="数据所属期开始")
    period_end: datetime = Field(description="数据所属期结束")
    effective_at: datetime = Field(description="数据生效时间")
    visible_at: datetime = Field(description="研究者可见时间")
    collected_at: datetime = Field(description="系统采集时间")

    @field_validator("period_start", "period_end", "effective_at", "visible_at", "collected_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("时间必须包含时区")
        return value.astimezone(timezone.utc)

    @field_validator("period_end")
    @classmethod
    def validate_period(cls, value: datetime, info: ValidationInfo):
        start = info.data.get("period_start")
        if start and value < start:
            raise ValueError("所属期结束时间不能早于开始时间")
        return value


class DataProvenance(BaseModel):
    """数据来源、单位和质量状态。"""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True, allow_inf_nan=False)

    source: str = Field(min_length=1)
    source_record_id: str = ""
    unit: str = Field(min_length=1)
    currency: str | None = None
    quality: str = "normal"
    quality_notes: str = ""
    revision: int = Field(default=1, ge=1)


class PointInTimeObservation(BaseModel):
    """可用于回测和研究快照的不可变观测契约。"""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True, allow_inf_nan=False)

    ticker: str = Field(min_length=1)
    market: str = Field(min_length=1)
    value: float | None = None
    time: DataTimeSemantics
    provenance: DataProvenance

    @property
    def is_visible(self) -> bool:
        return self.time.visible_at <= self.time.collected_at
