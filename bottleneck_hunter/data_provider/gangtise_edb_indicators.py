"""Gangtise EDB 宏观指标 curated 映射表——L1 宏观层注入用。

每个条目 = 语义 key → (indicator_id, label, market_scope, transform)。
- indicator_id / label / 数值口径均经**真实 getData 活体验证**（2026-07 实测值见注释），非文档示例照搬。
- market_scope: "us"=仅美股宏观 / "cn"=仅A股本土宏观 / "global"=全市场（当前无，预留）。
- transform: "identity"=原值即百分比/指数；"index100"=基期100指数→同比%（value-100）。
  （中国全国 CPI 当月同比 EDB 只发布基期100指数形态 M00000002，故需此变换，实测 99.9→-0.1%）

批量 getData 上限 10 个 id/次；本表按 market 取子集恒 ≤7，单批即可。
"""

from __future__ import annotations

# key: (indicator_id, label, market_scope, transform)
# 实测值（2026-07 月度）标在行尾，作为「真实数据非代码桩」的可核对锚点。
EDB_INDICATORS: dict[str, tuple[str, str, str, str]] = {
    # ── 美国宏观（L1 美股）──
    "us_cpi_yoy":       ("M00012461", "美国CPI同比(%)",          "us", "identity"),  # 3.4
    "us_core_cpi_yoy":  ("M00009837", "美国核心CPI同比(%)",       "us", "identity"),  # 2.5
    "us_core_pce_yoy":  ("M00015469", "美国核心PCE同比(%,联储首选)", "us", "identity"),  # 3.3
    "us_fed_funds":     ("M00012340", "美国联邦基金利率(%)",       "us", "identity"),  # 3.63(日频)
    "us_unemployment":  ("M00006750", "美国失业率(%)",           "us", "identity"),  # 4.1
    "us_ppi_yoy":       ("M00015451", "美国PPI最终需求同比(%)",    "us", "identity"),  # 4.7
    "us_pmi_mfg":       ("M00005606", "美国制造业PMI(Markit)",    "us", "identity"),  # 53.9
    # ── 中国宏观（L1 A股本土锚）──
    "cn_cpi_yoy":       ("M00000002", "中国CPI当月同比(%)",       "cn", "index100"),  # 99.9→-0.1
    "cn_ppi_yoy":       ("M00007210", "中国PPI全国当月同比(%)",    "cn", "identity"),  # 3.5
    "cn_pmi_official":  ("M00001976", "中国官方制造业PMI",        "cn", "identity"),  # 49.2
    "cn_social_fin_yoy": ("M00001999", "中国社融存量同比(%)",       "cn", "identity"),  # 7.4
}


def indicators_for_market(market: str) -> dict[str, tuple[str, str, str, str]]:
    """按市场取该市场应注入 L1 的 EDB 指标子集。

    us_stock → 美国宏观；a_stock → 中国本土宏观。其它市场暂无（返回空，不臆造）。
    """
    scope = {"us_stock": "us", "a_stock": "cn"}.get(market)
    if not scope:
        return {}
    return {k: v for k, v in EDB_INDICATORS.items() if v[2] == scope}


def apply_transform(value: float, transform: str) -> float:
    """把 EDB 原值按 transform 归一到「可直接展示的宏观读数」。"""
    if transform == "index100":
        return round(value - 100, 2)  # 基期100同比指数 → 同比百分比
    return round(value, 2)


def _demo() -> None:
    assert indicators_for_market("us_stock").keys() == {
        "us_cpi_yoy", "us_core_cpi_yoy", "us_core_pce_yoy", "us_fed_funds",
        "us_unemployment", "us_ppi_yoy", "us_pmi_mfg"}
    assert set(indicators_for_market("a_stock")) == {
        "cn_cpi_yoy", "cn_ppi_yoy", "cn_pmi_official", "cn_social_fin_yoy"}
    assert indicators_for_market("hk_stock") == {}
    # index100 变换：99.9 指数 → -0.1% 同比
    assert apply_transform(99.9, "index100") == -0.1, apply_transform(99.9, "index100")
    assert apply_transform(3.4, "identity") == 3.4
    # 每个市场子集 ≤10（单批 getData 上限）
    for m in ("us_stock", "a_stock"):
        assert len(indicators_for_market(m)) <= 10
    print("gangtise_edb_indicators demo: OK")


if __name__ == "__main__":
    _demo()
