"""外部证据召回（券商研报 + 知识库 RAG）——把「凭模型记忆质疑」升级为「据研报质疑」。

统一入口，供 chain 交叉验证 / 投委会评审 / VIP 顾问三处复用。走 DataHub 的
CAP_RESEARCH / CAP_KB 能力：凭据、熔断、记账、用户隔离全部沿用 hub 既有机制
（Gangtise 凭据经 resolve_gangtise_credentials 受控解析，无凭据即返回空串，
上层行为与未接入前逐字节一致——纯增量，零回归）。
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_MAX_REPORTS = 3      # 研报取前 N（已按发布日降序）
_MAX_SNIPPETS = 4     # KB 片段取前 N
_BRIEF_CAP = 220      # 单条摘要/片段截断（控 prompt 体量）


def _clip(s: str, n: int = _BRIEF_CAP) -> str:
    s = (s or "").strip().replace("\n", " ")
    return s if len(s) <= n else s[:n] + "…"


async def gather_evidence(
    ticker: str,
    market: str,
    kb_query: str = "",
    *,
    user_id: str = "",
) -> str:
    """召回该标的近期研报摘要 + KB 片段，拼成「外部证据」文本块（无则空串）。

    - 研报：hub CAP_RESEARCH（按 ticker，中/外资研报深度/点评）。
    - 知识库：hub CAP_KB（按 kb_query 语义检索；缺省用 ticker）。
    任一异常/空结果都静默降级为空，绝不阻断验证主流程。
    """
    from bottleneck_hunter.data_provider.hub import CAP_KB, CAP_RESEARCH, get_hub
    hub = get_hub()
    lines: list[str] = []

    try:
        rr = await hub.fetch(CAP_RESEARCH, ticker, market, user_id)
    except Exception as e:  # noqa: BLE001
        logger.debug("gather_evidence 研报召回失败 %s: %s", ticker, e)
        rr = None
    reports = (rr or {}).get("reports") or []
    if reports:
        lines.append("### 近期券商研报")
        for r in reports[:_MAX_REPORTS]:
            broker = r.get("broker") or ""
            date = r.get("publish_date") or ""
            title = _clip(r.get("title") or "", 80)
            brief = _clip(r.get("brief") or "")
            head = " ".join(x for x in (date, broker, title) if x)
            lines.append(f"- {head}：{brief}" if brief else f"- {head}")

    try:
        kb = await hub.fetch(CAP_KB, kb_query or ticker, market, user_id)
    except Exception as e:  # noqa: BLE001
        logger.debug("gather_evidence KB 召回失败 %s: %s", ticker, e)
        kb = None
    snippets = (kb or {}).get("snippets") or []
    if snippets:
        lines.append("### 知识库片段")
        for s in snippets[:_MAX_SNIPPETS]:
            title = _clip(s.get("title") or "", 60)
            content = _clip(s.get("content") or "")
            lines.append(f"- {title}：{content}" if title else f"- {content}")

    if not lines:
        return ""
    return "## 外部证据（研报 + 知识库，作反方质疑依据）\n" + "\n".join(lines)


_NARRATIVE_CAP = 1600   # 一页通叙事整体截断（控报告体量）


async def gather_narrative(ticker: str, market: str, *, user_id: str = "") -> str:
    """召回该标的「一页通」AI 叙事，拼成报告增强段落（无则空串）。

    走 hub CAP_NARRATIVE（Gangtise agent，实测含目标价/机构观点等鲜活数据）。
    异常/空结果静默降级为空——默认关、按需开，绝不阻断报告主流程。
    """
    from bottleneck_hunter.data_provider.hub import CAP_NARRATIVE, get_hub
    try:
        r = await get_hub().fetch(CAP_NARRATIVE, ticker, market, user_id)
    except Exception as e:  # noqa: BLE001
        logger.debug("gather_narrative 叙事召回失败 %s: %s", ticker, e)
        r = None
    content = (r or {}).get("content") or ""
    content = content.strip()
    if not content:
        return ""
    return "## AI 研报叙事（一页通）\n" + _clip(content, _NARRATIVE_CAP)


def _demo() -> None:
    """自检：格式化与降级（全 mock，不打真实网络/hub）。"""
    import asyncio

    from bottleneck_hunter.data_provider import hub as hubmod

    class _FakeHub:
        async def fetch(self, cap, ticker, market, user_id=""):
            if cap == hubmod.CAP_RESEARCH:
                return {"reports": [
                    {"broker": "中金", "publish_date": "2026-08-20",
                     "title": "A" * 200, "brief": "看多" + "详" * 300},
                    {"broker": "华泰", "publish_date": "2026-08-18",
                     "title": "点评", "brief": ""},
                ]}
            if cap == hubmod.CAP_KB:
                return {"snippets": [{"title": "格局", "content": "竞争加剧" + "x" * 300}]}
            return None

    _orig = hubmod.get_hub
    hubmod.get_hub = lambda: _FakeHub()
    try:
        txt = asyncio.run(gather_evidence("600519", "a_stock", "贵州茅台 白酒 风险"))
        assert "外部证据" in txt and "中金" in txt and "华泰" in txt, txt
        assert "竞争加剧" in txt, txt
        # 截断生效：无超长原文透传
        assert "详" * 300 not in txt and len([ln for ln in txt.splitlines() if ln.startswith("- ")]) == 3, txt

        # 空返回 → 空串（降级路径，与未接入前一致）
        class _EmptyHub:
            async def fetch(self, *a, **k):
                return None
        hubmod.get_hub = lambda: _EmptyHub()
        assert asyncio.run(gather_evidence("X", "us_stock")) == ""

        # 异常 → 空串（不抛）
        class _BoomHub:
            async def fetch(self, *a, **k):
                raise RuntimeError("boom")
        hubmod.get_hub = lambda: _BoomHub()
        assert asyncio.run(gather_evidence("X", "us_stock")) == ""

        # 叙事：有内容 → 段落；空/异常 → 空串
        class _NarrHub:
            async def fetch(self, cap, *a, **k):
                return {"agent_type": "one-pager", "content": "目标价1572" + "叙" * 3000} \
                    if cap == hubmod.CAP_NARRATIVE else None
        hubmod.get_hub = lambda: _NarrHub()
        nt = asyncio.run(gather_narrative("600519", "a_stock"))
        assert "一页通" in nt and "目标价1572" in nt and "叙" * 3000 not in nt, nt
        hubmod.get_hub = lambda: _BoomHub()
        assert asyncio.run(gather_narrative("X", "us_stock")) == ""
        hubmod.get_hub = lambda: _EmptyHub()
        assert asyncio.run(gather_narrative("X", "us_stock")) == ""
    finally:
        hubmod.get_hub = _orig
    print("evidence demo: OK")


if __name__ == "__main__":
    _demo()
