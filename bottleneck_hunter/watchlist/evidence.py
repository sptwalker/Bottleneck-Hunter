"""P0-3 证据 ID：喂给 LLM 的每条数据行带短 ID，产出必须引用 ID，引用逐条回查快照。

- `evidence_id`：sha256(ticker|metric|as_of|value)[:12]——同一事实恒得同一 ID，可跨层/复盘回查。
- `EvidenceIndex`：本次运行登记过的全部证据；`render()` 给 prompt，`check()` 校验 LLM 引用。
- `Citation`：Pydantic 校验——只保留在本次索引里真实存在的 ID，臆造/串票的 ID 记入 invalid。

无有效引用的理由标「无据」：L3/L4 只标记不拦；投委会赞成票无据 → 不计入通过票（见 committee）。
"""

from __future__ import annotations

import hashlib

from pydantic import BaseModel, Field, ValidationInfo, field_validator

UNGROUNDED_LABEL = "无据"

CITE_INSTRUCTION = (
    "\n\n## 证据引用（硬性要求）\n\n"
    "上方「证据索引」每行形如 `[ID] 标的 指标=值 (日期)`。输出中的每个计划对象（评审则为整个 JSON）必须带"
    " `evidence_ids` 字段（字符串数组），列出该判断所依据的证据 ID，只能填索引里出现过的 ID，不得编造。"
    "没有引用任何有效 ID 的结论会被标记为「无据」。\n"
)


def evidence_id(ticker: str, metric: str, as_of: str, value) -> str:
    raw = f"{ticker}|{metric}|{as_of}|{value}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


class Citation(BaseModel):
    """LLM 输出里的引用。用 context={"valid": set[str]} 校验：只保留真实存在的 ID。"""

    evidence_ids: list[str] = Field(default_factory=list)

    @field_validator("evidence_ids", mode="before")
    @classmethod
    def _coerce(cls, v):
        if isinstance(v, str):
            v = [v]
        return [str(x).strip().strip("[]") for x in (v or []) if str(x).strip()]

    @field_validator("evidence_ids")
    @classmethod
    def _only_known(cls, v: list[str], info: ValidationInfo) -> list[str]:
        valid = (info.context or {}).get("valid", set())
        return [x for x in dict.fromkeys(v) if x in valid]

    @property
    def grounded(self) -> bool:
        return bool(self.evidence_ids)


class EvidenceIndex:
    def __init__(self) -> None:
        self.rows: dict[str, str] = {}  # id -> 渲染行

    def add(self, ticker: str, metric: str, as_of: str, value) -> str | None:
        if value is None or value == "":
            return None
        eid = evidence_id(ticker, metric, as_of, value)
        self.rows.setdefault(eid, f"[{eid}] {ticker} {metric}={value} ({as_of})")
        return eid

    def add_many(self, ticker: str, as_of: str, fields: dict) -> list[str]:
        """登记一行数据的多个字段，返回其 ID 列表（可直接挂到数据行上）。"""
        return [i for k, v in fields.items() if not isinstance(v, (dict, list))
                for i in [self.add(ticker, k, as_of, v)] if i]

    def __bool__(self) -> bool:
        return bool(self.rows)

    def render(self) -> str:
        return "## 证据索引\n\n" + "\n".join(self.rows.values()) if self.rows else ""

    def check(self, obj: dict) -> dict:
        """校验 obj['evidence_ids']，就地写回有效 ID + grounded 标记，返回 obj。索引为空不判（不误伤）。"""
        if not isinstance(obj, dict) or not self.rows:
            return obj
        raw = obj.get("evidence_ids") or []
        c = Citation.model_validate({"evidence_ids": raw}, context={"valid": set(self.rows)})
        obj["evidence_ids"] = c.evidence_ids
        bad = [x for x in Citation._coerce(raw) if x not in self.rows]
        if bad:
            obj["evidence_invalid_ids"] = bad
        obj["grounded"] = c.grounded
        if not c.grounded:
            obj["evidence_label"] = UNGROUNDED_LABEL
        return obj


if __name__ == "__main__":
    idx = EvidenceIndex()
    a = idx.add("NVDA", "close", "2026-10-09", 180.5)
    assert a == evidence_id("NVDA", "close", "2026-10-09", 180.5) and len(a) == 12
    assert idx.add("NVDA", "rsi", "2026-10-09", None) is None
    assert idx.check({"evidence_ids": [a, "deadbeef0000"]}) == {
        "evidence_ids": [a], "evidence_invalid_ids": ["deadbeef0000"], "grounded": True}
    out = idx.check({"evidence_ids": ["fake"]})
    assert out["grounded"] is False and out["evidence_label"] == UNGROUNDED_LABEL
    assert EvidenceIndex().check({"x": 1}) == {"x": 1}  # 空索引不判
    print("evidence self-check OK")
