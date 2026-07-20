"""AI 角色定义注册表 — 可扩展的角色元数据。

每个角色定义了一个 LLM 使用位置（如 L1 宏观策略、瓶颈交叉评分等），
包含默认模型配置和能力权重需求。新增角色只需调用 register_role()。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RoleDefinition:
    key: str
    label: str
    group: str  # decision / committee / pipeline / watchlist / bottleneck
    multi_model: bool = False
    max_slots: int = 1
    default_provider: str = "deepseek"
    default_model: str = ""  # 留空：由 factory.resolve_provider_model 按 provider_configs→种子解析，不写死
    capability_weights: dict[str, float] = field(default_factory=dict)
    slot_labels: list[str] = field(default_factory=list)  # 多槽角色各槽的语义标签（配置界面显示）
    min_context: int = 0  # 该角色最低上下文窗口(tokens)需求；>0 时智能调度不选容量不足的模型（0=无要求）


ROLE_REGISTRY: dict[str, RoleDefinition] = {}


def register_role(role: RoleDefinition):
    ROLE_REGISTRY[role.key] = role


def get_role(key: str) -> RoleDefinition | None:
    return ROLE_REGISTRY.get(key)


def list_roles(group: str | None = None) -> list[RoleDefinition]:
    roles = list(ROLE_REGISTRY.values())
    if group:
        roles = [r for r in roles if r.group == group]
    return roles


# 各角色组能力权重差异化：让不同组真正分化排名，避免单一模型通吃。
# - 决策层：重中文分析与指令遵循（长文推理、严格 JSON）
_DECISION_WEIGHTS = {
    "connectivity": 0.05, "json_output": 0.15,
    "chinese_analysis": 0.30, "speed": 0.05,
    "scoring_variance": 0.15, "instruction_follow": 0.30,
}
# - 投委会：重打分区分度（scoring_variance）与中文分析，速度无所谓（多模型并行）
_COMMITTEE_WEIGHTS = {
    "connectivity": 0.05, "json_output": 0.15,
    "chinese_analysis": 0.25, "speed": 0.05,
    "scoring_variance": 0.35, "instruction_follow": 0.15,
}
# - 产业链管线：拆解角色以「真实拆解力」为主导（chain_decompose 直接跑单节点拆解，量广度/结构/深度）。
#   记录 #10(deepseek 457节点) vs #13(minimax 79节点)证明：通用维度测不出拆解广度差，必须单列并给主导权重。
#   其余：长结构化输出稳(json_output)、严格遵循 schema(instruction_follow)、中文长文分析次之；速度弱化(快而脆的模型在几十次长输出里反而常失败)。
_PIPELINE_WEIGHTS = {
    "connectivity": 0.05, "json_output": 0.20,
    "chinese_analysis": 0.10, "speed": 0.05,
    "scoring_variance": 0.05, "instruction_follow": 0.15,
    "chain_decompose": 0.40,
}
# - 看板模块：重速度与 JSON（高频小任务），均衡
_WATCHLIST_WEIGHTS = {
    "connectivity": 0.05, "json_output": 0.25,
    "chinese_analysis": 0.15, "speed": 0.35,
    "scoring_variance": 0.05, "instruction_follow": 0.15,
}
# - 瓶颈评分：重打分区分度（多模型交叉），JSON 次之
_BOTTLENECK_WEIGHTS = {
    "connectivity": 0.05, "json_output": 0.20,
    "chinese_analysis": 0.15, "speed": 0.05,
    "scoring_variance": 0.40, "instruction_follow": 0.15,
}

# 重上下文角色的最低窗口需求：注入市场数据/新闻/多份评分卡的角色，8k 模型装不下（本次 kimi-8k 踩坑）。
# 16384 只排除已知 8k 小模型，保留 16k/32k/64k/128k+。
_HEAVY = 16_384

_INIT_ROLES = [
    # 决策层级
    RoleDefinition("L1_macro", "L1 宏观策略", "decision",
                   multi_model=True, max_slots=2,
                   slot_labels=["宏观市场分析师", "产业动向分析师"],
                   capability_weights=_DECISION_WEIGHTS, min_context=_HEAVY),
    RoleDefinition("L2_strategic", "L2 组合策略", "decision",
                   capability_weights=_DECISION_WEIGHTS, min_context=_HEAVY),
    RoleDefinition("L3_tactical", "L3 战术计划", "decision",
                   capability_weights=_DECISION_WEIGHTS, min_context=_HEAVY),
    RoleDefinition("L4_execution", "L4 执行方案", "decision",
                   capability_weights=_DECISION_WEIGHTS, min_context=_HEAVY),
    # 投委会
    RoleDefinition("committee_risk", "风险控制官", "committee",
                   default_provider="deepseek",
                   capability_weights=_COMMITTEE_WEIGHTS, min_context=_HEAVY),
    RoleDefinition("committee_growth", "成长投资人", "committee",
                   default_provider="qwen",
                   capability_weights=_COMMITTEE_WEIGHTS, min_context=_HEAVY),
    RoleDefinition("committee_value", "价值投资人", "committee",
                   default_provider="kimi",
                   capability_weights=_COMMITTEE_WEIGHTS, min_context=_HEAVY),
    RoleDefinition("committee_contrarian", "逆向投资人", "committee",
                   default_provider="glm",
                   capability_weights=_COMMITTEE_WEIGHTS, min_context=_HEAVY),
    RoleDefinition("committee_consensus", "圆桌讨论/共识", "committee",
                   capability_weights=_COMMITTEE_WEIGHTS, min_context=_HEAVY),
    # 产业链管线
    RoleDefinition("pipeline_decompose", "产业链拆解", "pipeline",
                   capability_weights=_PIPELINE_WEIGHTS),
    RoleDefinition("pipeline_eval", "供应商评估", "pipeline",
                   capability_weights=_PIPELINE_WEIGHTS, min_context=_HEAVY),
    RoleDefinition("pipeline_cross_val", "交叉验证", "pipeline",
                   capability_weights=_PIPELINE_WEIGHTS, min_context=_HEAVY),
    RoleDefinition("pipeline_roundtable", "圆桌讨论", "pipeline",
                   capability_weights=_PIPELINE_WEIGHTS, min_context=_HEAVY),
    # 看板模块
    RoleDefinition("watchlist_catalyst", "催化剂监控", "watchlist",
                   capability_weights=_WATCHLIST_WEIGHTS),
    RoleDefinition("watchlist_strategy", "策略引擎", "watchlist",
                   capability_weights=_WATCHLIST_WEIGHTS),
    RoleDefinition("watchlist_thesis", "论点追踪", "watchlist",
                   capability_weights=_WATCHLIST_WEIGHTS),
    RoleDefinition("watchlist_trade_review", "交易复盘", "watchlist",
                   capability_weights=_WATCHLIST_WEIGHTS),
    RoleDefinition("watchlist_tuning", "参数调优", "watchlist",
                   capability_weights=_WATCHLIST_WEIGHTS),
    RoleDefinition("watchlist_uzi", "深度分析(UZI)", "watchlist",
                   capability_weights=_WATCHLIST_WEIGHTS, min_context=_HEAVY),
    # 瓶颈交叉评分
    RoleDefinition("bottleneck", "瓶颈分析", "bottleneck",
                   multi_model=True, max_slots=3,
                   capability_weights=_BOTTLENECK_WEIGHTS),
]

for _r in _INIT_ROLES:
    register_role(_r)
