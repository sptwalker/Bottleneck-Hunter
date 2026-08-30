"""Gangtise 选股范围 curated 板块映射——瓶颈环节 → 中信一级行业 sectorId。

指标选股 payload 的 universe 需 10 位板块 ID（不能凭空构造，须来自平台搜索）。
完整的「口语板块名→sectorId」解析是 gangtise-screener skill 的活（universe_resolve.py，
含剥壳/同义改写/多体系消歧，~600 行）。此处按 EDB 指标表同一 curated 精神，只收录
**常见瓶颈行业的中信一级板块 ID**（均取自 skill references/universe.md 实测表，非臆造）。

ponytail: 静态小表，未收录的行业词 → sector_id_for 返回 None，选股源静默降级（供应商检索
  仍有 LLM/产业链/akshare 三源，零回归）。上升路径：需覆盖全部行业/概念/指数板块时，
  接入 skill 的 universe_resolve 三段式解析（剥壳→检索→重排），别在此表堆硬编码。
"""

from __future__ import annotations

# 中信一级行业 sectorId（10 位），实测截至 2026-08（见 references/universe.md）
_CITIC_SECTORS: dict[str, str] = {
    "银行": "1000000316",
    "半导体": "1000000366",
    "白酒": "1000000287",
    "医药": "1000000272",
    "汽车": "1000000198",
    "食品饮料": "1000000285",
    "电子": "1000000365",
    "计算机": "1000000399",
    "房地产": "1000000334",
    "煤炭": "1000000023",
    "有色金属": "1000000031",
    "国防军工": "1000000190",
    "电力设备及新能源": "1000000173",
}

# 行业简称/别名 → 规范中信名（universe.md「行业简称需先翻译再检索」同款）
_ALIASES: dict[str, str] = {
    "酿酒": "白酒", "芯片": "半导体", "集成电路": "半导体", "有色": "有色金属",
    "军工": "国防军工", "地产": "房地产", "新能源": "电力设备及新能源",
    "医疗": "医药", "生物医药": "医药", "汽车零部件": "汽车",
}


def sector_id_for(keyword: str) -> str | None:
    """瓶颈环节关键词 → 中信一级行业 sectorId。命中别名先归一；未收录返回 None。

    子串匹配：关键词包含某规范行业名即命中（如「功率半导体」→ 半导体）。别名同理。
    """
    kw = (keyword or "").strip()
    if not kw:
        return None
    for alias, canon in _ALIASES.items():
        if alias in kw:
            return _CITIC_SECTORS.get(canon)
    for name, sid in _CITIC_SECTORS.items():
        if name in kw:
            return sid
    return None


def _demo() -> None:
    assert sector_id_for("半导体") == "1000000366"
    assert sector_id_for("功率半导体设备") == "1000000366"   # 子串命中
    assert sector_id_for("芯片") == "1000000366"             # 别名→半导体
    assert sector_id_for("酿酒") == "1000000287"             # 别名→白酒
    assert sector_id_for("新能源") == "1000000173"           # 别名→电力设备及新能源
    assert sector_id_for("光刻胶") is None                    # 未收录 → 降级
    assert sector_id_for("") is None
    print("gangtise_sector_ids demo: OK")


if __name__ == "__main__":
    _demo()
