"""Tests for json_utils.py — JSON 提取工具函数。"""

import json

import pytest

from bottleneck_hunter.chain.json_utils import (
    strip_fences,
    extract_json_object,
    extract_json_array,
)


class TestStripFences:
    def test_with_json_fence(self):
        text = '```json\n{"key": "value"}\n```'
        assert strip_fences(text) == '{"key": "value"}'

    def test_with_plain_fence(self):
        text = '```\n{"a": 1}\n```'
        assert strip_fences(text) == '{"a": 1}'

    def test_no_fence(self):
        text = '{"key": "value"}'
        assert strip_fences(text) == '{"key": "value"}'

    def test_whitespace_padding(self):
        text = '  ```json\n  {"x": 1}  \n```  '
        result = strip_fences(text)
        assert json.loads(result) == {"x": 1}

    def test_empty_string(self):
        assert strip_fences("") == ""

    def test_no_closing_fence(self):
        text = '```json\n{"a": 1}'
        assert strip_fences(text) == text.strip()

    def test_multiline_content(self):
        text = '```json\n{\n  "a": 1,\n  "b": 2\n}\n```'
        result = strip_fences(text)
        assert json.loads(result) == {"a": 1, "b": 2}


class TestExtractJsonObject:
    def test_plain_json(self):
        assert extract_json_object('{"key": "val"}') == {"key": "val"}

    def test_fenced_json(self):
        text = '```json\n{"result": 42}\n```'
        assert extract_json_object(text) == {"result": 42}

    def test_json_with_surrounding_text(self):
        text = '以下是分析结果：\n{"score": 8, "reason": "good"}\n请参考。'
        result = extract_json_object(text)
        assert result["score"] == 8

    def test_nested_one_level(self):
        text = '{"outer": {"inner": 1}}'
        result = extract_json_object(text)
        assert result["outer"]["inner"] == 1

    def test_raises_on_no_json(self):
        with pytest.raises(ValueError, match="无法从 LLM"):
            extract_json_object("这是纯文本，没有任何 JSON")

    def test_raises_on_empty(self):
        with pytest.raises(ValueError):
            extract_json_object("")

    def test_malformed_fence_fallback_to_regex(self):
        text = '```json\n{invalid json}\n```\n说明：{"fallback": true}'
        with pytest.raises(ValueError):
            extract_json_object(text)

    def test_text_before_valid_json(self):
        text = '分析结果如下：{"fallback": true}'
        result = extract_json_object(text)
        assert result == {"fallback": True}

    def test_whitespace_only(self):
        with pytest.raises(ValueError):
            extract_json_object("   \n\n   ")

    def test_multiple_objects_takes_first(self):
        text = '{"a": 1} and {"b": 2}'
        result = extract_json_object(text)
        assert result == {"a": 1}

    def test_chinese_keys(self):
        text = '{"分数": 9, "理由": "表现优秀"}'
        result = extract_json_object(text)
        assert result["分数"] == 9

    # ── 回归：投委会深层嵌套 / 未闭合围栏 / 结尾逗号（曾致评审反复解析失败）──
    def test_deep_nested_with_surrounding_text(self):
        text = ('评审如下：{"vote":"approve","assessment":{"risk":{"level":"high"},'
                '"score":7},"concerns":["a","b"]} 完毕')
        r = extract_json_object(text)
        assert r["assessment"]["risk"]["level"] == "high"
        assert r["concerns"] == ["a", "b"]

    def test_unclosed_fence_deep_nested(self):
        # 模型输出被 ```json 包裹但截断/忘记收尾，无闭合 ```
        text = '```json\n{"vote":"reject","detail":{"why":{"k":"v"}}}'
        r = extract_json_object(text)
        assert r["vote"] == "reject"
        assert r["detail"]["why"]["k"] == "v"

    def test_trailing_comma(self):
        assert extract_json_object('{"a": 1, "b": 2,}') == {"a": 1, "b": 2}

    def test_braces_inside_string_not_confused(self):
        text = '结果：{"note": "含 } 右括号与 { 左括号", "n": {"m": 1}} 末尾'
        r = extract_json_object(text)
        assert r["n"]["m"] == 1
        assert "}" in r["note"]


class TestExtractJsonArray:
    def test_plain_array(self):
        result = extract_json_array('[{"a": 1}, {"b": 2}]')
        assert len(result) == 2
        assert result[0] == {"a": 1}

    def test_fenced_array(self):
        text = '```json\n[{"x": 1}]\n```'
        result = extract_json_array(text)
        assert result == [{"x": 1}]

    def test_empty_array(self):
        result = extract_json_array("[]")
        assert result == []

    def test_non_list_json_returns_none(self):
        text = '```json\n{"not": "array"}\n```'
        result = extract_json_array(text)
        assert result is None

    def test_array_with_surrounding_text(self):
        text = '结果如下：[{"id": 1}, {"id": 2}] 完毕。'
        result = extract_json_array(text)
        assert len(result) == 2

    def test_individual_objects_fallback(self):
        text = '发现以下：\n{"name": "A"}\n{"name": "B"}\n以上。'
        result = extract_json_array(text)
        assert len(result) == 2
        assert result[0]["name"] == "A"

    def test_mixed_valid_invalid_objects(self):
        text = '{"ok": 1}\n{broken json}\n{"ok": 2}'
        result = extract_json_array(text)
        assert len(result) == 2

    def test_no_json_returns_none(self):
        result = extract_json_array("没有任何 JSON 内容")
        assert result is None

    def test_empty_string_returns_none(self):
        result = extract_json_array("")
        assert result is None

    def test_nested_array_in_fence(self):
        text = '```json\n[{"items": [1, 2, 3]}]\n```'
        result = extract_json_array(text)
        assert result[0]["items"] == [1, 2, 3]

    def test_single_object_as_individual(self):
        text = '分析结果：{"score": 7}'
        result = extract_json_array(text)
        assert len(result) == 1
        assert result[0]["score"] == 7
