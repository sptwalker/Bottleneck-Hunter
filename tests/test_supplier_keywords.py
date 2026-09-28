"""_extract_keywords 硬化回归：论述长句必被拆散/剔除，短板块名保留。
根因：喂 key_insights 论述句 → 整句沦为「关键词」→ akshare str.contains 100% 0 命中（Loki 归因）。"""
from bottleneck_hunter.chain.supplier_search import SupplierSearcher

_kw = SupplierSearcher._extract_keywords


def test_short_board_names_survive():
    assert _kw("超低膨胀微晶玻璃/碳化硅陶瓷基板") == ["超低膨胀微晶玻璃", "碳化硅陶瓷基板"]
    assert _kw("纳米级精密运动台与气浮导轨") == ["纳米级精密运动台", "气浮导轨"]
    assert "电子光学系统" in _kw("电子光学系统")


def test_prose_sentence_is_shredded_not_kept_whole():
    prose = ("电子光学系统是 EBI 设备的绝对技术核心，其精度直接决定晶圆缺陷检测的灵敏度，"
             "全球仅三至四家厂商具备量产能力，供应链高度僵化。")
    out = _kw(prose)
    # 关键断言：没有任何超过 12 字的「关键词」整句残留（否则 akshare 必 0 命中）
    assert all(len(k) <= 12 for k in out), out
    assert prose not in out


def test_no_separator_long_phrase_dropped():
    # 无分隔的超长短语（>12）应被剔除而非整条塞进板块搜索
    assert _kw("某个特别特别长且没有任何分隔符号的整句论述内容示例") == []


def test_supplier_and_concentration_share_one_extractor():
    """两份提取器必须**是同一个实现**（此前各持一份，实测 15 个真实名里 3 个分叉）。

    分叉出来的是「落空」形态：「高端光刻胶（ArF 浸没式）」在弱的那份里整条留着，
    substring 匹配板块名必然 0 命中，且失败是静默的。
    """
    from bottleneck_hunter.chain.industry_concentration import _extract_keywords as other
    names = ["高端光刻胶（ArF 浸没式）", "电子特种气体 高纯", "光刻胶，显影液", "PCB/覆铜板"]
    for n in names:
        assert _kw(n) == other(n), f"{n} 两份提取器结论不同 —— 已再次分叉"
    # 括号/空白/全角逗号都必须切开，且不得留下超 12 字的整条
    assert _kw("高端光刻胶（ArF 浸没式）") == ["光刻胶", "ArF", "浸没式"]
    assert _kw("电子特种气体 高纯") == ["电子特种气体", "高纯"]


def test_akshare_search_uses_shared_retry_and_conversion():
    """`_try_akshare_search` 复用共用实现后仍要能出结果（该路径此前无测试覆盖）。

    板块匹配/成分股/市值换算全部改走 `industry_concentration`，此处用假 akshare
    走一遍真实函数体，钉住「改造没把这条检索打瘸」。
    """
    import sys
    from types import ModuleType
    from unittest.mock import patch

    import pandas as pd

    from bottleneck_hunter.chain import industry_concentration as ic
    from bottleneck_hunter.chain.supplier_search import _try_akshare_search

    ic.clear_cache()
    ak = ModuleType("akshare")
    ak.stock_board_concept_name_em = lambda: pd.DataFrame({"板块名称": ["光刻胶", "PCB"]})
    ak.stock_board_industry_name_em = lambda: pd.DataFrame({"板块名称": ["半导体"]})
    ak.stock_board_concept_cons_em = lambda symbol=None: pd.DataFrame({
        "代码": ["688008", "000001"], "名称": ["A公司", "B公司"],
        "总市值": [50e8, 20e8], "行业": ["半导体", "半导体"],
    })
    ak.stock_board_industry_cons_em = lambda symbol=None: pd.DataFrame(
        {"代码": ["1"], "名称": ["X"], "总市值": [5e8]})

    with patch.dict(sys.modules, {"akshare": ak}):
        out = _try_akshare_search(["光刻胶"], None)
    got = {s.ticker: s.market_cap for s in out}
    assert got == {"688008.SS": 50.0, "000001.SZ": 20.0}, got

    # 市值上限过滤（元→亿换算后比较）
    ic.clear_cache()
    with patch.dict(sys.modules, {"akshare": ak}):
        capped = _try_akshare_search(["光刻胶"], 30)
    assert [s.ticker for s in capped] == ["000001.SZ"]

    # 板块列表整体不可达 → 不抛，返回空（上游是 fire-and-forget 的并行源）
    ic.clear_cache()

    def _dead():
        raise ConnectionError("RemoteDisconnected")

    ak.stock_board_concept_name_em = _dead
    ak.stock_board_industry_name_em = _dead
    with patch.object(ic, "_BOARD_LIST_RETRIES", 1), patch.dict(sys.modules, {"akshare": ak}):
        assert _try_akshare_search(["光刻胶"], None) == []


if __name__ == "__main__":
    test_short_board_names_survive()
    test_prose_sentence_is_shredded_not_kept_whole()
    test_no_separator_long_phrase_dropped()
    test_supplier_and_concentration_share_one_extractor()
    test_akshare_search_uses_shared_retry_and_conversion()
    print("supplier keywords selfcheck OK")
