"""AI 分析师数据调用能力 —— 能力清单 + 结构化请求协商环（P0 底座）。

需求：决策中心/VIP 顾问在推理中若欠缺 facts 之外的数据，可发 [[DATA_REQ]] 块申请，
系统实时经 DataHub 取数、结果回注 prompt，最多两轮收敛；取数失败则解释原因、缺数据继续。

设计取向（见 docs/AI_DATA_TOOLS_PLAN_2026-08.md）：结构化 JSON 协商环，全 provider 统一、
零新增 LLM 层复杂度。执行走既有 DataHub.fetch（provider 优先级/熔断/按用户 Key 隔离/记账全复用）。

块协议（模型输出中）：
    [[DATA_REQ]]
    {"requests": [{"capability": "earnings", "ticker": "NVDA"}, ...]}
    [[/DATA_REQ]]
无块即视为无需补数据、直接进入最终回答。
"""

from __future__ import annotations

import json
import logging
import os
import re

from bottleneck_hunter.chain.json_utils import extract_json_object
from bottleneck_hunter.data_provider.hub import (
    CAP_DAILY,
    CAP_EARNINGS,
    CAP_FINANCIALS,
    CAP_INSIDER,
    CAP_INSTITUTIONAL,
    CAP_NEWS,
    CAP_NOTICE,
    CAP_OPTIONS,
    CAP_QUOTE,
    CAP_RESEARCH,
    CAP_SEC,
    CAP_SMARTMONEY,
    CAP_VALUATION,
    get_hub,
)
from bottleneck_hunter.watchlist.store_base import normalize_market, normalize_ticker

logger = logging.getLogger(__name__)

# 每任务取数预算与协商轮数（用户已定：8 次 / 2 轮）
MAX_FETCH_CALLS = 8
MAX_ROUNDS = 2
# 单条结果回注上限（防上下文膨胀）
_RESULT_CHARS = 1200

# 事故一键关：BH_AI_TOOLS_ENABLED=0 → 协商环退化为原 prompt 单趟（不注入清单、不取数）。
ENABLED = os.getenv("BH_AI_TOOLS_ENABLED", "1").strip().lower() not in ("0", "false", "no", "off")

# 能力人类标签（喂给模型的清单文案）。只描述当前有 provider 的能力。
_CAP_LABELS: dict[str, dict] = {
    CAP_QUOTE: {"label": "实时行情", "returns": "现价/涨跌%/币种"},
    CAP_DAILY: {"label": "日线历史", "returns": "近一段日线 K 线"},
    CAP_EARNINGS: {"label": "财报日期与业绩", "returns": "报告期/EPS 实际与预期/营收/指引"},
    CAP_FINANCIALS: {"label": "财务报表", "returns": "营收/净利(亿)/毛利率/券商一致预期 EPS·PE"},
    CAP_NEWS: {"label": "公司新闻", "returns": "近期标题/情绪"},
    CAP_SEC: {"label": "SEC/公告", "returns": "近期备案/公告摘要"},
    CAP_INSTITUTIONAL: {"label": "机构持仓(13F)", "returns": "季度增减方向/持有机构"},
    CAP_OPTIONS: {"label": "期权活动", "returns": "成交量/Put-Call Ratio"},
    CAP_INSIDER: {"label": "内部人交易", "returns": "内部人买卖动向"},
    CAP_NOTICE: {"label": "交易所公告", "returns": "近期公告"},
    CAP_SMARTMONEY: {"label": "聪明钱聚合", "returns": "内部人+机构+期权综合信号"},
    CAP_RESEARCH: {"label": "券商研报", "returns": "中/外资研报标题/评级/目标价/摘要（Gangtise）"},
    CAP_VALUATION: {"label": "估值分位", "returns": "PE/PB/PEG 近3年窗内分位（仅A股，Gangtise）"},
}

_REQ_RE = re.compile(r"\[\[DATA_REQ\]\](.*?)\[\[/DATA_REQ\]\]", re.DOTALL)


