"""yfinance 行业(industry, 细粒度英文)→ 细中文 映射。

用于观察池等处把行业统一显示为「较细的中文名称」：
- 已是中文的行业名（如分析生成的「电子测试测量设备」）直接保留；
- 英文/粗名（如 yfinance 的 sector "Technology"）用 industry 映射成细中文。

覆盖 yfinance 全部 11 大类的细分行业（industry），未命中时回退大类中文。
"""

from __future__ import annotations

import re

# 大类(sector, 粗) → 中文，仅作最终兜底
SECTOR_ZH: dict[str, str] = {
    "technology": "科技",
    "financial services": "金融服务",
    "healthcare": "医疗健康",
    "consumer cyclical": "可选消费",
    "consumer defensive": "必需消费",
    "industrials": "工业",
    "energy": "能源",
    "basic materials": "原材料",
    "communication services": "通信服务",
    "utilities": "公用事业",
    "real estate": "房地产",
}

# 细分行业(industry) → 细中文。键已规范化（小写、破折号统一为 " - "）。
INDUSTRY_ZH: dict[str, str] = {
    # ── Technology ──
    "information technology services": "信息技术服务",
    "software - application": "应用软件",
    "software - infrastructure": "基础软件",
    "communication equipment": "通信设备",
    "computer hardware": "计算机硬件",
    "consumer electronics": "消费电子",
    "electronic components": "电子元件",
    "electronics & computer distribution": "电子与计算机分销",
    "scientific & technical instruments": "电子测试测量设备",
    "semiconductor equipment & materials": "半导体设备与材料",
    "semiconductors": "半导体",
    "solar": "光伏",
    # ── Financial Services ──
    "asset management": "资产管理",
    "banks - diversified": "综合性银行",
    "banks - regional": "区域性银行",
    "capital markets": "资本市场",
    "credit services": "信贷服务",
    "financial conglomerates": "金融综合企业",
    "financial data & stock exchanges": "金融数据与交易所",
    "insurance - diversified": "综合保险",
    "insurance - life": "人寿保险",
    "insurance - property & casualty": "财产与意外险",
    "insurance - reinsurance": "再保险",
    "insurance - specialty": "专业保险",
    "insurance brokers": "保险经纪",
    "mortgage finance": "抵押贷款金融",
    "shell companies": "壳公司",
    # ── Healthcare ──
    "biotechnology": "生物科技",
    "diagnostics & research": "诊断与研究",
    "drug manufacturers - general": "综合制药",
    "drug manufacturers - specialty & generic": "专科与仿制药",
    "health information services": "医疗信息服务",
    "healthcare plans": "医疗保险计划",
    "medical care facilities": "医疗服务机构",
    "medical devices": "医疗器械",
    "medical distribution": "医药分销",
    "medical instruments & supplies": "医疗仪器与耗材",
    "pharmaceutical retailers": "医药零售",
    # ── Consumer Cyclical ──
    "apparel manufacturing": "服装制造",
    "apparel retail": "服装零售",
    "auto & truck dealerships": "汽车经销",
    "auto manufacturers": "汽车整车",
    "auto parts": "汽车零部件",
    "department stores": "百货商店",
    "footwear & accessories": "鞋类与配饰",
    "furnishings, fixtures & appliances": "家居与家电",
    "gambling": "博彩",
    "home improvement retail": "家居建材零售",
    "internet retail": "互联网零售",
    "leisure": "休闲用品",
    "lodging": "住宿",
    "luxury goods": "奢侈品",
    "packaging & containers": "包装与容器",
    "personal services": "个人服务",
    "recreational vehicles": "休闲车辆",
    "residential construction": "住宅建筑",
    "resorts & casinos": "度假村与赌场",
    "restaurants": "餐饮",
    "specialty retail": "专业零售",
    "textile manufacturing": "纺织制造",
    "travel services": "旅游服务",
    # ── Consumer Defensive ──
    "beverages - brewers": "啤酒酿造",
    "beverages - non-alcoholic": "非酒精饮料",
    "beverages - wineries & distilleries": "葡萄酒与烈酒",
    "confectioners": "糖果食品",
    "discount stores": "折扣零售",
    "education & training services": "教育与培训服务",
    "farm products": "农产品",
    "food distribution": "食品分销",
    "grocery stores": "食品杂货零售",
    "household & personal products": "家庭与个护用品",
    "packaged foods": "包装食品",
    "tobacco": "烟草",
    # ── Industrials ──
    "aerospace & defense": "航空航天与国防",
    "airlines": "航空运输",
    "airports & air services": "机场与航空服务",
    "building products & equipment": "建筑产品与设备",
    "business equipment & supplies": "商用设备与耗材",
    "conglomerates": "综合企业集团",
    "consulting services": "咨询服务",
    "electrical equipment & parts": "电气设备与零件",
    "engineering & construction": "工程与建筑",
    "farm & heavy construction machinery": "农机与重型工程机械",
    "industrial distribution": "工业品分销",
    "infrastructure operations": "基础设施运营",
    "integrated freight & logistics": "综合货运与物流",
    "marine shipping": "海运",
    "metal fabrication": "金属加工",
    "pollution & treatment controls": "污染治理设备",
    "railroads": "铁路运输",
    "rental & leasing services": "租赁服务",
    "security & protection services": "安防服务",
    "specialty business services": "专业商务服务",
    "specialty industrial machinery": "专用工业机械",
    "staffing & employment services": "人力资源服务",
    "tools & accessories": "工具与配件",
    "trucking": "公路货运",
    "waste management": "废物管理",
    # ── Energy ──
    "oil & gas drilling": "油气钻探",
    "oil & gas e&p": "油气勘探与开采",
    "oil & gas equipment & services": "油气设备与服务",
    "oil & gas integrated": "综合性油气",
    "oil & gas midstream": "油气中游",
    "oil & gas refining & marketing": "油气炼化与销售",
    "thermal coal": "动力煤",
    "uranium": "铀矿",
    # ── Basic Materials ──
    "agricultural inputs": "农业投入品",
    "aluminum": "铝业",
    "building materials": "建筑材料",
    "chemicals": "化工",
    "coking coal": "炼焦煤",
    "copper": "铜业",
    "gold": "黄金",
    "lumber & wood production": "木材加工",
    "other industrial metals & mining": "其他工业金属与采矿",
    "other precious metals & mining": "其他贵金属与采矿",
    "paper & paper products": "纸与纸制品",
    "silver": "白银",
    "specialty chemicals": "特种化工",
    "steel": "钢铁",
    # ── Communication Services ──
    "advertising agencies": "广告代理",
    "broadcasting": "广播电视",
    "electronic gaming & multimedia": "电子游戏与多媒体",
    "entertainment": "娱乐传媒",
    "internet content & information": "互联网内容与信息",
    "publishing": "出版",
    "telecom services": "电信服务",
    # ── Utilities ──
    "utilities - diversified": "综合公用事业",
    "utilities - independent power producers": "独立发电",
    "utilities - regulated electric": "受管制电力",
    "utilities - regulated gas": "受管制燃气",
    "utilities - regulated water": "受管制水务",
    "utilities - renewable": "可再生能源发电",
    # ── Real Estate ──
    "real estate - development": "房地产开发",
    "real estate - diversified": "综合房地产",
    "real estate services": "房地产服务",
    "reit - diversified": "综合型REIT",
    "reit - healthcare facilities": "医疗地产REIT",
    "reit - hotel & motel": "酒店REIT",
    "reit - industrial": "工业地产REIT",
    "reit - mortgage": "抵押型REIT",
    "reit - office": "办公地产REIT",
    "reit - residential": "住宅地产REIT",
    "reit - retail": "零售地产REIT",
    "reit - specialty": "专业地产REIT",
}

