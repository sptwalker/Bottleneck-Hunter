"""反向瓶颈分析流水线（SSE）。

正向流程是「终端产品 → 拆解 → 瓶颈 → 供应商 → 评估」；本模块做**反向**：
给定一家企业代码，自动判定其所处的瓶颈环节（优先匹配已有产业链数据，
缺失则 LLM 补全），复用现有评分体系产出与入围企业同构的 SupplierScorecard。
"""

from __future__ import annotations

import logging
import re
from collections.abc import AsyncGenerator
from pathlib import Path

from bottleneck_hunter.chain.bottleneck import BottleneckAnalyzer
from bottleneck_hunter.chain.catalyst import CatalystAnalyzer
from bottleneck_hunter.chain.financial_data import fetch_financial_snapshot
from bottleneck_hunter.chain.json_utils import extract_json_object
from bottleneck_hunter.chain.models import (
    BottleneckReport,
    ChainGraph,
    IndustryNode,
    LayerType,
    MarketRegion,
    SupplierInfo,
)
from bottleneck_hunter.chain.smart_money import track_batch as smart_money_batch
from bottleneck_hunter.chain.supplier_eval import AlphaScorer, FinalScorer, SupplierEvaluator
from bottleneck_hunter.llm_clients.factory import create_llm, get_llm_for_position, get_models_for_role
from bottleneck_hunter.web.streaming._common import _sanitize, _sse

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).resolve().parents[2] / "chain" / "prompts"

_VALID_LAYER_TYPES = {t.value for t in LayerType}


# ─────────────────────────────────────────────────────────
# 市场识别 + 公司信息抓取
# ─────────────────────────────────────────────────────────

def detect_market(ticker: str, fallback: str = "us_stock") -> str:
    """按代码格式自动判定市场。识别不出时返回 fallback（页面当前市场）。"""
    t = (ticker or "").strip().upper()
    if not t:
        return fallback
    # A 股：6 位数字，或带 .SH/.SZ/.BJ / SH/SZ 前缀
    if re.fullmatch(r"\d{6}", t) or re.search(r"\.(SH|SZ|BJ)$", t) or re.match(r"^(SH|SZ|BJ)\d{6}$", t):
        return "a_stock"
    # 纯字母（1-5 位）→ 美股
    if re.fullmatch(r"[A-Z]{1,5}", t):
        return "us_stock"
    return fallback


def _to_float(v) -> float | None:
    try:
        if v in (None, "", "-", "--"):
            return None
        return float(str(v).replace(",", "").replace("%", ""))
    except (ValueError, TypeError):
        return None


def _finnhub_company_profile(ticker: str) -> dict:
    """finnhub 兜底取美股公司名/行业——Yahoo 被 IP 级限流(429)时的非 Yahoo 源。

    无 finnhub key 或失败返 {}。目的：Yahoo 拿不到名字时仍能核实企业身份，
    避免带着「名称未知」进 LLM 让它按裸代码脑补公司（SPCX→错认 Space Perspective 的根因）。
    """
    try:
        from bottleneck_hunter.data_provider.data_source_catalog import resolve_data_source_key
        key = resolve_data_source_key("finnhub")
        if not key:
            return {}
        import finnhub
        prof = finnhub.Client(api_key=key).company_profile2(symbol=ticker) or {}
        return {"name": prof.get("name", "") or "", "sector": prof.get("finnhubIndustry", "") or ""}
    except Exception as e:  # noqa: BLE001
        logger.debug("finnhub 公司信息兜底失败 (%s): %s", ticker, e)
        return {}


