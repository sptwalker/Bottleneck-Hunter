"""复用研究快照保存当前阶段输入，不推断历史可见性。"""

import json
from datetime import datetime, timezone
from uuid import uuid4

from bottleneck_hunter.watchlist.research_contracts import ResearchSnapshot, StageInputCapture

STRATEGY_VERSION = "decision-p0-3-v1"
_REDACTED = "[REDACTED]"
_SECRET_KEYS = {"apikey", "token", "accesstoken", "refreshtoken", "password", "secret", "authorization", "cookie"}


def _redact(value):
    if isinstance(value, dict):
        return {_redact_key(k): (_REDACTED if _normal_key(k) in _SECRET_KEYS else _redact(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v) for v in value]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return value
        if isinstance(parsed, (dict, list)):
            return json.dumps(_redact(parsed), ensure_ascii=False, separators=(",", ":"))
    return value


def _normal_key(key):
    return "".join(ch.lower() for ch in str(key) if ch.isalnum())


def _redact_key(key):
    return key


def save_stage_snapshot(store, stage: str, inputs: dict) -> dict:
    """先持久化输入，返回生产写入必须使用的严格绑定参数。"""
    now = datetime.now(timezone.utc)
    capture = StageInputCapture(
        stage=stage,
        captured_at=now,
        run_id=uuid4().hex,
        payload_json=json.dumps(_redact(inputs), ensure_ascii=False, allow_nan=False),
    )
    snapshot = ResearchSnapshot(
        snapshot_id=uuid4().hex,
        market=store._market,
        strategy_version=STRATEGY_VERSION,
        as_of=now,
        created_at=now,
        observations=(),
        captures=(capture,),
    )
    store.save_research_snapshot(snapshot)
    return {"snapshot_id": snapshot.snapshot_id, "strategy_version": snapshot.strategy_version, "strict": True}
