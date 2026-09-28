"""行业集中度真实计算 —— 从 A 股板块成分股市值算 CR3/CR5/HHI。

瓶颈评分历史上 CR3/HHI 全由 LLM 估算（内部自洽≠事实正确）。本模块用东方财富
板块成分股的真实总市值，为【A 股】环节计算真实集中度，作为瓶颈评分的事实锚点。

数据源与 supplier_search.py 的 _try_akshare_search 同源（stock_board_*_cons_em）。
东财接口国内间歇不可达（实测 RemoteDisconnected）——全程 try/except，失败返回 None
让调用方降级回 LLM 估算，绝不阻断主流程。仅 A 股可用；美股无等价免费数据源。

实测（2026-09，生产容器）：同一接口连打 5 次可 0 命中，`industry_name` 尤其不稳
（连续 5 次 RemoteDisconnected）。此前没有任何重试，一次抖动就静默降级 —— 该功能
自 2026-07-03 上线至 2026-09-28，1479 个 A 股环节的 cr3_source **全部**是
llm_estimate，真实数据一次都没用上。故对板块列表加指数退避重试；重试耗尽仍失败
则抛 ProbeFailure，由调用方决定熔断（见 bottleneck._fetch_real_concentration）。
"""
from __future__ import annotations

import logging
import re

from bottleneck_hunter.watchlist.retry import with_retry

logger = logging.getLogger(__name__)


class ProbeFailure(RuntimeError):
    """板块列表接口重试耗尽 —— 数据源不可用，非「该节点无匹配」。"""


# 进程内缓存：同一板块在一次分析里反复命中时不重复拉网。key=board_name。
# 只缓存**成功**结果。失败不缓存：此前把 None 也写进去，且缓存从不清空
# （唯一调用方 compute_concentration 直接 return cached，clear_cache 无人调用），
# 于是进程存续期间该板块被永久判死 —— 尤其板块列表抖动时会把所有已查板块写死。
_CONCENTRATION_CACHE: dict[str, dict] = {}

# 板块列表：一次分析内所有节点共用，只拉一次。
_BOARD_LIST_CACHE: dict[str, object] = {}

_BOARD_LIST_RETRIES = 4


def _extract_keywords(node_name: str) -> list[str]:
    """从环节名/短语提取板块搜索关键词（不依赖 LLM）。

    句读/顿号/连接词/括号/空白全部作为切分点，只保留 2..12 字的短词：东财板块名
    本就 2-6 字，论述长句片段无法 substring 匹配板块名，留着只会让检索零命中，
    而且**失败是静默的**。

    此前本模块自持一份弱化版（不切括号/空白、无长度上限），与 `supplier_search`
    的那份分叉已久：15 个真实环节名实测 3 个分叉，全是「落空」形态 ——
    「高端光刻胶（ArF 浸没式）」整条拿去 substring 匹配板块名，必然 0 命中。
    两边匹配的是同一个东西（板块名），故统一到此处；本模块是无 LLM 依赖的轻模块，
    适合持有它（`supplier_search` 反过来 import 本模块）。
    """
    for prefix in ("高端", "先进", "精密", "超高纯", "高纯", "高性能",
                   "新型", "专用", "关键", "核心", "特种"):
        node_name = node_name.removeprefix(prefix)
    parts = re.split(r"[/、及和与，。；：,.;:\s（）()\[\]「」【】\"'’“”]+", node_name)
    keywords = [p.strip() for p in parts if 2 <= len(p.strip()) <= 12]
    if not keywords:
        kw = node_name.strip()
        keywords = [kw] if 2 <= len(kw) <= 12 else []
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


def _board_list(ak, name: str):
    """拉取板块列表（带重试 + 进程缓存）。

    返回 DataFrame / None（接口正常但结构异常，跳过该来源）。
    重试耗尽则抛 ProbeFailure —— 由调用方汇总判断数据源是否整体不可用。
    """
    if name in _BOARD_LIST_CACHE:
        return _BOARD_LIST_CACHE[name]

    try:
        df = with_retry(max_retries=_BOARD_LIST_RETRIES, base_delay=0.8)(getattr(ak, name))()
    except Exception as e:
        raise ProbeFailure(f"{name}: {type(e).__name__}: {e}") from e

    if df is None or "板块名称" not in getattr(df, "columns", []):
        logger.warning("板块列表结构异常(%s): cols=%s", name, getattr(df, "columns", None))
        return None
    _BOARD_LIST_CACHE[name] = df
    return df


