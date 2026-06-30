"""AI 投研圆桌会议 — 多角色比较性辩论选出最优投资标的。

4 个角色（成长/价值/风险/产业链）经过 3 轮讨论 + 总结，
从所有入围企业中横向比较，最终输出共识排名。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable, Coroutine

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from bottleneck_hunter.chain.json_utils import extract_json_object as _extract_json
from bottleneck_hunter.chain.models import (
    CrossValidationReport,
    MeetingMessage,
    MeetingRanking,
    RoundtableMeetingResult,
    SupplierScorecard,
)
from bottleneck_hunter.llm_clients.factory import create_llm

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).parent / "prompts"

ROLES = [
    {"id": "growth", "name": "成长型投资者", "avatar_letter": "成", "color": "#10a37f", "prompt_file": "roundtable_growth"},
    {"id": "value",  "name": "价值型投资者", "avatar_letter": "价", "color": "#d97706", "prompt_file": "roundtable_value"},
    {"id": "risk",   "name": "风险分析师",   "avatar_letter": "风", "color": "#dc2626", "prompt_file": "roundtable_risk"},
    {"id": "chain",  "name": "产业链专家",   "avatar_letter": "链", "color": "#6366f1", "prompt_file": "roundtable_chain"},
]

MeetingCallback = Callable[[str, dict[str, Any]], Coroutine[Any, Any, None]]


def _load_prompt(name: str) -> str:
    path = PROMPTS_DIR / f"{name}.md"
    if path.exists():
        return path.read_text(encoding="utf-8")
    raise FileNotFoundError(f"Prompt file not found: {path}")


class RoundtableMeeting:
    """AI 投研圆桌会议编排器。"""

    def __init__(
        self,
        validation_models: list[dict[str, str]],
        language: str = "zh",
        role_assignments: dict[str, dict[str, str]] | None = None,
    ):
        self.validation_models = validation_models
        self.language = language
        self.role_assignments = role_assignments
        self._role_prompts: dict[str, str] = {}
        for role in ROLES:
            self._role_prompts[role["id"]] = _load_prompt(role["prompt_file"])
        self._summary_prompt = _load_prompt("roundtable_summary")

    def _create_llms(self) -> list[tuple[str, BaseChatModel]]:
        llms = []
        for config in self.validation_models:
            provider = config["provider"]
            model = config["model"]
            name = f"{provider}/{model}"
            try:
                llm = create_llm(provider, model, temperature=0.5)
                llms.append((name, llm))
            except Exception as e:
                logger.warning(f"Failed to create LLM for {name}: {e}")
        return llms

    def _assign_roles(self, llms: list[tuple[str, BaseChatModel]]) -> dict[str, tuple[str, BaseChatModel]]:
        """分配 LLM 到 4 个角色。优先使用 role_assignments 指定分配，否则 round-robin。"""
        assignments: dict[str, tuple[str, BaseChatModel]] = {}

        if self.role_assignments:
            for role in ROLES:
                role_id = role["id"]
                if role_id in self.role_assignments:
                    ra = self.role_assignments[role_id]
                    provider = ra.get("provider", "")
                    model = ra.get("model", "")
                    name = f"{provider}/{model}"
                    try:
                        llm = create_llm(provider, model, temperature=0.5)
                        assignments[role_id] = (name, llm)
                        continue
                    except Exception as e:
                        logger.warning(f"指定模型创建失败 ({name}): {e}，回退到 round-robin")
                idx = list(r["id"] for r in ROLES).index(role_id)
                model_name, llm = llms[idx % len(llms)]
                assignments[role_id] = (model_name, llm)
        else:
            for i, role in enumerate(ROLES):
                model_name, llm = llms[i % len(llms)]
                assignments[role["id"]] = (model_name, llm)

        return assignments

    def _build_agenda(self, config: dict | None, company_count: int) -> str:
        """Layer A：会议议程 + 分析方法论。"""
        sector = config.get("sector", "") if config else ""
        end_product = config.get("end_product", "") if config else ""
        product_desc = f"（{sector} — {end_product}）" if sector and end_product else ""

        return f"""# 会议背景

## 分析方法论
本次分析采用"产业链瓶颈选股三步法"{product_desc}：
1. 产业链拆解 — 从终端产品逐层分解到核心零部件和原材料，识别关键环节
2. 瓶颈识别 — 在各层中找出高集中度、高壁垒、供需紧张的瓶颈环节
3. 供应商筛选 — 在瓶颈环节中找到被市场忽视的优质供应商，通过五维评估和交叉验证筛选

