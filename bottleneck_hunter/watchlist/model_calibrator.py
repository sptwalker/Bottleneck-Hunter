"""AI 模型预测校准系统。

定期统计每个 AI 模型在不同角色场景下的预测准确率，
并动态调整 calibration_weight 用于交叉验证加权共识。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from bottleneck_hunter.watchlist.store import WatchlistStore

logger = logging.getLogger(__name__)


class ModelCalibrator:
    """校准 AI 模型的预测权重。"""

    def __init__(self, store: WatchlistStore):
        self._store = store

    def recalibrate(self, market: str = "us_stock") -> int:
        """重新计算所有模型的 calibration_weight。

        Returns:
            校准的模型/角色组合数量。
        """
        stats = self._store.get_model_accuracy_stats(market=market)
        calibrated = 0

        for s in stats:
            total = s.get("total", 0)
            correct = s.get("correct", 0)
            pending = s.get("pending", 0)
            evaluated = total - pending

            if evaluated < 3:
                continue

            accuracy = correct / evaluated if evaluated else 0.5
            avg_delta = abs(s.get("avg_delta") or 0)

            recent_accuracy = self._get_recent_accuracy(
                s["model_provider"], s["model_name"],
                s.get("role_context", ""), market,
            )

            if recent_accuracy is not None and accuracy > 0:
                decay = recent_accuracy / accuracy
                decay = max(0.5, min(2.0, decay))
            else:
                decay = 1.0

            base_weight = accuracy * 2.0
            delta_penalty = max(0.5, 1.0 - avg_delta / 10.0)
            weight = max(0.3, min(3.0, base_weight * delta_penalty * decay))

            self._store.upsert_model_rating(
                provider=s["model_provider"],
                model=s["model_name"],
                role_context=s.get("role_context", ""),
                total=evaluated,
                correct=correct,
                accuracy=round(accuracy, 4),
                avg_delta=round(s.get("avg_delta") or 0, 2),
                weight=round(weight, 3),
                market=market,
            )
            calibrated += 1

            logger.info(
                "Calibrated %s/%s [%s]: accuracy=%.2f weight=%.3f (decay=%.2f)",
                s["model_provider"], s["model_name"],
                s.get("role_context", "global"),
                accuracy, weight, decay,
            )

        return calibrated

    def _get_recent_accuracy(
        self, provider: str, model: str,
        role_context: str, market: str,
        days: int = 30,
    ) -> float | None:
        """计算近 N 天的准确率。"""
        records = self._store.get_model_accuracy(
            provider, model, role_context=role_context, limit=200, market=market,
        )
        if not records:
            return None

        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=days)
        ).strftime("%Y-%m-%d")

        recent = [
            r for r in records
            if r.get("prediction_date", "") >= cutoff and r.get("is_correct", -1) >= 0
        ]

        if len(recent) < 3:
            return None

        correct = sum(1 for r in recent if r.get("is_correct") == 1)
        return correct / len(recent)


async def run_calibration(store: WatchlistStore, market: str = "us_stock"):
    """供 scheduler 调用的校准入口。"""
    calibrator = ModelCalibrator(store)
    count = calibrator.recalibrate(market=market)
    logger.info("Model calibration complete: %d models recalibrated (market=%s)", count, market)
    return count
