"""persona 渲染/取值自检 —— 空偏好走中性、有偏好含关键字段、未知键被丢弃、单一上限可被下游取用。"""
import json

from bottleneck_hunter.watchlist.persona import (
    format_persona_for_prompt, get_user_single_cap, load_style, DEFAULT_STYLE,
)


class _FakeStore:
    def __init__(self, val=""):
        self._val = val

    def get_preference(self, key, default=""):
        return self._val or default


def test_empty_returns_neutral():
    assert "未设定" in format_persona_for_prompt(_FakeStore())
    assert get_user_single_cap(_FakeStore()) is None
    assert load_style(_FakeStore()) == DEFAULT_STYLE


def test_styled_contains_key_fields():
    store = _FakeStore(json.dumps({
        "risk_appetite": "conservative", "max_drawdown_pct": 15,
        "max_single_pct": 12, "sector_notes": "偏好科技、排除白酒", "bogus": "x",
    }))
    txt = format_persona_for_prompt(store)
    assert "硬约束" in txt
    assert "保守" in txt and "15%" in txt and "12%" in txt
    assert "偏好科技、排除白酒" in txt
    assert get_user_single_cap(store) == 12
    assert "bogus" not in json.dumps(load_style(store))  # 未知键丢弃


def test_bad_json_falls_back_to_default():
    assert load_style(_FakeStore("not-json")) == DEFAULT_STYLE
    assert "未设定" not in format_persona_for_prompt(_FakeStore("not-json"))  # 有值但坏→默认档渲染
