"""回归：phase3/score 曾因 scorecard 含 NaN/Inf 导致 JSON 序列化 500。

根因：缺失财务数据的 float 字段为 NaN，phase3_score(纯JSON POST)返回时未清洗，
FastAPI 序列化抛 'Out of range float values are not JSON compliant: nan'。
修复：与 SSE 各阶段一致，在 phase2 入缓存/入库 + phase3 返回前用 _sanitize 清洗。
运行：pytest tests/test_nan_sanitize.py -q
"""
from __future__ import annotations

import json
import math

from bottleneck_hunter.web.streaming._common import _sanitize


def test_sanitize_nan_inf_to_none():
    assert _sanitize(float("nan")) is None
    assert _sanitize(float("inf")) is None
    assert _sanitize(float("-inf")) is None
    assert _sanitize(3.14) == 3.14
    assert _sanitize(0.0) == 0.0


def test_sanitize_nested_structure():
    data = {
        "ranked_results": [
            {"rank": 1, "scorecard": {"overall_score": float("nan"),
                                      "alpha": {"alpha_score": 5.0, "gap": float("inf")}}},
        ],
        "scoring_config": {"quality_weight": 0.55},
    }
    clean = _sanitize(data)
    # 嵌套的 NaN/Inf 都被清成 None
    assert clean["ranked_results"][0]["scorecard"]["overall_score"] is None
    assert clean["ranked_results"][0]["scorecard"]["alpha"]["gap"] is None
    assert clean["ranked_results"][0]["scorecard"]["alpha"]["alpha_score"] == 5.0


def test_sanitized_data_is_json_serializable():
    """核心回归：清洗前 json.dumps(allow_nan=False) 失败，清洗后通过。"""
    data = {"scores": [float("nan"), 1.0, float("inf")], "w": 0.5}
    # 修复前的失败路径（FastAPI 用 allow_nan=False 语义）
    try:
        json.dumps(data, allow_nan=False)
        raised = False
    except ValueError:
        raised = True
    assert raised, "含 NaN 的数据本应触发序列化错误"
    # 修复后：清洗使其合法
    out = json.dumps(_sanitize(data), allow_nan=False)
    assert '"scores": [null, 1.0, null]' in out


def test_sanitize_preserves_non_float():
    obj = {"a": "text", "b": 5, "c": True, "d": None, "e": [1, "x", None]}
    assert _sanitize(obj) == obj


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-q"])