def _fetch_company_basic(ticker: str, market_enum: MarketRegion) -> dict:
    """抓取公司名称/行业/简介/市值（用于校验有效性 + 构造 SupplierInfo）。"""
    out: dict = {"name": "", "name_cn": "", "sector": "", "description": "", "market_cap": None}
    try:
        if market_enum == MarketRegion.A_STOCK:
            import akshare as ak  # 延迟导入，避免无 akshare 环境下加载失败
            from bottleneck_hunter.chain.financial_data import _extract_astock_code
            code = _extract_astock_code(ticker)
            if not code:
                return out
            df = ak.stock_individual_info_em(symbol=code)
            if df is not None and not df.empty:
                info = dict(zip(df["item"], df["value"]))
                out["name"] = str(info.get("股票简称", "") or "")
                out["name_cn"] = out["name"]
                out["sector"] = str(info.get("行业", "") or "")
                mc = _to_float(info.get("总市值"))
                if mc is not None:
                    out["market_cap"] = round(mc / 1e8, 1)  # 元 → 亿
        else:
            from bottleneck_hunter.watchlist.price_pipeline import _fetch_company_info_us
            sym = ticker.split(".")[0].strip()
            info = _fetch_company_info_us(sym)
            if info:
                out["name"] = info.get("longName") or info.get("shortName") or ""
                out["sector"] = info.get("sector") or info.get("industry") or ""
                out["description"] = info.get("longBusinessSummary") or ""
                mc = _to_float(info.get("marketCap"))
                if mc is not None:
                    out["market_cap"] = round(mc / 1e9, 1)  # USD → $B
            # Yahoo 空/被限流 → finnhub 兜底核实身份（非 Yahoo 源）
            if not out["name"]:
                fb = _finnhub_company_profile(sym)
                out["name"] = fb.get("name", "")
                out["sector"] = out["sector"] or fb.get("sector", "")
    except Exception as e:
        logger.warning("反向分析公司信息抓取失败 (%s): %s", ticker, e)
    return out


# ─────────────────────────────────────────────────────────
# 瓶颈环节判定（混合：先匹配已有产业链，缺失则 LLM 补全）
# ─────────────────────────────────────────────────────────

def _norm_ticker(t: str) -> str:
    return (t or "").strip().upper().split(".")[0]


def _load_chain_context(analysis_store, owner_analysis_id: str) -> dict | None:
    """读取当前正向分析记录的产业链上下文（赛道 + 瓶颈环节清单），供 LLM 链内定位。

    返回 {sector, nodes:[{name, score, layer}]}；无 owner/读取失败/无节点 → None。
    """
    if not owner_analysis_id or analysis_store is None:
        return None
    try:
        rec = analysis_store.get(owner_analysis_id)
    except Exception:
        return None
    if not rec:
        return None
    rj = rec.get("result_json") or {}
    reports = rj.get("bottleneck_reports") or []
    if not reports:
        return None
    # 按瓶颈分降序，取前若干个环节名喂给 LLM
    nodes = sorted(
        ({"name": r.get("node_name", ""),
          "score": r.get("overall_score"),
          "layer": r.get("layer")}
         for r in reports if r.get("node_name")),
        key=lambda n: (n["score"] or 0), reverse=True,
    )
    if not nodes:
        return None
    return {"sector": rj.get("sector") or rec.get("sector", ""), "nodes": nodes}


def _match_in_record(rec: dict, norm: str) -> BottleneckReport | None:
    """在单条正向分析记录里找 ticker 对应的瓶颈节点。"""
    rj = rec.get("result_json") or {}
    reports = rj.get("bottleneck_reports") or []
    report_by_node = {r.get("node_name", ""): r for r in reports}
    # a) 命中已评估的供应商 → 取其瓶颈节点
    for sc in rj.get("supplier_scorecards") or []:
        sup = sc.get("supplier") or {}
        if _norm_ticker(sup.get("ticker", "")) == norm:
            node = (sc.get("bottleneck_node", "") or "").split(",")[0].strip()
            rep = report_by_node.get(node)
            if rep:
                try:
                    return BottleneckReport(**rep)
                except Exception:
                    pass
    # b) 命中节点的代表性公司
    for rep in reports:
        for c in rep.get("representative_companies") or []:
            if _norm_ticker(c.get("code", "")) == norm:
                try:
                    return BottleneckReport(**rep)
                except Exception:
                    pass
    return None


def _match_existing_bottleneck(
    analysis_store, ticker: str, owner_analysis_id: str = "",
) -> tuple[BottleneckReport | None, str]:
    """在已有正向分析里找该 ticker 对应的瓶颈节点。命中返回 (report, analysis_id)。

    owner_analysis_id 非空时【优先】在该正向记录（用户当前所在的产业链）里匹配，
    使同一企业在不同赛道得到该赛道内的定位；当前链未命中才回退全局搜索。
    """
    norm = _norm_ticker(ticker)
    if not norm or analysis_store is None:
        return None, ""

    # 优先：当前产业链记录内匹配
    if owner_analysis_id:
        try:
            rec = analysis_store.get(owner_analysis_id)
            if rec:
                rep = _match_in_record(rec, norm)
                if rep:
                    return rep, owner_analysis_id
        except Exception:
            pass

    # 回退：全局搜索（当前链未命中时，仍尽量复用已有瓶颈分）
    try:
        summaries = analysis_store.list_all()[:30]
    except Exception:
        return None, ""
    for summ in summaries:
        if summ["id"] == owner_analysis_id:
            continue  # 已在上面查过
        try:
            rec = analysis_store.get(summ["id"])
        except Exception:
            continue
        if not rec:
            continue
        rep = _match_in_record(rec, norm)
        if rep:
            return rep, rec["id"]
    return None, ""


