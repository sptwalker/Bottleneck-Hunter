"""四层决策引擎 — L1 宏观策略 + L2 组合策略 + L3 战术 + L4 执行

核心循环：
1. L1 run_macro_strategy / run_macro_check: 宏观环境判断（周度生成 / 日度检查）
2. L2 run_strategic_plan / run_deviation_check: 组合配置（周度生成 / 日度偏离检查）
3. L3 run_tactical_plans: 个股战术计划（日度）
4. L4 run_execution_plans: 具体执行方案（日度）

数据流：strategy_engine.py 输出个股信号 → 本引擎消费 → 组合级决策
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import Counter
from collections.abc import AsyncGenerator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from bottleneck_hunter.auth.current_user import get_current_user_id
from bottleneck_hunter.chain.json_utils import extract_json_object
from bottleneck_hunter.data_provider import ai_tools
from bottleneck_hunter.llm_clients.factory import get_llm_for_position, get_models_for_role
from bottleneck_hunter.watchlist.budget import BudgetTracker
from bottleneck_hunter.watchlist.persona import format_persona_for_prompt, get_user_single_cap
from bottleneck_hunter.watchlist.provenance import build_provenance
from bottleneck_hunter.watchlist.regime_mapper import format_bounds_for_prompt, get_allocation_bounds
from bottleneck_hunter.watchlist.stage_snapshot import save_stage_snapshot
from bottleneck_hunter.watchlist.store import WatchlistStore
from bottleneck_hunter.watchlist.store_base import normalize_market, normalize_ticker

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).resolve().parents[1] / "chain" / "prompts"


def _sse(event: str, **data) -> dict:
    return {"event": event, "data": {"event": event, **data}}


# 决策层 LLM 调用的输出 token 上限：L2/L3 要产出完整 JSON(多板块配置/多标的战术)，
# provider 默认上限偏小 → 输出被截断 → JSON 解析失败(曾致 L2/L3 整层无产出)。可用环境覆盖。
import os as _os

_DECISION_MAX_TOKENS = int(_os.getenv("BH_DECISION_MAX_TOKENS", "8192"))


async def _llm_json_object(llm, prompt: str, *, layer: str = "", input_prompts: list[str] | None = None) -> dict:
    """调 LLM 并抽取 JSON 对象，抗截断：加大 max_tokens；解析失败则纠偏重试一次。

    FallbackChatModel._generate 会把 max_tokens 透传给主/备各候选。首解析失败常因降级模型
    输出被截断/夹带说明 → 追加"只输出完整紧凑 JSON"提示重试(顺带经 fallback 重选模型)。
    仍失败则抛，交由各层既有 except 降级。
    """
    if input_prompts is not None:
        input_prompts.append(prompt)
    resp = await asyncio.to_thread(lambda: llm.invoke(prompt, max_tokens=_DECISION_MAX_TOKENS).content)
    try:
        return extract_json_object(resp)
    except ValueError:
        logger.warning("%s LLM 输出无法解析为 JSON(疑被截断)，纠偏重试一次", layer or "decision")
        retry_prompt = (
            prompt + "\n\n【重要】上一次输出无法解析。请**只**返回一个**完整且闭合**的 JSON 对象，"
            "不要任何解释文字、不要 markdown 代码围栏、不要在中途截断。"
        )
        if input_prompts is not None:
            input_prompts.append(retry_prompt)
        resp2 = await asyncio.to_thread(lambda: llm.invoke(retry_prompt, max_tokens=_DECISION_MAX_TOKENS).content)
        return extract_json_object(resp2)  # 再失败则向上抛，走各层降级


async def _run_data_negotiation(
    llm, prompt: str, *, market: str, layer: str, allowed_tickers: list[str], input_prompts: list[str] | None = None
) -> tuple[dict, list[dict]]:
    """带「数据调用协商环」的决策 LLM 调用：模型缺外部数据时发 [[DATA_REQ]] → DataHub 实时取数回注。

    - 用户 Key 一律经 get_current_user_id() 解析（与调度/Web 注入点同源，绝无全局 Key）。
    - allowed_tickers 限定可取数范围（本层输入标的 ∪ 市场观察池），越界 ticker 直接拒绝。
    - 取数失败/无能力 → 模型据现有信息继续，绝不因缺数据中断决策。
    - 任何异常 fail-open 降级为原始 _llm_json_object（只调一次、无协商、不取数），
      保证数据调用是增强而非依赖：数据链路挂了，决策照旧。
    - 返回 (result_dict, fetch_log)；fetch_log 只进 provenance，绝不落进决策产出。
    """
    user_id = get_current_user_id()

    async def _ask(p: str) -> str:
        """协商轮次内的单次 LLM 调用：与 _llm_json_object 同口径（max_tokens + 一次纠偏重试）。"""
        if input_prompts is not None:
            input_prompts.append(p)
        resp = await asyncio.to_thread(lambda: llm.invoke(p, max_tokens=_DECISION_MAX_TOKENS).content)
        try:
            extract_json_object(resp)
            return resp
        except ValueError:
            # 协商轮同样可能被降级模型截断 → 纠偏重试一次，避免拿"残 JSON"去探测 request block
            retry_prompt = (
                p + "\n\n【重要】上一次输出无法解析。请**只**返回一个**完整且闭合**的 JSON 对象，"
                "不要任何解释文字、不要 markdown 代码围栏、不要在中途截断。"
            )
            if input_prompts is not None:
                input_prompts.append(retry_prompt)
            return await asyncio.to_thread(lambda: llm.invoke(retry_prompt, max_tokens=_DECISION_MAX_TOKENS).content)

    try:
        final, fetch_log, _ = await ai_tools.negotiate(
            _ask, prompt, market=market, user_id=user_id, allowed_tickers=allowed_tickers
        )
        result = extract_json_object(final)  # 最终轮必是决策 JSON；解析失败照样降级
        return result, fetch_log
    except Exception as e:  # noqa: BLE001  fail-open：任何异常（含协商环内部错误）都不中断决策
        logger.warning("L%s 数据协商失败，降级为原始调用: %s", layer, str(e)[:160])
        return await _llm_json_object(llm, prompt, layer=layer, input_prompts=input_prompts), []


def _decision_allowed_tickers(store, market: str, *extra: str) -> list[str]:
    """本层可取数标的白名单 = 市场观察池 ∪ 各层自有输入，去重、按市场过滤后返回。"""
    pool: set[str] = set()
    try:
        entries = store.list_all() or []
    except Exception:  # noqa: BLE001  观察池读失败不阻塞协商，退化为层内标的
        entries = []
    for e in entries:
        tk = normalize_ticker((e.get("ticker") or "").strip(), market)
        if tk:
            pool.add(tk)
    for raw in extra:
        tk = normalize_ticker((raw or "").strip(), market)
        if tk:
            pool.add(tk)
    return sorted(pool)


def _load_prompt(name: str) -> str:
    path = PROMPTS_DIR / f"{name}.md"
    if path.exists():
        return path.read_text(encoding="utf-8")
    raise FileNotFoundError(f"Prompt 模板不存在: {path}")


def _today() -> str:
    """「今天」必须是**北京**日期。

    此前按 UTC 取，与 `store_base._today()`（北京）不一致：北京 00:00–08:00 生成 L3 计划时
    写入 plan_date=UTC 昨日，而同期的读取方（`get_tactical_plans_by_date()` 默认北京今日、
    `/tactical/latest`、L4 的 `_today()` 取计划）查的是北京今日 → 当场生成当场读不到，
    用户侧表现为"凌晨跑的战术计划消失"。日期字符串必须与消费侧同一时区。
    """
    return datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")


# (B) L3 上游新鲜度阈值：L1 宏观/L2 组合是【周度】生成（见模块 docstring + scheduler.job_weekly_strategy；
# 日常决策只跑 run_macro_check/run_deviation_check，不重生 L1/L2）。故"陈旧"按【周】判，而非计划字面的"今日"
# ——用"今日"会 6/7 天误杀 L3。8 天＝一个周度周期(7)+1 天宽限：当周计划(0–7d)放行，漏刷一个周期(≥8d)即阻断。
# ponytail: 纯 age 阈值，跨市场/时区免疫（不做北京日界比较）；升级路径＝带 macro_strategy_id 归属校验辨"L2 建于旧 L1"。
_STALE_UPSTREAM_DAYS = 8


def _upstream_age_days(created_at: str) -> float | None:
    """created_at(UTC ISO) 距今天数；空/不可解析 → None（视作不可信＝陈旧）。"""
    if not created_at:
        return None
    try:
        dt = datetime.fromisoformat(created_at)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() / 86400
    except (ValueError, TypeError):
        return None


def _days_until_date(date_str: str | None) -> float | None:
    """目标日期距今天数（负=已过）；空/不可解析 → None（=无催化剂，不臆测）。"""
    if not date_str:
        return None
    try:
        d = datetime.fromisoformat(str(date_str)[:10]).date()
    except (ValueError, TypeError):
        return None
    return (d - datetime.now(timezone.utc).date()).days


def _decision_provenance(prompts, models, market, layer, tickers=None) -> dict:
    """给决策产出打 provenance（prompt 哈希 + 实际模型 + 快照日 + 市场/层），嵌进 result_json（零表迁移）。

    复盘时可查「哪个 prompt + 哪个模型 + 哪日数据」生成此判断，分辨模型幻觉 vs 数据错。
    models 空 = 规则决策（如硬止损，非 LLM）；prompts 空 = 无模板。
    """
    return build_provenance(
        prompts=prompts, models=models, data_as_of=_today(), tickers=tickers, extra={"market": market, "layer": layer}
    )


# ─────────────────────────────────────────────────────────
# 市场上下文
# ─────────────────────────────────────────────────────────

_MARKET_CONTEXT = {
    "a_stock": """## 市场特性（A股）
- 涨跌停限制：主板 ±10%，创业板/科创板 ±20%
- 交易规则：T+1，无做空（融券除外）
- 关键指标：北向资金、融资余额、板块轮动
- 行业分类：申万一级行业
- 止损参考：-7%（涨跌停约束下更严格）
- 估值体系：PE/PB 中枢偏高，需参考行业分位数
- 政策敏感：关注监管政策、产业政策导向""",
    "us_stock": """## 市场特性（美股）
- 无涨跌幅限制（熔断除外）
- 交易规则：T+0，可做空
- 关键指标：VIX、期权 PCR、机构持仓 13F
- 行业分类：GICS 11 大类
- 止损参考：-10%
- 估值体系：DCF 为主，EV/EBITDA、P/S 常用
- 宏观驱动：联储利率决议、非农/CPI 数据""",
    "hk_stock": """## 市场特性（港股）