## 会议目标
从 {company_count} 家入围企业中，通过多角色横向比较，选出最值得投资的标的。
各位需从成长性、估值安全边际、风险因素、产业链卡位等角度进行比较性辩论。
"""

    def _build_chain_overview(
        self, chain_data: dict | None, bottleneck_reports: list[dict] | None
    ) -> str:
        """Layer B：产业链全景 + 瓶颈概览。"""
        if not chain_data:
            return ""

        lines = ["\n# 产业链结构与瓶颈分析\n"]

        layers = chain_data.get("layers", [])
        if layers:
            lines.append("## 层级结构")
            for layer in layers:
                layer_name = layer.get("layer_name", "")
                nodes = layer.get("nodes", [])
                node_names = [n.get("name", "") for n in nodes if n.get("name")]
                if node_names:
                    lines.append(f"- **{layer_name}**: {', '.join(node_names[:8])}")
            lines.append("")

        if bottleneck_reports:
            lines.append("## 已识别瓶颈环节")
            for i, rpt in enumerate(bottleneck_reports[:6], 1):
                name = rpt.get("node_name", "")
                score = rpt.get("overall_score", 0)
                insights = rpt.get("key_insights", [])
                insight_str = "; ".join(insights[:2]) if insights else ""
                lines.append(f"{i}. **{name}** — 瓶颈得分 {score:.1f}/10")
                if insight_str:
                    lines.append(f"   关键洞察: {insight_str}")
            lines.append("")

        return "\n".join(lines)

    def _build_company_briefing(
        self,
        scorecards: list[SupplierScorecard],
        cv_reports: list[CrossValidationReport],
    ) -> str:
        """Layer C：增强版企业档案。"""
        cv_map = {r.ticker: r for r in cv_reports}
        lines = [f"\n# 入围企业详细档案（共 {len(scorecards)} 家）\n"]

        for i, sc in enumerate(scorecards, 1):
            sup = sc.supplier
            cv = cv_map.get(sup.ticker)
            cv_score = f"{cv.consensus_score:.1f}" if cv else "N/A"

            fin = sc.financial_snapshot
            fin_parts = []
            if sup.market_cap is not None:
                fin_parts.append(f"市值{sup.market_cap:.0f}亿")
            if fin:
                if fin.consensus_pe is not None:
                    fin_parts.append(f"PE={fin.consensus_pe:.1f}")
                if fin.gross_margin_pct is not None:
                    fin_parts.append(f"毛利率{fin.gross_margin_pct:.1f}%")
                if fin.roe_pct is not None:
                    fin_parts.append(f"ROE={fin.roe_pct:.1f}%")
                if fin.revenue_yoy_pct is not None:
                    fin_parts.append(f"营收增速{fin.revenue_yoy_pct:+.1f}%")
                if fin.net_profit_yoy_pct is not None:
                    fin_parts.append(f"净利润增速{fin.net_profit_yoy_pct:+.1f}%")
                if fin.debt_ratio_pct is not None:
                    fin_parts.append(f"资产负债率{fin.debt_ratio_pct:.1f}%")
            fin_str = " | ".join(fin_parts) if fin_parts else "财务数据暂缺"

            moat_str = ""
            if sc.moat:
                m = sc.moat
                moat_str = (f" | 护城河: {m.overall_moat:.1f}"
                            f"（专利{m.patent_moat:.0f} 转换成本{m.switching_cost:.0f}"
                            f" 产能{m.capacity_lead_time:.0f} 成本{m.cost_advantage:.0f}）")
                if m.moat_reasoning:
                    moat_str += f"\n  护城河总结: {m.moat_reasoning}"

            cat_str = ""
            if sc.catalyst and sc.catalyst.events:
                events = sc.catalyst.events[:3]
                cat_parts = [f"{e.description[:30]}(紧迫度{e.impact_score:.0f})" for e in events]
                cat_str = f" | 催化剂: {'; '.join(cat_parts)}"

            final_score = ""
            if sc.final:
                final_score = f" | 最终评分: {sc.final.final_score:.1f}（质量{sc.final.quality_score:.1f} × 预期差{sc.final.alpha_score:.1f}）"

            alpha_str = ""
            if sc.alpha:
                alpha_str = f" | 预期差: {sc.alpha.alpha_score:.1f}"

            lines.append(f"## {i}. {sup.name} ({sup.ticker}) — {sc.bottleneck_node}")
            lines.append(f"- 综合评分: {sc.overall_score:.1f}{final_score} | 交叉验证: {cv_score}{alpha_str}")
            lines.append(f"- {fin_str}{moat_str}")
            if cat_str:
                lines.append(f"- {cat_str.lstrip(' | ')}")
            lines.append(f"- 五维: 市场{sc.market_position:.1f} 客户{sc.customer_validation:.1f} "
                         f"产能{sc.capacity_status:.1f} 财务{sc.financial_health:.1f} 估值{sc.valuation:.1f}")
            lines.append(f"- 优势: {', '.join(sc.strengths[:3])}")
            lines.append(f"- 风险: {', '.join(sc.weaknesses[:3])}")

            if cv and cv.validations:
                cv_detail = "; ".join(f"{s.model_name}: {s.score:.1f}" for s in cv.validations[:4])
                lines.append(f"- 交叉验证明细: {cv_detail}")

            lines.append("")

        return "\n".join(lines)

    def _build_participants_info(self, assignments: dict[str, tuple[str, BaseChatModel]]) -> list[dict]:
        result = []
        for role in ROLES:
            model_name, _ = assignments[role["id"]]
            result.append({
                "role": role["id"],
                "name": role["name"],
                "model_name": model_name,
                "avatar_letter": role["avatar_letter"],
                "color": role["color"],
            })
        return result

    async def _invoke_llm(
        self, llm: BaseChatModel, system: str, user: str, timeout: int = 180
    ) -> str:
        response = await asyncio.wait_for(
            llm.ainvoke([SystemMessage(content=system), HumanMessage(content=user)]),
            timeout=timeout,
        )
        return response.content.strip()

    async def run_round1(
        self,
        assignments: dict[str, tuple[str, BaseChatModel]],
        briefing: str,
        callback: MeetingCallback | None = None,
    ) -> list[MeetingMessage]:
        """第 1 轮：独立提名（并行）。"""
        lang_note = "请用中文回答。" if self.language == "zh" else "Answer in English."
        user_prompt = f"""{lang_note}

