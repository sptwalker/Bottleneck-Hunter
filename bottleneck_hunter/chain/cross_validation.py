"""Multi-model cross-validation for investment logic.

Uses multiple LLMs to independently challenge the investment thesis
for each candidate supplier from adversarial angles.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import Counter
from pathlib import Path

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from bottleneck_hunter.chain.models import (
    CrossValidationReport,
    ModelValidation,
    SupplierScorecard,
    ValidationResult,
)
from bottleneck_hunter.llm_clients.factory import create_llm

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).parent / "prompts"

ADVERSARIAL_QUESTIONS = [
    "这个稀缺性是真的唯一吗？还是存在你不知道的替代方案？",
    "技术路线是否会被颠覆？新一代技术是否会绕过这个环节？",
    "产能不足的判断是否可靠？扩产周期是否比预期短？",
    "客户验证信息是否过时？大客户是否可能自研替代？",
    "有没有地缘政治风险？贸易限制或制裁是否影响供应？",
    "估值是否已经反映了这些利好？市场是否已经充分认知？",
    "行业周期是否见顶？需求增长是否可持续？",
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
            except Exception:
                logger.warning(f"Failed to create LLM for {name}, skipping")
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

## 反面拷问角度
请从以下角度逐一质疑投资逻辑:
{chr(10).join(f'{i+1}. {q}' for i, q in enumerate(ADVERSARIAL_QUESTIONS))}

## 输出要求
综合判断后给出最终结论。返回严格 JSON:
{{
  "result": "pass" 或 "concern" 或 "fail",
  "reasoning": "综合判断理由（2-3句话）",
  "concerns": ["具体顾虑1", "具体顾虑2"]
}}

- "pass": 投资逻辑成立，核心论点经得起质疑
- "concern": 存在值得关注的顾虑，需要进一步研究
- "fail": 投资逻辑存在重大缺陷，不推荐"""

        async def _validate_one(name: str, llm: BaseChatModel) -> ModelValidation:
            try:
                response = await llm.ainvoke(
                    [
                        SystemMessage(content=self._system_prompt),
                        HumanMessage(content=user_prompt),
                    ]
                )
                text = response.content.strip()
                text = self._strip_fences(text)
                data = json.loads(text)

                result_str = data.get("result", "concern")
                try:
                    result = ValidationResult(result_str)
                except ValueError:
                    result = ValidationResult.CONCERN

                return ModelValidation(
                    model_name=name,
                    result=result,
                    reasoning=data.get("reasoning", ""),
                    concerns=data.get("concerns", []),
                )
            except Exception:
                logger.exception(f"Validation failed for model {name}")
                return ModelValidation(
                    model_name=name,
                    result=ValidationResult.FAIL,
                    reasoning="模型调用失败",
                    concerns=["无法完成验证"],
                )

        # Run all models in parallel
        tasks = [_validate_one(name, llm) for name, llm in llms]
        validations = await asyncio.gather(*tasks)

        # Compute consensus
        result_counts = Counter(v.result for v in validations)
        total = len(validations)
        pass_count = result_counts.get(ValidationResult.PASS, 0)
        fail_count = result_counts.get(ValidationResult.FAIL, 0)
        pass_rate = pass_count / total if total else 0

        if pass_rate >= self.pass_threshold and fail_count == 0:
            consensus = ValidationResult.PASS
        elif fail_count > pass_count:
            consensus = ValidationResult.FAIL
        else:
            consensus = ValidationResult.CONCERN

        # Build consensus reasoning
        all_concerns = []
        for v in validations:
            if v.result != ValidationResult.PASS:
                all_concerns.extend(v.concerns)

        if consensus == ValidationResult.PASS:
            consensus_reasoning = (
                f"{pass_count}/{total} 个模型通过验证。"
                f"投资逻辑整体成立，核心论点经得起质疑。"
            )
        elif consensus == ValidationResult.CONCERN:
            consensus_reasoning = (
                f"部分模型存在顾虑（通过 {pass_count}/{total}）。"
                f"主要关注点：{'；'.join(all_concerns[:3])}。建议进一步研究。"
            )
        else:
            consensus_reasoning = (
                f"多数模型否定（通过 {pass_count}/{total}）。"
                f"主要问题：{'；'.join(all_concerns[:3])}。不建议纳入。"
            )

        return CrossValidationReport(
            supplier_name=supplier.name,
            ticker=supplier.ticker,
            validations=list(validations),
            consensus=consensus,
            consensus_reasoning=consensus_reasoning,
            pass_rate=pass_rate,
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
                f"{report.consensus} (pass_rate={report.pass_rate:.0%})"
            )

        return reports

    @staticmethod
    def _strip_fences(text: str) -> str:
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:])
            if text.endswith("```"):
                text = text[:-3]
        return text.strip()