_CJK = re.compile(r"[一-鿿]")

# 少数英文「分析细名」（非 yfinance industry）→ 中文，避免被回退成粗名
ALIAS_ZH: dict[str, str] = {
    "electronic design automation": "EDA工具",
    "eda": "EDA工具",
    "test equipment": "半导体测试设备",
    "foundry": "晶圆代工",
    "memory": "存储芯片",
}


def _has_cjk(s: str) -> bool:
    return bool(_CJK.search(s or ""))


def _norm(s: str) -> str:
    """规范化英文行业名以匹配：小写、破折号(—/–/-)统一为 ' - '、压缩空格。"""
    s = (s or "").strip().lower()
    s = s.replace("—", "-").replace("–", "-")
    s = re.sub(r"\s*-\s*", " - ", s)
    s = re.sub(r"\s+", " ", s)
    return s


def to_zh_sector(sector: str = "", industry: str = "", broad_sector: str = "") -> str:
    """把行业统一为细中文。

    优先级：已是中文的 sector（分析细名）→ industry 映射细中文 →
    sector 映射（若英文行业本身可映射）→ 大类中文兜底 → 原值。
    """
    sector = (sector or "").strip()
    if _has_cjk(sector):
        return sector  # 已是细中文（分析生成），保留
    # 英文分析细名（EDA / test equipment 等，非 yfinance industry）
    alias = ALIAS_ZH.get(_norm(sector))
    if alias:
        return alias
    ind = INDUSTRY_ZH.get(_norm(industry))
    if ind:
        return ind
    # 英文 sector 本身若恰好是某 industry（少数场景）
    ind2 = INDUSTRY_ZH.get(_norm(sector))
    if ind2:
        return ind2
    broad = SECTOR_ZH.get(_norm(broad_sector)) or SECTOR_ZH.get(_norm(sector))
    if broad:
        return broad
    return sector or industry or ""


if __name__ == "__main__":
    # 自检
    assert to_zh_sector("Technology", "Semiconductors") == "半导体"
    assert to_zh_sector("Technology", "Semiconductor Equipment & Materials") == "半导体设备与材料"
    assert to_zh_sector("电子测试测量设备", "Scientific & Technical Instruments") == "电子测试测量设备"
    assert to_zh_sector("Technology", "Software - Infrastructure") == "基础软件"
    assert to_zh_sector("Basic Materials", "Gold") == "黄金"
    assert to_zh_sector("Technology", "") == "科技"  # 无 industry → 大类兜底
    assert to_zh_sector("EDA工具", "Software - Application") == "EDA工具"  # 中文保留
    print("industry_zh self-check passed")