{briefing}

请按照 **第 1 轮：独立提名** 的格式要求返回 JSON。"""

        messages: list[MeetingMessage] = []

        async def _run_one(role_id: str) -> MeetingMessage:
            model_name, llm = assignments[role_id]
            role_info = next(r for r in ROLES if r["id"] == role_id)
            system_prompt = self._role_prompts[role_id]

            try:
                text = await self._invoke_llm(llm, system_prompt, user_prompt)
                data = _extract_json(text)
                speech = data.get("speech", text[:500])
            except Exception as e:
                logger.exception(f"Round 1 failed for {role_id}")
                speech = f"[{role_info['name']}] 发言生成失败: {e}"
                data = None

            msg = MeetingMessage(
                round_num=1,
                role=role_id,
                participant_name=role_info["name"],
                model_name=model_name,
                content=speech,
                structured_data=data,
            )
            if callback:
                await callback("meeting_message", {
                    "round_num": 1,
                    "role": role_id,
                    "participant_name": role_info["name"],
                    "content": speech,
                    "model_name": model_name,
                    "avatar_letter": role_info["avatar_letter"],
                    "color": role_info["color"],
                })
            return msg

        tasks = [_run_one(role["id"]) for role in ROLES]
        results = await asyncio.gather(*tasks)
        messages.extend(results)
        return messages

    async def run_round2(
        self,
        assignments: dict[str, tuple[str, BaseChatModel]],
        briefing: str,
        round1_msgs: list[MeetingMessage],
        callback: MeetingCallback | None = None,
    ) -> list[MeetingMessage]:
        """第 2 轮：辩论与质疑（顺序执行，后发者能看到前面内容）。"""
        lang_note = "请用中文回答。" if self.language == "zh" else "Answer in English."

        round1_summary = "\n\n".join(
            f"### {m.participant_name}（{m.role}）的第 1 轮发言\n{m.content}"
            for m in round1_msgs
        )

        messages: list[MeetingMessage] = []
        debate_so_far = ""

        for role in ROLES:
            role_id = role["id"]
            model_name, llm = assignments[role_id]
            system_prompt = self._role_prompts[role_id]

            debate_context = ""
            if debate_so_far:
                debate_context = f"\n\n## 本轮已有发言\n{debate_so_far}"

            user_prompt = f"""{lang_note}

{briefing}

## 第 1 轮各方发言
{round1_summary}
{debate_context}