async def _llm_identify_bottleneck(llm, basic: dict, ticker: str, market: str,
                                   snap, language: str,
                                   chain_context: dict | None = None) -> BottleneckReport:
    """LLM 反推产业方向 + 瓶颈环节，再用 BottleneckAnalyzer 真实打分。

    chain_context（当前正向分析记录）非空时，把该产业链的赛道与瓶颈环节清单喂给 LLM，
    要求它在【这条链】里给企业定位，从而实现"同一企业在不同赛道得到不同结果"。
    """
    prompt_tpl = (PROMPTS_DIR / "reverse_identify.md").read_text(encoding="utf-8")
    lines = [
        f"- 代码: {ticker}",
        f"- 市场: {'A股' if market == 'a_stock' else '美股'}",
        f"- 名称: {basic.get('name') or '未知'}",
    ]
    if basic.get("sector"):
        lines.append(f"- 所属行业(粗): {basic['sector']}")
    if basic.get("market_cap") is not None:
        lines.append(f"- 市值: {basic['market_cap']}{'亿' if market == 'a_stock' else 'B'}")
    if basic.get("description"):
        lines.append(f"- 简介: {basic['description'][:800]}")
    if snap is not None:
        if snap.revenue_yoy_pct is not None:
            lines.append(f"- 营收同比: {snap.revenue_yoy_pct:.1f}%")
        if snap.gross_margin_pct is not None:
            lines.append(f"- 毛利率: {snap.gross_margin_pct:.1f}%")
    prompt = prompt_tpl.replace("{company_info}", "\n".join(lines))

    # 注入当前产业链上下文：要求 LLM 在这条链里给企业定位（跨链差异化的关键）
    if chain_context and chain_context.get("nodes"):
        sector = chain_context.get("sector", "")
        node_lines = "\n".join(
            f"  - {n['name']}（瓶颈分 {n.get('score', '?')}，第{n.get('layer', '?')}层）"
            for n in chain_context["nodes"][:30]
        )
        ctx_block = (
            f"\n## 当前分析的产业链：{sector}\n"
            f"该产业链已识别出以下瓶颈环节（节点名｜瓶颈分｜层级）：\n{node_lines}\n\n"
            "⚠ 重要：请【优先】判断该企业在【上述这条产业链】中最贴近的瓶颈环节，"
            "node_name 应尽量命中上面清单中的某个环节；只有当该企业确实与本链无关时，"
            "才另行判断其所属环节。这样同一企业在不同产业链中会得到该链内的差异化定位。"
        )
        prompt = prompt + ctx_block

    resp = await llm.ainvoke(prompt)
    data = extract_json_object(getattr(resp, "content", resp))

    sector = (data.get("sector") or basic.get("sector") or "未知行业").strip()
    end_product = (data.get("end_product") or sector).strip()
    node_name = (data.get("node_name") or sector).strip()
    layer_type = data.get("layer_type") if data.get("layer_type") in _VALID_LAYER_TYPES else "component"
    layer = data.get("layer")
    if not isinstance(layer, int) or layer < 1:
        layer = 1

    root = IndustryNode(
        name=end_product, description=f"{sector} 终端产品", layer=0,
        layer_type=LayerType.END_PRODUCT, function="终端产品",
    )
    node = IndustryNode(
        name=node_name, description=data.get("node_description", "") or node_name,
        layer=layer, layer_type=LayerType(layer_type),
        function=data.get("function", "") or "",
        key_parameters=data.get("key_parameters", []) or [],
        representative_companies=[{"name": basic.get("name") or ticker, "code": ticker}],
    )
    graph = ChainGraph(sector=sector, end_product=end_product, nodes=[root, node])

    analyzer = BottleneckAnalyzer(llm=llm, language=language, industry=sector, market=market)
    reports = await analyzer.analyze(graph)
    if reports:
        return reports[0]
    # 兜底：分析失败也给一个最小 report，保证流程不中断
    return BottleneckReport(
        node_name=node_name, node_description=node.description, layer=layer,
        scores=[], overall_score=5.0,
        key_insights=["瓶颈打分失败，使用中性默认值"], risks=[],
    )