- 无涨跌幅限制；设有 VCM 市调机制
- 交易规则：T+0，可做空，港币计价
- 关键指标：恒生指数/恒生科技、南向资金（港股通）、AH 溢价
- 行业分类：恒生行业分类（HSICS）
- 止损参考：-10%
- 估值体系：PE/PB 偏低，注意流动性折价与仙股风险
- 宏观驱动：美联储（联系汇率下利率同步）、中国内地政策、南向资金流""",
}


def _get_market_context_text(markets: list[str] | None = None) -> str:
    """根据观察池涉及的市场生成上下文文本。"""
    if not markets:
        return _MARKET_CONTEXT["us_stock"]
    parts = []
    for m in sorted(set(markets)):
        if m in _MARKET_CONTEXT:
            parts.append(_MARKET_CONTEXT[m])
    return "\n\n".join(parts) if parts else _MARKET_CONTEXT["us_stock"]


def _as_num(v, default):
    """把 LLM 返回的数字字段容错转 float，失败返回 default。"""
    if isinstance(v, bool):
        return default
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v.strip().rstrip("%"))
        except ValueError:
            return default
    return default


def _normalize_result_tickers(result: dict) -> None:
    """就地把 LLM 决策结果里所有 ticker 字段归一为 canonical（A股 .SH→.SS 等）。
    杜绝 LLM 输出 .SH 与观察池 .SS 精确匹配失败导致的 L2/L3/L4 连接漏配、场景估值跳过。"""
    if not isinstance(result, dict):
        return
    ss = result.get("stock_selection")
    if isinstance(ss, dict):
        for bucket in ("core_holdings", "tactical_holdings"):
            for h in ss.get(bucket, []) or []:
                if isinstance(h, dict) and h.get("ticker"):
                    h["ticker"] = normalize_ticker(h["ticker"])
        wl = ss.get("watchlist_only")
        if isinstance(wl, list):
            ss["watchlist_only"] = [normalize_ticker(t) for t in wl if t]
    for key in ("tactical_plans", "execution_plans"):
        for p in result.get(key, []) or []:
            if isinstance(p, dict) and p.get("ticker"):
                p["ticker"] = normalize_ticker(p["ticker"])


def _union_strs(lists: list) -> list:
    """把多个字符串列表并集去重（保序），用于合并各模型的风险/信号。"""
    out: list = []
    seen: set = set()
    for lst in lists:
        if not isinstance(lst, list):
            continue
        for item in lst:
            s = item if isinstance(item, str) else json.dumps(item, ensure_ascii=False, sort_keys=True)
            if s not in seen:
                seen.add(s)
                out.append(item)
    return out


def _merge_key_signals(lists: list) -> list:
    """合并各模型的 key_signals（dict 列表），按 name 去重，保留首次出现。"""
    out: list = []
    seen: set = set()
    for lst in lists:
        if not isinstance(lst, list):
            continue
        for sig in lst:
            if not isinstance(sig, dict):
                continue
            name = str(sig.get("name", "")).strip().lower()
            key = name or json.dumps(sig, ensure_ascii=False, sort_keys=True)
            if key not in seen:
                seen.add(key)
                out.append(sig)
    return out


def _merge_sector_rotation(dicts: list) -> dict:
    """合并 sector_rotation 的 strengthening/weakening/neutral 三桶（各自并集）。"""
    merged = {"strengthening": [], "weakening": [], "neutral": []}
    for d in dicts:
        if not isinstance(d, dict):
            continue
        for bucket in merged:
            merged[bucket] = _union_strs([merged[bucket], d.get(bucket)])
    # 同一板块被某模型判为强、另一模型判为弱时，从 neutral 移除以免自相矛盾
    strong_weak = set(merged["strengthening"]) | set(merged["weakening"])
    merged["neutral"] = [s for s in merged["neutral"] if s not in strong_weak]
    return merged


def _merge_macro_results(results: list[dict]) -> dict:
    """合并多个 L1 宏观策略结果 — 真交叉验证。

    - regime / risk_appetite：多数投票
    - regime_confidence / recommended_cash_pct：数值均值；有分歧时下调 confidence（不确定性惩罚）
    - risk_factors / key_signals / sector_rotation：并集去重（保留每个模型的风险与信号，不丢弃）
    - strategy_text 等自由文本：取命中多数 regime 的模型（保持内在一致），而非盲取第一个
    - 分歧时写入 _divergence_warning（regime 与 appetite 各自判定）
    """
    if len(results) == 1:
        return results[0]

    regimes = [r.get("regime", "sideways") for r in results]
    regime = Counter(regimes).most_common(1)[0][0]

    appetites = [r.get("risk_appetite", "balanced") for r in results]
    risk_appetite = Counter(appetites).most_common(1)[0][0]

    regime_divergent = any(x != regime for x in regimes)
    appetite_divergent = any(x != risk_appetite for x in appetites)

    # 以"命中多数 regime"的模型为主体，保其 strategy_text/sector_rotation 内在一致
    base = next((r for r in results if r.get("regime") == regime), results[0]).copy()
    base["regime"] = regime
    base["risk_appetite"] = risk_appetite

    # confidence 均值，分歧即不确定：每类分歧 -1，下限 1
    avg_conf = sum(_as_num(r.get("regime_confidence"), 5) for r in results) / len(results)
    penalty = (1 if regime_divergent else 0) + (1 if appetite_divergent else 0)
    base["regime_confidence"] = max(1, round(avg_conf - penalty, 1))

    cash_vals = [_as_num(r.get("recommended_cash_pct"), None) for r in results]
    cash_vals = [c for c in cash_vals if c is not None]
    if cash_vals:
        base["recommended_cash_pct"] = round(sum(cash_vals) / len(cash_vals), 1)

    # 列表字段并集：两个模型的风险/信号都保留，避免丢弃 model B 的告警
    base["risk_factors"] = _union_strs([r.get("risk_factors") for r in results])
    base["key_signals"] = _merge_key_signals([r.get("key_signals") for r in results])
    base["sector_rotation"] = _merge_sector_rotation([r.get("sector_rotation") for r in results])

    warnings = []
    if regime_divergent:
        warnings.append(f"regime 不一致 ({regimes})")
    if appetite_divergent:
        warnings.append(f"risk_appetite 不一致 ({appetites})")
    if warnings:
        base["_divergence_warning"] = "模型分歧: " + "; ".join(warnings)

    return base


def _clamp_target_allocation(result: dict, bounds: dict) -> list[str]:
    """把 L2 target_allocation 钳制到 L1 alloc_bounds 内，返回被钳制项的说明列表。

    bounds 来自 get_allocation_bounds：equity_min/equity_max/max_single_pct/beta_limit。
    这是确定性硬约束落地——避免 LLM 给出 equity 99%/单股 40% 后被下游原样放行。

    P1-G（N-14）：类型不对时**不再静默跳过**。原先三处 `isinstance(x, (int,float))` 为假就既不
    钳、也不告警、也不填默认值——而 LLM 返回 `"equity_pct": "55%"`（带百分号的字符串）是常见畸变。
    污染的后果不是"少钳一次"：`_compute_deviation_drift` 用同一判据读它 → `has_target` 只剩
    `sector_targets` → `rebalance_suggested` 可能变 None → `_reuse_escape_reason` 见 None 直接
    `return {}`，**整个逃逸机制永久沉默**，且不留任何痕迹。这里把每一次静默改成 ① 能修的修
    （字符串数字 → float）、② 修不了的记 warning（既有 `_clamp_warnings` 通道会发 SSE）。
    """
    ta = result.get("target_allocation")
    if not isinstance(ta, dict) or not bounds:
        return []
    warnings: list[str] = []

    def _num(v) -> bool:
        """bool 是 int 的子类，`True` 不该被当成数字 1 混进仓位计算。"""
        return isinstance(v, (int, float)) and not isinstance(v, bool)

    # 先做一轮就地修复：LLM 把数字写成带 % 的字符串 / 混合类型，是可救的，不该升级成"静默丢弃"。
    for _k in ("equity_pct", "cash_pct", "max_single_stock_pct", "max_portfolio_beta"):
        _v = ta.get(_k)
        if _v is None or _num(_v):
            continue
        if isinstance(_v, str):
            try:
                ta[_k] = float(_v.strip().rstrip("%"))
                warnings.append(f"{_k} 类型修正 {_v!r}→{ta[_k]}")
                continue
            except ValueError:
                pass
        # 不可救：只留这一条（下面 equity_pct 的填默认值会给出路，不再叠一条重复说明）
        warnings.append(f"{_k} 类型不可用（{type(_v).__name__}: {_v!r}），该字段本轮不参与钳制与偏离判据")

    eq = ta.get("equity_pct")
    if _num(eq):
        lo, hi = bounds.get("equity_min", 0), bounds.get("equity_max", 100)
        if eq > hi:
            ta["equity_pct"] = hi
            warnings.append(f"equity_pct {eq}→{hi}（上限）")
        elif eq < lo:
            ta["equity_pct"] = lo
            warnings.append(f"equity_pct {eq}→{lo}（下限）")
    else:
        # 缺失/不可救 → 填 L1 推荐值，而不是留个 None 让下游的偏离判据静默失效（N-14 的病根）
        _rec = bounds.get("recommended_equity")
        if _num(_rec):
            ta["equity_pct"] = _rec
            warnings.append(f"equity_pct 缺失或不可用，填 L1 推荐值 {_rec}")

    ms = ta.get("max_single_stock_pct")
    cap = bounds.get("max_single_pct")
    if _num(ms) and cap and ms > cap:
        ta["max_single_stock_pct"] = cap
        warnings.append(f"max_single_stock_pct {ms}→{cap}")

    mb = ta.get("max_portfolio_beta")
    blimit = bounds.get("beta_limit")
    if _num(mb) and blimit and mb > blimit:
        ta["max_portfolio_beta"] = blimit
        warnings.append(f"max_portfolio_beta {mb}→{blimit}")

    return warnings


def _portfolio_risk_summary(store: WatchlistStore, positions: list[dict], total_equity: float) -> dict:
    """计算组合风险摘要（HHI/VaR/CVaR/beta/最大单股·板块/高相关对/预警），供 L2 与投委会消费。

    复用 risk_metrics.compute_portfolio_risk（与 /risk-dashboard 同一取数逻辑）。无持仓返回空摘要。
    """
    if not positions:
        return {}
    try:
        from bottleneck_hunter.watchlist.risk_metrics import compute_portfolio_risk

        price_histories = {}
        for pos in positions:
            tk = pos.get("ticker", "")
            snaps = store.get_snapshots(tk, days=60)
            prices = [s["close"] for s in reversed(snaps) if s.get("close")] if snaps else []
            if prices:
                price_histories[tk] = prices
        # 0-2: 接通基准日收益率 → portfolio_beta 生效（此前 VIP/L2 路径恒为伪数 0.0）。
        # 复用 value_series 同一基准口径（default_benchmark_ticker + 共享桶快照），收益率按最旧→最新排列；
        # compute 内改按"最近对齐"取各序列尾部同长窗口（截至最新交易日按日历对齐，防稀疏史错配）。
        from bottleneck_hunter.watchlist.macro_data import default_benchmark_ticker

        bench_code, _ = default_benchmark_ticker(getattr(store, "_market", "") or "us_stock")
        bench_snaps = store.get_snapshots(bench_code, days=60)
        bench_closes = [s["close"] for s in reversed(bench_snaps) if s.get("close")] if bench_snaps else []
        benchmark_returns = [
            bench_closes[i] / bench_closes[i - 1] - 1 for i in range(1, len(bench_closes)) if bench_closes[i - 1] > 0
        ]
        m = compute_portfolio_risk(
            positions=positions,
            price_histories=price_histories,
            benchmark_returns=benchmark_returns or None,
            total_equity=total_equity or 100000.0,
        )
        return {
            "concentration_hhi": m.concentration_index,
            "max_single_weight_pct": m.max_single_weight,
            "max_sector_weight_pct": m.max_sector_weight,
            "var_95": m.var_95,
            "cvar_95": m.cvar_95,
            "portfolio_beta": m.portfolio_beta,
            "portfolio_volatility_pct": m.portfolio_volatility,
            "risk_coverage": {"priced": m.priced_count, "total": m.total_count, "weight_pct": m.priced_weight_pct},
            "high_correlation_pairs": m.correlation_pairs,
            "warnings": m.warnings,
        }
    except Exception as e:
        logger.warning("组合风险摘要计算失败: %s", e)
        return {}


def _compute_deviation_drift(
    store: WatchlistStore, plan_rj: dict, account: dict, positions: list[dict], market: str
) -> dict:
    """确定性计算 L2 偏离度：实际 vs 目标（equity/cash/sector 权重），代替 LLM 心算。

    返回 {equity_drift_pct, cash_drift_pct, sector_drift: [...], max_abs_drift_pct, rebalance_suggested}。
    """
    total_equity = account.get("total_equity", 100000) or 100000
    cash = account.get("cash_balance", 100000)
    pos_value = sum(p.get("market_value", 0) for p in positions)
    actual_equity_pct = round(pos_value / total_equity * 100, 1)
    actual_cash_pct = round(cash / total_equity * 100, 1)

    ta = plan_rj.get("target_allocation", {}) if isinstance(plan_rj, dict) else {}
    if not isinstance(ta, dict):
        ta = {}
    target_sector = plan_rj.get("sector_targets", {}) if isinstance(plan_rj, dict) else {}
    if not isinstance(target_sector, dict):
        target_sector = {}

    target_equity = ta.get("equity_pct")
    has_target = isinstance(target_equity, (int, float)) or bool(target_sector)

    equity_drift = 0.0
    cash_drift = 0.0
    if isinstance(target_equity, (int, float)):
        target_cash = ta.get("cash_pct", 100 - target_equity)
        equity_drift = round(actual_equity_pct - target_equity, 1)
        if isinstance(target_cash, (int, float)):
            cash_drift = round(actual_cash_pct - target_cash, 1)
    else:
        target_equity = None
        target_cash = ta.get("cash_pct")

    # sector 实际权重（用观察池 sector 映射）
    sector_map = {
        e["ticker"]: e.get("sector", "未分类")
        for e in store.list_all()
        if normalize_market(e.get("market")) == normalize_market(market)
    }
    actual_sector: dict[str, float] = {}
    for p in positions:
        sec = sector_map.get(p.get("ticker", ""), "未分类")
        actual_sector[sec] = actual_sector.get(sec, 0) + p.get("market_value", 0) / total_equity * 100

    sector_drift = []
    for sec in set(list(actual_sector.keys()) + list(target_sector.keys())):
        act = round(actual_sector.get(sec, 0), 1)
        tgt = target_sector.get(sec)
        tgt = tgt.get("target_pct") if isinstance(tgt, dict) else tgt
        if isinstance(tgt, (int, float)):
            sector_drift.append({"sector": sec, "actual_pct": act, "target_pct": tgt, "drift_pct": round(act - tgt, 1)})

    drifts = [abs(d["drift_pct"]) for d in sector_drift]
    if isinstance(target_equity, (int, float)):
        drifts += [abs(equity_drift), abs(cash_drift)]
    max_abs = max(drifts) if drifts else 0
    return {
        "actual_equity_pct": actual_equity_pct,
        "target_equity_pct": target_equity,
        "equity_drift_pct": equity_drift,
        "actual_cash_pct": actual_cash_pct,
        "target_cash_pct": target_cash,
        "cash_drift_pct": cash_drift,
        "sector_drift": sector_drift,
        "max_abs_drift_pct": round(max_abs, 1),
        # 有明确目标才做确定性判定；无目标(旧格式/缺失)返回 None → 交由 LLM 判断
        "rebalance_suggested": (max_abs > 5) if has_target else None,
    }


def _holder_qoq(store: WatchlistStore, ticker: str) -> dict | None:
    """个股 13F 近两季环比：两季共同机构的净增减方向/幅度 + 增/减仓机构名单。

    只算两季都在的机构（规避 yfinance 只给 top-N 持有人导致的进出榜噪声＝非真加减仓）。
    <2 申报季 或 无共同机构 → None（诚实降级，不编方向）。默认多季读，故不传 latest_only。
    """
    rows = store.get_institutional_holders(ticker, limit=200)
    dates = sorted({r.get("date") for r in rows if r.get("date")}, reverse=True)
    if len(dates) < 2:
        return None
    cur = {r["holder_name"]: (r.get("shares") or 0) for r in rows if r.get("date") == dates[0]}
    old = {r["holder_name"]: (r.get("shares") or 0) for r in rows if r.get("date") == dates[1]}
    common = set(cur) & set(old)
    if not common:
        return None
    deltas = {h: cur[h] - old[h] for h in common}
    net = sum(deltas.values())
    added = sorted((h for h in common if deltas[h] > 0), key=lambda h: deltas[h], reverse=True)
    trimmed = sorted((h for h in common if deltas[h] < 0), key=lambda h: deltas[h])
    return {
        "cur_quarter": dates[0],
        "prev_quarter": dates[1],
        "direction": "净增持" if net > 0 else "净减持" if net < 0 else "持平",
        "net_shares": net,
        "common_holders": len(common),
        "added_holders": added[:5],
        "trimmed_holders": trimmed[:5],
    }


def _chip_context(store: WatchlistStore, ticker: str) -> dict:
    """B5: 汇总该股的筹码/估值锚信号——机构持仓 Top + 分析师评级分布 + 一致目标价（读库，零抓取）。

    数据由 scheduler 定时采集入库；无数据则返回空 dict，L3 提示词按缺省处理。
    """
    out = {}
    try:
        holders = store.get_institutional_holders(ticker, limit=5, latest_only=True)
        if holders:
            out["top_institutions"] = [
                {"name": h.get("holder_name", ""), "pct_held": h.get("pct_held", 0)} for h in holders[:5]
            ]
            out["institution_count"] = len(store.get_institutional_holders(ticker, limit=50, latest_only=True))
    except Exception:
        pass
    try:
        ratings = store.get_analyst_ratings(ticker, limit=20)
        if ratings:
            dist: dict[str, int] = {}
            targets = []
            for r in ratings:
                rat = (r.get("rating", "") or "").strip().lower() or "unknown"
                dist[rat] = dist.get(rat, 0) + 1
                tp = r.get("target_price")
                if isinstance(tp, (int, float)) and tp > 0:
                    targets.append(tp)
            out["rating_distribution"] = dist
            if targets:
                out["consensus_target_price"] = round(sum(targets) / len(targets), 2)
                out["target_price_range"] = [min(targets), max(targets)]
    except Exception:
        pass
    # P1-⑤：个股 13F 近两季环比(方向+增/减仓机构)，自动流向 L3 chip_signals 与宏观咨询焦点块
    qoq = _holder_qoq(store, ticker)
    if qoq:
        out["institutional_qoq"] = qoq
    return out


# ─────────────────────────────────────────────────────────
# L1: 宏观策略
# ─────────────────────────────────────────────────────────


async def _inject_market_news(
    store: WatchlistStore, market: str, market_data: dict, llm, budget: BudgetTracker | None
) -> None:
    """把市场/主题级近期新闻注入 market_data['news']（优先读库，未采集则实时兜底）。"""
    from bottleneck_hunter.watchlist.news_pipeline import fetch_market_news, market_sentinel
    from bottleneck_hunter.watchlist.prompt_guard import sanitize_external_text

    _mnews = store.get_news(market_sentinel(market), limit=15)
    if _mnews:
        market_data["news"] = [
            {
                "topic": sanitize_external_text(n.get("llm_analysis", "")),
                "title": sanitize_external_text(n.get("title", "")),
                "summary": sanitize_external_text(n.get("summary", "")),
                "date": n.get("date", ""),
                "source_name": n.get("source_name", ""),
                "sentiment": n.get("sentiment", ""),
            }
            for n in _mnews
        ]
    else:
        market_data["news"] = await fetch_market_news(market, llm, budget)


async def run_macro_strategy(
    store: WatchlistStore,
    budget: BudgetTracker | None = None,
    market: str = "us_stock",
    *,
    market_data: dict | None = None,
) -> AsyncGenerator[dict, None]:
    """生成全新的 L1 宏观策略（通常每周一次）。

    market_data 可由上游（如 L1 日检判定重大修订时）传入已采集好的市场数据，
    避免重复跑一轮 yfinance/akshare 采集与新闻注入。
    """
    store = store.for_market(market)
    yield _sse("decision_start", layer="L1", action="generate", market=market, message="开始生成 L1 宏观策略...")

    llm, provider, model = get_llm_for_position(position="L1_macro")
    if not llm:
        yield _sse("decision_error", layer="L1", error="无可用 LLM")
        return

    if budget and not budget.can_spend(estimated_tokens=5000):
        yield _sse("decision_error", layer="L1", error="预算不足")
        return

    try:
        if market_data is None:
            market_data = await _collect_market_context(store, market)
            await _inject_market_news(store, market, market_data, llm, budget)
        active_markets = market_data.get("markets", [])
        market_ctx = _get_market_context_text(active_markets)
        prompt_template = _load_prompt("decision_macro")
        prompt = (
            prompt_template.replace("{market_context}", market_ctx)
            .replace("{market_indices}", json.dumps(market_data.get("indices", {}), ensure_ascii=False))
            .replace("{sector_performance}", json.dumps(market_data.get("sectors", {}), ensure_ascii=False))
            .replace("{sentiment_indicators}", json.dumps(market_data.get("sentiment", {}), ensure_ascii=False))
            .replace("{macro_economic}", json.dumps(market_data.get("macro", {}), ensure_ascii=False))
            .replace("{market_news}", json.dumps(market_data.get("news", []), ensure_ascii=False))
            .replace("{user_persona}", format_persona_for_prompt(store))
        )

        input_prompts = []
        all_models = get_models_for_role("L1_macro")
        use_cross = len(all_models) >= 2

        if use_cross:
            yield _sse(
                "decision_progress",
                layer="L1",
                step="llm_reasoning",
                message=f"L1 双模型交叉验证中... ({len(all_models)} 路)",
            )

            async def _invoke_model(m_llm, m_prov, m_mod):
                input_prompts.append(prompt)
                r = await asyncio.to_thread(lambda: m_llm.invoke(prompt).content)
                if budget:
                    budget.record(m_prov, m_mod, 5000, 2000, "macro_strategy")
                return extract_json_object(r), m_prov, m_mod

            tasks = [_invoke_model(*m) for m in all_models[:2]]
            results_raw = await asyncio.gather(*tasks, return_exceptions=True)
            valid_results = [item for item in results_raw if not isinstance(item, Exception)]

            if len(valid_results) >= 2:
                result = _merge_macro_results([v[0] for v in valid_results])
                result["_cross_validated"] = True
                result["_models_used"] = [f"{v[1]}:{v[2]}" for v in valid_results]
                logger.info("L1 宏观策略双模型交叉验证完成: regime=%s", result.get("regime"))
            elif valid_results:
                result = valid_results[0][0]
            else:
                raise RuntimeError("所有模型调用均失败")
        else:
            yield _sse("decision_progress", layer="L1", step="llm_reasoning", message="L1 LLM 推理中...")
            allowed_tk = _decision_allowed_tickers(store, market)
            result, _fetch_log = await _run_data_negotiation(
                llm, prompt, market=market, layer="1", allowed_tickers=allowed_tk, input_prompts=input_prompts
            )
            if _fetch_log:
                yield _sse(
                    "decision_progress",
                    layer="L1",
                    step="data_fetch_round",
                    message=f"L1 数据补充 {len(_fetch_log)} 条",
                )
            if budget:
                budget.record(provider, model, 5000, 2000, "macro_strategy")
        _l1_models = [tuple(s.split(":", 1)) for s in result.get("_models_used", []) if ":" in s] or [
            (provider, model)
        ]  # 交叉验证用实际参与的多模型，否则单模型
        result["_provenance"] = _decision_provenance(["decision_macro"], _l1_models, market, "L1")
        _l1_binding = save_stage_snapshot(store, "L1", {"market_data": market_data, "prompts": input_prompts})
        strategy_id = store.create_macro_strategy(result, **_l1_binding)

        yield _sse(
            "decision_done",
            layer="L1",
            strategy_id=strategy_id,
            regime=result.get("regime", "sideways"),
            risk_appetite=result.get("risk_appetite", "balanced"),
            message=f"L1 宏观策略已生成：{result.get('regime', '?')} / {result.get('risk_appetite', '?')}",
        )

    except Exception as e:
        logger.exception("L1 宏观策略生成失败")
        yield _sse("decision_error", layer="L1", error=str(e))


def _format_downstream_feedback(store: WatchlistStore, *, days: int = 30, min_count: int = 2) -> str:
    """P2-D（N-15）：把"被投委会反复否掉"的标的渲染成 L1 日检 prompt 里的一小段。

    只留痕、只进 prompt，**不**自动改 regime：让下游改写上游的宏观判断风险远大于收益。
    任何异常都吞成"无"——L1 日检是每日必跑的链路，一个附属读库失败不该让它整轮挂掉
    （反馈是**参考信息**，不是日检的判据）。
    """
    try:
        rows = store.get_committee_rejection_summary(days=days, min_count=min_count, limit=5)
    except Exception:
        logger.warning("L1 日检：投委会否决聚合读取失败，本轮不带下游反馈", exc_info=True)
        return "无"
    if not rows:
        return "无"
    lines = []
    for r in rows:
        _sector = (r.get("sector") or "").strip()
        lines.append(
            f"- {r['ticker']}　意图：{r.get('action', '?')}　近 {days} 天被否 {r['rejects']} 次"
            f"　最近一次：{(r.get('last_at') or '')[:10]}"
            + (f"　板块：{_sector}" if _sector else "")
        )
    return "\n".join(lines)


async def run_macro_check(
    store: WatchlistStore,
    budget: BudgetTracker | None = None,
    market: str = "us_stock",
) -> AsyncGenerator[dict, None]:
    """L1 日常检查 — 判断现有宏观策略是否仍然有效"""
    store = store.for_market(market)
    yield _sse("decision_start", layer="L1", action="check", message="L1 日常检查中...")

    current = store.get_latest_macro_strategy()
    if not current:
        yield _sse("decision_info", layer="L1", message="无现有 L1 策略，需要先全面生成")
        async for evt in run_macro_strategy(store, budget, market=market):
            yield evt
        return

    llm, provider, model = get_llm_for_position(position="L1_macro")
    if not llm:
        yield _sse("decision_error", layer="L1", error="无可用 LLM")
        return

    if budget and not budget.can_spend(estimated_tokens=2000):
        yield _sse("decision_error", layer="L1", error="预算不足")
        return

    try:
        market_data = await _collect_market_context(store, market)
        await _inject_market_news(store, market, market_data, llm, budget)
        active_markets = market_data.get("markets", [])
        market_ctx = _get_market_context_text(active_markets)
        created_at = current.get("created_at", "")
        days_ago = 0
        if created_at:
            try:
                created_dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                days_ago = (datetime.now(timezone.utc) - created_dt).days
            except (ValueError, TypeError):
                pass

        prompt_template = _load_prompt("decision_macro_check")
        prompt = (
            prompt_template.replace("{market_context}", market_ctx)
            .replace("{strategy_date}", created_at[:10] if created_at else "未知")
            .replace("{days_ago}", str(days_ago))
            .replace("{version}", str(current.get("version", 1)))
            .replace("{current_strategy}", json.dumps(current.get("result_json", {}), ensure_ascii=False))
            .replace("{today_market_data}", json.dumps(market_data, ensure_ascii=False))
            .replace("{downstream_feedback}", _format_downstream_feedback(store))
        )

        result = await _llm_json_object(llm, prompt, layer="L1-check")

        if budget:
            budget.record(provider, model, 2000, 500, "macro_check")

        status = result.get("strategy_status", "valid")

        if status == "needs_major_revision":
            yield _sse("decision_info", layer="L1", message="L1 宏观策略需要重大修订，开始重新生成...")
            # 复用日检已采集的 market_data，避免重复跑一轮 yfinance/akshare 采集
            async for evt in run_macro_strategy(store, budget, market=market, market_data=market_data):
                yield evt
        else:
            store.update_macro_status(
                current["id"],
                status,
                minor_tweaks=result.get("minor_tweaks"),
                # 日检的推理结论（"今日市场与策略的一致性"）此前只有下面那句 SSE，生成完即丢 ——
                # 任何界面都看不到。落进策略的 result_json，随 L1 面板一起展示（见 update_macro_status）。
                daily_commentary=result.get("daily_commentary", ""),
            )
            yield _sse(
                "decision_done",
                layer="L1",
                action="check",
                status=status,
                commentary=result.get("daily_commentary", ""),
                message=f"L1 检查完成：{status}",
            )

    except Exception as e:
        logger.exception("L1 日常检查失败")
        yield _sse("decision_error", layer="L1", error=str(e))


# ─────────────────────────────────────────────────────────
# L2: 组合策略
# ─────────────────────────────────────────────────────────


# P1-1 漂移逃逸阈值：实际权益/现金偏离该 L2 目标超过此幅度 → 日常流程不再只做偏离检查，强制重生成 L2。
# 15pct 是"目标已失效"的量级（远超 drift 的 5pct 告警线），避免日常小波动就烧一次 L2 算力。
_DECISION_DRIFT_ESCAPE_PCT = float(_os.getenv("BH_DECISION_DRIFT_ESCAPE_PCT", "15"))
# 逃逸冷却：L2 不下单，重生成后偏离照旧 → 不设冷却会每轮都重生成。L2 须至少这么"老"才允许逃逸，
# 即偏离持续期间每天最多重生成一次。
# ponytail: 固定 20h（≈复用窗口），偏离长期不收敛时仍每天一次 L2 算力；要更省就改成指数退避。
_DECISION_ESCAPE_MIN_AGE_H = 20.0


def _reuse_escape_reason(store: WatchlistStore, plan_rj: dict, market: str) -> dict:
    """P1-1：L2 逃逸判据。组合实际配置已严重偏离该 L2 目标 → 返回逃逸原因 dict，否则 {}。

    纯确定性读库，不调 LLM。无明确目标(equity_pct/sector_targets 都缺)时返回 {}（无从判偏离，
    与 _compute_deviation_drift 的 has_target 降级口径一致）。纯现金账户照判——那正是"钱没配出去"的最坏情形。
    """
    try:
        account = store.get_sim_account()
        if not account:
            return {}
        positions = store.get_sim_positions(account.get("id"))
        drift = _compute_deviation_drift(store, plan_rj, account, positions, market)
        if drift.get("rebalance_suggested") is None:
            return {}
        # P1-F（N-12）：候选集必须**含板块漂移**。`_compute_deviation_drift` 早就在算 `sector_drift`
        # 并把它并进了 `max_abs_drift_pct`，但这里只取权益/现金两项 —— 于是最典型的"策略已失效"
        # 形态（板块结构被打乱）算得出、却逃逸不了：权益/现金都贴着目标时整体啥也不做。
        # 板块用了 `target_pct` 明细的票才进候选，与 `max_abs_drift_pct` 同口径。
        _cands = [("权益", drift["equity_drift_pct"]), ("现金", drift["cash_drift_pct"])]
        _sector_drift = drift.get("sector_drift") or []
        if _sector_drift:
            _sec_top = max(_sector_drift, key=lambda d: abs(d["drift_pct"]))
            _cands.append((f"板块[{_sec_top['sector']}]", _sec_top["drift_pct"]))
        top = max(_cands, key=lambda kv: abs(kv[1]))
        if abs(top[1]) > _DECISION_DRIFT_ESCAPE_PCT:
            if top[0].startswith("板块["):
                # 板块逃逸：括号里报权益目标会误导（那条线本来就在带内），要报的是这个板块自己的数
                _sec = next(d for d in _sector_drift if f"板块[{d['sector']}]" == top[0])
                _ctx = f"实际 {_sec['actual_pct']}% / 目标 {_sec['target_pct']}%"
            else:
                _ctx = (f"实际 {drift['actual_equity_pct']}%权益/{drift['actual_cash_pct']}%现金，"
                        f"目标 {drift['target_equity_pct']}%权益")
            return {
                "reason": f"{top[0]}偏离目标 {top[1]:+.1f}pct（{_ctx}）",
                "drift_pct": top[1],
                "threshold_pct": _DECISION_DRIFT_ESCAPE_PCT,
                "drift": drift,
            }
    except Exception:
        logger.debug("L2 复用逃逸判据计算跳过", exc_info=True)
    return {}


# ─────────────────────────────────────────────────────────
# P1-C：缺口未纠正留痕（run_daily_decision 与 run_full_refresh 共用判据）
# ─────────────────────────────────────────────────────────

def _gap_deviation(store: WatchlistStore, market: str) -> dict:
    """读库算出账户相对 regime 区间的**两侧**偏差（只算不拦）：权益不足 / 现金超配。

    P1-C：判断"该配没配"的判据此前只长在 run_daily_decision 里，UI 上"一键全量刷新"
    这条路径看不到——同一件事两处各写一遍迟早分叉，抽成一份供两条路径调用。
    """
    macro = store.get_latest_macro_strategy()
    account = store.get_sim_account()
    if not macro or not account:
        return {}
    mj = macro.get("result_json", {}) or {}
    bounds = get_allocation_bounds(
        mj.get("regime", "sideways"), mj.get("risk_appetite", "balanced"), mj.get("regime_confidence", 5)
    )
    from bottleneck_hunter.watchlist.constraint_validator import compute_underweight_gap

    return compute_underweight_gap(account, store.get_sim_positions(account.get("id")), bounds)


def _l4_plan_count(evt: dict, prev: int | None) -> int | None:
    """从 `decision_done` 里取"跑了但没产出"的确证数；只在事件**带 `plan_count`** 时取值。

    L3（`run_tactical_plans` 末尾）与 L4（`run_execution_plans` 末尾）都发带 `plan_count` 的
    `decision_done`，但 L4 另有两种**不带该字段**的提前返回：无 L3 计划时发 `decision_info`、
    L3 全持有时的 `decision_done`。按 `.get(...) or 0` 取值会把「没跑到产出那一步」误读成
    「零产出」，凭空造出一条缺口告警——故只认字段真存在的那一种，其余保持 None（＝没跑到）。
    """
    if evt.get("event") != "decision_done":
        return prev
    data = evt.get("data", {}) if isinstance(evt.get("data"), dict) else {}
    return int(data["plan_count"] or 0) if "plan_count" in data else None


def _uncorrected_gap_cause(store: WatchlistStore, *, l4_blocked: bool = False,
                           l4_plan_count: int | None = None) -> str:
    """本轮扩张侧停摆的原因；正常运行（能补仓且未被拦）返回 ""。

    L1/L2 是周度生成、L3/L4 每日跑，故上游陈旧＝本轮没资格谈补仓；pre_l4 红灯＝L4 被拦；
    L4 跑完却零产出＝方案层没能把战术计划变成任何可执行单。三种情形都只能停手
    （不能盲下单），但"该配的钱没配出去"必须留下痕迹。

    `l4_plan_count is None` 表示 L4 没跑到产出那一步（无战术计划/全持有/被拦/未运行），
    与"跑了但产出 0 条"是两回事，不并为一谈——猜错方向会把「没跑到」写成一条假告警。
    """
    macro = store.get_latest_macro_strategy()
    if not macro:
        return ""
    for label, plan in (("L2", store.get_latest_strategic_plan()), ("L1", macro)):
        age = _upstream_age_days((plan or {}).get("created_at", ""))
        if plan and (age is None or age > _STALE_UPSTREAM_DAYS):
            return f"上游 {label} 陈旧（L3 已中止）"
    if l4_blocked:
        return "pre_l4 质量门红灯"
    return "L4 未产出任何新方案" if l4_plan_count == 0 else ""


def _record_uncorrected_gap(store: WatchlistStore, market: str, cause: str) -> None:
    """P1-2 + P1-D 留痕：扩张侧停摆时，把"该配没配 / 钱趴着"记进 operation_log。

    cause 为空（本轮正常运行）时不记——正常运行下现金仍高于上限，那是 L2 图纸本身的保守
    选择，属于策略而非事故；只有"本来能纠偏却停摆了"才值得进推送白名单打扰用户。
    P1-D：`cash_max` 此前算出来没人读，比不算更糟（会让人以为权益下限已把现金上限对称化了）。
    这里给它第一个真实消费方，与 `equity_min` 对称。
    """
    if not cause:
        return
    uid = getattr(store, "_user_id", "") or ""
    if not uid:
        return
    gap = _gap_deviation(store, market)
    sides = []
    if gap.get("underweight"):
        sides.append(
            f"权益配置不足 —— 实际权益 {gap['equity_pct']}% < 下限 {gap['equity_min']}%，"
            f"缺口 {gap['gap_pct']}pct、可部署 {gap['deployable_cash']:.0f} 元未部署"
        )
    if gap.get("overweight_cash"):
        sides.append(
            f"现金超配 —— 实际现金 {gap['cash_pct']}% > 上限 {gap['cash_max']}%，"
            f"超出 {gap['excess_cash_pct']}pct、约 {gap['idle_cash']:.0f} 元闲置"
        )
    if not sides:
        return
    from bottleneck_hunter.web.oplog import record_operation

    record_operation(
        uid, "缺口未纠正",
        category="error",  # 同「质量门阻断」进推送白名单：钱趴着不配是要被看见的偏差
        detail=(f"{market}：{cause}，本轮未纠正配置偏差 —— " + "；".join(sides))[:300],
        result="partial", market=market,
        meta={"cause": cause, "equity_pct": gap["equity_pct"], "equity_min": gap["equity_min"],
              "gap_pct": gap["gap_pct"], "cash_pct": gap["cash_pct"], "cash_max": gap["cash_max"],
              "excess_cash_pct": gap["excess_cash_pct"]},
    )


async def run_strategic_plan(
    store: WatchlistStore,
    budget: BudgetTracker | None = None,
    market: str = "us_stock",
    force: bool = False,
) -> AsyncGenerator[dict, None]:
    """生成全新的 L2 组合策略。force=True 忽略当日复用缓存强制重生成。"""
    store = store.for_market(market)
    yield _sse("decision_start", layer="L2", action="generate", message="开始生成 L2 组合策略...")

    macro = store.get_latest_macro_strategy()
    if not macro:
        yield _sse("decision_info", layer="L2", message="无 L1 宏观策略，需要先生成")
        async for evt in run_macro_strategy(store, budget, market=market):
            yield evt
        macro = store.get_latest_macro_strategy()
        if not macro:
            yield _sse("decision_error", layer="L2", error="L1 策略生成失败，无法继续")
            return

    llm, provider, model = get_llm_for_position(position="L2_strategic")
    if not llm:
        yield _sse("decision_error", layer="L2", error="无可用 LLM")
        return

    # 复用未过时的 L2：若最新有效组合策略的上游 macro_strategy_id == 当前 L1 的 id（L1 未变→
    # L2 输入未变）且够新（默认 20h≈当日），直接复用、跳过重生成，省 L2 LLM(约 8000+3000 token)。
    # L2 是策略层非交易执行；每日仍有 run_deviation_check 做偏离检查，故当日复用安全。
    # BH_DECISION_REUSE_HOURS=0 可关闭。
    import os as _os

    _reuse_h = float(_os.getenv("BH_DECISION_REUSE_HOURS", "20"))
    if not force and _reuse_h > 0:
        try:
            _prev = store.get_latest_strategic_plan()
        except Exception:
            _prev = None
        if _prev and _prev.get("macro_strategy_id") == macro.get("id") and _prev.get("result_json"):
            _fresh = False
            try:
                from datetime import datetime, timezone

                _t = datetime.fromisoformat(_prev.get("created_at", ""))
                if _t.tzinfo is None:
                    _t = _t.replace(tzinfo=timezone.utc)
                _fresh = (datetime.now(timezone.utc) - _t).total_seconds() <= _reuse_h * 3600
            except (ValueError, TypeError):
                _fresh = False
            if _fresh:
                _rj = _prev.get("result_json") or {}
                yield _sse(
                    "decision_done",
                    layer="L2",
                    reused=True,
                    plan_id=_prev.get("id", ""),
                    stance=(_rj.get("overall_stance", "balanced") if isinstance(_rj, dict) else "balanced"),
                    result=_rj,
                    message=f"♻ 复用当日 L2 组合策略 v{_prev.get('version', '?')}（上游 L1 未变），跳过重生成省算力",
                )
                return

    if budget and not budget.can_spend(estimated_tokens=8000):
        yield _sse("decision_error", layer="L2", error="预算不足")
        return

    try:
        watchlist_signals = _collect_watchlist_signals(store, market)
        market_ctx = _get_market_context_text([market])  # 单市场：仅本市场规则，避免混入他市场交易规则
        account_status = store.get_sim_account()
        positions = store.get_sim_positions(account_status.get("id"))
        previous_plan = store.get_latest_strategic_plan()
        feedback = store.get_rejection_patterns(limit=10)

        lessons = ""
        if feedback:
            lessons = json.dumps(
                [{"ticker": f["ticker"], "reason": f["reason"]} for f in feedback[:5]],
                ensure_ascii=False,
            )

        prompt_template = _load_prompt("decision_strategic")

        macro_json = macro.get("result_json", {})
        regime = macro_json.get("regime", "sideways")
        risk_appetite = macro_json.get("risk_appetite", "balanced")
        confidence = macro_json.get("regime_confidence", 5)
        alloc_bounds = get_allocation_bounds(regime, risk_appetite, confidence)
        # 用户个人「单一持仓上限」是硬约束：与 L1 市场档位取更严者（min），确保用户偏好不被市场放大
        _user_cap = get_user_single_cap(store)
        if _user_cap:
            alloc_bounds["max_single_pct"] = min(alloc_bounds["max_single_pct"], _user_cap)
        bounds_text = format_bounds_for_prompt(alloc_bounds)

        prompt = (
            prompt_template.replace("{market_context}", market_ctx)
            .replace("{macro_strategy}", json.dumps(macro_json, ensure_ascii=False))
            .replace("{allocation_bounds}", bounds_text)
            .replace("{watchlist_signals}", json.dumps(watchlist_signals, ensure_ascii=False))
            .replace(
                "{account_status}",
                json.dumps(
                    {
                        "total_equity": account_status.get("total_equity", 100000),
                        "cash_balance": account_status.get("cash_balance", 100000),
                        "positions": [
                            {
                                "ticker": p["ticker"],
                                "weight_pct": p.get("weight_pct", 0),
                                "unrealized_pnl": p.get("unrealized_pnl", 0),
                            }
                            for p in positions
                        ],
                    },
                    ensure_ascii=False,
                ),
            )
            .replace(
                "{portfolio_risk}",
                json.dumps(
                    _portfolio_risk_summary(store, positions, account_status.get("total_equity", 100000)),
                    ensure_ascii=False,
                )
                or "暂无持仓风险数据",
            )
            .replace("{lessons_learned}", lessons or "暂无历史复盘数据")
            .replace(
                "{previous_strategic_plan}",
                json.dumps(
                    previous_plan.get("result_json", {}) if previous_plan else {},
                    ensure_ascii=False,
                ),
            )
            .replace("{user_persona}", format_persona_for_prompt(store))
        )

        yield _sse("decision_progress", layer="L2", step="llm_reasoning", message="L2 LLM 推理中...")

        _pos_tk = [p.get("ticker", "") for p in (positions or [])]
        allowed_tk = _decision_allowed_tickers(store, market, *_pos_tk)
        input_prompts = []
        result, _fetch_log = await _run_data_negotiation(
            llm, prompt, market=market, layer="2", allowed_tickers=allowed_tk, input_prompts=input_prompts
        )
        if _fetch_log:
            yield _sse(
                "decision_progress", layer="L2", step="data_fetch_round", message=f"L2 数据补充 {len(_fetch_log)} 条"
            )

        if budget:
            budget.record(provider, model, 8000, 3000, "strategic_plan")

        _normalize_result_tickers(result)  # 归一 holding ticker(.SH→.SS)，与观察池对齐
        # A4: 确定性钳制 L2 目标配置到 L1 alloc_bounds（防止 LLM 给出越界仓位/beta 后被下游放行）
        clamp_warnings = _clamp_target_allocation(result, alloc_bounds)
        if clamp_warnings:
            result["_clamp_warnings"] = clamp_warnings
            for w in clamp_warnings:
                yield _sse("decision_warning", layer="L2", message=f"⚠ L2 配置越界已钳制：{w}")
        # P0-E（N-23）：L2 自洽校验——顶层 target_allocation 与逐票明细必须对得上。
        # 生产实测过这个形态：顶层 equity_pct=51 而 core_holdings 四票合计只有 33%，相差 18pct。
        # 危害不是"数字难看"：下游（缺口驱动器 / 偏离报告 / 前端对照条）**全部只读明细**，顶层那个数
        # 没有任何消费者，于是 51 成了装饰字段，而驱动器按 L1 下限 40% 天天追一个 L2 从未答应过的
        # 目标——这正是"缺口驱动器天天跑、组合却纹丝不动"的根因之一（缺口恒为 40−33=7pct 且永不收敛）。
        # 只报警、**不自动改数**：改数等于替 LLM 编仓位，会把"模型自相矛盾"这个信号抹掉。
        _incons = _allocation_inconsistency(result)
        if _incons:
            result["allocation_inconsistent"] = _incons
            yield _sse("decision_warning", layer="L2", message=f"⚠ L2 配置自相矛盾：{_incons['detail']}")
            # 光发 SSE 不够：`runDaily` 只把它当进度条文案，下一条事件一到就被覆盖，流一断
            # 什么都不剩。而 P0-E 的全部意义就是让这处矛盾**被人看见**——所以落一条 operation_log。
            # 用 category=user_action 而非 error：这是"模型自相矛盾"的提示，不是流程失败，
            # 不该进推送白名单（否则每次 L2 取整差异都推一条 IM，很快就被忽略）。
            try:
                uid = getattr(store, "_user_id", "") or ""
                if uid:
                    from bottleneck_hunter.web.oplog import record_operation

                    record_operation(
                        uid, "L2 配置自相矛盾",
                        detail=f"{market}：{_incons['detail']}"[:300],
                        result="partial", market=market,
                        meta={"equity_pct": _incons["equity_pct"],
                              "detail_sum_pct": _incons["detail_sum_pct"],
                              "diff_pct": _incons["diff_pct"]},
                    )
            except Exception as e:  # noqa: BLE001 —— 留痕失败不得影响决策
                logger.debug("L2 自洽告警留痕失败: %s", e)
        _sel = result.get("stock_selection", {})
        _l2_tk = [h.get("ticker", "") for h in (_sel.get("core_holdings", []) + _sel.get("tactical_holdings", []))]
        result["_provenance"] = _decision_provenance(["decision_strategic"], [(provider, model)], market, "L2", _l2_tk)
        binding = save_stage_snapshot(
            store,
            "L2",
            {
                "prompts": input_prompts,
                "macro": macro,
                "allocation_bounds": alloc_bounds,
            },
        )
        plan_id = store.create_strategic_plan(macro["id"], result, **binding)

        # Phase 20D: 解析并保存三场景估值
        # 诚信原则：写入失败/跳过必须计数并告警，不再静默吞（历史上此表长期 0 行无人知）。
        sv_saved, sv_skipped_no_entry, sv_missing = 0, 0, 0
        try:
            stock_selection = result.get("stock_selection", {})
            entry_map = {
                e["ticker"]: e["id"]
                for e in store.list_all()
                if normalize_market(e.get("market")) == normalize_market(market)
            }
            for holding in stock_selection.get("core_holdings", []) + stock_selection.get("tactical_holdings", []):
                sv = holding.get("scenario_valuation")
                if not sv:
                    sv_missing += 1
                    continue
                ticker = holding.get("ticker", "")
                entry_id = entry_map.get(ticker, "")
                if not entry_id:
                    sv_skipped_no_entry += 1
                    logger.warning("场景估值跳过：%s 在市场 %s 的观察池中无匹配 entry", ticker, market)
                    continue
                snap = store.get_latest_snapshot(ticker)
                current_price = snap.get("close", 0) if snap else 0
                store.create_scenario_valuation(
                    entry_id=entry_id,
                    ticker=ticker,
                    strategic_plan_id=plan_id,
                    bear_price=sv.get("bear_price", 0),
                    bear_probability=sv.get("bear_probability", 20),
                    bear_rationale=sv.get("bear_rationale", ""),
                    base_price=sv.get("base_price", 0),
                    base_probability=sv.get("base_probability", 60),
                    base_rationale=sv.get("base_rationale", ""),
                    bull_price=sv.get("bull_price", 0),
                    bull_probability=sv.get("bull_probability", 20),
                    bull_rationale=sv.get("bull_rationale", ""),
                    current_price=current_price,
                    valuation_method=sv.get("valuation_method", "relative"),
                )
                sv_saved += 1
        except Exception as e:
            logger.error("场景估值保存异常: %s", e, exc_info=True)
        if sv_skipped_no_entry or (sv_missing and not sv_saved):
            yield _sse(
                "decision_warning",
                layer="L2",
                message=f"⚠ 场景估值：已存 {sv_saved}，无匹配entry跳过 {sv_skipped_no_entry}，LLM未产出 {sv_missing}",
            )
        logger.info("场景估值 L2: saved=%d skipped_no_entry=%d missing=%d", sv_saved, sv_skipped_no_entry, sv_missing)

        yield _sse(
            "decision_done",
            layer="L2",
            plan_id=plan_id,
            stance=result.get("overall_stance", "balanced"),
            message=f"L2 组合策略已生成：{result.get('overall_stance', '?')}",
        )

    except Exception as e:
        logger.exception("L2 组合策略生成失败")
        yield _sse("decision_error", layer="L2", error=str(e))


async def run_deviation_check(
    store: WatchlistStore,
    budget: BudgetTracker | None = None,
    market: str = "us_stock",
) -> AsyncGenerator[dict, None]:
    """L2 偏离检查 — 对比实际持仓与目标策略"""
    store = store.for_market(market)
    yield _sse("decision_start", layer="L2", action="deviation_check", message="L2 偏离检查中...")

    plan = store.get_latest_strategic_plan()
    if not plan:
        yield _sse("decision_info", layer="L2", message="无 L2 组合策略，跳过偏离检查")
        return

    llm, provider, model = get_llm_for_position(position="L2_strategic")
    if not llm:
        yield _sse("decision_error", layer="L2", error="无可用 LLM")
        return

    if budget and not budget.can_spend(estimated_tokens=3000):
        yield _sse("decision_error", layer="L2", error="预算不足")
        return

    try:
        account = store.get_sim_account()
        positions = store.get_sim_positions(account.get("id"))
        positions_data = []
        for p in positions:
            positions_data.append(
                {
                    "ticker": p["ticker"],
                    "shares": p.get("shares", 0),
                    "market_value": p.get("market_value", 0),
                    "weight_pct": p.get("weight_pct", 0),
                    "unrealized_pnl": p.get("unrealized_pnl", 0),
                }
            )

        prompt_template = _load_prompt("decision_deviation_check")
        # B7: 确定性计算偏离度，代替 LLM 心算；注入数值让 LLM 只做叙述与优先级
        drift = _compute_deviation_drift(store, plan.get("result_json", {}), account, positions, market)

        # P0-1 扩张侧信号（只算不拦、纯观测）：实际权益低于 regime 权益下限时，量化"配置不足"缺口。
        # 与收缩侧的天花板校验(validate_against_regime)对称，先让缺口在偏离报告/日志里可见，
        # 供后续缺口驱动器(P0-2)消费。ponytail: 观测阶段不下任何单，确认缺口计算准确再接执行。
        underweight_gap: dict = {}
        try:
            from bottleneck_hunter.watchlist.constraint_validator import compute_underweight_gap
            _macro = store.get_latest_macro_strategy()
            if _macro:
                _mj = _macro.get("result_json", {}) or {}
                _bounds = get_allocation_bounds(
                    _mj.get("regime", "sideways"),
                    _mj.get("risk_appetite", "balanced"),
                    _mj.get("regime_confidence", 5),
                )
                underweight_gap = compute_underweight_gap(account, positions, _bounds)
                if underweight_gap.get("underweight"):
                    # 带账户标识：多账户同机跑时，光看缺口/金额无法分辨是哪本账（此前只能靠金额量级猜）
                    logger.info(
                        "L2 偏离[%s/%s/账户 %s]：权益配置不足 实际 %.1f%% < 下限 %s%%，缺口 %.1fpct，可部署 %.0f",
                        market, account.get("account_ref") or "sim", account.get("id"),
                        underweight_gap["equity_pct"], underweight_gap["equity_min"],
                        underweight_gap["gap_pct"], underweight_gap["deployable_cash"],
                    )
        except Exception:
            logger.debug("underweight gap 计算跳过", exc_info=True)

        prompt = (
            prompt_template.replace("{strategic_plan}", json.dumps(plan.get("result_json", {}), ensure_ascii=False))
            .replace("{computed_drift}", json.dumps(drift, ensure_ascii=False))
            .replace(
                "{current_positions}",
                json.dumps(
                    {
                        "total_equity": account.get("total_equity", 100000),
                        "cash_balance": account.get("cash_balance", 100000),
                        "cash_pct": round(
                            account.get("cash_balance", 100000) / max(account.get("total_equity", 100000), 1) * 100, 1
                        ),
                        "positions": positions_data,
                    },
                    ensure_ascii=False,
                ),
            )
        )

        result = await _llm_json_object(llm, prompt, layer="L2-deviation")

        if budget:
            budget.record(provider, model, 3000, 800, "deviation_check")

        # rebalance_needed：有确定性判定时以 drift 为准；无明确目标(None)时退回 LLM
        if drift["rebalance_suggested"] is None:
            rebalance_needed = bool(result.get("rebalance_needed", False))
        else:
            rebalance_needed = bool(result.get("rebalance_needed", False)) or drift["rebalance_suggested"]

        yield _sse(
            "decision_done",
            layer="L2",
            action="deviation_check",
            rebalance_needed=rebalance_needed,
            deviation_pct=drift["max_abs_drift_pct"],
            underweight_gap=underweight_gap,
            commentary=result.get("commentary", ""),
            message=f"L2 偏离检查完成：{'需要调仓' if rebalance_needed else '在容忍范围内'}（最大偏离 {drift['max_abs_drift_pct']}%）",
        )

    except Exception as e:
        logger.exception("L2 偏离检查失败")
        yield _sse("decision_error", layer="L2", error=str(e))


# ─────────────────────────────────────────────────────────
# L2 缺口驱动器（P0-2）：扩张侧确定性补仓
# ─────────────────────────────────────────────────────────

# 单轮最多补的缺口比例（步长）。防一次性梭哈，同时让"分批建仓"这个现实旋钮可调。
# 缺口 30pct、步长 1/3 → 每轮最多把权益抬 10pct，约 3 轮收敛到下限带内。
_GAP_STEP_FRACTION = float(_os.getenv("BH_GAP_STEP_FRACTION", "0.3333"))


def _plan_gap_fills(account: dict, positions: list[dict], bounds: dict, core_holdings: list[dict],
                    market: str) -> list[dict]:
    """P0-2 核心（纯函数，无 DB/LLM）：算"缺口该补哪些票、各补多少钱"。

    三条安全旋钮：① 仅 equity_pct < equity_min 时触发（收敛后自然停）；② 单轮最多补
    _GAP_STEP_FRACTION 倍缺口（步长上限，防梭哈）；③ 每票不超过 L2 目标权重 + 只用可用现金。
    返回 [{"ticker","amount","action","target_weight_pct","current_weight_pct"}]，按缺口倒序。
    """
    from bottleneck_hunter.watchlist.constraint_validator import compute_underweight_gap

    gap = compute_underweight_gap(account, positions, bounds)
    if not gap.get("underweight"):
        return []  # 权益已回到下限之上 → 无缺口，驱动器不动作（收敛判据）

    equity = float(account.get("total_equity") or account.get("current_capital") or 0)
    cash = float(account.get("cash_balance") or 0)
    if equity <= 0 or cash <= 0:
        return []
    gap_dollars = gap["gap_pct"] / 100 * equity
    step_cap = gap_dollars * _GAP_STEP_FRACTION
    # 收尾（两个条件任一成立就一把补满整个缺口，真正**进入**下限带内，而不是无限逼近）：
    #   ① 步长本身已小到产不出一单（step_cap < 碎单阈值）；
    #   ② 按步长走完剩下的尾巴也小到产不出一单。
    # 少了这一句，步长在数学上只能无限逼近下限（残差 = 缺口×(1-θ)ⁿ，永不归零），叠加碎单过滤与
    # 比例分摊就**恒停在带外**——实测 5 只低配票时停在权益 34.80%，带下限是 40%。
    if step_cap < equity * 0.005 or gap_dollars - step_cap < equity * 0.005:
        step_cap = gap_dollars
    budget = min(gap["deployable_cash"], step_cap)

    # 持仓市值归一化比对（600519 与 600519.SS 同票），与 L3 的口径一致
    held = {normalize_ticker(p.get("ticker", ""), market) for p in positions if p.get("ticker")}

    # 低配核心票：L2 给了目标权重、但实际权重还没到 → 按差额倒序，先补最缺的
    cur_by_ticker: dict[str, float] = {}
    for p in positions:
        if p.get("ticker"):
            k = normalize_ticker(p["ticker"], market)
            cur_by_ticker[k] = cur_by_ticker.get(k, 0.0) + float(p.get("market_value") or 0)
    wants = []
    for s in core_holdings or []:
        tk = (s or {}).get("ticker", "")
        tw = float((s or {}).get("target_weight_pct") or 0)
        if not tk or tw <= 0:
            continue
        cur_w = cur_by_ticker.get(normalize_ticker(tk, market), 0.0) / equity * 100
        if tw - cur_w > 0.5:  # 差距 <0.5pct 视为已达标，不产零头单
            wants.append((tw - cur_w, tk, tw, cur_w))
    if not wants:
        return []
    wants.sort(reverse=True)

    # 碎单阈值：单票不足权益 0.5% 的补仓无意义（整手取整后多半为 0 股）。但**碎片判据必须看"总额"**：
    # 缺口小 → 按比例分摊后每票都 <0.5% → 五票全被丢 → 驱动器在带外**永久停手**。
    # 实测：5 票 × 12% 目标、θ=1/3 时恒停在权益 34.80%，而带下限是 40%——「天天跑、离下限差 5pct 就是进不去」。
    # 收尾（缺口已不足 0.5%×票数）时改为**只看总额**：总额够就补最缺的那几只，单票自然大额，不会出碎单。
    _frag = equity * 0.005
    # 收尾态：预算已不足给每只票都分出够一手的小额 → 放弃"按比例分摊"（那必然全员碎单被丢），
    # 改为按缺口倒序**递减填满**预算：最缺的先拿到整份额度，单票自然大额，不会出碎单。
    _collapse = budget < _frag * len(wants)

    total_short = sum(w[0] for w in wants)
    fills, left, budget_left = [], cash, budget
    for short_pct, tk, tw, cur_w in wants:
        if _collapse:
            amount = min(budget_left, (tw - cur_w) / 100 * equity, left)
        else:
            amount = min(budget * (short_pct / total_short), (tw - cur_w) / 100 * equity, left)
        if amount < _frag:
            continue
        fills.append({
            "ticker": tk,
            "amount": amount,
            "action": "add" if normalize_ticker(tk, market) in held else "buy",
            "target_weight_pct": tw,
            "current_weight_pct": cur_w,
        })
        left -= amount
        budget_left -= amount
    return fills


def _generate_gap_driven_plans(store, market: str, strategic: dict) -> list[dict]:
    """P0-2 缺口驱动器：把"配置不足"落成今日战术计划（返回新计划 id 列表）。

    动机：整条链的扩张力（建仓到目标）此前只有 L3 的逐票 opt-in，LLM 不给买信号时现金就永远趴着。
    本函数在 L3 之后补一层确定性扩张力——实际权益低于 regime 下限且有现金时，按 L2 的 core 目标权重
    对低配核心票产 add/buy 战术计划，再交回 L4 走既有的 sizing + 合规 + 投委会（本函数不下任何单，
    也不碰风控：现金硬顶、单票上限、熔断、投委会否决全在 L4 原样生效）。
    """
    from bottleneck_hunter.watchlist.constraint_validator import compute_underweight_gap
    from bottleneck_hunter.watchlist.regime_mapper import get_allocation_bounds

    macro = store.get_latest_macro_strategy()
    if not macro:
        return []
    # 上游陈旧：连"确定性补仓"也停（P1-2 口径——陈旧时扩张侧一致停摆，且在日志里记账）。
    # LLM 那层已因陈旧 return，这里补上驱动器不被剩下的代码路径绕过。
    for _label, _plan in (("L2 组合策略", strategic), ("L1 宏观策略", macro)):
        _age = _upstream_age_days(_plan.get("created_at", ""))
        if _age is None or _age > _STALE_UPSTREAM_DAYS:
            logger.warning("缺口驱动跳过：上游 %s 陈旧（%s 天），不在陈旧数据上补仓", _label, _age)
            return []
    mj = macro.get("result_json", {}) or {}
    bounds = get_allocation_bounds(mj.get("regime", "sideways"), mj.get("risk_appetite", "balanced"),
                                   mj.get("regime_confidence", 5))
    account = store.get_sim_account()
    positions = store.get_sim_positions(account.get("id"))
    core = (strategic.get("result_json", {}).get("stock_selection", {}) or {}).get("core_holdings", [])
    fills = _plan_gap_fills(account, positions, bounds, core, market)
    if not fills:
        return []
    gap = compute_underweight_gap(account, positions, bounds)  # fills 非空 ⇒ 必然 underweight
    equity = float(account.get("total_equity") or account.get("current_capital") or 0) or 1.0

    entry_map = {e["ticker"]: e["id"] for e in store.list_all()}
    plan_ids, deployed = [], 0.0
    for f in fills:
        tp = {
            "ticker": f["ticker"],
            "action": f["action"],
            "urgency": "this_week",
            "entry_plan": {
                "ideal_price": None,
                "split_strategy": f"缺口驱动：单轮至多补 {_GAP_STEP_FRACTION:.0%} 缺口，分批建仓",
            },
            "exit_plan": {},
            "catalyst_watch": [],
            "risk_assessment": {
                "confidence": 6,
                "key_risk": "缺口驱动为配置修正，非个股择时；买点由 L4 按现价定",
            },
            "reasoning": (
                f"L2 缺口驱动：实际权益 {gap['equity_pct']:.1f}% 低于 regime 下限 {gap['equity_min']}%，"
                f"该票目标权重 {f['target_weight_pct']:.1f}% / 现 {f['current_weight_pct']:.1f}%，"
                f"本轮拟补 {f['amount']:,.0f}"
            ),
            # 供 L4 的建仓侧门禁识别（P0-3）：这笔买是"补配置"，不是 LLM 的择时主张
            "gap_driven": True,
            "_planned_amount": round(f["amount"], 2),
            # P0-C 必需：L4 定股要判"离 L2 目标还有多远"，而 L4 是**另一次运行**、只读得到计划本身，
            # 拿不到本函数的局部变量。目标权重（绝对水位）必须持久化在计划里，否则 L4 只能拿
            # 本轮增量（_planned_amount）当水位——那正是 N-21：任何持仓额超过本轮小增量的票
            # 都被判"已达标"而恒返 0，缺口驱动永久冻结。
            "target_weight_pct": round(f["target_weight_pct"], 2),
        }
        tp["_provenance"] = _decision_provenance(["gap_driver"], [], market, "L3", [f["ticker"]])
        binding = save_stage_snapshot(store, "L3", {"gap_driven": True, "gap": gap, "fills": fills})
        plan_ids.append(
            store.create_tactical_plan(
                strategic_plan_id=strategic["id"], entry_id=entry_map.get(f["ticker"], ""),
                ticker=f["ticker"], plan_date=_today(), result_json=tp, **binding,
            )
        )
        deployed += f["amount"]
    logger.info(
        "L2 缺口驱动[%s/账户 %s]：权益 %.1f%% < 下限 %s%%，缺口 %.1fpct，"
        "本轮补 %d 只共 %.0f（步长上限 %.0f），权益将升至约 %.1f%%",
        market, account.get("id"), gap["equity_pct"], gap["equity_min"], gap["gap_pct"],
        len(plan_ids), deployed, gap["deployable_cash"] * _GAP_STEP_FRACTION,
        gap["equity_pct"] + deployed / equity * 100,
    )
    return plan_ids


# ─────────────────────────────────────────────────────────
# L3: 战术计划
# ─────────────────────────────────────────────────────────


async def run_tactical_plans(
    store: WatchlistStore,
    budget: BudgetTracker | None = None,
    market: str = "us_stock",
) -> AsyncGenerator[dict, None]:
    """生成 L3 战术计划 — 每只目标股票的买卖时机"""
    store = store.for_market(market)
    yield _sse("decision_start", layer="L3", action="generate", message="开始生成 L3 战术计划...")

    strategic = store.get_latest_strategic_plan()
    if not strategic:
        yield _sse("decision_error", layer="L3", error="无 L2 组合策略，无法生成战术计划")
        return

    macro = store.get_latest_macro_strategy()
    if not macro:
        yield _sse("decision_error", layer="L3", error="无 L1 宏观策略")
        return

    # (B) 上游新鲜度闸：strategic/macro 超 _STALE_UPSTREAM_DAYS 天＝周度刷新漏跑 → 阻断，
    #     勿据陈旧上游产今日战术（今日 L1/L2 刷新失败时 get_latest_* 会静默取到旧计划）。
    for _layer, _label, _plan in (("L2", "组合策略", strategic), ("L1", "宏观策略", macro)):
        _age = _upstream_age_days(_plan.get("created_at", ""))
        if _age is None or _age > _STALE_UPSTREAM_DAYS:
            _why = (
                "创建时间无法解析"
                if _age is None
                else f"已 {_age:.0f} 天未刷新（超 {_STALE_UPSTREAM_DAYS} 天周度阈值）"
            )
            yield _sse(
                "decision_error",
                layer="L3",
                error=f"上游 {_layer} {_label}{_why}，跳过 L3 避免据陈旧上游产今日战术；请先刷新 L1/L2",
            )
            return

    llm, provider, model = get_llm_for_position(position="L3_tactical")
    if not llm:
        yield _sse("decision_error", layer="L3", error="无可用 LLM")
        return

    if budget and not budget.can_spend(estimated_tokens=8000):
        yield _sse("decision_error", layer="L3", error="预算不足")
        return

    try:
        watchlist_signals = _collect_watchlist_signals(store, market)
        market_ctx = _get_market_context_text([market])  # 单市场：仅本市场规则，避免混入他市场交易规则
        catalysts = store.get_upcoming_catalysts(days=30)
        catalyst_by_ticker = {}
        for c in catalysts:
            catalyst_by_ticker.setdefault(c["ticker"], []).append(
                {
                    "title": c.get("title", ""),
                    "type": c.get("catalyst_type", ""),
                    "expected_date": c.get("expected_date", ""),
                    "impact_level": c.get("impact_level", "medium"),
                    "confidence": c.get("confidence", 5),
                }
            )

        # P1.1 已判定催化剂(realized/failed/partial) → 买卖信号
        judged = store.get_recently_judged_catalysts(days=7)
        outcome_by_ticker = {}
        catalyst_outcome_tickers = set()
        for c in judged:
            tk = c.get("ticker", "")
            if not tk:
                continue
            outcome_by_ticker.setdefault(tk, []).append(
                {
                    "title": c.get("title", ""),
                    "outcome": c.get("outcome", ""),
                    "impact": c.get("outcome_impact", 0),
                    "judged_at": (c.get("judged_at", "") or "")[:10],
                }
            )
            catalyst_outcome_tickers.add(tk)

        stock_data = []
        entries = store.list_all()
        entries = [e for e in entries if normalize_market(e.get("market")) == normalize_market(market)]

        # 19C: 基于 L2 stock_selection 过滤，确保 L3 只为 L2 选定的标的生成战术计划
        strategic_json = strategic.get("result_json", {})
        stock_selection = strategic_json.get("stock_selection", {})
        core_holdings = stock_selection.get("core_holdings", [])
        tactical_holdings = stock_selection.get("tactical_holdings", [])
        core_tickers = {s.get("ticker", "") for s in core_holdings if s.get("ticker")}
        tactical_tickers = {s.get("ticker", "") for s in tactical_holdings if s.get("ticker")}
        watch_tickers = set(stock_selection.get("watchlist_only", []))
        selected_tickers = core_tickers | tactical_tickers | watch_tickers

        # P1.1 强制纳入：持仓中且催化剂已落空的标的(即使不在 L2 选股)，确保能生成止损/减仓战术
        held_tickers = {
            p["ticker"] for p in store.get_sim_positions(store.get_sim_account().get("id")) if p.get("shares", 0) > 0
        }
        forced = catalyst_outcome_tickers & held_tickers

        # B3: 论点失效(invalidated/weakened)且在持仓 → 强制纳入 L3 并注入告警（倾向 reduce/sell/收紧止损）
        thesis_alerts = {}
        for e in store.list_all():
            if normalize_market(e.get("market")) != normalize_market(market) or e["ticker"] not in held_tickers:
                continue
            for th in store.get_theses_for_entry(e["id"], active_only=True):
                st = th.get("status", "")
                if st in ("invalidated", "weakened"):
                    thesis_alerts.setdefault(e["ticker"], []).append(
                        {
                            "thesis": th.get("thesis_title", ""),
                            "status": st,
                            "conviction": th.get("conviction", ""),
                        }
                    )
        thesis_forced = set(thesis_alerts.keys())
        forced = forced | thesis_forced
        if forced:
            selected_tickers = selected_tickers | forced

        if selected_tickers:
            entries = [e for e in entries if e["ticker"] in selected_tickers]
            if not entries:
                logger.warning("L2 选股 %s 未匹配到观察池标的，降级为全量处理", selected_tickers)
                entries = [e for e in store.list_all() if normalize_market(e.get("market")) == normalize_market(market)]
                yield _sse(
                    "decision_info",
                    layer="L3",
                    degraded=True,
                    message=f"⚠️ L2 选股 {sorted(selected_tickers)} 未匹配到本市场观察池标的，"
                    "L3 降级为全观察池处理（结果非 L2 精选，请知悉）",
                )
        else:
            # (B) 空 L2 选股不再静默全量：如实标注降级信号，让用户知道本轮 L3 未受 L2 约束
            yield _sse(
                "decision_info",
                layer="L3",
                degraded=True,
                message="⚠️ L2 未选出任何标的，L3 降级为全观察池处理（结果非 L2 精选，请知悉）",
            )

        for entry in entries:
            ticker = entry["ticker"]
            snap = store.get_latest_snapshot(ticker)
            signal = next((s for s in watchlist_signals if s["ticker"] == ticker), {})

            l2_role = "core" if ticker in core_tickers else "tactical" if ticker in tactical_tickers else "watch"
            l2_target_weight = 0
            for s in core_holdings + tactical_holdings:
                if s.get("ticker") == ticker:
                    l2_target_weight = s.get("target_weight_pct", 0)
                    break

            stock_data.append(
                {
                    "ticker": ticker,
                    "company_name": entry.get("company_name", ticker),
                    "sector": entry.get("sector", ""),
                    "tier": entry.get("tier", "track"),
                    "l2_role": l2_role,
                    "l2_target_weight": l2_target_weight,
                    "signal": signal.get("signal", "neutral"),
                    "confidence": signal.get("confidence", 5),
                    "price": snap.get("close") if snap else None,
                    "change_pct": snap.get("change_pct") if snap else None,
                    "rsi_14": snap.get("rsi_14") if snap else None,
                    "sma_50": snap.get("sma_50") if snap else None,
                    "volume": snap.get("volume") if snap else None,
                    "chip_signals": _chip_context(store, ticker),  # B5: 机构持仓/评级/目标价
                }
            )

        prompt_template = _load_prompt("decision_tactical")
        macro_text = (
            macro.get("market_summary", "") or json.dumps(macro.get("result_json", {}), ensure_ascii=False)[:500]
        )

        recent_map = _recent_executed_by_ticker(store)
        recent_trades_text = _format_recent_trades(recent_map)

        prompt = (
            prompt_template.replace("{market_context}", market_ctx)
            .replace("{macro_summary}", macro_text)
            .replace("{strategic_plan}", json.dumps(strategic.get("result_json", {}), ensure_ascii=False))
            .replace("{stock_data}", json.dumps(stock_data, ensure_ascii=False))
            .replace("{catalyst_timeline}", json.dumps(catalyst_by_ticker, ensure_ascii=False))
            .replace(
                "{catalyst_outcomes}",
                json.dumps(outcome_by_ticker, ensure_ascii=False) if outcome_by_ticker else "暂无已判定催化剂",
            )
            .replace(
                "{thesis_alerts}",
                json.dumps(thesis_alerts, ensure_ascii=False) if thesis_alerts else "暂无失效投资论点",
            )
            .replace("{recent_trades}", recent_trades_text)
            .replace("{user_persona}", format_persona_for_prompt(store))
        )

        yield _sse("decision_progress", layer="L3", step="llm_reasoning", message="L3 LLM 推理中...")

        _l3_tk = [e.get("ticker", "") for e in entries] + list(held_tickers)
        allowed_tk = _decision_allowed_tickers(store, market, *_l3_tk)
        input_prompts = []
        result, _fetch_log = await _run_data_negotiation(
            llm, prompt, market=market, layer="3", allowed_tickers=allowed_tk, input_prompts=input_prompts
        )
        if _fetch_log:
            yield _sse(
                "decision_progress", layer="L3", step="data_fetch_round", message=f"L3 数据补充 {len(_fetch_log)} 条"
            )

        if budget:
            budget.record(provider, model, 8000, 3000, "tactical_plans")

        _normalize_result_tickers(result)  # 归一战术计划 ticker，与观察池/L2 对齐
        tactical_plans = result.get("tactical_plans", [])

        entry_map = {e["ticker"]: e["id"] for e in entries}
        plan_ids = []
        # 去重：仅在确有新计划时，先清理今日同市场已有的 active 战术计划再写入，
        # 避免「日常决策 / 全量刷新 / 定时任务 / 重复点击」多次运行累积重复；
        # LLM 返回空时不清空当日，保留既有计划。
        if tactical_plans:
            binding = save_stage_snapshot(
                store,
                "L3",
                {
                    "prompts": input_prompts,
                    "strategic": strategic,
                    "macro": macro,
                    "entries": entries,
                    "held_tickers": sorted(held_tickers),
                    "recent_trades": recent_map,
                },
            )
            cleared = store.delete_tactical_plans_by_date(_today())
            if cleared:
                logger.info("L3 重新生成：清理今日旧战术计划 %d 条", cleared)
        seen_tickers: set[str] = set()
        for tp in tactical_plans:
            ticker = tp.get("ticker", "")
            if not ticker or ticker in seen_tickers:
                continue  # 跳过空标的 / 同批次内 LLM 重复返回的标的
            seen_tickers.add(ticker)
            entry_id = entry_map.get(ticker, "")
            tp["_provenance"] = _decision_provenance(["decision_tactical"], [(provider, model)], market, "L3", [ticker])
            plan_id = store.create_tactical_plan(
                strategic_plan_id=strategic["id"],
                entry_id=entry_id,
                ticker=ticker,
                plan_date=_today(),
                result_json=tp,
                **binding,
            )
            plan_ids.append(plan_id)

        # L2 缺口驱动器（P0-2）：在 LLM 的逐票择时之外补一层确定性的"把钱配回去"。
        # 放在清理/写入之后——驱动器自己的计划不能被 delete_tactical_plans_by_date 清掉。
        try:
            gap_ids = _generate_gap_driven_plans(store, market, strategic)
        except Exception as e:  # noqa: BLE001 —— 缺口驱动同样是增益层，失败不该中断当日决策
            # 实探过的崩法（都是 LLM 输出的常见畸变，非理论风险）：
            #   · 目标权重写成字符串 "12%" → float() 抛 ValueError
            #   · core_holdings 不是 list[dict]（LLM 偶尔直接给 dict）→ .get 抛 AttributeError
            #   · 账户/持仓的数值字段是字符串（导入侧留的脏值）→ 除/加 抛 TypeError
            # 没有这层兜底，一次畸形的 L2 输出会把**当日全部决策**（含 L1-L3 与投委会）一起带崩。
            logger.warning("缺口驱动失败，本轮无缺口驱动计划: %s", e)
            gap_ids = []

        # 机会/信念驱动器（P0-4）：与缺口驱动互补的另一股扩张力——缺口驱动止于 L2 目标权重（修正），
        # 本驱动器可越过目标权重去抓高信念机会（阿尔法）。产出的计划一律带 mandate_exception 标记，
        # 必须经投委会/人工确认，绝不自动执行（见 run_daily_decision 的自动执行步骤）。
        try:
            opp_ids = _generate_opportunity_driven_plans(store, market, strategic)
        except Exception as e:  # noqa: BLE001 —— 机会驱动是增益层，失败不该中断当日决策
            logger.warning("机会驱动失败，本轮无机会驱动计划: %s", e)
            opp_ids = []

        notes = []
        if gap_ids:
            notes.append(f"缺口驱动补仓 {len(gap_ids)} 只")
        if opp_ids:
            notes.append(f"机会驱动 {len(opp_ids)} 只（待确认）")
        gap_note = f"（含{'、'.join(notes)}）" if notes else ""
        yield _sse(
            "decision_done",
            layer="L3",
            plan_count=len(plan_ids) + len(gap_ids) + len(opp_ids),
            gap_driven_count=len(gap_ids),
            opportunity_driven_count=len(opp_ids),
            priority_ranking=result.get("priority_ranking", []),
            message=f"L3 战术计划已生成：{len(plan_ids)} 只股票{gap_note}",
        )

    except Exception as e:
        logger.exception("L3 战术计划生成失败")
        yield _sse("decision_error", layer="L3", error=str(e))


# ─────────────────────────────────────────────────────────
# L4: 执行方案
# ─────────────────────────────────────────────────────────

# 执行去重：5天冷却窗口
EXECUTION_COOLDOWN_DAYS = 5
_BUY_FAMILY = {"buy", "add", "accumulate", "open"}
_SELL_FAMILY = {"sell", "reduce", "trim", "close"}


def _recent_executed_by_ticker(store, days=EXECUTION_COOLDOWN_DAYS) -> dict[str, list[dict]]:
    """返回 {ticker: [{side, shares, date}]}，仅含近 days 天已执行的 sim_trades。"""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    _dc_id = store.get_sim_account().get("id")
    trades = store.get_sim_trades(limit=200, account_id=_dc_id)
    out = {}
    for t in trades:
        ticker = t.get("ticker")
        if not ticker:
            continue  # 坏数据（ticker 为空）跳过，避免 KeyError 中断整批生成
        created = t.get("created_at", "") or ""
        try:
            created_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
            if created_dt.tzinfo is None:
                created_dt = created_dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue  # 时间戳缺失/非法，无法判定冷却窗口，跳过
        if created_dt < cutoff:
            continue
        out.setdefault(ticker, []).append(
            {
                "side": t.get("side", ""),
                "shares": t.get("shares", 0),
                "date": created[:10],
            }
        )
    return out


def _is_recent_duplicate(action, ticker, recent_map) -> bool:
    """该 ticker 的同向操作族近期是否已执行过。"""
    if action in _BUY_FAMILY:
        fam = _BUY_FAMILY
    elif action in _SELL_FAMILY:
        fam = _SELL_FAMILY
    else:
        # 未知动作无法归入买/卖族，去重失效 → 记日志告警而非静默放行
        logger.warning("去重：未知 action=%r (ticker=%s)，跳过冷却检查", action, ticker)
        return False
    return any(t["side"] in fam for t in recent_map.get(ticker, []))


# execute_trade 只认这四种真实成交动作（trade_executor.py:128/132）；其余不可成交。
_EXECUTABLE_ACTIONS = ("buy", "add", "sell", "reduce")
# L4 最小建仓权重：低于此权重的 buy/add 会被代码顶到此仓位（B 兜底），
# 根除「$1M 账户买 5 股 MU=0.44%」这类 LLM 拍脑袋的零头仓。
_MIN_BUILD_WEIGHT_PCT = 3.0


def _conviction_score(expected_return_pct: float, catalyst_days_left: int | None,
                      catalyst_impact: str = "", composite_score: float = 0.0) -> float:
    """P0-4 信念分（纯函数，0~1）：机会驱动器的唯一门槛依据，全部来自现成数据、零新增 LLM。

    三个来源加权：① 场景期望上行（`create_scenario_valuation` 已按概率加权存好的 expected_return_pct）
    ② 催化剂临近度×影响力（越近越强，>30 天衰减到 0）③ 观察池综合评分（弱信号，权重最低）。
    阈值与档位的对应关系写在 _OPPORTUNITY_TIERS：分数越高，允许的越线缓冲越大（到硬顶为止）。

    ponytail: 权重是拍的先验（期望上行主导、催化剂次之），不是拟合出来的——回测校准前别当最优解，
    但三个输入方向都对（上行越高/催化剂越近越强/评分越高 → 越敢买），先跑起来再调。
    """
    # ① 期望上行：0% 得 0 分，+40% 及以上满分
    up = max(0.0, min(1.0, float(expected_return_pct or 0) / 40.0))
    # ② 催化剂：临近度 × 影响力；刚发生(days_left<0)少量衰减，不当作新催化
    prox = 0.0
    if catalyst_days_left is not None:
        d = catalyst_days_left
        prox = max(0.0, min(1.0, 1.0 - d / 30.0)) if d >= 0 else max(0.0, 1.0 - abs(d) / 30.0) * 0.5
    imp = {"critical": 1.0, "high": 0.75, "medium": 0.45, "low": 0.2}.get((catalyst_impact or "").lower(), 0.45)
    event = prox * imp
    # ③ 综合评分：0~100 → 0~1
    comp = max(0.0, min(1.0, float(composite_score or 0) / 100.0))
    return round(up * 0.5 + event * 0.3 + comp * 0.2, 4)


# 机会驱动器档位：信念分下限 → (本轮可补到的目标权重上限, 单轮单票加仓金额上限占权益比)
# 越线缓冲**只对单票有效**：账户级 equity_max 由 validate_against_regime 硬拦，这里不碰。
_OPPORTUNITY_TIERS = (
    (0.75, 8.0, 0.04),  # 顶档：可越过 L2 目标权重至 8%，单轮最多吃进 4% 权益
    (0.6, 6.0, 0.02),   # 中档
    (0.45, 4.0, 0.01),  # 低档：只是"顺手把好球补到 4%"
)
# 单轮机会驱动器占总权益的上限（防中等信念的票叠成一次性满仓）
_OPPORTUNITY_ROUND_CAP = float(_os.getenv("BH_OPPORTUNITY_ROUND_CAP", "0.06"))


def _opportunity_tier(score: float) -> tuple[float, float] | None:
    """信念分 → (目标权重上限%, 单轮金额上限占权益比)。低于最低档 → None（不产意图）。"""
    for floor, target_pct, step_pct in _OPPORTUNITY_TIERS:
        if score >= floor:
            return target_pct, step_pct
    return None


def _oplog_mandate_exception(store, ticker: str, market: str, l2_w: float, post_w: float,
                             ep: dict) -> None:
    """N-29：机会驱动越线落一条 oplog（`result="exception"`）。留痕失败绝不影响决策。"""
    try:
        uid = getattr(store, "_user_id", "") or ""
        if not uid:
            return
        from bottleneck_hunter.web.oplog import record_operation

        record_operation(
            uid, "机会驱动越过 L2 目标授权",
            detail=f"{market}：{ticker} 目标 {l2_w:.1f}% → 执行后 {post_w:.1f}%，"
                   f"{ep.get('shares')} 股 ≈ {float(ep.get('estimated_amount') or 0):.0f}，须人工确认"[:300],
            result="exception", market=market,
            meta={"ticker": ticker, "l2_target_pct": round(l2_w, 2), "post_weight_pct": round(post_w, 2),
                  "shares": ep.get("shares"), "amount": ep.get("estimated_amount")},
        )
    except Exception as e:  # noqa: BLE001 —— 留痕失败不得影响决策
        logger.debug("越线留痕失败 %s: %s", ticker, e)


def _plan_opportunity_fills(account: dict, positions: list[dict], valuations: dict[str, dict],
                            catalysts: dict[str, dict], composites: dict[str, float],
                            market: str) -> list[dict]:
    """P0-4 核心（纯函数，无 DB/LLM）：信念足够高的票 → 追加买入意图（可越 L2 目标权重、不越硬顶）。

    与缺口驱动器（P0-2）的分工：那边是均值回归（补到目标就停），这边是阿尔法（敢超过目标）。
    三条边界：① 只对已有估值数据的票动作（没有信念依据就不出手）；② 越线只发生在"L2 目标权重"
    这一维（档位上限），单票硬上限(单票 max_single_pct)、账户 equity_max、现金、熔断全部照旧——
    它们由 L4 既有校验与投委会执行，本函数只产出意图、不下任何单；③ 单轮总额受
    _OPPORTUNITY_ROUND_CAP 与可用现金双封顶。
    返回 [{"ticker","amount","action","target_weight_pct","current_weight_pct","score","tier_pct","reason"}]。
    """
    equity = float(account.get("total_equity") or account.get("current_capital") or 0)
    cash = float(account.get("cash_balance") or 0)
    if equity <= 0 or cash <= 0:
        return []

    cur_by_ticker: dict[str, float] = {}
    for p in positions:
        if p.get("ticker"):
            k = normalize_ticker(p["ticker"], market)
            cur_by_ticker[k] = cur_by_ticker.get(k, 0.0) + float(p.get("market_value") or 0)

    budget = equity * _OPPORTUNITY_ROUND_CAP
    # N-26：先算全部候选 → **按信念排序** → 再分预算。原实现是"边遍历边扣预算、遍历完才排序"，
    # 于是 `get_valuation_map()` 的字典序（用户不可控）决定了谁先吃到单轮预算：实测两个中档票
    # （各 2%）排在顶档票前面就能把 6% 预算吃光，顶档票只剩 2000（本应 4000）。末尾那次 sort
    # 只是把**已定金额**重排，钱早就分完了 —— "信念最高的先补"是排序后的假象。
    cands: list[dict] = []
    for tk_raw, v in (valuations or {}).items():
        tk = normalize_ticker(tk_raw, market)
        # 与持仓/归一化的键对齐后再取值（估值表单存 600519 而持仓存 600519.SS 时也要命中）
        cur_val = cur_by_ticker.get(tk, 0.0)
        cat = (catalysts or {}).get(tk)
        score = _conviction_score(
            v.get("expected_return_pct") or 0,
            cat.get("days_left") if cat else None,
            (cat or {}).get("impact_level", ""),
            composites.get(tk, 0.0),
        )
        tier = _opportunity_tier(score)
        if not tier:
            continue
        tier_target_pct, step_pct = tier
        cur_w = cur_val / equity * 100
        if cur_w >= tier_target_pct:
            continue  # 已达档位允许的目标权重 → 不再追高（同一信念不无限加）
        cands.append({
            "ticker": tk_raw,
            "action": "add" if cur_val > 0 else "buy",
            "target_weight_pct": tier_target_pct,
            "current_weight_pct": round(cur_w, 2),
            "score": score,
            "tier_pct": tier_target_pct,
            "reason": (cat or {}).get("title", ""),
            # 两个与预算无关的静态上限，预存下来供分配阶段取用
            "_room": tier_target_pct / 100 * equity - cur_val,
            "_step": step_pct * equity,
        })
    cands.sort(key=lambda c: c["score"], reverse=True)  # 信念最高的先分预算（这里是真分配顺序）
    out: list[dict] = []
    for c in cands:
        amount = min(budget, c.pop("_room"), c.pop("_step"), cash)
        if amount < equity * 0.005:  # 不足权益 0.5% 的碎单无意义
            continue
        c["amount"] = amount
        out.append(c)
        budget -= amount
        cash -= amount
    return out


def _generate_opportunity_driven_plans(store, market: str, strategic: dict) -> list[dict]:
    """P0-4 机会/信念驱动器：把"高信念机会"落成今日战术计划（返回新计划 id 列表）。

    与缺口驱动器的区别：缺口驱动止于 L2 目标权重（修正力），本驱动器可**超过**目标权重
    （阿尔法），但硬上限（单票 max_single_pct、权益 equity_max、现金、熔断）一个都不碰——
    越线只体现在"单票敢配到档位上限"，超出部分由 L4 既有校验与投委会处置。
    所有意图都带 `mandate_exception=True`：不是绕过风控，而是把"超出目标授权"显式申报，
    强制人工确认（L4 自动执行会跳过带此标记的计划）。
    """
    macro = store.get_latest_macro_strategy()
    if not macro:
        return []
    for _label, _plan in (("L2 组合策略", strategic), ("L1 宏观策略", macro)):
        _age = _upstream_age_days(_plan.get("created_at", ""))
        if _age is None or _age > _STALE_UPSTREAM_DAYS:
            logger.warning("机会驱动跳过：上游 %s 陈旧（%s 天），不在陈旧数据上追高", _label, _age)
            return []
    account = store.get_sim_account()
    if not account:
        return []
    positions = store.get_sim_positions(account.get("id"))
    valuations = store.get_valuation_map()
    if not valuations:
        return []  # 无估值数据 → 无信念依据，不出手
    try:
        # limit 放大到 200：get_recent_catalysts 的默认 8 是给论坛发帖挑热点用的，
        # 这里要覆盖全市场估值票，被截断会让高信念票白白丢掉催化剂分。
        catalysts = {}
        for c in store.get_recent_catalysts(days_ahead=30, days_back=14, limit=200):
            tk = normalize_ticker(c.get("ticker", ""), market)
            if tk and tk not in catalysts:  # 同票多条取最近一条（已按活动时间倒序）
                catalysts[tk] = {**c, "days_left": _days_until_date(c.get("expected_date"))}
    except Exception as e:  # noqa: BLE001 —— 催化剂取数失败不该拖垮驱动器
        logger.debug("机会驱动催化剂取数失败: %s", e)
        catalysts = {}
    composites = {normalize_ticker(e.get("ticker", ""), market): float(e.get("composite_score") or 0)
                  for e in store.list_all() if e.get("ticker")}
    fills = _plan_opportunity_fills(account, positions, valuations, catalysts, composites, market)
    if not fills:
        return []

    equity = float(account.get("total_equity") or account.get("current_capital") or 0) or 1.0
    entry_map = {e["ticker"]: e["id"] for e in store.list_all()}
    # 快照按轮存一次（与缺口驱动器的逐票存法不同）：机会驱动的输入是全市场估值表，逐票重复存同一份
    # payload 只是把同一张估值表写 N 遍。
    binding = save_stage_snapshot(store, "L3", {"opportunity_driven": True, "fills": fills})
    plan_ids = []
    for f in fills:
        tp = {
            "ticker": f["ticker"],
            "action": f["action"],
            "urgency": "this_week",
            "entry_plan": {
                "ideal_price": None,
                "split_strategy": f"机会驱动：信念分 {f['score']:.2f}（顶档 {f['tier_pct']:.0f}%），单轮分批",
            },
            "exit_plan": {},
            "catalyst_watch": [f["reason"]] if f["reason"] else [],
            "risk_assessment": {
                "confidence": int(round(f["score"] * 10)),
                "key_risk": "机会驱动可越过 L2 目标权重（止于档位上限与单票硬顶），越线必人工确认",
            },
            "reasoning": (
                f"机会驱动：信念分 {f['score']:.2f}，当前权重 {f['current_weight_pct']:.1f}% "
                f"→ 档位上限 {f['tier_pct']:.0f}%，本轮拟买 {f['amount']:,.0f}"
                + (f"；催化：{f['reason']}" if f["reason"] else "")
            ),
            # 供 L4 建仓侧门禁识别（复用 P0-3 通道）+ 强制上会标记
            "opportunity_driven": True,
            "mandate_exception": True,
            "_planned_amount": round(f["amount"], 2),
            # 同缺口驱动：档位上限（绝对水位）必须持久化。L4 拿它判"实际定出的股数是否真越过
            # L2 目标"，以及给 _gap_fill_shares 的收敛判据。**绝不能**改用 _planned_amount
            # 当水位——那是本轮增量（首轮 = 档位−现值），拿增量当水位会阻断后续几轮的追加。
            "target_weight_pct": round(float(f["tier_pct"]), 2),
        }
        tp["_provenance"] = _decision_provenance(["opportunity_driver"], [], market, "L3", [f["ticker"]])
        plan_ids.append(
            store.create_tactical_plan(
                strategic_plan_id=strategic["id"], entry_id=entry_map.get(f["ticker"], ""),
                ticker=f["ticker"], plan_date=_today(), result_json=tp, **binding,
            )
        )
    logger.info(
        "机会驱动[%s/账户 %s]：%d 只信念达标（准入 %d 只估值），本轮拟买 %.0f（单轮上限 %.0f），最高信念 %.2f",
        market, account.get("id"), len(plan_ids), len(valuations),
        sum(f["amount"] for f in fills), equity * _OPPORTUNITY_ROUND_CAP,
        max(f["score"] for f in fills),
    )
    return plan_ids


def _gap_driven_plan_details(actionable: list[dict], market: str) -> dict[str, dict]:
    """P0-C：驱动计划的**全字段**视图 → {ticker: {"amount": 封顶金额, "target_pct": 计划目标权重%}}。

    与 `_gap_driven_plans` 同源同判据，只是多带回目标权重。定股要判"离目标还剩多少空间"，
    而目标权重此刻在 L4 之外**根本不存在**（它来自 L2 的 core_holdings / 机会档位表，两者都不是
    L4 的输入）——所以要靠 L3 把它写进计划、L4 再读回来。两个 map 都留：`_gap_driven_plans`
    是「这票是否由驱动器管」的粗判（豁免门禁、判定上会都只需 ticker 集合），本函数供定股取水位。
    合并成一个大 dict 会让前者的阅读者以为自己在读金额。
    """
    out: dict[str, dict] = {}
    for tp in actionable or []:
        tk = tp.get("ticker", "")
        rj = tp.get("result_json") or {}
        if not tk or not (rj.get("gap_driven") or rj.get("opportunity_driven")):
            continue
        if normalize_market(tp.get("market")) != normalize_market(market):
            continue
        try:
            amt = max(float(rj.get("_planned_amount") or 0), 0.0)
        except (TypeError, ValueError):
            amt = 0.0
        try:
            tpct = max(float(rj.get("target_weight_pct") or 0), 0.0)
        except (TypeError, ValueError):
            tpct = 0.0
        # N-32：同票可能同时挂着缺口计划与机会计划（两张库行）。此时取**更大**的金额与更高的
        # 水位，与调用方 `driver_plans` 的取大合并**同口径**——否则 L4 从 `_det["amount"]` 拿到的
        # 是"列表里靠后的那张"的金额，而"这票是否被驱动"用的是取大后的集合，两处对不上。
        # 水位也取高，理由不是"取大总没错"，而是两个水位是**两种语义的天花板**：缺口驱动的是
        # L2 承诺（如 9%），机会驱动的是档位上限（如 6%）。取小的后果是具体的：这票若已持 7%，
        # 取 6% 会让 `_gap_fill_shares` 的 `existing_value >= target_value` 立刻判"已达标"→
        # 缺口驱动距 L2 那 2% 被机会驱动的小水位掐死，正是 N-32 换了个方向复发。取高则两方
        # 都留有余地，代价只是**可能**多一次人工确认（opportunity 见下取或）。
        _prev = out.get(tk) or {}
        out[tk] = {
            "amount": max(amt, _prev.get("amount", 0.0)),
            "target_pct": max(tpct, _prev.get("target_pct", 0.0)),
            # 任一侧来自机会驱动，这票就得强制上会 —— 取或，不由"后写的那张"决定。
            # 偏向保守：多问一次人，好过越线单被当成普通单静默成交。
            "opportunity": bool(rj.get("opportunity_driven")) or bool(_prev.get("opportunity")),
        }
    return out


def _gap_driven_plans(actionable: list[dict], market: str) -> dict[str, float]:
    """P0-3/P0-4：挑出本轮来自确定性驱动器（缺口 P0-2 / 机会 P0-4）的战术计划 → {ticker: 封顶金额}。

    只认 L3 自己写的 gap_driven / opportunity_driven 标记，不凭「持仓低配」或「估值上行」推断——
    否则任何 LLM 想加仓的票都会被豁免建仓侧门禁，豁免面失控。金额缺失/非正时记 0，调用方按 0 处理。
    market 必须传本市场：`normalize_ticker` 只对 6 位数字码做 A股归一，字母 ticker 原样返回，
    故跨市场的字面同号（如 A股码 "YYYY" vs 美股 "YYYY"）能被识别并排除——排除即回落到普通门禁，
    而回落的后果只是「照旧跳过」，不会错放一笔本市场的买入。
    """
    out: dict[str, float] = {}
    for tp in actionable or []:
        tk = tp.get("ticker", "")
        rj = tp.get("result_json") or {}
        if not tk or not (rj.get("gap_driven") or rj.get("opportunity_driven")):
            continue
        if normalize_market(tp.get("market")) != normalize_market(market):
            continue
        try:
            out[tk] = max(float(rj.get("_planned_amount") or 0), 0.0)
        except (TypeError, ValueError):
            out[tk] = 0.0
    return out


def _opportunity_driven_plans(actionable: list[dict], market: str) -> dict[str, float]:
    """P0-4：挑出本轮来自机会/信念驱动器的战术计划 → {ticker: 计划金额}。

    与 _gap_driven_plans 平行但语义不同，刻意不复用同一份映射：缺口驱动是「补到 L2 目标权重」
    （天然有界、不必每次上会），机会驱动是「越过目标授权」（必须每次人工确认）。若混成一个 dict，
    调用方就再也分不出哪笔需要强制上会——越线的可审计性会丢。
    """
    out: dict[str, float] = {}
    for tp in actionable or []:
        tk = tp.get("ticker", "")
        rj = tp.get("result_json") or {}
        if not tk or not rj.get("opportunity_driven"):
            continue
        if normalize_market(tp.get("market")) != normalize_market(market):
            continue
        try:
            out[tk] = max(float(rj.get("_planned_amount") or 0), 0.0)
        except (TypeError, ValueError):
            out[tk] = 0.0
    return out


def _allocation_inconsistency(result: dict, tolerance_pct: float = 5.0) -> dict | None:
    """P0-E（N-23）：检查 L2 顶层 target_allocation 与其逐票明细是否自洽。

    逐票明细（core + tactical 的 target_weight_pct 合计）与顶层 `equity_pct` 偏差 > tolerance_pct
    时返回说明，否则 None。

    **量纲别搞错**：`equity_pct / cash_pct / hedge_pct` 是**三分法**、三者相加 = 100
    （`chain/prompts/decision_strategic.md` 的示例就是 70/25/5）。所以"明细该等于多少"的答案是
    `equity_pct`（权益那一条腿），**不是** 明细+现金+对冲 再去比 equity_pct——那样每份健康的计划
    都会被判成差 30pct。等价写法是 `明细 + cash + hedge` 对 100，但直接比明细对 equity_pct 更聚焦：
    它问的正是"这个权益承诺，票装得下吗"。`hedge_pct` 是独立腿，不属于权益。

    为什么要这条：顶层 `equity_pct` **没有任何下游消费者**（缺口驱动器/偏离报告/前端对照条全部只读
    `core_holdings[].target_weight_pct`）。于是它写错时不会有人报错，但会误导读者、并与 L1 下限一起
    制造一个**够不着的缺口**（生产实测：顶层 51 vs 明细合计 33，L1 下限 40 → 缺口恒 7pct 永不收敛）。
    把"两层是否对得上"变成一条可见断言：谁写错谁被点名，而不是被默默忽略。

    只报警不改数——自动改数等于替 LLM 编仓位，会抹掉"模型自相矛盾"这个信号本身。
    """
    if not isinstance(result, dict):
        return None
    ta = result.get("target_allocation")
    if not isinstance(ta, dict):
        return None
    eq = ta.get("equity_pct")
    if not isinstance(eq, (int, float)):
        return None
    def _pct(v) -> float:
        # LLM 偶发把权重写成 "12%" / None → 这种票按 0 计（少算会被 diff 放大而报警，方向安全）
        try:
            return float(v or 0)
        except (TypeError, ValueError):
            return 0.0

    sel = result.get("stock_selection") or {}
    detail, n = 0.0, 0
    for bucket in ("core_holdings", "tactical_holdings"):
        for h in sel.get(bucket, []) or []:
            detail += _pct((h or {}).get("target_weight_pct"))
            n += 1
    if n == 0:
        return None  # 没选票（空仓/仅观察）→ 无可比对，不报
    _cash_hedge = sum(_pct(ta.get(k)) for k in ("cash_pct", "hedge_pct"))
    diff = round(float(eq) - detail, 2)
    if abs(diff) <= tolerance_pct:
        return None
    return {
        "equity_pct": float(eq),
        "detail_sum_pct": round(detail, 2),
        "cash_hedge_pct": round(_cash_hedge, 2),
        "diff_pct": diff,
        "tolerance_pct": tolerance_pct,
        "detail": (
            f"顶层 equity_pct={float(eq):.1f}%，但逐票明细合计只有 {detail:.1f}%"
            f"（{n} 票，另现金 {round(_cash_hedge, 1)}% 含对冲），相差 {diff:+.1f}pct，"
            f"超出容差 {tolerance_pct:.0f}pct"
        ),
    }


def _l2_target_weights(strategic: dict, market: str) -> dict[str, float]:
    """{归一 ticker: L2 目标权重%}——判定「是否真的越过了目标授权」的基准线。"""
    core = ((strategic or {}).get("result_json", {}) or {}).get("stock_selection", {}) or {}
    out: dict[str, float] = {}
    for s in core.get("core_holdings", []) or []:
        tk = (s or {}).get("ticker", "")
        tw = float((s or {}).get("target_weight_pct") or 0)
        if tk and tw > 0:
            out[normalize_ticker(tk, market)] = tw
    return out


def _gap_fill_shares(planned_amount: float, price: float, equity: float,
                     existing_value: float, cap_pct: float, market: str,
                     target_value: float) -> int:
    """P0-3：缺口驱动补仓的确定性定股。金额取 min(本轮增量, 距目标剩余空间, 单股上限剩余空间)。

    **三个数必须分清，混作一谈就是 N-21（组合永久卡在 19.30%、天天跑却纹丝不动）**：
      · `planned_amount` —— L3 给的**本轮增量**（缺口×步长按票分摊），随缺口缩小而缩水；
      · `target_value`   —— 该票 **L2 目标持仓额**（目标权重% × 权益），是**绝对水位**、不缩水；
      · `existing_value` —— 当前持仓额。

    收敛判据只能看绝对水位：`existing_value >= target_value` → 补到目标，停。
    旧实现拿 `planned_amount` 当水位（守卫与 planned_room **两处**都减 existing_value），
    于是**任何持仓额已超过本轮小增量的票都被判「已达标」而恒返 0**。缺口驱动因此冻结在
    权益 19.30%；机会驱动同因同病，只是它更晚发作（持仓过半档位上限时才恒返 0）。

    与 LLM 路径的 target_shares_for_buy 的差别是**不设 3% 建仓地板**：地板是防 LLM 拍零头仓的，
    这里的小额是刻意的「分批补到 L2 目标权重」。

    `target_value` 无默认值、**必填**：这三个数混淆过一次，签名就该逼每个调用方表态。
    """
    if price <= 0 or equity <= 0:
        return 0
    if existing_value >= target_value:
        return 0  # 已达 L2 目标 → 收敛即停（防「每天刷单」）

    from bottleneck_hunter.watchlist.position_sizing import _round_lot

    # 三个上限各按自己的口径算，每个都已是「还能动多少」，互不重复扣减：
    round_room = planned_amount                       # 本轮增量（L3 已按持仓扣过一次，见 _plan_gap_fills）
    target_room = target_value - existing_value       # 距 L2 目标还有多少
    cap_room = cap_pct / 100 * equity - existing_value if cap_pct else target_room
    amount = round(min(round_room, target_room, cap_room), 2)
    if amount <= 0:
        return 0
    # 直接走 _round_lot 而非 target_shares_for_buy：本路径刻意不要它的 3% 建仓地板（分批补的小额
    # 正是要放的）与波动率天花板（缺口驱动的金额已由 L3 封顶）。金额先落整——浮点残差会把「刚好
    # 补满」算成 5e-11 的尾巴，被 _round_lot 的 int() 截成 0 股而整单作废。
    return _round_lot(amount / price, market)


def _is_executable_plan(ep: dict) -> bool:
    """待确认区只收可成交指令：真实买卖动作 + 正股数。

    LLM 有时输出 hold 或漏填 shares（缺省=0），这类计划过 constraint_validator 会 fail-open
    （缺股数即早返 valid），却在确认执行时被 execute_trade 挡下（缺关键字段）、UI 显示“--股”。
    生成期就地剔除，不落库污染待确认队列。
    """
    try:
        sh = int(float(ep.get("shares") or 0))
    except (TypeError, ValueError):
        sh = 0
    return ep.get("action") in _EXECUTABLE_ACTIONS and sh > 0


def _format_recent_trades(recent_map: dict[str, list[dict]]) -> str:
    """把近期已执行交易 map 格式化为 prompt 文本（L3/L4 复用）。"""
    if not recent_map:
        return "暂无近期已执行交易"
    lines = []
    for tk, trades in recent_map.items():
        for tr in trades:
            lines.append(f"{tk} {tr['side']} {tr['shares']}股 ({tr['date']})")
    return "\n".join(lines) if lines else "暂无近期已执行交易"


def _format_constraints_for_prompt(
    constraints: dict,
    alloc_bounds: dict,
    account: dict,
    positions: list[dict],
    cash_balance: float,
    market: str = "us_stock",
) -> str:
    """把【当前真实生效】的动态约束 + 组合现状余量格式化成 prompt 文本。

    关键：LLM 必须看到 regime 收紧后的实际上限（如熊市单股 5%），否则会按
    prompt 写死的宽松值（20%）生成计划，随后被 L4 硬校验大批拦截，用户无操作可执行。
    单笔上限与校验器同源(_effective_single_trade_cap)，币种符号按 market，避免对 A股仍标 $。
    """
    from bottleneck_hunter.watchlist.constraint_validator import _ccy_symbol, _effective_single_trade_cap

    equity = account.get("total_equity") or account.get("current_capital", 100000) or 100000
    sym = _ccy_symbol(market)
    single_cap = _effective_single_trade_cap(constraints, equity)
    lines = [
        f"- 可用现金：{sym}{cash_balance:,.0f}（占总资产 {cash_balance / equity * 100:.1f}%）",
        f"- 单股持仓上限：总资产 {constraints.get('max_single_position_pct', 25):.0f}%",
        f"- 单板块上限：总资产 {constraints.get('max_sector_pct', 40):.0f}%",
        f"- 最低现金保留：总资产 {constraints.get('min_cash_pct', 15):.0f}%",
        f"- 单笔金额上限：{sym}{single_cap:,.0f}",
        f"- 单日交易规模上限：总资产 {constraints.get('max_daily_turnover_pct', 30):.0f}%",
    ]
    if constraints.get("max_portfolio_beta"):
        lines.append(f"- 组合 beta 上限：{constraints['max_portfolio_beta']:.2f}")
    if alloc_bounds.get("equity_max") is not None:
        lines.append(f"- 权益总仓位上限：总资产 {alloc_bounds['equity_max']:.0f}%")
    # 组合现状：已用板块占比，帮 LLM 直接算出还能加多少
    if positions and equity > 0:
        by_sector: dict[str, float] = {}
        for p in positions:
            sec = p.get("sector", "") or "未分类"
            by_sector[sec] = by_sector.get(sec, 0) + (p.get("market_value", 0) or 0)
        hot = sorted(by_sector.items(), key=lambda kv: kv[1], reverse=True)[:3]
        if hot:
            lines.append("- 当前板块占比：" + "，".join(f"{s} {v / equity * 100:.1f}%" for s, v in hot))
    return "\n".join(lines)


def _repair_execution_plan(llm, ep: dict, violations: list[str], account: dict, constraints: dict) -> dict | None:
    """P0.2 LLM 自修正：带违规详情重新生成单个执行计划。

    返回修正后的 ep dict；若 LLM 判定不可行或调用失败，返回 None。
    """
    try:
        template = _load_prompt("decision_execution_repair")
        prompt = (
            template.replace("{original_plan}", json.dumps(ep, ensure_ascii=False))
            .replace("{violations}", "\n".join(f"- {v}" for v in violations))
            .replace(
                "{account_status}",
                json.dumps(
                    {
                        "total_equity": account.get("total_equity", 100000),
                        "cash_balance": account.get("cash_balance", 0),
                    },
                    ensure_ascii=False,
                ),
            )
            .replace("{constraints}", json.dumps(constraints, ensure_ascii=False))
        )
        response = llm.invoke(prompt).content
        fixed = extract_json_object(response)
        if not fixed or not fixed.get("feasible", False):
            return None
        # 合并修正字段回原计划
        ep = dict(ep)
        if fixed.get("shares") is not None:
            ep["shares"] = fixed["shares"]
        if fixed.get("estimated_price") is not None:
            ep["estimated_price"] = fixed["estimated_price"]
            ep["target_price"] = fixed["estimated_price"]
        if fixed.get("execution_method"):
            ep["execution_method"] = fixed["execution_method"]
        ep["estimated_amount"] = (ep.get("shares", 0) or 0) * (
            fixed.get("estimated_price") or ep.get("estimated_price", 0) or 0
        )
        ep["auto_repaired"] = True
        ep["repair_note"] = fixed.get("adjustment_note", "")
        return ep
    except Exception as e:
        logger.warning("执行计划自修正失败: %s", e)
        return None


async def run_execution_plans(
    store: WatchlistStore,
    budget: BudgetTracker | None = None,
    market: str = "us_stock",
) -> AsyncGenerator[dict, None]:
    """生成 L4 执行方案 — 可执行操作序列"""
    store = store.for_market(market)
    yield _sse("decision_start", layer="L4", action="generate", message="开始生成 L4 执行方案...")

    tactical_plans = store.get_tactical_plans_by_date(_today())
    if not tactical_plans:
        yield _sse("decision_info", layer="L4", message="今日无 L3 战术计划，跳过 L4")
        return

    actionable = [tp for tp in tactical_plans if tp.get("action") not in ("hold", "wait_for_pullback")]
    if not actionable:
        yield _sse("decision_done", layer="L4", message="L3 计划全部为持有，无需生成执行方案")
        return

    llm, provider, model = get_llm_for_position(position="L4_execution")
    if not llm:
        yield _sse("decision_error", layer="L4", error="无可用 LLM")
        return

    if budget and not budget.can_spend(estimated_tokens=5000):
        yield _sse("decision_error", layer="L4", error="预算不足")
        return

    try:
        market_ctx = _get_market_context_text([market])  # 单市场：仅本市场规则，避免混入他市场交易规则
        account = store.get_sim_account()
        positions = store.get_sim_positions(account.get("id"))
        # P2.5 账户级熔断：单日巨亏/深度回撤时，本轮只允许减仓，禁止新开/加仓
        from bottleneck_hunter.watchlist.constraint_validator import check_account_circuit_breaker

        _cb = check_account_circuit_breaker(account)
        if not _cb.valid:
            logger.warning("账户级熔断触发：%s", "; ".join(_cb.violations))
        feedback = store.get_rejection_patterns(limit=10)
        preferences = store.get_preferences()

        cash_balance = account.get("cash_balance", 100000)

        prompt_template = _load_prompt("decision_execution")
        tactical_json = json.dumps([tp.get("result_json", tp) for tp in actionable], ensure_ascii=False)
        account_json = json.dumps(
            {
                "total_equity": account.get("total_equity", 100000),
                "cash_balance": cash_balance,
                "positions": [
                    {
                        "ticker": p["ticker"],
                        "shares": p.get("shares", 0),
                        "avg_cost": p.get("avg_cost", 0),
                        "market_value": p.get("market_value", 0),
                        "weight_pct": p.get("weight_pct", 0),
                        "unrealized_pnl": p.get("unrealized_pnl", 0),
                    }
                    for p in positions
                ],
            },
            ensure_ascii=False,
        )
        feedback_text = (
            json.dumps(
                [{"ticker": f.get("ticker", ""), "reason": f.get("reason", "")} for f in feedback[:5]],
                ensure_ascii=False,
            )
            if feedback
            else "暂无历史拒绝记录"
        )
        pref_text = (
            json.dumps({p["key"]: p["value"] for p in preferences}, ensure_ascii=False)
            if preferences
            else "暂无用户偏好"
        )

        tickers_in_play = [tp.get("ticker", "") for tp in actionable if tp.get("ticker")]
        experience_text = "暂无历史经验"
        applied_card_ids = []
        if tickers_in_play:
            all_cards = []
            for tk in tickers_in_play:
                entry = next((e for e in store.list_all() if e["ticker"] == tk), {})
                sector = entry.get("sector", "")
                cards = store.get_relevant_cards(tk, sector, limit=3)
                for c in cards:
                    if c["id"] not in [ac["id"] for ac in all_cards]:
                        all_cards.append(c)
            if all_cards:
                experience_text = json.dumps(
                    [
                        {
                            "title": c["title"],
                            "content": c["content"],
                            "scope": c["scope"],
                            "confidence": c["confidence"],
                        }
                        for c in all_cards[:8]
                    ],
                    ensure_ascii=False,
                )
                applied_card_ids = [c["id"] for c in all_cards[:8]]

        layer_perf = store.get_layer_performance_summary()
        layer_perf_text = json.dumps(layer_perf, ensure_ascii=False) if layer_perf else "暂无分层绩效数据"

        # 近期已执行交易：同一函数内 prompt 构建与下方去重循环复用同一份 recent_map
        recent_map = _recent_executed_by_ticker(store)
        recent_trades_text = _format_recent_trades(recent_map)

        # P0.6 动态约束：按 L1 风险偏好 + regime 收紧的约束集。
        # 先算好再注入 prompt，让 LLM 按【真实生效】的约束生成，而非 prompt 里写死的 20%/40%
        # （否则 LLM 以为单股上限 20%，实际熊市校验按 5% 拦，生成的计划几乎必被拦→用户无操作可执行）。
        from bottleneck_hunter.watchlist.constraint_validator import (
            get_constraints_for_appetite,
            max_compliant_shares,
            validate_against_regime,
            validate_execution_plan,
            validate_portfolio_beta,
        )
        from bottleneck_hunter.watchlist.position_sizing import (
            PositionSizer,
            target_shares_for_buy,
        )

        macro = store.get_latest_macro_strategy()
        macro_rj = (macro or {}).get("result_json", {}) if macro else {}
        risk_appetite = (macro or {}).get("risk_appetite", "")
        regime = macro_rj.get("regime", "sideways")
        confidence = macro_rj.get("regime_confidence", 5)
        alloc_bounds = get_allocation_bounds(regime, risk_appetite, confidence)
        # 用户个人「单一持仓上限」是硬约束：与 L1 市场档位取更严者(min)，与 L2 同源(get_user_single_cap)，
        # 确保 L4 硬校验也尊重个人设置——否则用户设 12% 上限，L4 仍按市场档位 20% 校验，个人约束形同虚设。
        _user_cap = get_user_single_cap(store)
        if _user_cap:
            alloc_bounds["max_single_pct"] = min(alloc_bounds.get("max_single_pct", 100), _user_cap)
        constraints = get_constraints_for_appetite(risk_appetite)
        if alloc_bounds.get("max_single_pct"):
            constraints["max_single_position_pct"] = min(
                constraints.get("max_single_position_pct", 100), alloc_bounds["max_single_pct"]
            )
        if alloc_bounds.get("beta_limit"):
            constraints["max_portfolio_beta"] = min(
                constraints.get("max_portfolio_beta", 10), alloc_bounds["beta_limit"]
            )
        constraints_text = _format_constraints_for_prompt(
            constraints, alloc_bounds, account, positions, cash_balance, market
        )

        prompt = (
            prompt_template.replace("{market_context}", market_ctx)
            .replace("{tactical_plans}", tactical_json)
            .replace("{account_status}", account_json)
            .replace("{available_cash}", f"{cash_balance:,.0f}")
            .replace("{constraints}", constraints_text)
            .replace("{trade_feedback}", feedback_text)
            .replace("{recent_trades}", recent_trades_text)
            .replace("{user_preferences}", pref_text)
            .replace("{user_persona}", format_persona_for_prompt(store))
            .replace("{experience_cards}", experience_text)
            .replace("{layer_performance}", layer_perf_text)
        )

        yield _sse("decision_progress", layer="L4", step="llm_reasoning", message="L4 LLM 推理中...")

        _pos_tk = [p.get("ticker", "") for p in (positions or [])]
        allowed_tk = _decision_allowed_tickers(store, market, *tickers_in_play, *_pos_tk)
        input_prompts = []
        result, _fetch_log = await _run_data_negotiation(
            llm, prompt, market=market, layer="4", allowed_tickers=allowed_tk, input_prompts=input_prompts
        )
        if _fetch_log:
            yield _sse(
                "decision_progress", layer="L4", step="data_fetch_round", message=f"L4 数据补充 {len(_fetch_log)} 条"
            )

        if budget:
            budget.record(provider, model, 5000, 2000, "execution_plans")

        _normalize_result_tickers(result)  # 归一执行计划 ticker，与观察池/持仓对齐
        exec_plans = result.get("execution_plans", [])

        entry_map = {
            e["ticker"]: e["id"]
            for e in store.list_all()
            if normalize_market(e.get("market")) == normalize_market(market)
        }
        sector_map = {
            e["ticker"]: e.get("sector", "")
            for e in store.list_all()
            if normalize_market(e.get("market")) == normalize_market(market)
        }
        tactical_map = {tp["ticker"]: tp["id"] for tp in actionable}
        created_ids = []
        skipped = 0
        blocked = 0
        repaired = 0

        # 「已有计划」= pending + 挂单中，避免对同一标的重复生成执行计划。
        # （原先是同一表达式写两遍、`|=` 追加在第二遍上——P1-I/N-25 的驱动直通要在这之后合成，
        #   故把两处并到一处，语义不变，只是不再让后来的读者以为其中一处是笔误。）
        _pending_set = {ep["ticker"] for ep in store.get_pending_executions() if ep.get("ticker")}
        _resting_set = {ep["ticker"] for ep in store.get_resting_executions() if ep.get("ticker")}
        existing_tickers = _pending_set | _resting_set
        beta_map = {}
        for tk in set(list(entry_map.keys()) + [p["ticker"] for p in positions]):
            try:
                prof = store.get_company_profile(tk)
                b = (prof or {}).get("raw", {}).get("beta")
                if b is not None:
                    beta_map[tk] = float(b)
            except Exception:
                pass

        # P0-3：来自 P0-2 缺口驱动器的补仓意图 → 豁免建仓侧三道门（见下）
        gap_plans = _gap_driven_plans(actionable, market)
        # P0-4：来自机会驱动器的越线意图，各自独立（越线须强制上会，见下方 mandate_exception 标记）
        opp_plans = _opportunity_driven_plans(actionable, market)
        # 两类驱动都是「有确定金额上限的系统补仓」，不是 LLM 的择时主张 → 共用同一套建仓侧豁免；
        # 单独一张 dict 只为保留「谁需要上会」的分辨力。
        # N-32：同票被两个驱动器同时选中时**不得静默覆盖**。两个驱动器是各自对着全额现金独立
        # 算钱的（缺口驱动看"离 L2 目标还差多少"，机会驱动看"档位上限还差多少"），覆盖等于让后
        # 合并的一方把另一方的预算凭空蒸发掉——账面上少一笔钱、日志上没一个字。
        # 取**更大**的金额：两条都是"本轮最多投这么多"的上限语义，取大即取谁更想买；取小会把
        # 缺口驱动在本轮彻底挤掉（机会驱动的档位金额通常更小）。水位由下方 L4 按 `opportunity`
        # 分别取，与本处的金额合并互不干扰。
        driver_plans = dict(gap_plans)
        for _tk, _opp_amt in opp_plans.items():
            _gap_amt = driver_plans.get(_tk)
            if _gap_amt is None:
                driver_plans[_tk] = _opp_amt
            elif _opp_amt > _gap_amt:
                logger.warning("N-32：%s 同时被缺口/机会驱动选中，本币预算取大 %.2f（机会）而非 %.2f（缺口）",
                               _tk, _opp_amt, _gap_amt)
                driver_plans[_tk] = _opp_amt
            else:
                logger.warning("N-32：%s 同时被缺口/机会驱动选中，本币预算取大 %.2f（缺口）而非 %.2f（机会）",
                               _tk, _gap_amt, _opp_amt)
        # 判定「是否真的越过 L2 目标授权」的基准线（L1/L2 各自的最新计划，单行查询）
        l2_targets = _l2_target_weights(store.get_latest_strategic_plan() or {}, market)
        # P0-C 续：驱动计划的**目标权重**（绝对水位）+ 机会票的水位取小（见下方注释）
        _driver_details = _gap_driven_plan_details(actionable, market)

        # ── P1-I（N-25）确定性直通：驱动意图无条件并入执行计划表 ──
        # 病史：`driver_plans` 合并完就交回 LLM 的 `execution_plans` 去筛，能不能落地取决于 LLM
        # 这一轮**恰好选中同一票**。也就是「确定性驱动器」的确定性止于算金额那一步，后面全是运气；
        # 实测缺口驱动 5 票里只有 2 票被 LLM 同选，其余**无声消失**（计划仍在库里，所以没人看得见）。
        # 这里把 driver 票按计划自带的 action 合成一条执行计划塞进去：金额/水位/冷却/熔断/投委会
        # 全部照走既有通道，只是不再需要 LLM 点头。`mandate_exception` 也照抄，故机会驱动的越线
        # 依旧强制人工确认（auto_execute 会把它从自动执行集里摘出去）。
        # 注：上面的 `existing_tickers`（pending ∪ 挂单）**挡不住** driver 票——下方放行判据
        # `ticker not in driver_plans` 是方向盲的，只要在 driver_plans 里就不看 pending。所以同一张
        # 驱动票每轮重跑都会再合成一条，pending 里逐轮叠卡（实测第 1/2/3 轮 = 1/2/3 张）。此处两道
        # 防守：① 挂单中的票（`_resting_set`）跳过，不与挂单重复下单；② **pending 已存在的票也跳过**
        # ——pending 卡还没被确认/作废时再叠一张，用户一次全选就是重复下单，而 `auto_execute_pending`
        # 逐条成交、不按 ticker 去重，会直接把单轮步长上限击穿。
        # ② 的判据只用 pending（不含挂单），因为挂单已由 ① 单独管；两者都在 `existing_tickers` 里，
        # 这里分开判是为了让"跳过"的日志原因可区分。
        _synth = 0
        _exec_tk = {ep.get("ticker", "") for ep in exec_plans}
        for _tp in actionable or []:
            _tk = _tp.get("ticker", "")
            _rj = _tp.get("result_json") or {}
            if not _tk or _tk in _exec_tk or _tk not in driver_plans:
                continue
            if normalize_market(_tp.get("market")) != normalize_market(market):
                continue
            # 与 L4 同口径的现价（target_price 是上一轮的意图价，会漂移；estimated_price 才是现价）
            _snap = store.get_latest_snapshot(_tk) or {}
            _px = _snap.get("close")
            if not _px:
                logger.info("驱动直通跳过 %s：无最新收盘价，本轮无法定股", _tk)
                continue
            _ep = {
                "ticker": _tk,
                "action": _tp.get("action") or _rj.get("action") or "buy",
                "estimated_price": float(_px),
                "reasoning": _rj.get("reasoning", ""),
                "market": market,
                "sector": sector_map.get(_tk, ""),
            }
            if _tk in _resting_set:
                logger.info("驱动直通跳过 %s：已有挂单在途，不重复下单", _tk)
                continue
            if _tk in _pending_set:
                logger.info("驱动直通跳过 %s：已有待确认执行计划在途，不重复下单", _tk)
                continue
            for _k in ("mandate_exception", "mandate_exception_note"):
                if _rj.get(_k):
                    _ep[_k] = _rj[_k]
            exec_plans.append(_ep)
            _exec_tk.add(_tk)
            _synth += 1
        if _synth:
            logger.info("驱动直通：合成 %d 条执行计划（不经 LLM 取舍）", _synth)

        batch_tickers = set()
        pending_writes = []
        repair_inputs = []
        # recent_map 已在上方 prompt 构建时计算，此处直接复用（同批生成期间无新成交）

        # ── P0-D（N-22）事前卡口：本批共享的「当天已用额度」影子账本 ──
        # 两件事此前都缺：① 生成期循环体从不改写 account/positions，每笔都按「买入前满额」校验；
        # ② 日换手是累加量，逐笔比总额度在数学上守不住（实测 10 笔各 5% 权益 → 单日 50% vs 上限 30%）。
        # 这里把「当日已真实成交额」当基线，每放行一笔买入就把它加进去，后续计划只按剩余额度缩量/放行。
        # 纯本地变量、不写库（落库仍由下方 pending_writes 一次性做），不引入半执行状态。
        _turnover_used = 0.0
        try:
            _turnover_used = float(store.daily_turnover_amount(account.get("id", ""), market) or 0.0)
        except Exception as e:  # noqa: BLE001 —— 取不到就按 0（放行，不误拦真实委托）
            logger.debug("当日已成交额读取失败，日额度按 0 计: %s", e)
        _cash_left = float(account.get("cash_balance") or 0.0)
        if _turnover_used > 0:
            logger.info("当日已成交额 %s: %.0f（本批计划仅可使用剩余日额度）", market, _turnover_used)

        risk_snapshots = {}
        for ticker in {ep.get("ticker", "") for ep in exec_plans if ep.get("action") in ("buy", "add")}:
            try:
                risk_snapshots[ticker] = store.get_snapshots(ticker, days=60)
            except Exception:
                risk_snapshots[ticker] = []
        stage_inputs = {
            "prompts": input_prompts,
            "tactical_plans": actionable,
            "account": account,
            "positions": positions,
            "macro": macro,
            "constraints": constraints,
            "allocation_bounds": alloc_bounds,
            "beta_map": beta_map,
            "risk_snapshots": risk_snapshots,
            "entry_map": entry_map,
            "sector_map": sector_map,
            "existing_tickers": sorted(existing_tickers),
            "recent_trades": recent_map,
            "circuit_breaker": {"valid": _cb.valid, "violations": _cb.violations},
        }

        for ep in exec_plans:
            ticker = ep.get("ticker", "")
            if not ticker:
                continue
            # 已有 pending/挂单 → 不重复下单。**驱动票不再豁免这道门**。
            # 原来写的是 `and ticker not in driver_plans`（6fd48fb「建仓侧三道门」的豁免之一），
            # 理由是"缺口驱动的票本来就该补仓"。但那道豁免方向盲：它不看这票是不是**已经有一张
            # 没确认的 pending 卡**，于是每轮重跑都会再叠一张 —— 实测第 1/2/3 轮 = 1/2/3 张卡，
            # 而 `auto_execute_pending` 逐条成交、不按 ticker 去重，用户一次全选就把单轮步长上限
            # 击穿（pending 老化还有 14 天，叠的卡不会自己消失）。
            # 单独一条 pending 的存在本身就是"这票还没处理完"的信号，与它是不是驱动票无关；
            # 驱动该不该补仓由 L3 的方向和 L4 的水位决定，不该由"要不要无视在途单"决定。
            # 注意：6fd48fb「建仓侧三道门」里对驱动票的豁免原本有**两**处，另两处现在的状态是：
            #   · 5 日同向冷却 —— 豁免**已收回**（本批 P1-I/N-24，见下方 `_is_recent_duplicate` 处）；
            #   · 建仓下限（`_MIN_BUILD_WEIGHT_PCT`）—— 豁免**仍在**，但它是**结构性**的：驱动票走
            #     下方 `_gap_fill_shares` 独立分支，压根不进 `target_shares_for_buy`，故这里没有
            #     对应的判据可删。它的替代约束是"达标即停 + 步长上限 + 现金"。
            if ticker in existing_tickers:
                logger.info("跳过已有 pending/挂单执行计划的 %s", ticker)
                skipped += 1
                continue
            if ticker in batch_tickers:
                logger.info("跳过本批次重复的 %s", ticker)
                skipped += 1
                continue
            # P1-I（N-24）：缺口驱动**不再**豁免 5 日同向冷却。
            # 原豁免的理由是「配平到 L2 目标权重、非择时，防刷单靠步长上限 + 达标即停」——但这道
            # 刹车有两个洞：① 它曾经是坏的（N-21，已由 P0-C 修）；② 它的方向与风险相反：价格下跌
            # → 市值下降 → 离目标更远 → **反而更容易通过**，于是跌得最狠的核心票被反复加仓，
            # 形成越跌越买的回路。P0-C 修好之后这个回路才真正打开（以前是被"恒返 0"误挡住的）。
            # 冷却按**已成交**的 sim_trades 计，是这套确定性的"按权重补仓"逻辑上唯一与价格无关的
            # 频率刹车。代价是补仓变慢（同一票 5 日内只补一次），但这远好过越跌越买。
            # 机会驱动同样不豁免（它没有"缺口归零"这种天然收敛判据，冷却期是它的主要刹车）。
            if _is_recent_duplicate(ep.get("action", ""), ticker, recent_map):
                logger.info("跳过近期已执行同向操作的 %s (%s)", ticker, ep.get("action"))
                skipped += 1
                continue
            # P2.5 熔断期只放行减仓/清仓：新开/加仓计划直接拦截进"已拦截"区
            if not _cb.valid and ep.get("action") in ("buy", "add"):
                pending_writes.append(
                    (
                        True,
                        dict(
                            tactical_plan_id=tactical_map.get(ticker, ""),
                            entry_id=entry_map.get(ticker, ""),
                            ticker=ticker,
                            result_json=ep,
                            reason="账户级熔断：" + "; ".join(_cb.violations),
                            marker=store.BLOCK_MARKER_SYSTEM,
                        ),
                    )
                )
                blocked += 1
                logger.info("熔断拦截加仓计划 %s (%s)", ticker, ep.get("action"))
                continue
            batch_tickers.add(ticker)
            entry_id = entry_map.get(ticker, "")
            tactical_id = tactical_map.get(ticker, "")
            ep["applied_card_ids"] = applied_card_ids
            ep.setdefault("market", market)
            ep.setdefault("sector", sector_map.get(ticker, ""))

            # ── A+B 确定性定股：用「目标权重×波动率风险×下限兜底」重算 shares，替代 LLM 直觉 ──
            # 病根：L4 直接采信 LLM 拍脑袋的 shares（$1M 账户买 5 股 MU=0.44%）；position_sizing 空有其名从未接入。
            # 仅对 buy/add 生效（sell/reduce 由持仓量决定，不动）。下限 3% 顶零头、波动率设风险天花板、
            # 单股上限沿用已收紧的 constraints、A股按 100 手取整；
            # 之后的 validate/max_compliant_shares 仍向下钳制现金/板块/beta。
            if ep.get("action") in ("buy", "add"):
                _price = float(ep.get("target_price") or ep.get("estimated_price") or 0) or 0.0
                _equity = float(account.get("total_equity") or account.get("cash_balance") or 0) or 0.0
                _existing = next(
                    (float(p.get("market_value") or 0) for p in positions if p.get("ticker") == ticker), 0.0
                )
                # LLM 意图权重：优先其自报 after_weight_pct，否则由它提的 shares 反推（=它真实想要的仓位）
                _impact = ep.get("position_impact") or {}
                try:
                    _llm_w = float(_impact.get("after_weight_pct") or 0)
                except (TypeError, ValueError):
                    _llm_w = 0.0
                if _llm_w <= 0 and _price > 0 and _equity > 0:
                    try:
                        _llm_w = float(ep.get("shares") or 0) * _price / _equity * 100
                    except (TypeError, ValueError):
                        _llm_w = 0.0
                # 个股年化波动率（近60日快照）；算不出→0，helper 内退化为「只按下限/上限」不设风险帽
                _vol = 0.0
                try:
                    _snaps = risk_snapshots.get(ticker, [])
                    _closes = [float(s["close"]) for s in reversed(_snaps or []) if s.get("close") not in (None, "")]
                    _rets = [_closes[i] / _closes[i - 1] - 1.0 for i in range(1, len(_closes)) if _closes[i - 1] > 0]
                    _vol = PositionSizer.compute_stock_volatility(_rets)
                except Exception:
                    _vol = 0.0
                if ticker in driver_plans:
                    # P0-3/P0-4：驱动式补仓。金额由 L3 的 _planned_amount 封顶（已含单票剩余空间 +
                    # 单轮步长/预算 + 现金多重约束）。单股上限在 helper 内再钳一次。
                    _cap_w = float(constraints.get("max_single_position_pct") or 0)
                    _det = _driver_details.get(ticker) or {}
                    _gap_amt = float(_det.get("amount") or driver_plans[ticker] or 0.0)
                    # P0-C（N-21）：收敛判据改用**绝对水位**（离目标还有多远），不是本轮计划额
                    # （那是增量）——见 `_gap_fill_shares` 的病史。水位取 L3 计划自带的目标权重，
                    # **两条驱动分开取**，因为它们的"目标"本来就不是一回事：
                    #   · 缺口驱动：目标 == L2 承诺，故再与**当前** L2 目标取小 —— 挡住陈旧计划里
                    #     残留的旧水位（L2 降配后计划未重算）被当成现价水位去追。
                    #   · 机会驱动：档位上限（8%）**生来就是要越过 L2 承诺**（这是 P0-4 的全部意义）。
                    #     对它取小＝把水位压回 L2 承诺＝越线再也发生不了，等于把这个驱动器关掉。
                    #     它的边界另有其人且都还在：档位上限、单票硬上限(_cap_room)、现金、
                    #     单轮总额、账户 equity_max 硬拦、以及越线后的 mandate_exception 强制人工确认。
                    _plan_w = float(_det.get("target_pct") or 0.0)
                    if _det.get("opportunity"):
                        _tgt_w = _plan_w
                    else:
                        _l2_cap_w = float(l2_targets.get(normalize_ticker(ticker, market), 0.0) or 0.0)
                        _tgt_w = (min(_plan_w, _l2_cap_w) if (_plan_w > 0 and _l2_cap_w > 0)
                                  else (_plan_w or _l2_cap_w))
                    # 取不到水位时**不能拿 0 充数**：`_gap_fill_shares` 的守卫是
                    # `existing_value >= target_value`，0 会被读成「已达目标」→ 该票永久定不到股，
                    # 正是 N-21 的形态换了个入口。退化为「只受单票硬上限约束」；连硬上限都没有时
                    # 才真正不设水位（inf），由 `_planned_amount` 单独封顶。
                    if _tgt_w > 0:
                        _target_value = _tgt_w / 100 * _equity
                    elif _cap_w > 0:
                        _target_value = _cap_w / 100 * _equity
                    else:
                        _target_value = float("inf")
                    _sized = _gap_fill_shares(_gap_amt, _price, _equity, _existing, _cap_w, market,
                                              _target_value)
                    if _sized <= 0:
                        logger.info("跳过定不到仓位的驱动补仓 %s（无价/整手/已达标）", ticker)
                        skipped += 1
                        continue
                    ep["shares"] = _sized
                    ep["estimated_amount"] = round(_sized * _price, 2)
                    ep["_auto_sized"] = True
                    logger.info(
                        "驱动补仓 %s：%d 股 ≈ %.0f（计划 %.0f，现有持仓 %.0f）",
                        ticker, _sized, _sized * _price, _gap_amt, _existing,
                    )
                else:
                    _sized = target_shares_for_buy(
                        price=_price,
                        account_equity=_equity,
                        existing_value=_existing,
                        llm_weight_pct=_llm_w,
                        floor_pct=_MIN_BUILD_WEIGHT_PCT,
                        cap_pct=float(constraints.get("max_single_position_pct") or 0),
                        stock_vol=_vol,
                        market=market,
                    )
                    if _sized <= 0:
                        # 够不到 3% 下限（加仓已达标 / A股不足一手 / 无价）→ 不开零头仓
                        logger.info(
                            "跳过无法定到 %.0f%% 下限仓位的 %s（已达标/整手/无价）", _MIN_BUILD_WEIGHT_PCT, ticker
                        )
                        skipped += 1
                        continue
                    if _sized != int(float(ep.get("shares") or 0)):
                        ep["shares"] = _sized
                        ep["estimated_amount"] = round(_sized * _price, 2)
                        ep["_auto_sized"] = True

            # ── P0.1 前置约束校验 + P2.1 组合 beta 校验 + B1 regime 仓位校验 ──
            # P0-D：校验口径里账户现金用【影子账本余额】——循环体本身从不改写 account，
            # 每笔都按「买入前满额现金」过闸，同轮 5 笔大额就都能过（现金下限要到成交期才拦得住，
            # 代价是一批注定失败的待确认单）。影子余额只影响现金下限这一项判据。
            _acct_view = {**account, "cash_balance": _cash_left}

            def _full_validate(plan_ep, _view=_acct_view, _used=_turnover_used):
                # 两个循环变量显式绑定为默认参数：闭包捕获的是【本笔】的影子余额与已用额度，
                # 而不是循环变量在整轮结束后的最终值（晚绑定会让每笔都按同一口径过闸）。
                vr = validate_execution_plan(plan_ep, _view, positions, constraints,
                                             daily_turnover_used=_used)
                br = validate_portfolio_beta(plan_ep, _view, positions, beta_map, constraints)
                if not br.valid:
                    vr.violations.extend(br.violations)
                    vr.valid = False
                # B1: 启用原死代码 validate_against_regime，让 regime 收紧的 equity 上限在 L4 真正生效
                rr = validate_against_regime(plan_ep, _view, positions, alloc_bounds)
                if not rr.valid:
                    vr.violations.extend(rr.violations)
                    vr.valid = False
                return vr

            vres = _full_validate(ep)

            if not vres.valid:
                # ── P0.2 LLM 自修正（最多 2 轮）──
                for _ in range(2):
                    repair_inputs.append(json.loads(json.dumps({"plan": ep, "violations": vres.violations})))
                    fixed = await asyncio.to_thread(
                        _repair_execution_plan, llm, ep, vres.violations, account, constraints
                    )
                    if fixed is None:
                        break
                    fixed.setdefault("market", market)
                    fixed.setdefault("sector", sector_map.get(ticker, ""))
                    vres2 = _full_validate(fixed)
                    if vres2.valid:
                        ep = fixed
                        vres = vres2
                        repaired += 1
                        break
                    ep, vres = fixed, vres2

            if not vres.valid:
                # ── P0.3 自动降级：缩量到合规（buy/add 缩买量；sell/reduce 缩到持仓，超卖→按实际持仓卖）──
                # P0-D：缩量必须与校验同口径——按【剩余日额度】+【影子账本现金】缩，
                # 否则同一轮多笔各按满额现金缩量，缩完仍是一批注定在成交期失败的待确认单。
                n = max_compliant_shares(ep, _acct_view, positions, constraints,
                                         daily_turnover_used=_turnover_used)
                if n > 0 and ep.get("action") in ("buy", "add", "sell", "reduce"):
                    ep["shares"] = n
                    price = ep.get("target_price") or ep.get("estimated_price", 0)
                    ep["estimated_amount"] = n * price
                    ep["auto_adjusted"] = True
                    vres = _full_validate(ep)

            if not vres.valid:
                # ── P0.3 无法降级：拦截，写入"已拦截"区 + 回灌反馈 ──
                pending_writes.append(
                    (
                        True,
                        dict(
                            tactical_plan_id=tactical_id,
                            entry_id=entry_id,
                            ticker=ticker,
                            result_json=ep,
                            reason="; ".join(vres.violations),
                            marker=store.BLOCK_MARKER_SYSTEM,
                        ),
                    )
                )
                blocked += 1
                logger.info("拦截不合规执行计划 %s: %s", ticker, vres.violations)
                continue

            if not _is_executable_plan(ep):
                # hold / 漏填股数的计划过校验会 fail-open 成 valid，落库即“--股”不可执行指令
                logger.info("跳过不可执行计划 %s: action=%s shares=%s", ticker, ep.get("action"), ep.get("shares"))
                skipped += 1
                continue

            # ── P0-4 / N-28 越线标记：**必须在股数最终确定之后**才判 ──
            # 判据是"执行后权重是否真越过 L2 目标授权"，所以它依赖最终 shares。原实现把标记打在
            # `_sized` 之后、而 `_full_validate`/两轮 LLM 自修正（会替换整个 ep）/`max_compliant_shares`
            # 降级（会改写 shares）都在其后 —— 被压回目标之内的计划仍带着 mandate_exception。
            # 方向是安全的（多要一次人工确认），但人工确认队列被污染，与注释声称的"不必再打扰人"相反。
            # 回到这里判：降级后 shares 已定，被拦的计划已在上面 continue 走不到这里。
            if ticker in opp_plans and ep.get("action") in ("buy", "add"):
                _price_f = float(ep.get("target_price") or ep.get("estimated_price") or 0) or 0.0
                _shares_f = float(ep.get("shares") or 0)
                _equity_f = float(account.get("total_equity") or account.get("cash_balance") or 0) or 0.0
                _l2_w = l2_targets.get(normalize_ticker(ticker, market), 0.0)
                if _equity_f > 0 and _price_f > 0:
                    _exist_f = next(
                        (float(p.get("market_value") or 0) for p in positions if p.get("ticker") == ticker), 0.0
                    )
                    _post_w = (_exist_f + _shares_f * _price_f) / _equity_f * 100
                    if _post_w > _l2_w + 0.05:  # +0.05pct 容差：躲开浮点噪声造成的假越线
                        ep["mandate_exception"] = True
                        ep["mandate_exception_note"] = (
                            f"机会驱动越过 L2 目标授权：目标 {_l2_w:.1f}% → 执行后 {_post_w:.1f}%"
                        )
                        # N-29：越线必须留痕。这条此前只在 result_json 里躺着，投委会看不到、
                        # oplog 无记录 —— 事后既无法复盘"为什么这笔越了线"，也无法统计越线频率。
                        _oplog_mandate_exception(store, ticker, market, _l2_w, _post_w, ep)

            ep["_provenance"] = _decision_provenance(
                ["decision_execution"], [(provider, model)], market, "L4", [ticker]
            )
            pending_writes.append(
                (
                    False,
                    dict(
                        tactical_plan_id=tactical_id,
                        entry_id=entry_id,
                        ticker=ticker,
                        result_json=ep,
                    ),
                )
            )
            # P0-D：本批已放行、但尚未落库的计划金额也要计入日额度与影子现金，
            # 否则同轮 10 条各 5% 权益仍会全部通过（每条都看不见彼此）。
            _amt = float(ep.get("estimated_amount") or 0) or float(ep.get("shares") or 0) * float(
                ep.get("target_price") or ep.get("estimated_price") or 0)
            if ep.get("action") in ("buy", "add"):
                _cash_left -= _amt
            _turnover_used += _amt

        if pending_writes:
            binding = save_stage_snapshot(store, "L4", {**stage_inputs, "repair_inputs": repair_inputs})
            for is_blocked, values in pending_writes:
                if is_blocked:
                    store.create_blocked_execution(**values, **binding)
                else:
                    created_ids.append(store.create_execution_plan(**values, **binding))

        for cid in applied_card_ids:
            store.increment_card_applied(cid)

        extra = []
        if skipped:
            extra.append(f"跳过 {skipped} 条重复")
        if repaired:
            extra.append(f"自修正 {repaired} 条")
        if blocked:
            extra.append(f"拦截 {blocked} 条不合规")
        extra_msg = ("，" + "，".join(extra)) if extra else ""
        yield _sse(
            "decision_done",
            layer="L4",
            plan_count=len(created_ids),
            blocked_count=blocked,
            repaired_count=repaired,
            execution_summary=result.get("execution_summary", {}),
            skipped=result.get("skipped_plans", []),
            message=f"L4 执行方案已生成：{len(created_ids)} 条待确认操作{extra_msg}",
        )

        # P3.2 过度交易监控：近 7 天成交超阈值则告警
        try:
            recent = store.get_sim_trades(limit=100)
            cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
            recent_count = sum(1 for t in recent if (t.get("created_at", "") or "") >= cutoff)
            if recent_count >= 15:
                yield _sse(
                    "decision_warning",
                    layer="L4",
                    message=f"⚠ 过度交易提示：近7天已成交 {recent_count} 笔，注意手续费与择时损耗",
                )
        except Exception:
            pass

    except Exception as e:
        logger.exception("L4 执行方案生成失败")
        yield _sse("decision_error", layer="L4", error=str(e))


# ─────────────────────────────────────────────────────────
# 完整日常决策流程
# ─────────────────────────────────────────────────────────


async def _hard_stop_loss_sweep(store: WatchlistStore, market: str) -> AsyncGenerator[dict, None]:
    """硬止损巡检：持仓现价跌破 L3 战术计划的 stop_loss 即生成卖出执行计划（绕过 LLM）。

    这是买方风控闭环最不可缺的一环——两次 L3 生成之间持仓大幅下破止损位时，
    系统应自动减仓而非等下一轮 LLM 决策。零 LLM 成本。
    """
    try:
        account = store.get_sim_account()
        positions = store.get_sim_positions(account.get("id"))
    except Exception as e:
        logger.warning("硬止损巡检读取持仓失败: %s", e)
        return

    triggered = 0
    for pos in positions:
        ticker = pos.get("ticker", "")
        shares = pos.get("shares", 0)
        if not ticker or shares <= 0:
            continue
        plan = store.get_latest_tactical_plan_for_ticker(ticker)
        if not plan:
            continue
        exit_plan = plan.get("exit_plan") or {}
        stop = exit_plan.get("stop_loss") or {}
        stop_price = stop.get("price")
        if not stop_price:
            continue
        snap = store.get_latest_snapshot(ticker)
        close = snap.get("close") if snap else None
        if not close or close > stop_price:
            continue
        # 已跌破止损位 → 生成清仓卖出执行计划（待投委会/确认）
        try:
            binding = save_stage_snapshot(
                store.for_market(market),
                "hard_stop",
                {
                    "account": account,
                    "position": pos,
                    "tactical_plan": plan,
                    "price_snapshot": snap,
                },
            )
            eid = store.create_execution_plan(
                tactical_plan_id=plan.get("id", ""),
                entry_id=pos.get("entry_id", ""),
                ticker=ticker,
                result_json={
                    "action": "sell",
                    "shares": shares,
                    "target_price": close,
                    "priority": 1,
                    "reasoning": f"硬止损触发：现价 {close} 已跌破止损位 {stop_price}（自动风控，非 LLM 决策）",
                    "_hard_stop": True,
                    "_provenance": _decision_provenance([], [], market, "L4", [ticker]),
                },
                **binding,
            )
            triggered += 1
            yield _sse(
                "decision_warning",
                layer="risk_control",
                ticker=ticker,
                message=f"⚠ 硬止损触发 {ticker}：现价 {close} < 止损 {stop_price}，已生成清仓计划 {eid[:8]}",
            )
        except Exception as e:
            logger.warning("硬止损生成卖出计划失败 %s: %s", ticker, e)

    if triggered:
        logger.info("硬止损巡检(%s): %d 只触发", market, triggered)


# 时效门单次扫描的最大标的数（防超大观察池扫描过慢；抽样足以判定数据源是否整体过期）
MAX_STALE_SCAN = 30


async def _ensure_price_freshness(
    store: WatchlistStore,
    market: str,
    halt: dict,
) -> AsyncGenerator[dict, None]:
    """决策启动前的数据时效门：核对行情快照是否过期，过期则主动更新，更新失败则请求硬停。

    - 全新鲜 → data_freshness_pass，继续。
    - 有过期票 → data_refresh_start → 仅对过期票 fetch_price_batch（scheduler 同一原语，落共享快照层）。
      判定「更新失败」仅看数据源是否响应（返回 error 而非 no_data/ok），不看更新后的日历天数
      （周一跑决策时周五收盘即最新合法数据，日历过期不算失败，避免周末假告警）。
    - error 数 > 尝试数一半 → data_refresh_block + 置 halt['stop']=True（调用方硬停，不出任何建议）；
      否则 data_refresh_done 继续（个别退市/停牌 no_data 只计缺数据、不算失败）。
    """
    from bottleneck_hunter.watchlist.quality_gate import validate_data_freshness

    entries = [e for e in store.list_all() if normalize_market(e.get("market")) == normalize_market(market)]
    if not entries:
        return

    stale = []
    for entry in entries[:MAX_STALE_SCAN]:
        ticker = entry.get("ticker", "")
        if not ticker:
            continue
        snaps = store.get_snapshots(ticker, days=5)
        if not snaps:
            stale.append((ticker, "无数据"))
            continue
        # get_snapshots 按 date DESC → [0] 为最新一条；用 fetched_at 判「数据管线是否新鲜」
        latest = snaps[0].get("fetched_at", "")
        color, days = validate_data_freshness(latest, "market_snapshots")
        if color != "green":
            stale.append((ticker, f"{days}天"))

    if not stale:
        yield _sse(
            "data_freshness_pass",
            layer="data",
            message=f"数据时效核对通过（{min(len(entries), MAX_STALE_SCAN)} 票新鲜）",
        )
        return

    stale_tickers = [t for t, _ in stale]
    detail = ", ".join(f"{t}({d})" for t, d in stale[:5]) + ("…" if len(stale) > 5 else "")
    yield _sse("data_refresh_start", layer="data", message=f"检测到 {len(stale)} 票行情过期（{detail}），启动主动更新…")

    from bottleneck_hunter.watchlist.price_pipeline import fetch_price_batch

    try:
        results = await fetch_price_batch(stale_tickers, store, market=market)
    except Exception as e:  # noqa: BLE001
        logger.error("数据时效门主动更新整体失败 (%s): %s", market, e)
        halt["stop"] = True
        yield _sse(
            "data_refresh_block",
            layer="data",
            message=f"⛔ 行情主动更新失败（{e}）——决策已中止。请检查数据源/网络，修复后重跑。",
        )
        return

    err = [t for t, v in results.items() if isinstance(v, str) and v.startswith("error")]
    no_data = [t for t, v in results.items() if v == "no_data"]
    ok = [t for t, v in results.items() if v == "ok"]

    # 仅过半失败才算「更新失败」→ 硬停；个别票失败（退市/停牌代码）只告警、继续
    if len(err) > len(results) / 2:
        halt["stop"] = True
        yield _sse(
            "data_refresh_block",
            layer="data",
            message=(
                f"⛔ 行情主动更新过半失败（{len(err)}/{len(results)} 票：{', '.join(err[:5])}）"
                f"——决策已中止。请检查数据源/网络，修复后重跑。"
            ),
        )
        return

    msg = f"数据已主动更新：成功 {len(ok)} 票"
    if err:
        msg += f"，失败 {len(err)} 票（{', '.join(err[:3])}，个别票不影响，继续）"
    if no_data:
        msg += f"，无数据 {len(no_data)} 票"
    yield _sse("data_refresh_done", layer="data", message=msg)


async def run_daily_decision(
    store: WatchlistStore,
    budget: BudgetTracker | None = None,
    scope: str = "full",
    market: str = "us_stock",
) -> AsyncGenerator[dict, None]:
    """完整日常决策流程：L1→L2→L3→L4→投委会

    scope: "l1" = 仅 L1 检查
           "l3l4" = 仅 L3-L4 更新
           "full" = 全流程
    """
    store = store.for_market(market)
    yield _sse("daily_start", scope=scope, market=market, message="开始日常决策流程...")

    # Step -1: 数据时效门 —— 启动即核对行情时效，过期则主动更新；更新过半失败则硬停不出建议。
    #          仅在消费个股数据的 scope 跑（l1 纯宏观/指数，不依赖逐票快照）。
    if scope in ("l3l4", "full"):
        halt: dict = {}
        try:
            async for evt in _ensure_price_freshness(store, market, halt):
                yield evt
        except Exception as e:  # noqa: BLE001 —— 时效门自身异常不得吞掉决策，降级为告警继续
            logger.warning("数据时效门异常（降级继续）: %s", e)
        if halt.get("stop"):
            yield _sse("daily_done", message="已中止：行情数据源异常，请检查修复后重跑（未输出任何决策建议）")
            return

    # Step 0: 催化剂时效检查
    try:
        from bottleneck_hunter.watchlist.catalyst_monitor import check_catalyst_expiry

        async for evt in check_catalyst_expiry(store):
            yield evt
    except Exception as e:
        logger.warning("催化剂时效检查失败: %s", e)

    # Step 0.5: 投资论点有效性检查
    try:
        from bottleneck_hunter.watchlist.thesis_tracker import check_all_theses

        async for evt in check_all_theses(store):
            yield evt
    except Exception as e:
        logger.warning("论点检查失败: %s", e)

    # Step 0.6: 硬止损巡检（现价跌破止损位即自动生成清仓计划，绕过 LLM）
    if scope in ("l3l4", "full"):
        try:
            async for evt in _hard_stop_loss_sweep(store, market):
                yield evt
        except Exception as e:
            logger.warning("硬止损巡检失败: %s", e)

    # Step 1: L1 宏观检查
    if scope in ("l1", "full"):
        async for evt in run_macro_check(store, budget, market=market):
            yield evt

    # Step 2: L2 偏离检查 + pre_l2 质量门控
    if scope in ("full",):
        try:
            from bottleneck_hunter.watchlist.quality_gate import run_quality_checks

            async for evt in run_quality_checks(store, "pre_l2"):
                yield evt

            macro = store.get_latest_macro_strategy()
            plan = store.get_latest_strategic_plan()

            if not plan and macro:
                yield _sse("decision_info", layer="L2", message="无 L2 组合策略，自动生成...")
                async for evt in run_strategic_plan(store, budget, market=market):
                    yield evt
            elif plan:
                # P1-1 漂移逃逸阀：日常流程对已有 L2 只做偏离检查（不重生成），组合严重偏离目标时
                # 等于一直拿失效的旧图纸，且静默发生。偏离超阈值 + 冷却已过 → 改为强制重生成 L2。
                _plan_age = _upstream_age_days(plan.get("created_at", ""))
                _escape = (
                    _reuse_escape_reason(store, plan.get("result_json") or {}, market)
                    if _plan_age is not None and _plan_age * 24 >= _DECISION_ESCAPE_MIN_AGE_H
                    else {}
                )
                if _escape:
                    logger.info("L2 漂移逃逸[%s]：%s，强制重生成以面对现实组合", market, _escape["reason"])
                    yield _sse(
                        "decision_warning",
                        layer="L2",
                        message=f"⚠ 组合实际配置已偏离该 L2 目标 {_escape['drift_pct']:+.1f}pct（阈值 "
                        f"{_escape['threshold_pct']:.0f}pct），不再沿用旧组合策略，强制重生成 L2",
                    )
                    async for evt in run_strategic_plan(store, budget, market=market, force=True):
                        yield evt
                else:
                    async for evt in run_deviation_check(store, budget, market=market):
                        yield evt
        except Exception as e:
            logger.exception("L2 阶段失败")
            yield _sse("decision_error", layer="L2", error=str(e))

    if scope == "l1":
        yield _sse("daily_done", message="L1 检查完成")
        return

    # Step 2.5: pre_l3 质量门控
    if scope in ("l3l4", "full"):
        try:
            from bottleneck_hunter.watchlist.quality_gate import run_quality_checks

            async for evt in run_quality_checks(store, "pre_l3"):
                yield evt
        except Exception as e:
            logger.warning("pre_l3 质量门控失败: %s", e)

    # Step 3: L3 战术计划
    if scope in ("l3l4", "full"):
        async for evt in run_tactical_plans(store, budget, market=market):
            yield evt

    # Step 3.5: pre_l4 质量门控
    l4_blocked = False
    l4_block_reason = ""
    l4_plan_count: int | None = None
    if scope in ("l3l4", "full"):
        try:
            from bottleneck_hunter.watchlist.quality_gate import run_quality_checks

            async for evt in run_quality_checks(store, "pre_l4"):
                yield evt
                d = evt.get("data", {}) if isinstance(evt.get("data"), dict) else {}
                if evt.get("event") == "quality_check_block":
                    l4_blocked = True
                    l4_block_reason = str(d.get("reason") or d.get("message") or "质量门红灯")
        except Exception as e:
            logger.warning("pre_l4 质量门控失败: %s", e)

    # 质量门红灯＝本次 L4 零产出的明确原因。落一条 operation_log，让「被闸门拦下」在日志里
    # 留下痕迹 —— 此前只发 SSE，容器一重启证据全丢，事后根本分不清「被拦」和「没跑」。
    if l4_blocked:
        try:
            uid = getattr(store, "_user_id", "") or ""
            if uid:
                from bottleneck_hunter.web.oplog import record_operation

                record_operation(
                    uid, "质量门阻断 L4",
                    # category=error 是为了让它进推送白名单（_PUSH_CATEGORIES），用户能立刻看到被拦；
                    # result=partial 表达「跑完了但没产出」，与真异常 fail 区分。
                    category="error",
                    detail=f"{market}：pre_l4 质量门红灯，未生成任何新建执行方案。原因：{l4_block_reason}"[:300],
                    result="partial", market=market,
                    meta={"stage": "pre_l4", "reason": l4_block_reason},
                )
        except Exception as e:  # noqa: BLE001 —— 留痕失败不得影响决策
            logger.debug("质量门阻断留痕失败: %s", e)

    # P1-2 缺口未纠正留痕：上游陈旧（L3 已中止）/ pre_l4 红灯时停买是对的（不能盲下单），但扩张侧那笔
    # "该配没配"也得像"被拦的买"一样在 operation_log 记账 —— 否则事后只看到"买了的被拦"，看不到"没买的欠着"。
    # （Step -1 行情硬停已提前 return：价格本身不可信时算不出可信缺口，不记。）
    # P1-C：判据已抽成 _uncorrected_gap_cause/_record_uncorrected_gap，与 run_full_refresh 共用一份。
    try:
        if scope in ("l3l4", "full"):
            _record_uncorrected_gap(
                store, market,
                _uncorrected_gap_cause(store, l4_blocked=l4_blocked, l4_plan_count=l4_plan_count),
            )
    except Exception as e:  # noqa: BLE001 —— 留痕失败不得影响决策
        logger.debug("缺口未纠正留痕失败: %s", e)

    # Step 4: L4 执行方案（质量门 red 时阻断新建执行计划，避免在数据严重过期/超限下下单；
    #          A1 硬止损已生成的卖出计划不受影响，仍进入投委会）
    if scope in ("l3l4", "full"):
        if l4_blocked:
            yield _sse(
                "decision_warning",
                layer="L4",
                message="⛔ 质量门红灯，已阻断 L4 新建执行计划（数据过期/仓位超限），仅保留风控性卖出",
            )
        else:
            async for evt in run_execution_plans(store, budget, market=market):
                yield evt
                l4_plan_count = _l4_plan_count(evt, l4_plan_count)

    # Step 5: 投委会评审
    if scope in ("l3l4", "full"):
        try:
            pending = store.get_pending_executions()
            if pending:
                from bottleneck_hunter.watchlist.committee import run_committee_review

                yield _sse("decision_info", layer="committee", message=f"启动投委会评审 {len(pending)} 条执行计划...")
                async for evt in run_committee_review(store, pending, budget, market=market):
                    yield evt
            else:
                yield _sse("decision_info", layer="committee", message="无待评审执行计划，跳过投委会")
        except Exception as e:
            logger.exception("投委会评审失败")
            yield _sse("decision_error", layer="committee", error=str(e))

    # Step 5.5: L4 自动执行（用户开启「自动执行」时，投委会通过的待确认操作免人工确认直接成交）
    #           挂在投委会之后：拦截/否决的计划已进「已拦截」区不在 pending，只自动执行合规且通过评审的。
    if scope in ("l3l4", "full"):
        try:
            from bottleneck_hunter.watchlist.auto_execute import (
                auto_execute_pending,
                is_auto_execute_enabled,
            )

            if is_auto_execute_enabled(store):
                async for evt in auto_execute_pending(store, market):
                    yield evt
        except Exception as e:
            logger.exception("L4 自动执行失败")
            yield _sse("decision_error", layer="auto_execute", error=str(e))

    # Step 6: 更新观察池综合评分（裸调用需保护，否则崩溃会中断 SSE 流导致前端面板空白）
    try:
        _update_composite_scores(store, market)
    except Exception as e:
        logger.exception("综合评分更新失败")
        yield _sse("decision_error", layer="composite", error=str(e))

    yield _sse("daily_done", message="日常决策流程完成")


async def run_full_refresh(
    store: WatchlistStore,
    budget: BudgetTracker | None = None,
    market: str = "us_stock",
) -> AsyncGenerator[dict, None]:
    """全量刷新：重新抓取市场新闻 + 重新生成 L1 + L2 + L3 + L4 + 投委会"""
    store = store.for_market(market)
    yield _sse("refresh_start", message="开始全量决策刷新...")

    # 数据时效门：全量刷新同样先核对+主动更新行情，过半失败则硬停不出建议。
    halt: dict = {}
    try:
        async for evt in _ensure_price_freshness(store, market, halt):
            yield evt
    except Exception as e:  # noqa: BLE001
        logger.warning("全量刷新数据时效门异常（降级继续）: %s", e)
    if halt.get("stop"):
        yield _sse("refresh_done", message="已中止：行情数据源异常，请检查修复后重跑（未输出任何决策建议）")
        return

    # 先刷新市场新闻源（拉新 RSS 落库），供 L1 与宏观咨询读到最新新闻
    try:
        from bottleneck_hunter.watchlist.news_pipeline import refresh_market_news

        llm, _p, _m = get_llm_for_position(position="L1_macro")
        n = await refresh_market_news(store, market, llm=llm, budget=budget)
        yield _sse("refresh_progress", step="market_news", message=f"市场新闻已更新（{n} 条）")
    except Exception as e:  # noqa: BLE001
        logger.warning("全量刷新：市场新闻刷新失败: %s", e)

    async for evt in run_macro_strategy(store, budget, market=market):
        yield evt

    async for evt in run_strategic_plan(store, budget, market=market):
        yield evt

    async for evt in run_tactical_plans(store, budget, market=market):
        yield evt

    # P1-C：全量刷新路径的缺口未纠正留痕。这里 L1/L2 刚被强制重生成，"上游陈旧"这一支
    # 天然不可能成立（故 _uncorrected_gap_cause 里那条判据在此是死枝，不是漏检）；本路径
    # 也不跑 pre_l4 质量门，所以能停摆的原因只剩「L4 跑完却一条方案都没产出」。
    l4_plan_count: int | None = None
    async for evt in run_execution_plans(store, budget, market=market):
        yield evt
        l4_plan_count = _l4_plan_count(evt, l4_plan_count)

    try:
        _record_uncorrected_gap(store, market, _uncorrected_gap_cause(store, l4_plan_count=l4_plan_count))
    except Exception as e:  # noqa: BLE001 —— 留痕失败不得影响决策
        logger.debug("全量刷新缺口未纠正留痕失败: %s", e)

    pending = store.get_pending_executions()
    if pending:
        from bottleneck_hunter.watchlist.committee import run_committee_review

        async for evt in run_committee_review(store, pending, budget, market=market):
            yield evt

    # L4 自动执行（同 run_daily_decision Step 5.5：开启时投委会通过的待确认操作免确认直接成交）
    try:
        from bottleneck_hunter.watchlist.auto_execute import (
            auto_execute_pending,
            is_auto_execute_enabled,
        )

        if is_auto_execute_enabled(store):
            async for evt in auto_execute_pending(store, market):
                yield evt
    except Exception as e:
        logger.exception("L4 自动执行失败")
        yield _sse("decision_error", layer="auto_execute", error=str(e))

    try:
        _update_composite_scores(store, market)
    except Exception as e:
        logger.exception("综合评分更新失败")
        yield _sse("decision_error", layer="composite", error=str(e))
    yield _sse("refresh_done", message="全量决策刷新完成")


# ─────────────────────────────────────────────────────────
# 数据收集辅助
# ─────────────────────────────────────────────────────────

# EDB 宏观按付费计分（30 积分/指标/次；美股 7 项=210、A股 4 项=120）。指标为月频（联邦基金
# 日频但按 FOMC 步进），L1 每轮重取必得同值 → 纯烧分。故设「重取节流窗」：窗内直接复用
# macro_snapshots 缓存值注入，只在窗外才真打 EDB。25 天覆盖一个月频发布周期，省 ~95% 积分。
_EDB_REFRESH_DAYS = 25


def _edb_cache_fresh(store: WatchlistStore, market: str) -> dict | None:
    """若本市场 EDB 指标缓存仍在节流窗内，返回可直接注入的 {key:{value,change_pct,label,as_of}}；
    否则返回 None（表示需真打 EDB）。凭 macro_snapshots.fetched_at 的**批次新鲜度**判定：缓存全空
    或最近批次超窗即真取；窗内则复用已落库指标（永久无覆盖的指标交下游兜底，不因其而反复付费）。
    """
    from bottleneck_hunter.data_provider.gangtise_edb_indicators import indicators_for_market

    want = indicators_for_market(market)  # {key: (fid, label, scope, transform)}
    if not want:
        return None
    try:
        rows = {r["indicator"]: r for r in store.get_latest_macro_snapshots()}
    except Exception:  # noqa: BLE001
        return None
    cached = {k: rows[k] for k in want if k in rows}
    if not cached:
        return None  # 全空（首取）→ 需真打
    # 节流信号取「批次新鲜度」而非「全指标齐备」：EDB 同批取数共享 fetched_at；若某指标为 Gangtise
    # 永久无覆盖，苛求齐备会令节流永不生效、每轮重复付费（US 210/CN 120 分）却拿不到那条 → 成本泄漏。
    # 故只要最近一次批次取数在窗内，即复用；缺失键交下游 yfinance/FRED 兜底，不因其永缺而反复付费。
    cutoff = datetime.now(timezone.utc) - timedelta(days=_EDB_REFRESH_DAYS)

    def _parse_ts(v: str):
        try:
            t = datetime.fromisoformat(v or "")
            return t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t
        except (ValueError, TypeError):
            return None

    ts_list = [_parse_ts(row.get("fetched_at")) for row in cached.values()]
    newest = max((t for t in ts_list if t is not None), default=None)
    if newest is None or newest < cutoff:
        return None  # 批次超窗 or 时间戳全不可解析 → 保守真取
    out: dict = {}
    for key, row in cached.items():
        out[key] = {
            "value": row.get("value"),
            "change_pct": row.get("change_pct", 0.0) or 0.0,
            "label": want[key][1],
            "as_of": row.get("date"),
        }
    return out


async def _inject_edb_macro(store: WatchlistStore, market: str, macro: dict) -> None:
    """把 Gangtise EDB 官方宏观并入 macro 段并落 macro_snapshot。就地改 macro，凭据缺/未开则空操作。

    EDB 官方口径（如中国官方 PMI、社融同比）优先级高于 yfinance/FRED 兜底，故**覆盖同 key**。
    落库用 EDB 的真实 as_of 日期，供下游 as-of 标注（防日期臆造）。

    付费节流：EDB 计分 30/指标/次，指标月频。窗（_EDB_REFRESH_DAYS）内复用缓存注入、不打接口；
    仅窗外真取。既省积分又保 L1 宏观段仍有本土/官方口径读数。
    """
    fresh = _edb_cache_fresh(store, market)
    if fresh is not None:
        for key, v in fresh.items():
            macro[key] = v  # 缓存复用：同样覆盖 yfinance/FRED 兜底口径
        logger.debug("EDB 宏观命中节流窗(%s)，复用缓存 %d 项，跳过计费取数", market, len(fresh))
        return
    try:
        from bottleneck_hunter.data_provider.hub import CAP_MACRO_EDB, get_hub

        edb = await get_hub().fetch(CAP_MACRO_EDB, "", market)
    except Exception as e:  # noqa: BLE001
        logger.debug("EDB 宏观注入失败: %s", e)
        return
    if not edb:
        return
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for key, v in edb.items():
        as_of = v.get("as_of") or ""
        if len(as_of) == 8 and as_of.isdigit():  # EDB yyyymmdd → yyyy-mm-dd（与表内其它源一致）
            as_of = f"{as_of[:4]}-{as_of[4:6]}-{as_of[6:]}"
            v["as_of"] = as_of
        macro[key] = v  # EDB 官方口径覆盖兜底
        try:
            store.save_macro_snapshot(
                key, as_of or now_iso[:10], v["value"], now_iso, change_pct=v.get("change_pct", 0.0)
            )
        except Exception as e:  # noqa: BLE001
            logger.debug("EDB 宏观落库失败 %s: %s", key, e)


async def _collect_market_context(store: WatchlistStore, market: str = "us_stock") -> dict:
    """收集市场宏观数据：真实大盘指数 + 观察池广度聚合，并附带市场类型列表。"""
    from bottleneck_hunter.watchlist.macro_data import MARKET_INDEX_KEYS, fetch_macro_data

    by_market = store.get_tickers_by_market()
    tickers = by_market.get(market, [])
    active_markets = [market]

    # 观察池聚合的『市场结构/持仓定位』：期权 PCR + 机构 13F 季度增减方向。
    # 数据早已按 ticker 落库(options_pipeline/institutional_pipeline)但从未聚合进快照——
    # US 市场特性提示词点名「关键指标：期权 PCR、机构持仓 13F」却拿不到，属『算了没接』的接线缺口。
    positioning = _positioning_signals(store, tickers)

    # 先取真实宏观（含真实大盘指数），保证即使观察池为空 L1 也有真实大盘输入
    try:
        macro = await fetch_macro_data(store, active_markets)
    except Exception as e:
        logger.warning("宏观数据采集失败，使用缓存: %s", e)
        macro = {}
        from bottleneck_hunter.watchlist.macro_data import foreign_indicator_keys

        foreign = foreign_indicator_keys(active_markets)  # 剔除他市专属指标，防缓存兜底串味
        cached = store.get_latest_macro_snapshots()
        for row in cached:
            if row["indicator"] in foreign:
                continue
            macro[row["indicator"]] = {
                "value": row["value"],
                "change_pct": row.get("change_pct", 0.0) or 0.0,
                "label": row["indicator"],
                "as_of": row.get("date"),
            }

    # Gangtise EDB 官方宏观（CPI/PPI/利率/PMI/社融）注入 L1——填补 macro 段本土/官方口径薄弱。
    # 走 hub（享受凭据双开关 + 熔断）；无凭据/未开则空返回，静默跳过不影响既有 macro。
    await _inject_edb_macro(store, market, macro)

    # 真实大盘指数（区别于 VIX/汇率等宏观指标）
    real_indices = {k: macro[k] for k in MARKET_INDEX_KEYS.get(market, ["sp500"]) if k in macro}

    # VIX 属市场情绪而非宏观经济：移入 sentiment 段，macro 段保留利率/汇率等真宏观
    macro_sentiment = {}
    if "vix" in macro:
        macro_sentiment["vix"] = macro.pop("vix")

    all_snapshots = []
    for ticker in tickers:
        snap = store.get_latest_snapshot(ticker)
        if snap:
            all_snapshots.append(snap)

    if not all_snapshots:
        return {
            "indices": dict(real_indices),
            "sectors": {},
            "sentiment": dict(macro_sentiment),
            "macro": macro,
            "news": [],
            "markets": active_markets,
            "positioning": positioning,
        }

    avg_change = sum(s.get("change_pct", 0) or 0 for s in all_snapshots) / max(len(all_snapshots), 1)
    avg_rsi = sum(s.get("rsi_14", 50) or 50 for s in all_snapshots) / max(len(all_snapshots), 1)

    entries = [e for e in store.list_all() if normalize_market(e.get("market")) == normalize_market(market)]
    sectors = {}
    for entry in entries:
        sector = entry.get("sector", "未分类")
        if sector not in sectors:
            sectors[sector] = {"tickers": [], "avg_change": 0}
        sectors[sector]["tickers"].append(entry["ticker"])

    for sector, info in sectors.items():
        changes = []
        for t in info["tickers"]:
            snap = store.get_latest_snapshot(t)
            if snap and snap.get("change_pct") is not None:
                changes.append(snap["change_pct"])
        info["avg_change"] = round(sum(changes) / max(len(changes), 1), 2) if changes else 0
        info["count"] = len(info["tickers"])
        del info["tickers"]

    news_items = []
    for ticker in tickers[:5]:
        recent = store.get_news(ticker, limit=2)
        for n in recent:
            news_items.append({"ticker": ticker, "title": n.get("title", ""), "sentiment": n.get("sentiment", "")})

    return {
        "indices": {
            **real_indices,  # 真实大盘指数（标普/纳指 或 上证/沪深300）
            "watchlist_breadth": {  # 观察池广度（自选股均值，明确区分于大盘）
                "avg_change_pct": round(avg_change, 2),
                "avg_rsi": round(avg_rsi, 1),
                "stocks_tracked": len(all_snapshots),
            },
        },
        "sectors": sectors,
        "sentiment": {
            **macro_sentiment,  # VIX 恐慌指数（真市场情绪）
            "avg_rsi": round(avg_rsi, 1),
            "stocks_above_sma50": sum(
                1 for s in all_snapshots if s.get("close") and s.get("sma_50") and s["close"] > s["sma_50"]
            ),
            "stocks_total": len(all_snapshots),
        },
        "macro": macro,
        "news": news_items[:10],
        "markets": active_markets,
        "positioning": positioning,
    }


def _positioning_signals(store: WatchlistStore, tickers: list[str]) -> dict:
    """观察池聚合的『市场结构/持仓定位』信号：期权 PCR + 机构 13F 季度增减方向。

    数据源均为美股(options_activity/institutional_holders 由 yfinance 落库)：A股/港股 无数据 → 返回空，
    不污染其宏观口径。任一子项无数据则该键缺省(诚实降级)，绝不编造。
    ponytail: 逐 ticker 读库(观察池上界 ~60 只、本地 SQLite)，量级足够；真成瓶颈再上批量查询。
    """
    out: dict = {}

    # 期权 put/call：取每只最近一条，按成交量加权聚合(代表资金体量，>1 偏空/对冲需求高)
    tot_call = tot_put = 0
    pcr_names = 0
    for t in tickers:
        opt = store.get_options(t, limit=1)
        if not opt:
            continue
        c = opt[0].get("total_call_volume") or 0
        p = opt[0].get("total_put_volume") or 0
        if c or p:
            tot_call += c
            tot_put += p
            pcr_names += 1
    if pcr_names and tot_call:
        out["options"] = {
            "put_call_ratio": round(tot_put / tot_call, 3),
            "coverage": pcr_names,
            "universe": len(tickers),
        }

    # 13F 机构持仓：逐票取近两季环比(复用 _holder_qoq 的两季共同机构口径)，聚合成观察池增/减/平家数。
    added = trimmed = flat = covered = 0
    for t in tickers:
        qoq = _holder_qoq(store, t)
        if qoq is None:
            continue
        covered += 1
        if qoq["net_shares"] > 0:
            added += 1
        elif qoq["net_shares"] < 0:
            trimmed += 1
        else:
            flat += 1
    if covered:
        out["institutional"] = {
            "quarter_net": {"added": added, "trimmed": trimmed, "flat": flat},
            "coverage": covered,
            "universe": len(tickers),
            "note": "基于两个申报季共同机构的净增减股数(季频/覆盖有限，随季度积累更全)",
        }
    return out


def _collect_watchlist_signals(store: WatchlistStore, market: str = "us_stock") -> list[dict]:
    """从已有的 strategy_records 收集个股信号"""
    entries = store.list_all()
    entries = [e for e in entries if normalize_market(e.get("market")) == normalize_market(market)]
    signals = []

    strategy_summaries = store.get_all_strategy_summaries()

    for entry in entries:
        entry_id = entry["id"]
        ticker = entry["ticker"]
        summary = strategy_summaries.get(entry_id, {})

        snap = store.get_latest_snapshot(ticker)

        if snap and snap.get("data_quality") == "suspended":
            logger.info("跳过停牌股 %s", ticker)
            continue

        signals.append(
            {
                "ticker": ticker,
                "company_name": entry.get("company_name", ticker),
                "sector": entry.get("sector", ""),
                "tier": entry.get("tier", "track"),
                "signal": summary.get("signal", "neutral"),
                "confidence": summary.get("confidence", 5),
                "price": snap.get("close") if snap else None,
                "change_pct": snap.get("change_pct") if snap else None,
                "rsi_14": snap.get("rsi_14") if snap else None,
            }
        )

    return signals


def _update_composite_scores(store: WatchlistStore, market: str = "us_stock") -> None:
    """根据策略信心、投委会评分、催化剂活跃度计算并更新观察池综合评分。"""
    entries = store.list_all()
    entries = [e for e in entries if normalize_market(e.get("market")) == normalize_market(market)]
    strategy_summaries = store.get_all_strategy_summaries()

    # P3.3 绩效驱动的动态层权重(样本不足时回退默认 0.4/0.3)
    w_review, w_conf = _layer_weight_factors(store)

    for entry in entries:
        entry_id = entry["id"]
        ticker = entry["ticker"]
        try:
            strategy = strategy_summaries.get(entry_id, {})
            confidence = strategy.get("confidence", 5)

            reviews = _get_latest_reviews_for_ticker(store, ticker)
            avg_score = sum(r.get("score", 5) or 5 for r in reviews) / len(reviews) if reviews else 5.0

            catalysts = store.get_catalysts_for_ticker(ticker)
            active_catalysts = [c for c in catalysts if c.get("status") in ("pending", "monitoring")]
            catalyst_score = min(len(active_catalysts) * 3, 10)

            snap = store.get_latest_snapshot(ticker)
            if snap and snap.get("fetched_at"):
                try:
                    fetched = datetime.fromisoformat(snap["fetched_at"].replace("Z", "+00:00"))
                    age_hours = (datetime.now(timezone.utc) - fetched).total_seconds() / 3600
                    freshness = max(0, min(10, 10 - age_hours / 12))
                except (ValueError, TypeError):
                    freshness = 5.0
            else:
                freshness = 0.0

            composite = round(
                avg_score * w_review + confidence * w_conf + catalyst_score * 0.15 + freshness * 0.15,
                2,
            )

            store.update(entry_id, composite_score=composite)
        except Exception as e:
            # 单标的失败不阻断其余标的评分更新
            logger.warning("综合评分更新失败 %s: %s", ticker, e)

    logger.info("更新了 %d 个标的的综合评分", len(entries))


def _layer_weight_factors(store: WatchlistStore) -> tuple[float, float]:
    """P3.3 绩效驱动：基于 layer_performance 历史表现，返回(委评权重, 信心权重)。

    L2(选股)历史准→委评层加权；L3(择时)历史准→信心层加权。
    仅在样本≥5时启用，调整幅度限制在基准 ±30% 内，且两者之和恒为 0.7。
    """
    base_review, base_conf = 0.4, 0.3
    try:
        summary = store.get_layer_performance_summary()
    except Exception:
        return base_review, base_conf
    l2 = summary.get("L2", {})
    l3 = summary.get("L3", {})
    if l2.get("count", 0) < 5 or l3.get("count", 0) < 5:
        return base_review, base_conf
    # 以 5 分为中性基准，>5 加权 <5 减权，归一化到总和 0.7
    l2_avg = l2.get("avg", 5)
    l3_avg = l3.get("avg", 5)
    # 限制偏移 ±30%
    l2_factor = max(0.7, min(1.3, l2_avg / 5))
    l3_factor = max(0.7, min(1.3, l3_avg / 5))
    raw_review = base_review * l2_factor
    raw_conf = base_conf * l3_factor
    total = raw_review + raw_conf
    if total <= 0:
        return base_review, base_conf
    # 归一化到原总和 0.7
    scale = 0.7 / total
    return round(raw_review * scale, 3), round(raw_conf * scale, 3)


def _get_latest_reviews_for_ticker(store: WatchlistStore, ticker: str) -> list[dict]:
    """获取某 ticker 最近一批投委会评审。"""
    conn = store._connect()
    try:
        q, p = store._filtered(
            """SELECT cr.score FROM committee_reviews cr
               JOIN execution_plans ep ON cr.execution_plan_id = ep.id
               WHERE ep.ticker = ?
               ORDER BY cr.created_at DESC LIMIT 4""",
            (ticker,),
            table="cr",
        )
        rows = conn.execute(q, p).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