请按照 **第 2 轮：辩论与质疑** 的格式要求返回 JSON。"""

            try:
                text = await self._invoke_llm(llm, system_prompt, user_prompt)
                data = _extract_json(text)
                speech = data.get("speech", text[:500])
            except Exception as e:
                logger.exception(f"Round 2 failed for {role_id}")
                speech = f"[{role['name']}] 辩论发言生成失败: {e}"
                data = None

            msg = MeetingMessage(
                round_num=2,
                role=role_id,
                participant_name=role["name"],
                model_name=model_name,
                content=speech,
                structured_data=data,
            )
            messages.append(msg)
            debate_so_far += f"\n### {role['name']}（{role_id}）的辩论发言\n{speech}\n"

            if callback:
                await callback("meeting_message", {
                    "round_num": 2,
                    "role": role_id,
                    "participant_name": role["name"],
                    "content": speech,
                    "model_name": model_name,
                    "avatar_letter": role["avatar_letter"],
                    "color": role["color"],
                })

        return messages

    def compute_borda(self, round2_msgs: list[MeetingMessage]) -> list[MeetingRanking]:
        """Score-Weighted Ranking: 使用 0-100 绝对信心分 + 保留 Borda 作为辅助。"""
        points: dict[str, int] = defaultdict(int)
        weighted_scores: dict[str, list[float]] = defaultdict(list)
        supporters: dict[str, int] = defaultdict(int)
        supporter_roles: dict[str, list[str]] = defaultdict(list)
        name_map: dict[str, str] = {}

        borda_weights = {1: 3, 2: 2, 3: 1}
        all_roles = set()

        for msg in round2_msgs:
            all_roles.add(msg.role)
            data = msg.structured_data
            if not data:
                continue
            ranking = data.get("revised_ranking", [])
            for entry in ranking:
                rank = entry.get("rank", 0)
                ticker = entry.get("ticker", "")
                name = entry.get("name", ticker)
                score = entry.get("score", 0)
                if rank in borda_weights and ticker:
                    points[ticker] += borda_weights[rank]
                    name_map[ticker] = name
                    if score > 0:
                        weighted_scores[ticker].append(float(score))
                    else:
                        weighted_scores[ticker].append(borda_weights[rank] * 33.0)
                    if rank == 1:
                        supporters[ticker] += 1
                    if msg.role not in supporter_roles[ticker]:
                        supporter_roles[ticker].append(msg.role)

        def _avg_score(ticker: str) -> float:
            scores = weighted_scores.get(ticker, [])
            return sum(scores) / len(scores) if scores else 0.0

        sorted_tickers = sorted(
            points.keys(),
            key=lambda t: (_avg_score(t), points[t]),
            reverse=True,
        )
        rankings = []
        for i, ticker in enumerate(sorted_tickers, 1):
            sup = supporter_roles.get(ticker, [])
            opp = [r for r in all_roles if r not in sup]
            rankings.append(MeetingRanking(
                rank=i,
                ticker=ticker,
                name=name_map.get(ticker, ticker),
                borda_points=points[ticker],
                weighted_score=round(_avg_score(ticker), 1),
                supporter_count=supporters.get(ticker, 0),
                supporters=sup,
                opposers=opp,
            ))
        return rankings

    async def run_summary(
        self,
        assignments: dict[str, tuple[str, BaseChatModel]],
        all_msgs: list[MeetingMessage],
        borda_ranking: list[MeetingRanking],
        callback: MeetingCallback | None = None,
    ) -> tuple[MeetingMessage, dict]:
        """第 3 轮：主席总结。"""
        lang_note = "请用中文回答。" if self.language == "zh" else "Answer in English."

        transcript = "\n\n".join(
            f"### [{m.participant_name}] 第 {m.round_num} 轮\n{m.content}"
            for m in all_msgs if m.role != "host"
        )
        borda_str = "\n".join(
            f"{r.rank}. {r.name}({r.ticker}) — 加权分 {r.weighted_score} / Borda {r.borda_points}分"
            for r in borda_ranking
        )

        user_prompt = f"""{lang_note}

## 完整会议记录
{transcript}

## Borda 计分排名
{borda_str}

