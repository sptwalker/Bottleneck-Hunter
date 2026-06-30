"""Multi-model cross-validation for investment logic.

Uses multiple LLMs to independently challenge the investment thesis
for each candidate supplier from adversarial angles.

Upgraded with:
- Differentiated perspective input (4 viewpoints)
- Weighted consensus with trimmed mean
- Outlier challenge round
- Fatal risk kill-zone veto
"""

from __future__ import annotations

import asyncio
import json
import logging
import statistics
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

PERSPECTIVES = ["financial", "chain", "sentiment", "blind"]

PERSPECTIVE_PROMPTS = {
    "financial": "cv_financial_auditor",
    "chain": "cv_chain_analyst",
    "sentiment": "cv_sentiment_observer",
    "blind": "cv_blind_test",
}


def _load_prompt(name: str) -> str:
    path = PROMPTS_DIR / f"{name}.md"
    if path.exists():
        return path.read_text(encoding="utf-8")
    raise FileNotFoundError(f"Prompt file not found: {path}")


def _build_financial_prompt(sc: SupplierScorecard, lang_note: str) -> str:
    s = sc.supplier
    return f"""{lang_note}

## 候选标的
- 公司名称: {s.name}
- 代码: {s.ticker}
- 市场: {s.market}
- 市值: {s.market_cap}
- 行业: {s.sector}

## 财务数据
- 财务健康评分: {sc.financial_health}/10
- 估值评分: {sc.valuation}/10
- 综合评分: {sc.overall_score}/10

请基于上述财务信息独立评估。"""


def _build_chain_prompt(sc: SupplierScorecard, lang_note: str) -> str:
    s = sc.supplier
    return f"""{lang_note}

## 候选标的
- 公司名称: {s.name}
- 代码: {s.ticker}
- 市场: {s.market}
- 行业: {s.sector}

## 产业链数据
- 瓶颈环节: {sc.bottleneck_node}
- 市场地位评分: {sc.market_position}/10
- 客户验证评分: {sc.customer_validation}/10
- 产能状况评分: {sc.capacity_status}/10
- 优势: {', '.join(sc.strengths)}
- 风险: {', '.join(sc.weaknesses)}

请基于上述产业链信息独立评估。"""


def _build_sentiment_prompt(sc: SupplierScorecard, lang_note: str) -> str:
    s = sc.supplier
    return f"""{lang_note}

## 候选标的
- 公司名称: {s.name}
- 代码: {s.ticker}
- 市场: {s.market}
- 行业: {s.sector}
- 市值: {s.market_cap}

## 市场信号
- 综合评分: {sc.overall_score}/10
- 优势: {', '.join(sc.strengths)}
- 风险: {', '.join(sc.weaknesses)}

请基于上述市场信号和你对该公司的了解独立评估。"""


def _build_blind_prompt(sc: SupplierScorecard, lang_note: str) -> str:
    s = sc.supplier
    return f"""{lang_note}

## 候选标的（盲测）
- 公司名称: {s.name}
- 行业: {s.sector}
- 市场: {s.market}

请仅基于你对该公司和行业的已有知识独立评估其投资价值。"""


PERSPECTIVE_BUILDERS = {
    "financial": _build_financial_prompt,
    "chain": _build_chain_prompt,
    "sentiment": _build_sentiment_prompt,
    "blind": _build_blind_prompt,
}