def build_manifest(market: str, user_id: str = "") -> list[dict]:
    """当前市场+当前用户【真可用】的能力清单（口径与 DataHub.fetch 一致）。

    只列可用能力——清单即承诺，AI 据此提请求才必然可兑现（对齐需求 4）。
    """
    market = normalize_market(market)
    try:
        avail = get_hub().available_capabilities(market, user_id)
    except Exception as e:  # noqa: BLE001
        logger.warning("能力清单构建失败（降级为空，AI 不发数据请求）: %s", e)
        return []
    out = []
    for cap in sorted(avail):
        meta = _CAP_LABELS.get(cap)
        if not meta:  # 未收录标签的能力不暴露给模型（如 macro_edb 非逐标的、kb 关键词检索，均不走逐标的 DATA_REQ）
            continue
        out.append({"capability": cap, "label": meta["label"], "returns": meta["returns"]})
    return out


def manifest_prompt(manifest: list[dict], allowed_tickers: list[str]) -> str:
    """把清单+规则拼成注入 prompt 的文本块。清单为空则返回空串（不引导发请求）。"""
    if not manifest:
        return ""
    lines = ["【可申请的实时数据能力】你在下方 facts/数据之外若确需补充数据，"
             "可在回答前发出请求块，系统会实时取数回注；取数失败会说明原因，你据缺失继续分析。"]
    for m in manifest:
        lines.append(f"- {m['capability']}（{m['label']}）：{m['returns']}")
    scope = "、".join(allowed_tickers[:20]) if allowed_tickers else "（无）"
    lines.append(f"可查标的范围：{scope}")
    lines.append(
        "如需补数据，输出如下块（可含多条，最多两轮、共 8 次取数）：\n"
        "[[DATA_REQ]]\n"
        '{"requests": [{"capability": "earnings", "ticker": "NVDA"}]}\n'
        "[[/DATA_REQ]]\n"
        "无需补数据则不要输出该块，直接给出最终回答。")
    return "\n".join(lines)


def extract_requests(text: str) -> tuple[list[dict], str]:
    """从模型输出剥离 [[DATA_REQ]] 块，返回 (请求列表, 去块后的文本)。

    无块 → ([], 原文)。块内 JSON 解析失败 → ([], 去块后文本)（fail-open，不阻塞）。
    """
    m = _REQ_RE.search(text or "")
    if not m:
        return [], text or ""
    stripped = _REQ_RE.sub("", text).strip()
    try:
        obj = extract_json_object(m.group(1))
        reqs = obj.get("requests", [])
        if not isinstance(reqs, list):
            return [], stripped
        return [r for r in reqs if isinstance(r, dict)], stripped
    except (ValueError, AttributeError):
        return [], stripped


def _validate(req: dict, market: str, valid_caps: set[str],
              allowed: set[str]) -> tuple[str, str, str]:
    """校验单条请求，返回 (capability, canonical_ticker, error)。error 非空即拒绝。"""
    cap = str(req.get("capability", "")).strip().lower()
    raw_tk = str(req.get("ticker", "")).strip()
    if cap not in valid_caps:
        return cap, "", f"能力 {cap or '(空)'} 不在可申请清单"
    if not raw_tk:
        return cap, "", "缺少 ticker"
    tk = normalize_ticker(raw_tk, market)
    if allowed and tk not in allowed:
        return cap, tk, f"标的 {tk} 不在可查范围（仅限观察池/持仓/候选池）"
    return cap, tk, ""