请按输出要求返回 JSON。排名应覆盖所有得分企业。"""

        # 用第一个 LLM 做总结
        first_role = ROLES[0]["id"]
        model_name, llm = assignments[first_role]

        try:
            text = await self._invoke_llm(llm, self._summary_prompt, user_prompt, timeout=240)
            data = _extract_json(text)
        except Exception as e:
            logger.exception("Meeting summary failed")
            data = {
                "final_ranking": [r.model_dump() for r in borda_ranking],
                "key_agreements": [],
                "key_disagreements": [],
                "risk_warnings": [f"总结生成失败: {e}"],
                "investment_thesis": "会议总结生成失败，请参考 Borda 排名。",
                "speech": f"会议总结生成失败: {e}",
            }

        speech = data.get("speech", "会议总结完成。")
        msg = MeetingMessage(
            round_num=3,
            role="host",
            participant_name="主持人",
            model_name=model_name,
            content=speech,
            structured_data=data,
        )

        if callback:
            await callback("meeting_message", {
                "round_num": 3,
                "role": "host",
                "participant_name": "主持人",
                "content": speech,
                "model_name": model_name,
                "avatar_letter": "主",
                "color": "#64748b",
            })

        return msg, data

    async def run(
        self,
        scorecards: list[SupplierScorecard],
        cv_reports: list[CrossValidationReport],
        *,
        chain_data: dict | None = None,
        bottleneck_reports: list[dict] | None = None,
        analysis_config: dict | None = None,
        market_data_text: str = "",
        callback: MeetingCallback | None = None,
    ) -> RoundtableMeetingResult:
        """运行完整圆桌会议。"""
        llms = self._create_llms()
        if not llms:
            raise ValueError("没有可用的 LLM 模型")

        assignments = self._assign_roles(llms)
        participants = self._build_participants_info(assignments)

        agenda = self._build_agenda(analysis_config, len(scorecards))
        chain_overview = self._build_chain_overview(chain_data, bottleneck_reports)
        company_briefing = self._build_company_briefing(scorecards, cv_reports)
        briefing = agenda + chain_overview + company_briefing
        if market_data_text:
            briefing += "\n" + market_data_text

        transcript: list[MeetingMessage] = []

        if callback:
            await callback("meeting_start", {
                "participants": participants,
                "company_count": len(scorecards),
            })

        # 第 0 轮：主持人开场
        opening = MeetingMessage(
            round_num=0,
            role="host",
            participant_name="主持人",
            content=f"欢迎参加本次 AI 投研圆桌会议。今天我们将讨论 {len(scorecards)} 家入围企业，"
                    f"请各位从各自视角选出最值得投资的前三名，并说明为什么它们优于其他候选公司。",
        )
        transcript.append(opening)
        if callback:
            await callback("meeting_message", {
                "round_num": 0,
                "role": "host",
                "participant_name": "主持人",
                "content": opening.content,
                "model_name": "",
                "avatar_letter": "主",
                "color": "#64748b",
            })

        # 第 1 轮：独立提名
        if callback:
            await callback("meeting_round", {"round_num": 1, "round_name": "独立提名"})
        round1_msgs = await self.run_round1(assignments, briefing, callback)
        transcript.extend(round1_msgs)

        # 第 2 轮：辩论
        if callback:
            await callback("meeting_round", {"round_num": 2, "round_name": "辩论与质疑"})
        round2_msgs = await self.run_round2(assignments, briefing, round1_msgs, callback)
        transcript.extend(round2_msgs)

        # Borda 计分
        borda_ranking = self.compute_borda(round2_msgs)
        if callback:
            await callback("meeting_ranking", {
                "ranking": [r.model_dump() for r in borda_ranking],
            })

        # 第 3 轮：总结
        if callback:
            await callback("meeting_round", {"round_num": 3, "round_name": "会议总结"})
        summary_msg, summary_data = await self.run_summary(assignments, transcript, borda_ranking, callback)
        transcript.append(summary_msg)

        # 用 LLM 总结更新最终排名
        final_ranking_data = summary_data.get("final_ranking", [])
        final_ranking = []
        for i, entry in enumerate(final_ranking_data, 1):
            ticker = entry.get("ticker", "")
            borda_entry = next((b for b in borda_ranking if b.ticker == ticker), None)
            final_ranking.append(MeetingRanking(
                rank=i,
                ticker=ticker,
                name=entry.get("name", ticker),
                borda_points=borda_entry.borda_points if borda_entry else 0,
                supporter_count=borda_entry.supporter_count if borda_entry else 0,
                supporters=borda_entry.supporters if borda_entry else [],
                opposers=borda_entry.opposers if borda_entry else [],
                reasoning=entry.get("reasoning", ""),
            ))
        if not final_ranking:
            final_ranking = borda_ranking

        result = RoundtableMeetingResult(
            participants=participants,
            transcript=transcript,
            final_ranking=final_ranking,
            key_agreements=summary_data.get("key_agreements", []),
            key_disagreements=summary_data.get("key_disagreements", []),
            risk_warnings=summary_data.get("risk_warnings", []),
            investment_thesis=summary_data.get("investment_thesis", ""),
        )

        if callback:
            await callback("meeting_complete", {"result": result.model_dump()})

        return result
