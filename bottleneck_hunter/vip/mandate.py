"""每账户「投资纲领」— 用户手动填写的该账户投资设想与目标，作为 LLM 分析决策依据。

这是 watchlist/persona.py（模拟盘个人风格）的**账户维度版本**：persona 按用户+市场，
纲领按 account_ref（一个人的多个真实券商账户可各有不同目标：某个专注美股科技激进、
某个港股防守收息）。存储复用 user_preferences（key 带 account_ref 前缀，market 由 store 自动隔离），
零建表 —— account_ref 唯一于 (user,market)，key 不冲突。

渲染成中文约束块注入各 LLM 决策面（VIP 报告叙事 / 实时咨询 / Phase B 分层建议）：
- 最大回撤 / 排除清单 = 硬约束（建议不得突破 / 不得推荐）
- 年度收益目标 / 市场行业聚焦 / 长期原则 = 软偏好（指引方向，不强制）
"""
from __future__ import annotations

import json

# 复用 persona 的档位标签，避免重造（风险偏好/持有周期两档语义一致）
from bottleneck_hunter.watchlist.persona import _HORIZON_LABELS, _RISK_LABELS

MANDATE_KEY = "vip_mandate"          # 实际存储 key = f"{MANDATE_KEY}::{account_ref}"
MANDATE_CATEGORY = "vip_mandate"

DEFAULT_MANDATE = {
    "risk_appetite": "balanced",       # aggressive | balanced | conservative
    "annual_return_target_pct": 0,     # 年度收益目标%（0=未设定，不强制）
    "max_drawdown_pct": 25,            # 最大可接受回撤%（硬约束，5-60）
    "horizon": "swing",                # short | swing | long
    "focus_markets": "",               # 专注市场，自由文本（如「美股为主，少量港股」）
    "focus_sectors": "",               # 专注行业方向，自由文本（如「AI 算力、半导体」）
    "principles": "",                  # 长期投资原则，自由文本
    "exclusions": "",                  # 排除/禁投清单，自由文本（硬约束：不得推荐）
}

# 数值范围（API 层 clamp 用）
RETURN_TARGET_RANGE = (0, 200)
DRAWDOWN_RANGE = (5, 60)


def _key(account_ref: str) -> str:
    return f"{MANDATE_KEY}::{(account_ref or '').strip()}"


def _resolve(store, account_ref: str) -> str:
    if hasattr(store, "resolve_vip_account_ref"):
        try:
            return store.resolve_vip_account_ref(account_ref)
        except Exception:  # noqa: BLE001 —— 无账户/多账户未指定时，退回原值（走默认档）
            pass
    return (account_ref or "").strip()


def _parse(raw: str) -> dict:
    """解析存储 JSON 并与默认档合并（丢弃未知键）；失败返回默认档。"""
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return dict(DEFAULT_MANDATE)
    if not isinstance(data, dict):
        return dict(DEFAULT_MANDATE)
    merged = dict(DEFAULT_MANDATE)
    merged.update({k: v for k, v in data.items() if k in DEFAULT_MANDATE})
    return merged


def load_mandate(store, account_ref: str = "") -> dict:
    """读取该账户纲领（dict）；未设定返回默认档副本。"""
    ref = _resolve(store, account_ref)
    raw = store.get_preference(_key(ref), "")
    return _parse(raw) if raw else dict(DEFAULT_MANDATE)


def save_mandate(store, mandate: dict, account_ref: str = "") -> dict:
    """写入该账户纲领（做范围 clamp + 枚举校验），返回落库后的规范化 dict。"""
    ref = _resolve(store, account_ref)
    clean = dict(DEFAULT_MANDATE)
    for k, v in (mandate or {}).items():
        if k not in DEFAULT_MANDATE:
            continue
        clean[k] = v
    # 枚举校验
    if clean["risk_appetite"] not in _RISK_LABELS:
        clean["risk_appetite"] = "balanced"
    if clean["horizon"] not in _HORIZON_LABELS:
        clean["horizon"] = "swing"
    # 数值 clamp
    clean["annual_return_target_pct"] = _clamp(clean["annual_return_target_pct"], RETURN_TARGET_RANGE, 0)
    clean["max_drawdown_pct"] = _clamp(clean["max_drawdown_pct"], DRAWDOWN_RANGE, 25)
    # 文本裁剪（防超长注入）
    for k in ("focus_markets", "focus_sectors", "principles", "exclusions"):
        clean[k] = (str(clean.get(k) or "")).strip()[:500]
    store.save_preference(_key(ref), json.dumps(clean, ensure_ascii=False), category=MANDATE_CATEGORY)
    return clean