async def execute_requests(reqs: list[dict], *, market: str, user_id: str,
                           valid_caps: set[str], allowed_tickers: list[str],
                           spent: int) -> tuple[str, list[dict], int]:
    """执行一批请求，返回 (回注文本块, fetch_log, 新的已花费次数)。

    去重（同 capability+ticker 一次）、预算内、失败只影响单条（DataHub 内部已多源+熔断）。
    """
    hub = get_hub()
    allowed = {normalize_ticker(t, market) for t in (allowed_tickers or [])}
    seen: set[tuple[str, str]] = set()
    lines: list[str] = []
    log: list[dict] = []
    for req in reqs:
        if spent >= MAX_FETCH_CALLS:
            lines.append("【取数已达本次预算上限，其余请求跳过】")
            break
        cap, tk, err = _validate(req, market, valid_caps, allowed)
        if err:
            lines.append(f"【请求被拒】{cap}/{req.get('ticker', '')}: {err}")
            log.append({"capability": cap, "ticker": tk, "ok": False, "error": err})
            continue
        if (cap, tk) in seen:
            continue
        seen.add((cap, tk))
        spent += 1
        try:
            data = await hub.fetch(cap, tk, market, user_id)
        except Exception as e:  # noqa: BLE001  取数异常不崩，落失败条目
            data = None
            err = str(e)[:120]
        if data:
            blob = json.dumps(data, ensure_ascii=False, default=str)[:_RESULT_CHARS]
            lines.append(f"【数据补充·{cap}·{tk}】{blob}")
            log.append({"capability": cap, "ticker": tk, "ok": True})
        else:
            why = err or "无数据返回（可能该源无覆盖/退市停牌/已尝试全部候选源）"
            lines.append(f"【取数失败·{cap}·{tk}】{why}；该数据缺失，请据现有信息继续。")
            log.append({"capability": cap, "ticker": tk, "ok": False, "error": why})
    return "\n".join(lines), log, spent


async def negotiate(ask, base_prompt: str, *, market: str, user_id: str,
                    allowed_tickers: list[str]) -> tuple[str, list[dict], str]:
    """协商环：注入清单→调模型→有请求则取数回注→至多 MAX_ROUNDS 轮→返回 (最终文本, fetch_log, 补数据文本)。

    ask: async callable，ask(prompt:str)->str，由调用方提供（决策中心用 _llm_json_object 的
         原始文本路径、chat 用流式收集后的整段）。协商期不流式（需完整文本判有无请求块）。
    返回：最终模型输出文本（已剥离所有请求块）+ 全部 fetch_log（供 provenance/审计）+
         全部补数据回注文本（供 chat 并入 number_guard 白名单语料，防合法数字被误标"未核到"）。
    """
    if not ENABLED:
        _, stripped = extract_requests(await ask(base_prompt))  # 关也剥离，绝不漏块给用户
        return stripped, [], ""
    market = normalize_market(market)
    manifest = build_manifest(market, user_id)
    if not manifest:
        # 无可用能力 → 不协商，直接原 prompt 走一趟（仍剥离潜在残块）
        _, stripped = extract_requests(await ask(base_prompt))
        return stripped, [], ""
    valid_caps = {m["capability"] for m in manifest}
    inject = manifest_prompt(manifest, allowed_tickers)
    prompt = base_prompt + "\n\n" + inject
    fetch_log: list[dict] = []
    data_parts: list[str] = []
    spent = 0
    for round_i in range(MAX_ROUNDS + 1):  # 最后一轮只出最终答案、不再取数
        text = await ask(prompt)
        reqs, stripped = extract_requests(text)
        if not reqs or round_i >= MAX_ROUNDS or spent >= MAX_FETCH_CALLS:
            return stripped, fetch_log, "\n".join(data_parts)
        block, log, spent = await execute_requests(
            reqs, market=market, user_id=user_id, valid_caps=valid_caps,
            allowed_tickers=allowed_tickers, spent=spent)
        fetch_log.extend(log)
        data_parts.append(block)
        prompt = prompt + f"\n\n[第{round_i + 1}轮补充数据]\n{block}"
    return stripped, fetch_log, "\n".join(data_parts)


