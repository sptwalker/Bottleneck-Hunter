"""全局自动更新时间表配置（管理员级，存 auth.db system_config）。

时间表对所有用户一致（开盘/收盘/决策时间是市场事实）。默认值即 scheduler 原硬编码时间；
管理员可覆盖，覆盖以单个 JSON blob 存于 system_config['auto_update_schedule']。
用户级"是否参与"开关另见 store_budget.AUTO_UPDATE_DEFAULTS。
"""
from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

# 系统级总开关（kill-switch）与时间表 blob 的 system_config key
GLOBAL_ENABLED_KEY = "auto_update_global_enabled"
SCHEDULE_KEY = "auto_update_schedule"

# 各定时任务的默认触发时间（时/分均为北京时间；weekly/interval 特殊字段）。
# 全系统统一北京时区：美股任务按「美股时段前后」换算为固定北京时刻（中国无夏令时，全年不漂移）。
# 依据：美股收盘 16:00ET=北京次日04:00(夏)/05:00(冬)，开盘09:30ET=北京21:30(夏)/22:30(冬)。
GLOBAL_SCHEDULE_DEFAULTS: dict[str, dict] = {
    # 美股（北京时间；收盘后/开盘前留足余量）
    "us_price_premarket":     {"hour": 21, "minute": 0},   # US 开盘前（<21:30）抓前收
    "us_price_postmarket":    {"hour": 5,  "minute": 30},  # US 收盘后（>05:00）抓当日收盘
    "us_daily_scan":          {"hour": 6,  "minute": 0},
    "macro_update":           {"hour": 5,  "minute": 45},
    "us_daily_decision":      {"hour": 6,  "minute": 30},  # 在数据 job 之后
    "us_catalyst_scan":       {"hour": 7,  "minute": 0},
    "us_weekly_strategy":     {"day_of_week": "sat", "hour": 10, "minute": 0},
    "us_auto_review":         {"hour": 7,  "minute": 30},
    "us_institutional_update": {"day_of_week": "sat", "hour": 11, "minute": 0},
    "us_earnings_update":     {"day_of_week": "sat", "hour": 11, "minute": 30},
    "cn_earnings_update":     {"day_of_week": "sat", "hour": 12, "minute": 0},   # 与美股财报错峰
    "datasource_report":      {"hour": 7, "minute": 30},
    "model_calibration":      {"day_of_week": "sun", "hour": 12, "minute": 0},
    "model_capability_refresh": {"hour": 3, "minute": 0},   # 月度(每月1号)：能力分重测
    # A股（北京时间）
    "cn_price_premarket":     {"hour": 9,  "minute": 0},
    "cn_price_postmarket":    {"hour": 16, "minute": 0},
    "cn_daily_scan":          {"hour": 18, "minute": 0},
    "cn_daily_decision":      {"hour": 18, "minute": 30},
    "cn_catalyst_scan":       {"hour": 8,  "minute": 0},
    # A股周末任务与美股错峰半小时，避免两市同刻并发写导致的锁竞争
    "cn_weekly_strategy":     {"day_of_week": "sat", "hour": 10, "minute": 30},
    "cn_auto_review":         {"hour": 20, "minute": 15},
    # 新增任务
    "stale_refresh":          {"interval_hours": 6},
    "resting_limit_poll":     {"interval_hours": 1},   # 挂单撮合轮询（开市时段每小时）
    "us_full_refresh":        {"day_of_week": "sun", "hour": 7, "minute": 0},
    "cn_full_refresh":        {"day_of_week": "sun", "hour": 8, "minute": 0},
}


def get_global_schedule(auth_store) -> dict[str, dict]:
    """返回合并后的完整时间表（默认值 + 管理员覆盖）。无 auth_store 时返回默认。"""
    merged = {k: dict(v) for k, v in GLOBAL_SCHEDULE_DEFAULTS.items()}
    if auth_store is None:
        return merged
    try:
        raw = auth_store.get_config(SCHEDULE_KEY, "")
        if raw:
            overrides = json.loads(raw)
            for job_id, fields in overrides.items():
                if job_id in merged and isinstance(fields, dict):
                    merged[job_id].update(fields)
    except Exception as e:
        logger.warning("读取全局时间表失败，用默认: %s", e)
    return merged


def set_global_schedule(auth_store, updates: dict[str, dict]) -> None:
    """合并保存管理员对时间表的覆盖（只接受已知 job_id）。"""
    if auth_store is None:
        return
    current = {}
    try:
        raw = auth_store.get_config(SCHEDULE_KEY, "")
        current = json.loads(raw) if raw else {}
    except Exception:
        current = {}
    for job_id, fields in (updates or {}).items():
        if job_id not in GLOBAL_SCHEDULE_DEFAULTS or not isinstance(fields, dict):
            continue
        clean = {k: v for k, v in fields.items()
                 if k in ("hour", "minute", "day_of_week", "interval_hours")}
        if clean:
            current.setdefault(job_id, {}).update(clean)
    auth_store.set_config(SCHEDULE_KEY, json.dumps(current, ensure_ascii=False))


def is_global_enabled(auth_store) -> bool:
    """系统级总开关。无 auth_store（单用户）默认开启。"""
    if auth_store is None:
        return True
    return auth_store.get_config(GLOBAL_ENABLED_KEY, "1").lower() in ("1", "true")


def set_global_enabled(auth_store, enabled: bool) -> None:
    if auth_store is not None:
        auth_store.set_config(GLOBAL_ENABLED_KEY, "1" if enabled else "0")
