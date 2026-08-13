"""决策证据溯源：给 L1-L4 / VIP 产出打 provenance（prompt 哈希 + 实际 model + 快照日 + ticker 集）。

嵌进各层 result_json（零表迁移）。目的：复盘「哪个 prompt + 哪个模型 + 哪日数据」生成此判断，
排障「模型幻觉 vs 数据错」，VIP 可辩护性。
ponytail: 只做可复现溯源；不做 fsync 防篡改 hash 链 / prev_hash 轻链（YAGNI，需防篡改时再升级）。
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

# 与 decision_engine.PROMPTS_DIR 同一解析（本文件在 watchlist/，prompts 在 ../chain/prompts）。
_PROMPTS_DIR = Path(__file__).resolve().parents[1] / "chain" / "prompts"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def hash_text(text: str) -> str:
    """sha256[:12]，用于哈希**内联** prompt 字符串（如 VIP 的 _DRAFT_PROMPT，非 .md 文件）。"""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:12]


@lru_cache(maxsize=128)
def prompt_hash(name: str) -> str:
    """读 chain/prompts/{name}.md 取 sha256[:12]；文件改一字即哈希变，缺失返回 'missing'。

    lru_cache 缓存进程内结果；prompt.md 属静态资源，运行期不改，无需失效。
    """
    try:
        return hashlib.sha256((_PROMPTS_DIR / f"{name}.md").read_bytes()).hexdigest()[:12]
    except OSError:
        return "missing"


def build_provenance(*, prompts, models, data_as_of: str = "", tickers=None,
                     generated_at: str = "", extra: dict | None = None,
                     extra_prompt_hashes: dict | None = None) -> dict:
    """产出可复现溯源 dict。

    prompts: prompt 名列表（如 ["decision_macro"]）→ 逐个取 sha256[:12]。
    models: [(provider, model), ...] 或 [{"provider","model"}] → 归一为 [{"provider","model"}]。
    data_as_of: 数据快照日（如 _today()）。
    generated_at: UTC ISO；缺省用当前 UTC 时刻。
    extra: 额外键（如 {"market": "us_stock"}）合并进结果。
    extra_prompt_hashes: 内联 prompt 的 {名: 哈希}（如 {"vip_draft": hash_text(_DRAFT_PROMPT)}），并入 prompt_hashes。
    """
    models_used = []
    for m in models or []:
        if isinstance(m, dict):
            models_used.append({"provider": m.get("provider", ""), "model": m.get("model", "")})
        else:
            models_used.append({"provider": m[0], "model": m[1]})
    ph = {n: prompt_hash(n) for n in (prompts or [])}
    if extra_prompt_hashes:
        ph.update(extra_prompt_hashes)
    out = {
        "prompt_hashes": ph,
        "models_used": models_used,
        "data_as_of": data_as_of,
        "tickers": sorted({t for t in (tickers or []) if t}),
        "generated_at": generated_at or _now_iso(),
    }
    if extra:
        out.update(extra)
    return out
