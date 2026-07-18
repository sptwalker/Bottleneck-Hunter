"""用户个人持仓风格 — 存取 + 渲染为各层决策 prompt 的硬约束块。

存储复用 user_preferences 表（key="portfolio_style"，已按用户+市场隔离，
见 store_watchlist.save_preference/get_preference）。风格是叠加在 L1 市场研判
之上的**硬约束**：只会让下游更保守，不会放大市场给的仓位。
"""
from __future__ import annotations

import json

STYLE_KEY = "portfolio_style"
STYLE_CATEGORY = "portfolio_style"

# 枚举档位 → 中文说明（注入 prompt 用）
_RISK_LABELS = {
    "aggressive": "激进（追求收益，容忍波动）",
    "balanced": "中性（收益与风险平衡）",
    "conservative": "保守（回撤敏感，优先防守）",
}
_CONC_LABELS = {
    "concentrated": "集中（3-5 只）",
    "balanced": "均衡（6-10 只）",
    "diversified": "分散（10 只以上）",
}
_HORIZON_LABELS = {
    "short": "短线（日/周）",
    "swing": "波段（月级）",
    "long": "长线（季/年）",
}
_CASH_LABELS = {
    "full": "满仓型（尽量少留现金）",
    "balanced": "平衡",
    "timing": "留现金择时（保留较高现金伺机）",
}
_STOP_LABELS = {
    "strict": "严格止损（触及止损线坚决离场）",
    "tolerant": "容忍波动（不轻易止损）",
}

DEFAULT_STYLE = {
    "risk_appetite": "balanced",   # aggressive | balanced | conservative
    "max_drawdown_pct": 25,        # 5-60
    "max_single_pct": 20,          # 5-50
    "concentration": "balanced",   # concentrated | balanced | diversified
    "horizon": "swing",            # short | swing | long
    "cash_timing": "balanced",     # full | balanced | timing
    "stop_loss": "strict",         # strict | tolerant
    "sector_notes": "",            # 自由文本
}

# 数值范围（API 层 clamp 用）
DRAWDOWN_RANGE = (5, 60)
SINGLE_PCT_RANGE = (5, 50)


def _parse(raw: str) -> dict:
    """把存储的 JSON 串解析并与默认档合并（丢弃未知键）；解析失败返回默认档。"""
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return dict(DEFAULT_STYLE)
    if not isinstance(data, dict):
        return dict(DEFAULT_STYLE)
    merged = dict(DEFAULT_STYLE)
    merged.update({k: v for k, v in data.items() if k in DEFAULT_STYLE})
    return merged


def load_style(store) -> dict:
    """读取用户风格（dict）；无则返回默认档副本。"""
    raw = store.get_preference(STYLE_KEY, "")
    return _parse(raw) if raw else dict(DEFAULT_STYLE)


def save_style(store, style: dict) -> None:
    store.save_preference(STYLE_KEY, json.dumps(style, ensure_ascii=False), category=STYLE_CATEGORY)


def get_user_single_cap(store) -> int | None:
    """用户设定的单一持仓上限%（供 L2 确定性钳制取 min）；未设返回 None。"""
    raw = store.get_preference(STYLE_KEY, "")
    if not raw:
        return None
    v = _parse(raw).get("max_single_pct")
    return int(v) if isinstance(v, (int, float)) else None


def format_persona_for_prompt(store) -> str:
    """把用户风格渲染成中文硬约束块，注入各层决策 prompt。

    无自定义时返回中性提示（避免误导 LLM 以为用户偏保守/激进）。
    """
    raw = store.get_preference(STYLE_KEY, "")
    if not raw:
        return "## 用户个人持仓风格\n用户未设定个人风格，按市场中性处理即可。"
    style = _parse(raw)
    lines = [
        "## 用户个人持仓风格（硬约束 — 市场再好也不得突破回撤与单一持仓上限）",
        f"- 风险偏好：{_RISK_LABELS.get(style['risk_appetite'], style['risk_appetite'])}",
        f"- 最大可接受回撤：{style['max_drawdown_pct']}%（组合回撤逼近此值须显著降险）",
        f"- 单一持仓上限：{style['max_single_pct']}%（任何单只股票权重不得超过）",
        f"- 持仓集中度：{_CONC_LABELS.get(style['concentration'], style['concentration'])}",
        f"- 持有周期：{_HORIZON_LABELS.get(style['horizon'], style['horizon'])}",
        f"- 现金择时倾向：{_CASH_LABELS.get(style['cash_timing'], style['cash_timing'])}",
        f"- 止损纪律：{_STOP_LABELS.get(style['stop_loss'], style['stop_loss'])}",
    ]
    notes = (style.get("sector_notes") or "").strip()
    if notes:
        lines.append(f"- 行业偏好/排除：{notes}")
    return "\n".join(lines)


if __name__ == "__main__":
    # ponytail: 自检 —— 空偏好走中性提示；有偏好含关键字段且能被下游钳制取用
    class _FakeStore:
        def __init__(self, val=""):
            self._val = val
        def get_preference(self, key, default=""):
            return self._val or default

    empty = format_persona_for_prompt(_FakeStore())
    assert "未设定" in empty, empty
    assert get_user_single_cap(_FakeStore()) is None

    styled = _FakeStore(json.dumps({
        "risk_appetite": "conservative", "max_drawdown_pct": 15,
        "max_single_pct": 12, "sector_notes": "偏好科技、排除白酒",
        "bogus": "drop_me",
    }))
    txt = format_persona_for_prompt(styled)
    assert "保守" in txt and "15%" in txt and "12%" in txt, txt
    assert "偏好科技、排除白酒" in txt, txt
    assert get_user_single_cap(styled) == 12
    assert "bogus" not in json.dumps(load_style(styled), ensure_ascii=False)  # 未知键被丢弃
    print("persona self-check OK")
