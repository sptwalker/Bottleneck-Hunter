"""P2-2 LLM 调用审计与成本模型专项测试。"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from bottleneck_hunter.watchlist.llm_audit import (
    LlmCallAudit,
    build_call_audit,
    estimate_cost,
    estimate_tokens,
    price_for,
    redact_secrets,
    summarize,
)
from bottleneck_hunter.watchlist.provenance import prompt_hash

# ---------------------------------------------------------------------------
# 定价表：最长前缀命中 + provider 缺省 + 全局回退
# ---------------------------------------------------------------------------


def test_price_longest_prefix_wins():
    assert price_for("openai", "gpt-4o") == (0.005, 0.015)
    # 更长前缀 gpt-4o-mini 必须压过 gpt-4o
    assert price_for("openai", "gpt-4o-mini") == (0.00015, 0.0006)


def test_price_provider_default_for_unknown_model():
    assert price_for("openai", "gpt-3.5-turbo") == (0.0005, 0.0015)


def test_price_global_fallback_for_unknown_provider():
    assert price_for("unknown-vendor", "whatever") == (0.001, 0.002)


def test_price_local_provider_is_free():
    assert price_for("ollama", "llama3") == (0.0, 0.0)


def test_price_case_insensitive():
    assert price_for("OpenAI", "GPT-4O") == price_for("openai", "gpt-4o")


# ---------------------------------------------------------------------------
# token→USD 成本
# ---------------------------------------------------------------------------


def test_estimate_cost_deepseek_default():
    # 1000 in @0.00027/1k + 500 out @0.0011/1k = 0.00027 + 0.00055
    assert estimate_cost("deepseek", "deepseek-chat", 1000, 500) == 0.00082


def test_estimate_cost_gpt4o():
    assert estimate_cost("openai", "gpt-4o", 1000, 1000) == 0.02


def test_estimate_cost_local_is_zero():
    assert estimate_cost("ollama", "llama3", 9999, 9999) == 0.0


def test_estimate_cost_negative_tokens_clamped():
    assert estimate_cost("openai", "gpt-4o", -5, -9) == 0.0


def test_estimate_cost_rounds_to_6dp():
    c = estimate_cost("google", "gemini-1.5-flash", 333, 777)
    assert c == round(c, 6)


# ---------------------------------------------------------------------------
# token 粗估
# ---------------------------------------------------------------------------


def test_estimate_tokens_cjk_one_each():
    assert estimate_tokens("你好世界") == 4


def test_estimate_tokens_ascii_four_per_token():
    assert estimate_tokens("hello") == 2  # 5 字符 → ceil(5/4)=2


def test_estimate_tokens_empty():
    assert estimate_tokens("") == 0
    assert estimate_tokens(None) == 0  # type: ignore[arg-type]


def test_estimate_tokens_mixed():
    # 2 CJK + 4 ascii → 2 + ceil(4/4)=1 → 3
    assert estimate_tokens("中文abcd") == 3


# ---------------------------------------------------------------------------
# 密钥遮蔽
# ---------------------------------------------------------------------------


def test_redact_openai_key():
    assert redact_secrets("key sk-abcdefghij1234567890 end") == "key *** end"


def test_redact_bearer():
    assert "***" in redact_secrets("Authorization: Bearer abc.def-123")
    assert "abc.def-123" not in redact_secrets("Authorization: Bearer abc.def-123")


def test_redact_long_token():
    out = redact_secrets("token=" + "A" * 40)
    assert "A" * 40 not in out and "***" in out


def test_redact_leaves_normal_text():
    assert redact_secrets("a normal short reason 频率限制") == "a normal short reason 频率限制"


def test_redact_empty():
    assert redact_secrets("") == ""
    assert redact_secrets(None) == ""  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 审计记录组装：用户/策略/快照关联 + prompt 哈希 + 成本
# ---------------------------------------------------------------------------


def _fixed(**kw):
    """带固定 call_id/created_at 的组装，便于断言。"""
    base = dict(call_id="c", created_at="2026-09-15T00:00:00Z")
    base.update(kw)
    return build_call_audit(**base)


def test_audit_links_user_strategy_snapshot():
    a = _fixed(user_id="u1", strategy_version="s:v1", snapshot_id="snap-1")
    assert (a.user_id, a.strategy_version, a.snapshot_id) == ("u1", "s:v1", "snap-1")


def test_audit_provider_normalized_lower():
    a = _fixed(provider="DeepSeek", model="deepseek-chat")
    assert a.provider == "deepseek"


def test_audit_cost_auto_estimated_from_tokens():
    a = _fixed(provider="deepseek", model="deepseek-chat", input_tokens=1000, output_tokens=500)
    assert a.cost_usd == 0.00082


def test_audit_explicit_cost_overrides_estimate():
    a = _fixed(provider="openai", model="gpt-4o", input_tokens=1000, output_tokens=1000, cost_usd=0.123456)
    assert a.cost_usd == 0.123456


def test_audit_prompt_names_hashed_and_inline_merged():
    a = _fixed(prompts=["decision_macro"], prompt_hashes={"inline_x": "abc123"})
    assert a.prompt_hashes["inline_x"] == "abc123"
    assert a.prompt_hashes["decision_macro"] == prompt_hash("decision_macro")


def test_audit_negative_tokens_clamped():
    a = _fixed(input_tokens=-3, output_tokens=-7)
    assert a.input_tokens == 0 and a.output_tokens == 0


def test_audit_negative_latency_clamped():
    a = _fixed(latency_ms=-1.0)
    assert a.latency_ms == 0.0


def test_audit_generates_call_id_and_timestamp_when_omitted():
    a = build_call_audit(provider="openai", model="gpt-4o")
    assert a.call_id and len(a.call_id) >= 8
    assert a.created_at.endswith("Z") and "T" in a.created_at


# ---------------------------------------------------------------------------
# 覆写记录 + 自由文本遮蔽
# ---------------------------------------------------------------------------


def test_audit_override_fields():
    a = _fixed(overridden=True, override_by="admin", override_reason="人工改判")
    assert a.overridden is True and a.override_by == "admin" and a.override_reason == "人工改判"


def test_audit_reason_and_override_reason_redacted():
    a = _fixed(reason="失败 sk-abcdefghij1234567890",
               override_reason="覆写 Bearer secrettoken123456")
    assert "sk-abcdefghij1234567890" not in a.reason
    assert "secrettoken123456" not in a.override_reason


def test_audit_ok_flag():
    assert _fixed(ok=False).ok is False
    assert _fixed().ok is True


# ---------------------------------------------------------------------------
# 不可变 + 确定性
# ---------------------------------------------------------------------------


def test_audit_is_frozen():
    a = _fixed(provider="openai", model="gpt-4o")
    with pytest.raises(FrozenInstanceError):
        a.cost_usd = 9.9  # type: ignore[misc]


def test_audit_deterministic_same_input_same_record():
    kw = dict(user_id="u", strategy_version="v", snapshot_id="s", provider="qwen", model="qwen-plus",
              input_tokens=321, output_tokens=123, call_id="fix", created_at="2026-09-15T00:00:00Z")
    assert build_call_audit(**kw) == build_call_audit(**kw)


def test_audit_as_dict_roundtrip():
    a = _fixed(provider="openai", model="gpt-4o", input_tokens=10, output_tokens=20)
    d = a.as_dict()
    assert d["provider"] == "openai" and d["input_tokens"] == 10
    assert d["call_id"] == "c"


# ---------------------------------------------------------------------------
# 汇总报表
# ---------------------------------------------------------------------------


def _sample_audits():
    a1 = _fixed(user_id="u1", strategy_version="s:v1", provider="deepseek", model="deepseek-chat",
                input_tokens=1000, output_tokens=500)
    a2 = _fixed(provider="openai", model="gpt-4o", input_tokens=100, output_tokens=100,
                overridden=True, ok=False)
    a3 = _fixed(provider="openai", model="gpt-4o", input_tokens=1000, output_tokens=1000, cost_usd=0.5)
    return [a1, a2, a3]


def test_summarize_totals():
    rep = summarize(_sample_audits())
    assert rep["total"]["calls"] == 3
    assert rep["total"]["overridden"] == 1
    assert rep["total"]["failed"] == 1
    assert rep["total"]["input_tokens"] == 2100


def test_summarize_by_model():
    rep = summarize(_sample_audits())
    assert rep["by_model"]["deepseek/deepseek-chat"]["calls"] == 1
    assert rep["by_model"]["openai/gpt-4o"]["calls"] == 2


def test_summarize_by_strategy():
    rep = summarize(_sample_audits())
    assert rep["by_strategy"]["s:v1"]["calls"] == 1


def test_summarize_empty():
    rep = summarize([])
    assert rep["total"]["calls"] == 0 and rep["total"]["cost_usd"] == 0.0
    assert rep["by_model"] == {} and rep["by_strategy"] == {}


def test_audit_is_dataclass_instance():
    assert isinstance(_fixed(), LlmCallAudit)
