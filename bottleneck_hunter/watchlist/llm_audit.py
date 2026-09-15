"""LLM 调用审计与成本模型（P2-2）

在「一次 LLM 调用」维度上产出一条**不可变、可追溯**的审计记录，把散落在四处的信号
（预算里的 token/成本、遥测里的耗时、provenance 里的 prompt/快照）收敛成单条记录，
使「每次调用可关联用户、策略与快照」可执行、可验证：

- 关联：`user_id` + `strategy_version` + `snapshot_id`（复用 P0-2/P0-3 的快照与策略版本）。
- prompt：复用 `provenance` 的 prompt 哈希（存哈希不存原文，天然不泄漏 Key/敏感上下文）。
- 版本：`strategy_version` + `model_version` + prompt 哈希三者共同定版。
- 耗时：`latency_ms`（调用方从 monotonic 计时传入，与 fallback 遥测同源口径）。
- 成本：内置**确定性**定价表 token→USD，补齐既有 `estimated_cost_usd` 无人计算的缺口。
- 覆写：`overridden` / `override_reason` / `override_by` 记录人工改判。

与既有能力的分工（互补，不替代）：
- `store_budget.record_llm_usage` 是**按日聚合**的预算账；本模块是**逐次调用**的审计条目。
- `store_ai_models.record_model_call` 是**按日聚合**的健康/延迟遥测；本模块保留单次口径 + 成本 + 溯源。
- `provenance.build_provenance` 把 prompt/model/快照嵌进决策 `result_json`；本模块把同类溯源
  提到「调用」粒度并补上成本/耗时/覆写，形成可查询的审计条目。

纯 stdlib、确定可复现（定价与哈希给定输入必得同一结果），不引入 numpy/scipy。
**诊断/审计层，不接线进生产 LLM 调用链**（回退=不调用，现有调用路径完全不变）；
持久化 Store 表留待接线时再建（YAGNI）。定价表是校准旋钮而非硬事实，接实盘按各家价目表校准。
"""

from __future__ import annotations

import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from bottleneck_hunter.watchlist.provenance import prompt_hash

# ---------------------------------------------------------------------------
# 成本模型：token → USD
# ---------------------------------------------------------------------------

# 定价表：provider → {model 前缀: (输入价/1K tok USD, 输出价/1K tok USD)}。
# "" 为该 provider 缺省价，按最长匹配前缀命中；未知 provider/model 回退 _DEFAULT_PRICE。
# ponytail: 价目是校准旋钮，随各家调价更新；回退宁可粗估不为 0（0 会让成本审计静默失真）。
_DEFAULT_PRICE: tuple[float, float] = (0.001, 0.002)
_PRICING: dict[str, dict[str, tuple[float, float]]] = {
    "openai": {"": (0.0005, 0.0015), "gpt-4o": (0.005, 0.015), "gpt-4o-mini": (0.00015, 0.0006),
               "gpt-4": (0.03, 0.06), "o1": (0.015, 0.06)},
    "anthropic": {"": (0.003, 0.015), "claude-3-5-haiku": (0.0008, 0.004),
                  "claude-3-5-sonnet": (0.003, 0.015), "claude-3-opus": (0.015, 0.075)},
    "deepseek": {"": (0.00027, 0.0011), "deepseek-reasoner": (0.00055, 0.00219)},
    "google": {"": (0.00035, 0.00105), "gemini-1.5-pro": (0.00125, 0.005),
               "gemini-1.5-flash": (0.000075, 0.0003)},
    "qwen": {"": (0.0004, 0.0012), "qwen-max": (0.0016, 0.0064)},
    "glm": {"": (0.0005, 0.0005)},
    "openrouter": {"": _DEFAULT_PRICE},
    "ollama": {"": (0.0, 0.0)},  # 本地自托管，无 API 费用
}


def price_for(provider: str, model: str) -> tuple[float, float]:
    """取 (输入价, 输出价)/1K tok（USD）。model 按最长前缀命中；缺省回退 provider 缺省价，再回退全局缺省价。"""
    p = (provider or "").lower().strip()
    m = (model or "").lower().strip()
    table = _PRICING.get(p)
    if table is None:
        return _DEFAULT_PRICE
    best: tuple[float, float] | None = None
    best_len = -1
    for prefix, price in table.items():
        if m.startswith(prefix) and len(prefix) > best_len:
            best, best_len = price, len(prefix)
    return best if best is not None else _DEFAULT_PRICE


def estimate_cost(provider: str, model: str, input_tokens: int, output_tokens: int) -> float:
    """token→USD，保留 6 位小数。负数当 0。"""
    in_p, out_p = price_for(provider, model)
    it = max(int(input_tokens or 0), 0)
    ot = max(int(output_tokens or 0), 0)
    return round(it / 1000.0 * in_p + ot / 1000.0 * out_p, 6)