# ─────────────────────────────────────────────────────────
# 主流水线
# ─────────────────────────────────────────────────────────

async def stream_reverse_analysis(
    *,
    ticker: str,
    market: str = "us_stock",
    language: str = "zh",
    provider: str = "",
    model: str = "",
    analysis_store=None,
    watchlist_store=None,
    user_id: str = "",
    owner_analysis_id: str = "",
) -> AsyncGenerator[dict, None]:
    """反向分析单只标的，逐步 emit SSE 事件，最后 emit reverse_complete + 落库。"""
    ticker = (ticker or "").strip()
    if not ticker:
        yield _sse("error", step="validate", message="请输入企业代码")
        return

    # 市场：自动识别 + 页面市场兜底
    resolved_market = detect_market(ticker, fallback=market or "us_stock")
    market_enum = MarketRegion.A_STOCK if resolved_market == "a_stock" else MarketRegion.US_STOCK

    # 归一化为 canonical 代码（大小写 / A股后缀），确保取数、落库、company_archive
    # 与观察池/决策用同一 key——否则反向建的档观察池按归一化 key 查不到。
    # 顺带早期格式拦截：明显不像代码（公司名/含空格）直接拒，省一次 LLM+取数。
    from bottleneck_hunter.watchlist.store_base import normalize_ticker, validate_ticker
    ticker = normalize_ticker(ticker, resolved_market)
    try:
        validate_ticker(ticker, resolved_market)
    except ValueError as e:
        yield _sse("error", step="validate", message=str(e))
        return

    # LLM：显式指定优先；否则自动使用用户在 AI 配置中为「入围评估(pipeline_eval)」选的主模型
    try:
        if provider:
            llm = create_llm(provider, model, with_fallback=True)
        else:
            # with_fallback：单模型位，主模型超时/失败自动换 provider（治 siliconflow 判瓶颈超时）
            results = get_models_for_role("pipeline_eval", user_id=user_id, with_fallback=True)
            if results:
                llm, provider, model = results[0]
            else:
                llm, provider, model = get_llm_for_position("pipeline_eval")
        if llm is None:
            raise ValueError("未配置可用的 LLM provider，请在 AI 配置中设置")
    except Exception as e:
        yield _sse("error", step="init", message=f"LLM 初始化失败: {e}")
        return
    yield _sse("step_progress", step="init", message=f"使用模型 {provider}/{model}", log=True)

    # ── 1. 校验企业 + 拉取财务 ──
    yield _sse("step_start", step="validate", message=f"校验企业 {ticker} ({resolved_market})...")
    basic = _fetch_company_basic(ticker, market_enum)
    supplier = SupplierInfo(
        name=basic.get("name") or ticker, name_cn=basic.get("name_cn", ""),
        ticker=ticker, market=market_enum, market_cap=basic.get("market_cap"),
        sector=basic.get("sector", ""), description=basic.get("description", ""),
        source="reverse",
    )
    snap = await fetch_financial_snapshot(supplier)
    if snap is not None:
        supplier.revenue_growth = snap.revenue_yoy_pct
        supplier.gross_margin = snap.gross_margin_pct
        supplier.pe_ratio = snap.consensus_pe

    # ① fail-safe：名称缺失 = 无法核实企业身份 → 直接报错，绝不带着「名称未知」进 LLM
    # 让它按裸代码脑补公司（SPCX 被错认成 Space Perspective 的根因）。宁可诚实报错，不臆测。
    if not basic.get("name"):
        yield _sse("error", step="validate",
                   message=f"无法核实 {ticker} 的企业信息（数据源可能被限流，或代码有误）。"
                           f"请稍后重试或核对代码——系统不会在身份未核实时臆测企业。")
        return
    yield _sse("step_done", step="validate",
               result={"name": supplier.name, "market": resolved_market, "sector": supplier.sector})

    # ── 2. 判定瓶颈环节（混合） ──
    yield _sse("step_start", step="locate_bottleneck", message="判定所处瓶颈环节...")
    # 当前产业链上下文（用户所在的正向记录）：既用于优先链内匹配，也喂给 LLM 兜底做链内定位
    owner_chain_ctx = _load_chain_context(analysis_store, owner_analysis_id)
    bottleneck, matched_id = _match_existing_bottleneck(analysis_store, ticker, owner_analysis_id)
    source = "matched" if bottleneck else "llm"
    if bottleneck:
        in_chain = " [当前链内]" if matched_id and matched_id == owner_analysis_id else ""
        yield _sse("step_progress", step="locate_bottleneck",
                   message=f"命中已有产业链：{bottleneck.node_name}（复用瓶颈分 {bottleneck.overall_score}）{in_chain}", log=True)
    else:
        ctx_note = f"（在 {owner_chain_ctx['sector']} 链内定位）" if owner_chain_ctx else ""
        yield _sse("step_progress", step="locate_bottleneck", message=f"无直接匹配，LLM 反推中...{ctx_note}", log=True)
        try:
            bottleneck = await _llm_identify_bottleneck(
                llm, basic, ticker, resolved_market, snap, language,
                chain_context=owner_chain_ctx)
        except Exception as e:
            logger.exception("反向分析 LLM 瓶颈识别失败")
            yield _sse("error", step="locate_bottleneck", message=f"瓶颈环节判定失败: {e}")
            return
    if not supplier.sector:
        supplier.sector = bottleneck.node_name
    yield _sse("step_done", step="locate_bottleneck",
               result={"node": bottleneck.node_name, "score": bottleneck.overall_score, "source": source})

    # ── 3. 聪明钱 ──
    yield _sse("step_start", step="smart_money", message="拉取聪明钱信号...")
    try:
        sm_map, _ = await smart_money_batch([supplier])
    except Exception:
        sm_map = {}
    yield _sse("step_done", step="smart_money", result={"fetched": len(sm_map)})

    # ── 4. 评估 ──
    yield _sse("step_start", step="evaluate", message="多维度评估中...")
    try:
        evaluator = SupplierEvaluator(llm=llm, language=language)
        sc = await evaluator.evaluate(supplier, bottleneck, financial_snapshot=snap)
    except Exception as e:
        logger.exception("反向分析评估失败")
        yield _sse("error", step="evaluate", message=f"评估失败: {e}")
        return
    if supplier.ticker in sm_map:
        sc.smart_money = sm_map[supplier.ticker]

    # ── 5. Alpha + 催化剂 + 最终分 ──
    yield _sse("step_start", step="score", message="计算预期差 / 催化剂 / 最终分...")
    bn_score_map = {bottleneck.node_name: bottleneck.overall_score}
    AlphaScorer.score_all([sc], bn_score_map)
    try:
        catalyst = CatalystAnalyzer(llm=llm, language=language)
        await catalyst.analyze_batch([sc], {bottleneck.node_name: bottleneck})
    except Exception:
        logger.warning("反向分析催化剂分析失败（忽略）", exc_info=True)
    AlphaScorer.score_all([sc], bn_score_map)
    FinalScorer.score_all([sc])
    yield _sse("step_done", step="score", result={})

    # ── 6. 持久化 + 完成 ──
    record_id = ""
    if watchlist_store is not None:
        try:
            record_id = watchlist_store.create_reverse_analysis(
                ticker=supplier.ticker, company_name=supplier.name,
                company_name_cn=supplier.name_cn, sector=supplier.sector,
                bottleneck_node=bottleneck.node_name,
                quality_score=sc.overall_score,
                alpha_score=sc.alpha.alpha_score if sc.alpha else 0.0,
                final_score=sc.final.final_score if sc.final else sc.overall_score,
                source=source, matched_analysis_id=matched_id,
                owner_analysis_id=owner_analysis_id,
                result_json=_sanitize(sc.model_dump()),
            )
        except Exception:
            logger.exception("反向分析落库失败")

    # 反查企业也建立/更新持久化档案（含简介+评分），供观察池/决策中心按 ticker 复用
    if analysis_store is not None:
        try:
            mkt = getattr(getattr(supplier, "market", None), "value", None) or market
            analysis_store.upsert_company_archive(
                ticker=supplier.ticker, scorecard=_sanitize(sc.model_dump()),
                market=mkt, name=supplier.name or supplier.ticker, source="reverse")
        except Exception:
            logger.warning("企业档案持久化失败(reverse)", exc_info=True)

    yield _sse("reverse_complete",
               scorecard=sc.model_dump(),
               meta={"id": record_id, "source": source, "market": resolved_market,
                     "matched_analysis_id": matched_id, "owner_analysis_id": owner_analysis_id,
                     "bottleneck_node": bottleneck.node_name})
