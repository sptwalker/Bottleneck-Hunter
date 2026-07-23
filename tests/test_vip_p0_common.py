"""VIP P0 公共件：number_guard 防幻觉数字 + compliance 免责声明。"""
from bottleneck_hunter.vip import number_guard as ng
from bottleneck_hunter.vip import compliance as cp


_FACTS = "GOOGL 数量 1030 股，市值 $1,205,022.50，占比 60.86%，未实现盈亏 $656,223.00；期权 5 contracts；净值 12.34"


def test_verify_real_numbers_pass():
    r = {x["token"]: x["status"] for x in ng.verify_numbers("市值 $1,205,022.50，占 60.86%", _FACTS)}
    assert r["$1,205,022.50"] == "verified"
    assert r["60.86%"] == "verified"


def test_unit_bare_number_verified():
    """带单位裸数（股/contracts/净值）纳入校验，真实值放行。"""
    assert ng.verify_numbers("持 1030 股", _FACTS)[0]["status"] == "verified"
    assert ng.verify_numbers("共 5 contracts", _FACTS)[0]["status"] == "verified"
    assert ng.verify_numbers("净值 12.34", _FACTS)[0]["status"] == "verified"


def test_fabricated_unit_number_flagged():
    """编造的带单位裸数被抓。"""
    assert ng.verify_numbers("持 8888 股", _FACTS)[0]["status"] == "unverified"


def test_date_and_serial_not_checked():
    """日期/页码等无单位裸数不纳入校验（不产生 token）。"""
    assert ng.verify_numbers("成交日 30JUN26 第 3 页", _FACTS) == []


def test_fabricated_number_flagged():
    r = ng.verify_numbers("另有臆造收益 $9,999,999.00", _FACTS)
    assert r and r[0]["status"] == "unverified"


def test_rounding_within_tolerance_passes():
    assert ng.verify_numbers("约 $1,205,000", _FACTS)[0]["status"] == "verified"


def test_annotate_marks_only_unverified():
    txt = "真实 $1,205,022.50，臆造 $9,999,999.00"
    m = ng.annotate_unverified(txt, _FACTS)
    assert "$9,999,999.00 ⚠未核到" in m
    assert "$1,205,022.50 ⚠未核到" not in m


def test_empty_text_no_tokens():
    assert ng.verify_numbers("", _FACTS) == []


def test_disclaimer_single_source():
    out = cp.with_disclaimer("正文")
    assert "正文" in out and cp.DISCLAIMER_ZH in out
    assert cp.DISCLAIMER_ZH in cp.with_disclaimer("")   # 空正文也带
    assert cp.DISCLAIMER_VERSION == "2026-07-v1"