def _clamp(v, rng, default):
    try:
        n = float(v)
    except (TypeError, ValueError):
        return default
    return int(min(max(n, rng[0]), rng[1]))


def format_mandate_for_prompt(store, account_ref: str = "") -> str:
    """渲染成中文约束块注入 LLM。未设定返回中性提示（避免误导 LLM 以为账户有特定倾向）。"""
    ref = _resolve(store, account_ref)
    raw = store.get_preference(_key(ref), "")
    if not raw:
        return "## 本账户投资纲领\n用户尚未为本账户设定投资目标，按中性稳健处理即可。"
    m = _parse(raw)
    lines = [
        "## 本账户投资纲领（用户为本账户设定的投资设想与目标 —— 分析与建议须遵循）",
        f"- 风险偏好：{_RISK_LABELS.get(m['risk_appetite'], m['risk_appetite'])}",
    ]
    if m.get("annual_return_target_pct"):
        lines.append(f"- 年度收益目标：约 {m['annual_return_target_pct']}%（目标非承诺，建议应朝此努力但不为达标而过度冒险）")
    lines.append(f"- 最大可接受回撤：{m['max_drawdown_pct']}%（**硬约束** —— 组合回撤逼近此值须显著降险，建议不得引导突破）")
    lines.append(f"- 持有周期取向：{_HORIZON_LABELS.get(m['horizon'], m['horizon'])}")
    if m.get("focus_markets"):
        lines.append(f"- 专注市场：{m['focus_markets']}")
    if m.get("focus_sectors"):
        lines.append(f"- 专注行业方向：{m['focus_sectors']}")
    if m.get("principles"):
        lines.append(f"- 长期投资原则：{m['principles']}")
    if m.get("exclusions"):
        lines.append(f"- 排除/禁投：{m['exclusions']}（**硬约束** —— 不得推荐此类标的）")
    return "\n".join(lines)


if __name__ == "__main__":
    # ponytail 自检：空纲领走中性；有纲领含关键字段+硬约束标记+新增字段；clamp/枚举生效；未知键丢弃
    class _FakeStore:
        def __init__(self, val=""):
            self._val = val
        def get_preference(self, key, default=""):
            return self._val or default
        def save_preference(self, key, value, category="general"):
            self._val = value
        def resolve_vip_account_ref(self, ref=""):
            return ref or "ACC-1"

    empty = format_mandate_for_prompt(_FakeStore())
    assert "尚未" in empty, empty

    s = _FakeStore()
    saved = save_mandate(s, {
        "risk_appetite": "aggressive", "annual_return_target_pct": 30,
        "max_drawdown_pct": 99,  # 超范围→clamp到60
        "horizon": "bogus_horizon",  # 非法枚举→回退swing
        "focus_sectors": "AI 算力、半导体", "principles": "只买看得懂的生意，长期持有",
        "exclusions": "白酒、纯题材小盘", "bogus": "drop_me",
    }, account_ref="ACC-1")
    assert saved["max_drawdown_pct"] == 60, saved
    assert saved["horizon"] == "swing", saved
    assert "bogus" not in json.dumps(saved, ensure_ascii=False)

    txt = format_mandate_for_prompt(s, account_ref="ACC-1")
    assert "激进" in txt and "30%" in txt and "60%" in txt, txt
    assert "AI 算力、半导体" in txt and "白酒" in txt, txt
    assert "硬约束" in txt, txt
    assert load_mandate(s, "ACC-1")["risk_appetite"] == "aggressive"
    print("mandate self-check OK")
