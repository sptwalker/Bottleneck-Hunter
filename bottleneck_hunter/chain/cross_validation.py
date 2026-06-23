"""Multi-model cross-validation for investment logic.

Uses multiple LLMs to independently challenge the investment thesis
for each candidate supplier from adversarial angles.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from bottleneck_hunter.chain.json_utils import extract_json_object as _extract_json
from bottleneck_hunter.chain.models import (
    CrossValidationReport,
    ModelValidation,
    SupplierScorecard,
)
from bottleneck_hunter.llm_clients.factory import create_llm

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).parent / "prompts"

EVALUATION_QUESTIONS = [
    "核心竞争优势（技术壁垒、客户粘性、稀缺资源）是否真实且可持续？",
    "行业供需格局和成长空间的判断是否合理？有无被忽视的替代方案？",
    "公司的市场地位和客户关系是否稳固？是否存在大客户自研替代风险？",
    "当前估值相对于成长性和行业地位是否合理？",
    "主要风险（技术路线变化、政策变动、周期性）是否可控？",
]


def _load_prompt(name: str) -> str:
    path = PROMPTS_DIR / f"{name}.md"
    if path.exists():
        return path.read_text(encoding="utf-8")
    raise FileNotFoundError(f"Prompt file not found: {path}")


class CrossValidator:
    """Run multi-model cross-validation on supplier candidates."""

    def __init__(
        self,
        validation_models: list[dict[str, str]],
        language: str = "zh",
        pass_threshold: float = 0.5,
    ):
        """Args:
            validation_models: List of {"provider": "...", "model": "..."} configs.
            language: Output language.
            pass_threshold: Fraction of models that must pass for consensus "pass".
        """
        self.validation_models = validation_models
        self.language = language
        self.pass_threshold = pass_threshold
        self._system_prompt = _load_prompt("cross_validate")

    def _create_llms(self) -> list[tuple[str, BaseChatModel]]:
        """Instantiate LLM clients for each configured model."""
        llms = []
        for config in self.validation_models:
            provider = config["provider"]
            model = config["model"]
            name = f"{provider}/{model}"
            try:
                llm = create_llm(provider, model, temperature=0.3)
                llms.append((name, llm))
            except Exception as e:
                logger.warning(f"Failed to create LLM for {name}: {e}")
        return llms

    async def validate_supplier(
        self,
        scorecard: SupplierScorecard,
        llms: list[tuple[str, BaseChatModel]],
    ) -> CrossValidationReport:
        """Run cross-validation for a single supplier across all models."""
        supplier = scorecard.supplier
        bn_node = scorecard.bottleneck_node

        lang_note = "请用中文回答" if self.language == "zh" else "Answer in English"

        user_prompt = f"""{lang_note}

## 候选标的
- 公司名称: {supplier.name}
- 代码: {supplier.ticker}
- 市场: {supplier.market}
- 市值: {supplier.market_cap}
- 行业: {supplier.sector}

## 对应瓶颈环节
- 环节: {bn_node}
- 供应商评分: {scorecard.overall_score}/10
  - 市场地位: {scorecard.market_position}/10
  - 客户验证: {scorecard.customer_validation}/10
  - 产能状况: {scorecard.capacity_status}/10
  - 财务健康: {scorecard.financial_health}/10
  - 估值水平: {scorecard.valuation}/10
- 优势: {', '.join(scorecard.strengths)}
- 风险: {', '.join(scorecard.weaknesses)}

## 请从以下维度独立评估
{chr(10).join(f'{i+1}. {q}' for i, q in enumerate(EVALUATION_QUESTIONS))}

## 输出要求
综合评估后给出 1-10 分的推荐评分。返回严格 JSON（不要包含其他文字）:
{{
  "score": <1-10 整数>,
  "reasoning": "评分理由（2-3句话）",
  "concerns": ["具体顾虑1", "具体顾虑2"]
}}

评分标准:
- 8-10: 投资逻辑成立，核心优势明确，风险可控
- 5-7: 存在值得关注的顾虑，但非致命，需进一步研究
- 1-4: 发现重大逻辑缺陷或致命风险，不推荐

注意：请基于事实做出独立判断。如果公司确实具备明确的竞争优势和合理的估值，应该给出高分。"""

        async def _validate_one(name: str, llm: BaseChatModel) -> ModelValidation:
            try:
                response = await asyncio.wait_for(
                    llm.ainvoke(
                        [
                            SystemMessage(content=self._system_prompt),
                            HumanMessage(content=user_prompt),
                        ]
                    ),
                    timeout=120,
                )
                text = response.content.strip()
                data = _extract_json(text)

                raw_score = data.get("score", 5)
                score = max(1.0, min(10.0, float(raw_score)))

                return ModelValidation(
                    model_name=name,
                    score=score,
                    reasoning=data.get("reasoning", ""),
                    concerns=data.get("concerns", []),
                )
            except Exception:
                logger.exception(f"Validation failed for model {name}")
                return ModelValidation(
                    model_name=name,
                    score=5.0,
                    reasoning=f"模型 {name} 调用或解析失败，按中性处理",
                    concerns=["模型未能完成验证"],
                )

        # Run all models in parallel
        tasks = [_validate_one(name, llm) for name, llm in llms]
        validations = await asyncio.gather(*tasks)

        # Compute consensus: average score
        scores = [v.score for v in validations]
        avg_score = sum(scores) / len(scores) if scores else 5.0
        consensus_score = round(avg_score, 1)

        # Build consensus reasoning
        all_concerns = []
        for v in validations:
            if v.score < 7:
                all_concerns.extend(v.concerns)

        score_strs = [f"{v.model_name.split('/')[-1]}={v.score:.0f}" for v in validations]
        score_summary = "、".join(score_strs)

        if consensus_score >= 7.5:
            consensus_reasoning = (
                f"多数模型看好（{score_summary}，均分 {consensus_score}）。"
                f"投资逻辑整体成立，核心论点经得起质疑。"
            )
        elif consensus_score >= 5:
            consensus_reasoning = (
                f"模型观点分化（{score_summary}，均分 {consensus_score}）。"
                f"主要关注点：{'；'.join(all_concerns[:3]) or '无重大顾虑'}。建议进一步研究。"
            )
        else:
            consensus_reasoning = (
                f"多数模型不看好（{score_summary}，均分 {consensus_score}）。"
                f"主要问题：{'；'.join(all_concerns[:3]) or '整体逻辑存疑'}。不建议纳入。"
            )

        return CrossValidationReport(
            supplier_name=supplier.name,
            ticker=supplier.ticker,
            validations=list(validations),
            consensus_score=consensus_score,
            consensus_reasoning=consensus_reasoning,
            avg_score=consensus_score,
        )

    async def validate_all(
        self,
        scorecards: list[SupplierScorecard],
    ) -> list[CrossValidationReport]:
        """Run cross-validation on all scorecards.

        Only validates the top N suppliers to control API costs.
        """
        llms = self._create_llms()
        if not llms:
            logger.warning("No validation models available, skipping cross-validation")
            return []

        # Only validate top suppliers (by overall score)
        # Take top 10 max to control cost
        top_scorecards = sorted(scorecards, key=lambda sc: sc.overall_score, reverse=True)[:10]

        reports = []
        for sc in top_scorecards:
            report = await self.validate_supplier(sc, llms)
            reports.append(report)
            logger.info(
                f"Cross-validated {sc.supplier.name}: "
                f"avg_score={report.avg_score:.1f}"
            )

        return reports
