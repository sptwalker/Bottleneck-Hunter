"""P1-1（2026-10 体检）：价值委员对成长池改用相对估值口径，杜绝"结构性必否"。

病史：A股观察池全是科技成长（PE 40-130、ROE 2-6%），价值委员按格雷厄姆绝对标尺
110 票里投出 71 张否决，买入几乎全部被否，而卖出不受阻 → 组合单向缩水至 2.7%。
"""
from bottleneck_hunter.watchlist.committee import _load_prompt


def test_value_prompt_has_growth_pool_stance():
    p = _load_prompt("committee_value")
    assert "估值口径" in p
    assert "相对估值优先" in p and "历史分位" in p
    # 绝对估值不得单独构成否决
    assert "不足以投反对票" in p
    # 否决门槛：分位 ≥90% 且同业中位数显著更低
    assert "90%" in p and "同业中位数" in p
    # 成熟价值池不放松
    assert "成熟价值池" in p


def test_value_prompt_keeps_hard_data_grounding():
    p = _load_prompt("committee_value")
    assert "数据接地要求" in p
    assert "引用具体数字" in p