def estimate_tokens(text: str) -> int:
    """极粗 token 估算：CJK 字≈1 token，其余≈4 字符/token。

    ponytail: 无 tokenizer 依赖的启发式，真实值应优先取 provider usage 回包；
    仅在上游未给 token 数时兜底，勿当计费真值。
    """
    if not text:
        return 0
    cjk = sum(1 for ch in text if "一" <= ch <= "鿿")
    other = len(text) - cjk
    return cjk + (other + 3) // 4


# ---------------------------------------------------------------------------
# 密钥遮蔽（自由文本字段的纵深防线）
# ---------------------------------------------------------------------------

_SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]+"),
    re.compile(r"[A-Za-z0-9_\-]{32,}"),  # 泛化的长令牌
]


def redact_secrets(text: str) -> str:
    """遮蔽疑似密钥/令牌（sk-*、Bearer *、超长令牌）。

    ponytail: 保守正则，宁可多遮不漏；审计只存 prompt 哈希不存原文，
    本函数是自由文本字段（原因/覆写说明）的纵深防线，过度遮蔽可接受。
    """
    if not text:
        return text or ""
    out = text
    for pat in _SECRET_PATTERNS:
        out = pat.sub("***", out)
    return out


# ---------------------------------------------------------------------------
# 审计记录
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LlmCallAudit:
    """单次 LLM 调用的不可变审计记录。字段即审计六维 + 用户/策略/快照关联。"""

    call_id: str
    created_at: str
    user_id: str = ""
    strategy_version: str = ""
    snapshot_id: str = ""
    task: str = ""                       # 角色/任务（如 l1_macro / committee_growth）
    provider: str = ""
    model: str = ""
    model_version: str = ""
    prompt_hashes: dict = field(default_factory=dict)   # {prompt 名: sha256[:12]}
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: float = 0.0
    ok: bool = True
    reason: str = ""
    overridden: bool = False             # 是否人工改判
    override_reason: str = ""
    override_by: str = ""
    extra: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        """扁平 dict，供序列化/持久化（接线时按需 json 编码 prompt_hashes/extra）。"""
        return asdict(self)


def build_call_audit(*, user_id: str = "", strategy_version: str = "", snapshot_id: str = "",
                     provider: str = "", model: str = "", model_version: str = "", task: str = "",
                     prompts=None, prompt_hashes: dict | None = None,
                     input_tokens: int = 0, output_tokens: int = 0, latency_ms: float = 0.0,
                     cost_usd: float | None = None, ok: bool = True, reason: str = "",
                     overridden: bool = False, override_reason: str = "", override_by: str = "",
                     call_id: str = "", created_at: str = "", extra: dict | None = None) -> LlmCallAudit:
    """组装一条审计记录。

    prompts: prompt 名列表 → 逐个取 sha256[:12]（复用 provenance）；prompt_hashes: 内联 prompt 的
    {名: 哈希} 合并进来。cost_usd 缺省由定价表按 token 估算。自由文本（reason/override_reason）过密钥遮蔽。
    call_id 缺省用 uuid4，created_at 缺省当前 UTC。
    """
    ph = {n: prompt_hash(n) for n in (prompts or [])}
    if prompt_hashes:
        ph.update(prompt_hashes)
    if cost_usd is None:
        cost = estimate_cost(provider, model, input_tokens, output_tokens)
    else:
        cost = round(float(cost_usd), 6)
    return LlmCallAudit(
        call_id=call_id or uuid.uuid4().hex,
        created_at=created_at or _now_iso(),
        user_id=user_id, strategy_version=strategy_version, snapshot_id=snapshot_id,
        task=task, provider=(provider or "").lower().strip(), model=model or "", model_version=model_version,
        prompt_hashes=ph,
        input_tokens=max(int(input_tokens or 0), 0), output_tokens=max(int(output_tokens or 0), 0),
        cost_usd=cost, latency_ms=max(float(latency_ms or 0.0), 0.0),
        ok=bool(ok), reason=redact_secrets(reason),
        overridden=bool(overridden), override_reason=redact_secrets(override_reason), override_by=override_by,
        extra=dict(extra or {}),
    )


def summarize(audits) -> dict:
    """把一批审计条目汇总成报表：总量 + 按 (provider/model) + 按 strategy_version。

    供增量消融成本核算与审计报表（P2-3/P2-4）复用。
    """
    audits = list(audits)
    total = {
        "calls": len(audits),
        "input_tokens": sum(a.input_tokens for a in audits),
        "output_tokens": sum(a.output_tokens for a in audits),
        "cost_usd": round(sum(a.cost_usd for a in audits), 6),
        "overridden": sum(1 for a in audits if a.overridden),
        "failed": sum(1 for a in audits if not a.ok),
    }
    by_model: dict[str, dict] = {}
    for a in audits:
        d = by_model.setdefault(f"{a.provider}/{a.model}",
                                {"calls": 0, "cost_usd": 0.0, "input_tokens": 0, "output_tokens": 0})
        d["calls"] += 1
        d["cost_usd"] = round(d["cost_usd"] + a.cost_usd, 6)
        d["input_tokens"] += a.input_tokens
        d["output_tokens"] += a.output_tokens
    by_strategy: dict[str, dict] = {}
    for a in audits:
        d = by_strategy.setdefault(a.strategy_version or "", {"calls": 0, "cost_usd": 0.0})
        d["calls"] += 1
        d["cost_usd"] = round(d["cost_usd"] + a.cost_usd, 6)
    return {"total": total, "by_model": by_model, "by_strategy": by_strategy}


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


