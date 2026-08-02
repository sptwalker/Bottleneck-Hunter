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


if __name__ == "__main__":
    test_short_board_names_survive()
    test_prose_sentence_is_shredded_not_kept_whole()
    test_no_separator_long_phrase_dropped()
    print("supplier keywords selfcheck OK")