class CrossValidator:
    """Run multi-model cross-validation on supplier candidates."""

    def __init__(
        self,
        validation_models: list[dict[str, str]],
        language: str = "zh",
        pass_threshold: float = 0.5,
    ):
        self.validation_models = validation_models
        self.language = language
        self.pass_threshold = pass_threshold
        self._perspective_prompts: dict[str, str] = {}
        for pkey, pname in PERSPECTIVE_PROMPTS.items():
            try:
                self._perspective_prompts[pkey] = _load_prompt(pname)
            except FileNotFoundError:
                self._perspective_prompts[pkey] = _load_prompt("cross_validate")

    def _create_llms(self) -> list[tuple[str, BaseChatModel]]:
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

    def _assign_perspectives(self, llm_count: int) -> list[str]:
        assigned = []
        for i in range(llm_count):
            assigned.append(PERSPECTIVES[i % len(PERSPECTIVES)])
        return assigned

    async def validate_supplier(
        self,
        scorecard: SupplierScorecard,
        llms: list[tuple[str, BaseChatModel]],
    ) -> CrossValidationReport:
        lang_note = "请用中文回答" if self.language == "zh" else "Answer in English"
        perspectives = self._assign_perspectives(len(llms))

        async def _validate_one(
            name: str, llm: BaseChatModel, perspective: str
        ) -> ModelValidation:
            sys_prompt = self._perspective_prompts.get(
                perspective, self._perspective_prompts.get("financial", "")
            )
            builder = PERSPECTIVE_BUILDERS.get(perspective, _build_financial_prompt)
            user_prompt = builder(scorecard, lang_note)

            try:
                response = await asyncio.wait_for(
                    llm.ainvoke([
                        SystemMessage(content=sys_prompt),
                        HumanMessage(content=user_prompt),
                    ]),
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
                    perspective=perspective,
                    fatal_risk=bool(data.get("fatal_risk", False)),
                    fatal_reason=data.get("fatal_reason", ""),
                )
            except Exception:
                logger.exception(f"Validation failed for model {name}")
                return ModelValidation(
                    model_name=name,
                    score=5.0,
                    reasoning=f"模型 {name} 调用或解析失败，按中性处理",
                    concerns=["模型未能完成验证"],
                    perspective=perspective,
                )

        tasks = [
            _validate_one(name, llm, persp)
            for (name, llm), persp in zip(llms, perspectives)
        ]
        validations = list(await asyncio.gather(*tasks))

        scores = [v.score for v in validations]
        raw_avg = sum(scores) / len(scores) if scores else 5.0

        if len(scores) >= 4:
            sorted_scores = sorted(scores)
            trimmed = sorted_scores[1:-1]
            trimmed_avg = sum(trimmed) / len(trimmed)
        else:
            trimmed_avg = statistics.median(scores) if scores else 5.0

        consensus_score = round(trimmed_avg, 1)

        fatal_risks = []
        for v in validations:
            if v.fatal_risk and v.fatal_reason:
                fatal_risks.append(f"{v.model_name}({v.perspective}): {v.fatal_reason}")
        has_fatal_risk = len(fatal_risks) > 0

        outlier_challenges: list[dict] = []
        if len(scores) >= 3:
            median_score = statistics.median(scores)
            outliers = [
                v for v in validations if abs(v.score - median_score) >= 3.0
            ]
            for ov in outliers:
                outlier_challenges.append({
                    "model": ov.model_name,
                    "perspective": ov.perspective,
                    "score": ov.score,
                    "median": median_score,
                    "deviation": round(ov.score - median_score, 1),
                    "reasoning": ov.reasoning,
                    "concerns": ov.concerns,
                    "status": "flagged",
                })

        if has_fatal_risk:
            consensus_score = min(consensus_score, 3.0)

        all_concerns = []
        for v in validations:
            if v.score < 7:
                all_concerns.extend(v.concerns)

        score_strs = [
            f"{v.model_name.split('/')[-1]}[{v.perspective}]={v.score:.0f}"
            for v in validations
        ]
        score_summary = "、".join(score_strs)

        if has_fatal_risk:
            consensus_reasoning = (
                f"⚠️ 触发致命风险一票否决（{score_summary}）。"
                f"致命风险：{'；'.join(fatal_risks)}。"
                f"共识分被限制为 {consensus_score}，需人工复核。"
            )
        elif outlier_challenges:
            outlier_info = "、".join(
                f"{o['model'].split('/')[-1]}={o['score']:.0f}(偏离{o['deviation']:+.1f})"
                for o in outlier_challenges
            )
            consensus_reasoning = (
                f"存在离群评分（{outlier_info}），去极值均分 {consensus_score}。"
                f"各视角评分：{score_summary}。"
                f"{'主要关注点：' + '；'.join(all_concerns[:3]) if all_concerns else '无重大顾虑'}。"
            )
        elif consensus_score >= 7.5:
            consensus_reasoning = (
                f"多视角验证通过（{score_summary}，加权分 {consensus_score}）。"
                f"投资逻辑整体成立，核心论点经得起多角度质疑。"
            )
        elif consensus_score >= 5:
            consensus_reasoning = (
                f"多视角观点分化（{score_summary}，加权分 {consensus_score}）。"
                f"主要关注点：{'；'.join(all_concerns[:3]) or '无重大顾虑'}。建议进一步研究。"
            )
        else:
            consensus_reasoning = (
                f"多视角不看好（{score_summary}，加权分 {consensus_score}）。"
                f"主要问题：{'；'.join(all_concerns[:3]) or '整体逻辑存疑'}。不建议纳入。"
            )

        return CrossValidationReport(
            supplier_name=scorecard.supplier.name,
            ticker=scorecard.supplier.ticker,
            validations=validations,
            consensus_score=consensus_score,
            consensus_reasoning=consensus_reasoning,
            avg_score=round(raw_avg, 1),
            raw_avg=round(raw_avg, 1),
            trimmed_avg=round(trimmed_avg, 1),
            has_fatal_risk=has_fatal_risk,
            fatal_risks=fatal_risks,
            outlier_challenges=outlier_challenges,
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

        top_scorecards = sorted(
            scorecards, key=lambda sc: sc.overall_score, reverse=True
        )[:10]

        reports = []
        for sc in top_scorecards:
            report = await self.validate_supplier(sc, llms)
            reports.append(report)
            logger.info(
                f"Cross-validated {sc.supplier.name}: "
                f"consensus={report.consensus_score:.1f} "
                f"fatal={report.has_fatal_risk}"
            )

        return reports
