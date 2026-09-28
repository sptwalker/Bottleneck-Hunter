"""Batch 6D 哨兵：供应商评估 prompt 不再要求「凑极端分」。

原「强制分布要求」要 LLM 在 9 个维度中至少给 2 个 ≤4 或 ≥8 —— 与同文件
「每个维度独立评分」直接冲突：一家中规中矩的公司被逼着编极端分。
数据触发的锚定规则（PE>100 → valuation≤4 等）是有依据的，必须保留。
"""
from bottleneck_hunter.chain.supplier_eval import _load_prompt


def test_forced_extremes_rule_is_gone():
    p = _load_prompt("supplier_eval")
    assert "至少 2 个维度" not in p
    assert "全部在 5-7 分之间" not in p


def test_data_triggered_anchors_survive():
    p = _load_prompt("supplier_eval")
    for rule in ("PE > 100 的公司：valuation 应 ≤ 4 分",
                 "行业龙头（市值 > 500亿）：market_position 应 ≥ 7 分",
                 "客户信息不明确时：customer_validation 应 ≤ 5 分"):
        assert rule in p
    assert "不要为了「拉开差异」编造极端分" in p
