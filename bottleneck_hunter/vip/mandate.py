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
import re

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


# ── P1-1：纲领数值校验器 —— 把硬约束从「prompt 文本 + LLM 自由推理」变确定性结构化对账 ──
# 单仓/板块集中度上限按风险偏好档位映射（single%, sector%）。数据来源=dossier（零 LLM、纯读）。
_CONCENTRATION_CEILINGS = {
    "conservative": (15.0, 30.0),
    "balanced": (20.0, 40.0),
    "aggressive": (30.0, 50.0),
}
_APPROACH_RATIO = 0.8  # 达上限 80% 即「逼近」预警（未破为硬违规，仅 warn）


def _split_keywords(text: str) -> list[str]:
    """把自由文本排除/聚焦清单切成关键词（顿号/逗号/斜杠/分号/空白分隔）；丢 <2 字噪声（防单字符乱命中）。"""
    parts = re.split(r"[、,，/／;；\s]+", str(text or ""))
    return [p.strip() for p in parts if len(p.strip()) >= 2]


def check_mandate_compliance(mandate: dict, dossier: dict) -> dict:
    """确定性核算持仓 vs 纲领硬约束（集中度/排除/回撤）+ 软偏好（聚焦板块），喂投委会 risk_officer + 前端对账面板。

    替代 _consensus 里「风控否决=硬约束破坏」的脆弱代理（关键词扫叙述）——这里直接结构化判定，
    让 risk_officer 收到**结构化违规信号**而非纯文本自由推理。纯函数、零 LLM、零网络。

    checks 每项：{key,label,severity(hard|soft),ok(bool|None),warn,detail,items?}
      ok=None → 数据不足/未配置，无法判定（不硬凑通过，也不算违规）。
    返回 compliant（无 hard 破坏）/ violations（hard 破坏项）/ warnings（逼近项）/ ceilings / basis_note。
    """
    m = mandate or DEFAULT_MANDATE
    ra = m.get("risk_appetite") if m.get("risk_appetite") in _CONCENTRATION_CEILINGS else "balanced"
    ceil_single, ceil_sector = _CONCENTRATION_CEILINGS[ra]
    holdings = [h for h in (dossier.get("holdings") or []) if isinstance(h, dict)]
    derivs = [d for d in (dossier.get("derivative_exposure") or []) if isinstance(d, dict)]
    checks: list[dict] = []

    # ① 排除/禁投（硬）：持仓/衍生品标的的 代码/板块/瓶颈节点 子串命中排除关键词即违规
    ex_kws = _split_keywords(m.get("exclusions", ""))
    if not ex_kws:
        checks.append({"key": "exclusions", "label": "排除清单", "severity": "hard",
                       "ok": None, "warn": False, "detail": "未设置排除清单"})
    else:
        hits: list[str] = []
        for h in holdings:
            hay = f"{h.get('ticker', '')} {h.get('sector', '')} {h.get('bottleneck_node', '')}".lower()
            if any(kw.lower() in hay for kw in ex_kws) and h.get("ticker"):
                hits.append(str(h["ticker"]))
        for d in derivs:
            hay = f"{d.get('underlying', '')} {d.get('family', '')}".lower()
            if any(kw.lower() in hay for kw in ex_kws) and d.get("underlying"):
                hits.append(str(d["underlying"]))
        hits = list(dict.fromkeys(hits))
        checks.append({"key": "exclusions", "label": "排除清单", "severity": "hard",
                       "ok": not hits, "warn": False, "items": hits,
                       "detail": (f"{len(hits)} 只标的命中排除清单：{'、'.join(hits)}" if hits
                                  else "持仓/衍生品未命中排除清单")})

    # ② 最大回撤（硬）：perf_summary.max_drawdown_pct（负值/稀疏近似）逼近/突破纲领上限
    perf = dossier.get("perf_summary") or {}
    dd = perf.get("max_drawdown_pct")
    limit = float(m.get("max_drawdown_pct") or 0) or 0.0
    n_pts = perf.get("n_points")
    basis = f"基于 {n_pts} 期结单点·指示性" if n_pts else "指示性"
    if dd is None or limit <= 0:
        checks.append({"key": "max_drawdown", "label": "最大回撤", "severity": "hard",
                       "ok": None, "warn": False,
                       "detail": "结算单点不足或未设回撤上限，暂无法核算"})
    else:
        dd_abs = abs(float(dd))
        breach = dd_abs >= limit
        approach = (not breach) and dd_abs >= _APPROACH_RATIO * limit
        checks.append({"key": "max_drawdown", "label": "最大回撤", "severity": "hard",
                       "ok": not breach, "warn": approach,
                       "detail": (f"组合最大回撤 {dd_abs:.2f}% "
                                  + ("已达/超" if breach else ("逼近" if approach else "低于"))
                                  + f"纲领上限 {limit:.0f}%（{basis}）")})

    # ③ 集中度（硬）：单仓 / 板块 权重上限按风险档位映射。weight_pct 为百分比口径。
    weights = [(str(h.get("ticker") or ""), float(h.get("weight_pct") or 0),
                (str(h.get("sector") or "").strip())) for h in holdings]
    if not weights:
        checks.append({"key": "single_concentration", "label": "单仓集中度", "severity": "hard",
                       "ok": None, "warn": False, "detail": "无持仓，未核算单仓集中度"})
    else:
        tk, w, _ = max(weights, key=lambda x: x[1])
        breach = w > ceil_single
        approach = (not breach) and w >= _APPROACH_RATIO * ceil_single
        checks.append({"key": "single_concentration", "label": "单仓集中度", "severity": "hard",
                       "ok": not breach, "warn": approach, "items": [tk] if breach else [],
                       "detail": f"最大单仓 {tk} {w:.1f}% vs {ra} 档上限 {ceil_single:.0f}%"})

    sector_w: dict[str, float] = {}
    for _, w, sec in weights:
        if sec and sec != "未知":
            sector_w[sec] = sector_w.get(sec, 0.0) + w
    if not sector_w:
        checks.append({"key": "sector_concentration", "label": "板块集中度", "severity": "hard",
                       "ok": None, "warn": False, "detail": "板块数据不全，未核算板块集中度"})
    else:
        sec, sw = max(sector_w.items(), key=lambda x: x[1])
        known = sum(1 for _, _, s in weights if s and s != "未知")
        cov = f"（板块覆盖 {known}/{len(weights)} 仓，为下限估计）" if known < len(weights) else ""
        breach = sw > ceil_sector
        approach = (not breach) and sw >= _APPROACH_RATIO * ceil_sector
        checks.append({"key": "sector_concentration", "label": "板块集中度", "severity": "hard",
                       "ok": not breach, "warn": approach, "items": [sec] if breach else [],
                       "detail": f"最大板块「{sec}」{sw:.1f}% vs {ra} 档上限 {ceil_sector:.0f}%{cov}"})

    # ④ 聚焦板块（软）：只作覆盖提示，永不硬拦
    fk = _split_keywords(m.get("focus_sectors", ""))
    if not fk or not weights:
        checks.append({"key": "focus_sectors", "label": "聚焦板块", "severity": "soft",
                       "ok": None, "warn": False, "detail": "未设置聚焦板块或无持仓"})
    else:
        focus_wt = sum(w for tk, w, sec in weights
                       if any(k.lower() in f"{tk} {sec}".lower() for k in fk))
        checks.append({"key": "focus_sectors", "label": "聚焦板块", "severity": "soft",
                       "ok": True, "warn": False,
                       "detail": f"专注方向覆盖约 {focus_wt:.1f}% 权益（软偏好，仅指引）"})

    hard_breaches = [c for c in checks if c["severity"] == "hard" and c["ok"] is False]
    return {
        "compliant": not hard_breaches,
        "risk_appetite": ra,
        "ceilings": {"single_pct": ceil_single, "sector_pct": ceil_sector},
        "checks": checks,
        "violations": [{"key": c["key"], "label": c["label"], "detail": c["detail"],
                        "items": c.get("items", [])} for c in hard_breaches],
        "warnings": [c["detail"] for c in checks if c.get("warn")],
        "basis_note": ("回撤基于稀疏结算单期末点为指示性；排除/板块为代码子串匹配（非公司名精确解析），"
                       "作硬约束提示而非替代人工尽调。"),
    }


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

    # ── P1-1 check_mandate_compliance 自检 ──
    # 保守档 single≤15/sector≤30；构造集中度破坏 + 排除命中 + 回撤逼近
    mdt = {"risk_appetite": "conservative", "max_drawdown_pct": 20,
           "exclusions": "白酒、TSLA", "focus_sectors": "AI 算力"}
    dsr = {
        "holdings": [
            {"ticker": "NVDA", "weight_pct": 40.0, "sector": "半导体", "bottleneck_node": "AI 算力"},
            {"ticker": "MU", "weight_pct": 25.0, "sector": "半导体", "bottleneck_node": ""},
            {"ticker": "TSLA", "weight_pct": 10.0, "sector": "汽车", "bottleneck_node": ""},
        ],
        "derivative_exposure": [],
        "perf_summary": {"max_drawdown_pct": -17.0, "n_points": 4},  # |−17| ≥ 0.8×20=16 → 逼近
    }
    comp = check_mandate_compliance(mdt, dsr)
    assert comp["compliant"] is False, comp
    keys = {v["key"] for v in comp["violations"]}
    assert "single_concentration" in keys, comp          # NVDA 40% > 15%
    assert "sector_concentration" in keys, comp           # 半导体 65% > 30%
    assert "exclusions" in keys, comp                      # TSLA 命中排除
    ex = next(c for c in comp["checks"] if c["key"] == "exclusions")
    assert "TSLA" in ex["items"], ex
    dd = next(c for c in comp["checks"] if c["key"] == "max_drawdown")
    assert dd["ok"] is True and dd["warn"] is True, dd     # 逼近未破 → ok 但 warn
    assert comp["ceilings"] == {"single_pct": 15.0, "sector_pct": 30.0}
    focus = next(c for c in comp["checks"] if c["key"] == "focus_sectors")
    assert focus["severity"] == "soft" and focus["ok"] is True, focus  # 软偏好永不硬拦

    # 全合规：均衡档、低集中、无排除命中、回撤远低于上限 → compliant，无违规
    ok_comp = check_mandate_compliance(
        {"risk_appetite": "balanced", "max_drawdown_pct": 30, "exclusions": "赌博"},
        {"holdings": [{"ticker": "AAPL", "weight_pct": 15.0, "sector": "科技"},
                      {"ticker": "KO", "weight_pct": 12.0, "sector": "消费"}],
         "perf_summary": {"max_drawdown_pct": -5.0, "n_points": 3}})
    assert ok_comp["compliant"] is True and not ok_comp["violations"], ok_comp

    # 数据不足：无持仓 + 无回撤点 → 集中度/回撤判 None（不硬凑通过、也不算违规）
    thin_comp = check_mandate_compliance({"risk_appetite": "aggressive", "max_drawdown_pct": 40},
                                         {"holdings": [], "perf_summary": {}})
    assert thin_comp["compliant"] is True, thin_comp       # 无 hard 破坏（None 不算破坏）
    assert all(c["ok"] is None for c in thin_comp["checks"] if c["key"] != "focus_sectors"), thin_comp

    print("mandate self-check OK")