def _fetch_cons(cons_fn, board_name: str):
    """拉某板块的成分股表（带重试）。

    实测同一板块 5 次里 3 次 RemoteDisconnected —— 不重试等于**随机判死**某个板块。
    与 `_board_list` 一样供 `supplier_search` 共用。
    """
    return with_retry(max_retries=3, base_delay=0.8)(cons_fn)(symbol=board_name)


def _match_boards(ak, terms: list[str], max_boards: int = 2) -> list[tuple[str, object]]:
    """按关键词匹配东财板块，返回 [(board_name, cons_fn)]（概念板块在前）。

    **概念在前**：节点名（光刻机/PCB/先进封装/光刻胶/MLCC…）在概念列表里是逐字命中，
    行业列表反而多是粗分类，而 `industry_name` 实测连续 5-6 次 RemoteDisconnected ——
    把最稳、名字又对得上的来源放在后面，等于白白降级。

    只有两个列表都拉不到才抛 `ProbeFailure`：单独一个接口挂掉是常态，算成「数据源
    整体不可用」会让调用方的熔断误伤正常路径。
    """
    sources = (
        ("stock_board_concept_name_em", ak.stock_board_concept_cons_em),
        ("stock_board_industry_name_em", ak.stock_board_industry_cons_em),
    )

    boards: list[tuple[str, object]] = []
    failures: list[str] = []
    for search_name, cons_fn in sources:
        try:
            df_boards = _board_list(ak, search_name)
        except ProbeFailure as e:
            failures.append(str(e))
            continue
        if df_boards is None:
            continue

        matched: list[str] = []
        for term in terms:
            try:
                # regex=False：term 可能含 . + ( ) 等正则元字符（如「光刻胶（ArF 浸没式）」
                # 切出的「ArF」没问题，但节点名里的「CMP」「SiC」等大小写/符号混排
                # 一旦被当正则解释就会抛或错配）。按字面匹配才是本意。
                hit = df_boards[df_boards["板块名称"].str.contains(term, na=False, regex=False)]
            except Exception:
                continue
            matched.extend(hit["板块名称"].tolist())
        seen = set()
        for b in [x for x in matched if not (x in seen or seen.add(x))][:max_boards]:
            boards.append((b, cons_fn))

    if not boards and failures:
        raise ProbeFailure("; ".join(failures))
    return boards


def compute_concentration(node_name: str, keywords: list[str] | None = None,
                          max_boards: int = 2) -> dict | None:
    """按环节名/关键词匹配 A 股板块，用成分股总市值算真实集中度。

    返回 {cr3, cr5, hhi, company_count, board_name, top_companies:[(name, share%)], source:'akshare'}；
    无匹配 → 返回 None（调用方降级回 LLM 估算）。

    **只有两个板块列表都拉不到时**才抛 `ProbeFailure`。单独一个接口挂掉是常态
    （`industry_name` 实测连续 5-6 次 RemoteDisconnected，而节点名恰好匹配的是
    `concept_name`），把它当成「数据源整体不可用」会让熔断误伤正常路径。

    概念板块在前：节点名（光刻机/PCB/先进封装/光刻胶/MLCC…）在概念列表里是逐字
    命中的，行业列表反而多是粗分类。
    """
    try:
        import akshare as ak
    except ImportError:
        return None

    terms = keywords or _extract_keywords(node_name)
    if not terms:
        return None

    boards = _match_boards(ak, terms, max_boards=max_boards)

    for board_name, cons_fn in boards:
        if board_name in _CONCENTRATION_CACHE:
            return _CONCENTRATION_CACHE[board_name]
        try:
            df_cons = _fetch_cons(cons_fn, board_name)
        except Exception as e:
            logger.debug("成分股拉取失败(%s): %s", board_name, e)
            continue
        if df_cons is None or df_cons.empty:
            continue

        mcap_col = next((c for c in df_cons.columns if "市值" in c), None)
        name_col = next((c for c in df_cons.columns if c in ("名称", "股票名称")), None)
        if not mcap_col:
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
    _BOARD_LIST_CACHE.clear()


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
