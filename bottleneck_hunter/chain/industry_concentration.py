"""行业集中度真实计算 —— 从 A 股板块成分股市值算 CR3/CR5/HHI。

瓶颈评分历史上 CR3/HHI 全由 LLM 估算（内部自洽≠事实正确）。本模块用东方财富
板块成分股的真实总市值，为【A 股】环节计算真实集中度，作为瓶颈评分的事实锚点。

数据源与 supplier_search.py 的 _try_akshare_search 同源（stock_board_*_cons_em）。
东财接口国内间歇不可达（实测 RemoteDisconnected）——全程 try/except，失败返回 None
让调用方降级回 LLM 估算，绝不阻断主流程。仅 A 股可用；美股无等价免费数据源。
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# 进程内缓存：同一板块在一次分析里反复命中时不重复拉网。key=board_name。
_CONCENTRATION_CACHE: dict[str, dict | None] = {}


def _extract_keywords(node_name: str) -> list[str]:
    """从环节名提取板块搜索关键词（与 supplier_search._extract_keywords 同逻辑，避免跨模块耦合）。"""
    for prefix in ("高端", "先进", "精密", "超高纯", "高纯", "高性能",
                   "新型", "专用", "关键", "核心", "特种"):
        node_name = node_name.removeprefix(prefix)
    parts = re.split(r"[/、及和与]", node_name)
    keywords = [p.strip() for p in parts if len(p.strip()) >= 2]
    if not keywords:
        keywords = [node_name] if node_name else []
    return keywords


def _mcap_to_yi(raw) -> float | None:
    """把成分股『总市值』字段转成亿元（akshare 返回的是元）。"""
    if raw is None:
        return None
    try:
        v = float(str(raw).replace(",", ""))
    except (ValueError, TypeError):
        return None
    if v <= 0:
        return None
    # >1e8 视为『元』，转亿；否则认为已是亿
    return round(v / 1e8, 4) if v > 1e8 else round(v, 4)


def _concentration_from_mcaps(mcaps: list[float]) -> dict | None:
    """给定一组成分股市值（亿），算 CR3/CR5/HHI/公司数。"""
    mcaps = sorted((m for m in mcaps if m and m > 0), reverse=True)
    n = len(mcaps)
    if n == 0:
        return None
    total = sum(mcaps)
    if total <= 0:
        return None
    shares = [m / total * 100 for m in mcaps]  # 各家市占率%（市值份额代理）
    cr3 = round(sum(shares[:3]), 1)
    cr5 = round(sum(shares[:5]), 1)
    hhi = round(sum(s * s for s in shares))  # HHI = Σ(份额%)²，范围 0~10000
    return {"cr3": cr3, "cr5": cr5, "hhi": hhi, "company_count": n, "shares": shares}


def compute_concentration(node_name: str, keywords: list[str] | None = None,
                          max_boards: int = 2) -> dict | None:
    """按环节名/关键词匹配 A 股板块，用成分股总市值算真实集中度。

    返回 {cr3, cr5, hhi, company_count, board_name, top_companies:[(name, share%)], source:'akshare'}；
    无匹配/网络失败/接口异常 → 返回 None（调用方降级回 LLM 估算）。
    """
    try:
        import akshare as ak
    except ImportError:
        return None

    terms = keywords or _extract_keywords(node_name)
    if not terms:
        return None

    for search_fn, cons_fn in (
        (ak.stock_board_industry_name_em, ak.stock_board_industry_cons_em),
        (ak.stock_board_concept_name_em, ak.stock_board_concept_cons_em),
    ):
        try:
            df_boards = search_fn()
        except Exception as e:
            logger.debug("板块列表拉取失败(%s): %s", getattr(search_fn, "__name__", "?"), e)
            continue
        if df_boards is None or "板块名称" not in getattr(df_boards, "columns", []):
            continue

        matched_boards: list[str] = []
        for term in terms:
            try:
                hit = df_boards[df_boards["板块名称"].str.contains(term, na=False)]
            except Exception:
                continue
            matched_boards.extend(hit["板块名称"].tolist())
        # 去重保序，限量
        seen = set()
        boards = [b for b in matched_boards if not (b in seen or seen.add(b))][:max_boards]

        for board_name in boards:
            if board_name in _CONCENTRATION_CACHE:
                cached = _CONCENTRATION_CACHE[board_name]
                if cached:
                    return cached
                continue
            try:
                df_cons = cons_fn(symbol=board_name)
            except Exception as e:
                logger.debug("成分股拉取失败(%s): %s", board_name, e)
                _CONCENTRATION_CACHE[board_name] = None
                continue
            if df_cons is None or df_cons.empty:
                _CONCENTRATION_CACHE[board_name] = None
                continue

            mcap_col = next((c for c in df_cons.columns if "市值" in c), None)
            name_col = next((c for c in df_cons.columns if c in ("名称", "股票名称")), None)
            if not mcap_col:
                _CONCENTRATION_CACHE[board_name] = None
                continue

            pairs: list[tuple[str, float]] = []
            for _, row in df_cons.iterrows():
                mc = _mcap_to_yi(row.get(mcap_col))
                if mc is None:
                    continue
                nm = str(row.get(name_col, "")) if name_col else ""
                pairs.append((nm, mc))

            conc = _concentration_from_mcaps([m for _, m in pairs])
            if not conc:
                _CONCENTRATION_CACHE[board_name] = None
                continue

            # Top companies（名称 + 市占率），按市值降序
            ranked = sorted(pairs, key=lambda x: x[1], reverse=True)
            total = sum(m for _, m in ranked)
            top_companies = [(nm, round(mc / total * 100, 1)) for nm, mc in ranked[:5]]

            result = {
                "cr3": conc["cr3"], "cr5": conc["cr5"], "hhi": conc["hhi"],
                "company_count": conc["company_count"],
                "board_name": board_name,
                "top_companies": top_companies,
                "source": "akshare",
            }
            _CONCENTRATION_CACHE[board_name] = result
            return result

    return None


def clear_cache() -> None:
    """清空进程内缓存（一次新分析开始时可调）。"""
    _CONCENTRATION_CACHE.clear()


def demo() -> None:
    """自检：纯计算逻辑不依赖网络。"""
    # 3 家各占 50/30/20 亿 → CR3=100, HHI=50²+30²+20²=3800
    c = _concentration_from_mcaps([50, 30, 20])
    assert c["cr3"] == 100.0 and c["hhi"] == 3800 and c["company_count"] == 3, c
    # 4 家 40/30/20/10 → CR3=90, CR5=100, HHI=1600+900+400+100=3000
    c2 = _concentration_from_mcaps([40, 30, 20, 10])
    assert c2["cr3"] == 90.0 and c2["cr5"] == 100.0 and c2["hhi"] == 3000, c2
    # 空/无效 → None
    assert _concentration_from_mcaps([]) is None
    assert _concentration_from_mcaps([0, -1]) is None
    # 市值单位转换
    assert _mcap_to_yi("50000000000") == 500.0  # 500 亿（元→亿）
    assert _mcap_to_yi(50) == 50.0               # 已是亿
    assert _mcap_to_yi(None) is None
    # 关键词提取
    assert "光刻胶" in _extract_keywords("高端光刻胶")
    print("PASS: industry_concentration demo")


if __name__ == "__main__":
    demo()