if __name__ == "__main__":
    # ponytail 自检：成本模型 + token 估算 + 密钥遮蔽 + 审计组装 + 汇总 + 不可变 + 确定性
    from dataclasses import FrozenInstanceError

    # 定价：最长前缀命中 + provider 缺省 + 全局回退
    assert price_for("openai", "gpt-4o") == (0.005, 0.015)
    assert price_for("openai", "gpt-4o-mini") == (0.00015, 0.0006), "更长前缀应压过 gpt-4o"
    assert price_for("openai", "gpt-3.5-turbo") == (0.0005, 0.0015), "provider 缺省价"
    assert price_for("unknown", "x") == _DEFAULT_PRICE, "未知 provider 全局回退"
    assert price_for("ollama", "llama3") == (0.0, 0.0), "本地无费用"

    # 成本：1000 in + 500 out @ deepseek 缺省(0.00027/0.0011) = 0.00027 + 0.00055 = 0.00082
    assert estimate_cost("deepseek", "deepseek-chat", 1000, 500) == 0.00082
    assert estimate_cost("openai", "gpt-4o", 1000, 1000) == 0.02
    assert estimate_cost("ollama", "llama3", 9999, 9999) == 0.0
    assert estimate_cost("openai", "gpt-4o", -5, 0) == 0.0, "负 token 当 0"

    # token 估算：CJK≈1、其余≈4 字符/token
    assert estimate_tokens("你好世界") == 4
    assert estimate_tokens("hello") == 2 and estimate_tokens("") == 0

    # 密钥遮蔽
    assert redact_secrets("key sk-abcdefghij1234567890 end") == "key *** end"
    assert "***" in redact_secrets("Authorization: Bearer abc.def-123")
    assert redact_secrets("a normal short reason") == "a normal short reason"

    # 组装：携带 用户/策略/快照，prompt 名→哈希 + 内联哈希合并，成本自动估算
    a1 = build_call_audit(user_id="u1", strategy_version="s:v1", snapshot_id="snap-1",
                          provider="DeepSeek", model="deepseek-chat", task="l1_macro",
                          prompts=["decision_macro"], prompt_hashes={"inline_x": "abc123"},
                          input_tokens=1000, output_tokens=500, latency_ms=1234.5,
                          call_id="c1", created_at="2026-09-15T00:00:00Z")
    assert (a1.user_id, a1.strategy_version, a1.snapshot_id) == ("u1", "s:v1", "snap-1")
    assert a1.provider == "deepseek" and a1.cost_usd == 0.00082
    assert a1.prompt_hashes["inline_x"] == "abc123" and "decision_macro" in a1.prompt_hashes
    assert a1.prompt_hashes["decision_macro"] == prompt_hash("decision_macro")

    # 覆写 + 自由文本遮蔽
    a2 = build_call_audit(provider="openai", model="gpt-4o", input_tokens=100, output_tokens=100,
                          overridden=True, override_reason="admin 用 sk-zzzzzzzzzzzzzzzzzz 覆写", override_by="admin",
                          ok=False, reason="频率限制", call_id="c2", created_at="2026-09-15T00:00:00Z")
    assert a2.overridden and a2.override_by == "admin" and "sk-" not in a2.override_reason
    assert a2.ok is False and a2.cost_usd == estimate_cost("openai", "gpt-4o", 100, 100)

    # 显式 cost_usd 覆盖估算
    a3 = build_call_audit(provider="openai", model="gpt-4o", input_tokens=1000, output_tokens=1000,
                          cost_usd=0.123456, call_id="c3", created_at="2026-09-15T00:00:00Z")
    assert a3.cost_usd == 0.123456

    # 汇总报表
    rep = summarize([a1, a2, a3])
    assert rep["total"]["calls"] == 3 and rep["total"]["overridden"] == 1 and rep["total"]["failed"] == 1
    assert rep["by_model"]["deepseek/deepseek-chat"]["calls"] == 1
    assert rep["by_model"]["openai/gpt-4o"]["calls"] == 2
    assert rep["by_strategy"]["s:v1"]["calls"] == 1

    # 不可变
    try:
        a1.cost_usd = 9.9  # type: ignore[misc]
        raise AssertionError("frozen 记录不可改")
    except FrozenInstanceError:
        pass

    # 确定性：同输入必得同记录
    kw = dict(user_id="u", strategy_version="v", snapshot_id="s", provider="qwen", model="qwen-plus",
              input_tokens=321, output_tokens=123, call_id="fix", created_at="2026-09-15T00:00:00Z")
    assert build_call_audit(**kw) == build_call_audit(**kw)

    print("llm_audit 自检通过：成本模型 + token 估算 + 密钥遮蔽 + 审计组装 + 汇总 + 不可变 + 确定性")