if __name__ == "__main__":  # assert 自检（假 hub 注入，GBK 控制台可直接跑）
    import asyncio

    import bottleneck_hunter.data_provider.hub as hubmod

    class _FakeHub:
        def available_capabilities(self, market, user_id=""):
            return {CAP_QUOTE, CAP_EARNINGS}  # 只开两项

        async def fetch(self, cap, ticker, market, user_id=""):
            if ticker == "NVDA" and cap == CAP_EARNINGS:
                return {"report_date": "2026-08-27", "eps_estimate": 1.2}
            if ticker == "FAIL":
                raise RuntimeError("boom")
            return None  # 其余无数据

    hubmod._hub = _FakeHub()

    # 1. 清单只含可用+已收录标签的能力
    man = build_manifest("us_stock", "u1")
    caps = {m["capability"] for m in man}
    assert caps == {CAP_QUOTE, CAP_EARNINGS}, caps

    # 2. 无块 → 无请求、原文返回
    reqs, txt = extract_requests("直接回答，无需补数据。")
    assert reqs == [] and "直接回答" in txt

    # 3. 有块 → 剥离 + 解析请求
    reqs, txt = extract_requests(
        '前言[[DATA_REQ]]{"requests":[{"capability":"earnings","ticker":"NVDA"}]}[[/DATA_REQ]]后语')
    assert len(reqs) == 1 and reqs[0]["ticker"] == "NVDA", reqs
    assert "前言" in txt and "DATA_REQ" not in txt

    # 4. 协商环：首轮发请求→取到 NVDA earnings→次轮出最终答案
    calls = {"n": 0}

    async def _ask(p):
        calls["n"] += 1
        if calls["n"] == 1:
            return '[[DATA_REQ]]{"requests":[{"capability":"earnings","ticker":"NVDA"}]}[[/DATA_REQ]]'
        assert "2026-08-27" in p, "补充数据未回注 prompt"  # 结果确实回注了
        return "综合财报日期，建议持有。"

    final, log, data_text = asyncio.run(negotiate(
        _ask, "分析 NVDA。", market="us_stock", user_id="u1", allowed_tickers=["NVDA"]))
    assert "建议持有" in final and calls["n"] == 2, (final, calls)
    assert log and log[0]["ok"] and log[0]["ticker"] == "NVDA", log
    assert "2026-08-27" in data_text, data_text  # 补数据文本回传（供 guard_corpus）

    # 5. 校验：能力不在清单 / 标的越界 → 拒绝，不消耗成功
    async def _ask_bad(p):
        if "第1轮补充数据" in p:
            return "据现有信息，维持中性。"
        return ('[[DATA_REQ]]{"requests":[{"capability":"insider","ticker":"NVDA"},'
                '{"capability":"earnings","ticker":"TSLA"}]}[[/DATA_REQ]]')

    final, log, _dt = asyncio.run(negotiate(
        _ask_bad, "分析。", market="us_stock", user_id="u1", allowed_tickers=["NVDA"]))
    assert all(not x["ok"] for x in log), log  # insider 不在清单、TSLA 越界，全拒
    assert "维持中性" in final

    # 6. 取数失败 → 失败条目回注、继续
    async def _ask_fail(p):
        if "第1轮补充数据" in p:  # 用轮次标记做哨兵，避开 manifest 文案里的"失败"字样
            return "该数据缺失，据现有信息维持观望。"
        return '[[DATA_REQ]]{"requests":[{"capability":"earnings","ticker":"FAIL"}]}[[/DATA_REQ]]'

    # FAIL 需在允许范围内才会真执行
    final, log, _dt = asyncio.run(negotiate(
        _ask_fail, "分析。", market="us_stock", user_id="u1", allowed_tickers=["FAIL"]))
    assert log and not log[0]["ok"] and "boom" in (log[0].get("error") or ""), log
    assert "观望" in final

    # 7. ENABLED=0 → 不注入清单、不取数，原 prompt 单趟（事故一键关）
    _saved = ENABLED
    globals()["ENABLED"] = False  # negotiate 读本模块全局；-m 下即 __main__.ENABLED
    try:
        seen_prompt = {"p": ""}

        async def _ask_off(p):
            seen_prompt["p"] = p
            return '[[DATA_REQ]]{"requests":[{"capability":"earnings","ticker":"NVDA"}]}[[/DATA_REQ]]留字'

        final, log, dt = asyncio.run(negotiate(
            _ask_off, "分析。", market="us_stock", user_id="u1", allowed_tickers=["NVDA"]))
        assert log == [] and dt == "", (log, dt)          # 关闭时零取数
        assert "可申请的实时数据能力" not in seen_prompt["p"]  # 未注入清单
        assert "留字" in final and "DATA_REQ" not in final    # 仍剥离块、返回文本
    finally:
        globals()["ENABLED"] = _saved

    print("ai_tools selfcheck OK")
