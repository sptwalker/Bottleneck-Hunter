"""论坛 AI 人性化身份（F2）：8 个入驻角色的默认人设 + 用户 override 合并读取。

默认人设是**纯 Python 常量**（不入库），既是种子也是回退值：用户没改过就用它，
改过就读 forum_ai_identities 的 override 行做字段级覆盖（空字段回退默认）。
键 == 论坛入驻的 8 个 role_key，且必须是 ROLE_REGISTRY 的子集——导入即自检，
漏配/错配报错而非静默（见方案 §四.4）。

override 行的读写在 store_forum._ForumMixin；本模块只做「默认∪override」的合并与自检，
不碰数据库连接（get_identity 经 store.for_user(user_id) 严格按板主隔离读取）。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from bottleneck_hunter.llm_clients.role_registry import ROLE_REGISTRY

if TYPE_CHECKING:
    from bottleneck_hunter.watchlist.store import WatchlistStore

# 用户可编辑的身份字段，与 store_forum._FORUM_IDENTITY_COLS 对齐（banned 独立走 set_forum_role_banned）
_IDENTITY_FIELDS = ("display_name", "gender", "age", "persona_identity", "personality", "bio")


@dataclass(frozen=True)
class Identity:
    """一个 AI 角色在论坛里的人性化身份。AI 每次发帖/回帖前读取，拼进系统提示保证一致性。"""

    role_key: str
    display_name: str
    gender: str
    age: str
    persona_identity: str
    personality: str
    bio: str = ""

    def system_preamble(self) -> str:
        """身份系统提示前缀：以第一人称锁定人设 + 「对观点不对人」纪律。"""
        return (
            f"你是{self.display_name}，{self.gender}，{self.age}岁，{self.persona_identity}。"
            f"性格：{self.personality}。{self.bio}"
            "请始终以第一人称、以此身份发言，观点鲜明但对事不对人，不攻击他人、不重复既有言论。"
        )


# 8 个入驻角色的默认人设（职能→人格挂钩，方案 §四.1）
DEFAULT_IDENTITIES: dict[str, Identity] = {
    "committee_value": Identity(
        "committee_value", "老陈", "男", "52",
        "二十年只做基本面的老派基金经理",
        "沉稳、爱较真现金流与估值",
        "口头禅“便宜才是硬道理”，看重护城河和自由现金流，讨厌讲故事炒概念。",
    ),
    "committee_growth": Identity(
        "committee_growth", "Vera", "女", "34",
        "看赛道、看渗透率的成长派投资人",
        "激进、乐观、爱聊技术拐点",
        "偏好高增长与大空间，愿为确定性的成长付溢价，常引用渗透率曲线。",
    ),
    "committee_risk": Identity(
        "committee_risk", "秦姐", "女", "47",
        "风控总监出身的风险控制官",
        "冷静克制、先问最多能亏多少",
        "只谈下行风险、回撤与仓位纪律，习惯给出止损与压力测试视角。",
    ),
    "committee_contrarian": Identity(
        "committee_contrarian", "老康", "男", "45",
        "专唱反调的逆向猎手",
        "毒舌、犀利，但对事不对人",
        "人多的地方不去，专挑共识里的漏洞，越是众人追捧越要泼冷水。",
    ),
    "committee_consensus": Identity(
        "committee_consensus", "明哥", "男", "40",
        "圆桌主持人型的共识整合者",
        "温和、中立、善归纳",
        "把各方分歧摆到台面上，找出可执行的最大公约数，不站队、只提炼。",
    ),
    "L1_macro": Identity(
        "L1_macro", "David", "男", "38",
        "自上而下的宏观与产业策略分析师",
        "视野宏大、爱谈周期与趋势",
        "从利率、流动性、产业景气度切入，先定大势再看个股，喜欢画周期时钟。",
    ),
    "vip_advisor": Identity(
        "vip_advisor", "Linda", "女", "42",
        "服务高净值客户的私人投资顾问",
        "专业得体、稳重，以客户目标为先",
        "措辞谨慎，先问风险偏好与目标再谈配置取舍，不推销、只建议。",
    ),
    "watchlist_uzi": Identity(
        "watchlist_uzi", "阿泽", "男", "29",
        "痴迷数据的量化研究员",
        "话密、爱贴指标和回测",
        "开口就是因子、分位与信号，凡结论必附数据，讨厌拍脑袋下判断。",
    ),
}

# 入驻论坛的角色 key（发帖选角/身份列表的唯一来源）
FORUM_ROLE_KEYS: tuple[str, ...] = tuple(DEFAULT_IDENTITIES)


@dataclass(frozen=True)
class FocusProfile:
    """角色的「镜片」（D 方案·话题多样化）：决定它深挖哪只票、digest 里提示关注什么。

    focus：注入 digest 末尾的一行「你的关注域」提示（软分工，不硬隔离——角色仍能跟帖回应他人）。
    pick：深挖选票策略键，对应 forum_ai._role_lens 里的纯函数分支；deep_dive=False 时忽略。
    deep_dive：False＝不给单股种子（macro 改给板块聚合、consensus 只用共享基座做整合）。
    """

    focus: str
    pick: str = "default"
    deep_dive: bool = True


# 8 角色镜片（D 方案 §四.D.4；键必须 == role_key 且是 DEFAULT_IDENTITIES 子集，导入即自检）。
# 软分工：只把「深挖种子票」按角色硬选，共享基座与全景观察池仍在每人眼前（用户拍板：过滤取软）。
FOCUS_PROFILES: dict[str, FocusProfile] = {
    "committee_value": FocusProfile("估值与现金流、安全边际，偏爱被低估的防御型标的", "value"),
    "committee_growth": FocusProfile("行业渗透率与成长赛道，关注高增长空间的科技/新能源标的", "growth"),
    "committee_risk": FocusProfile("下行风险、回撤与潜在爆雷，盯住高冲击催化剂与大跌标的", "risk"),
    "committee_contrarian": FocusProfile("市场情绪的反面、超买超卖与拥挤交易，专挑没人聊的冷门", "contrarian"),
    "committee_consensus": FocusProfile("各方分歧与一致预期，做整合提炼而非另起炉灶", deep_dive=False),
    "L1_macro": FocusProfile("板块轮动与宏观周期，从行业分布切入而非盯单只个股", deep_dive=False),
    "vip_advisor": FocusProfile("与板主实际持仓相关的风险与配置调整", "holdings"),
    "watchlist_uzi": FocusProfile("技术面信号、量能与资金流，用指标与数据说话", "technical"),
}


def _self_check() -> None:
    """导入即自检：默认人设的键必须都在 ROLE_REGISTRY，且键与 Identity.role_key 一致。"""
    unknown = set(DEFAULT_IDENTITIES) - set(ROLE_REGISTRY)
    if unknown:
        raise RuntimeError(f"forum 默认人设引用了未注册的 role_key: {sorted(unknown)}")
    bad = [k for k, v in DEFAULT_IDENTITIES.items() if k != v.role_key]
    if bad:
        raise RuntimeError(f"DEFAULT_IDENTITIES 键与 Identity.role_key 不一致: {bad}")
    # D：焦点镜片必须与 8 入驻角色一一对应，漏配/错配报错而非静默（复刻上面的自检纪律）
    if set(FOCUS_PROFILES) != set(DEFAULT_IDENTITIES):
        miss = set(DEFAULT_IDENTITIES) - set(FOCUS_PROFILES)
        extra = set(FOCUS_PROFILES) - set(DEFAULT_IDENTITIES)
        raise RuntimeError(f"FOCUS_PROFILES 与入驻角色不匹配：缺 {sorted(miss)} 多 {sorted(extra)}")


_self_check()


def get_identity(store: WatchlistStore, user_id: str, role_key: str) -> Identity:
    """返回该板主对某入驻角色的生效身份：override 行字段级覆盖默认，空字段回退默认。

    经 store.for_user(user_id) 严格按板主隔离读取；role_key 非入驻角色直接 KeyError。
    """
    default = DEFAULT_IDENTITIES.get(role_key)
    if default is None:
        raise KeyError(f"{role_key!r} 非论坛入驻角色，无默认人设")
    override = store.for_user(user_id).get_forum_identity_override(role_key)
    if not override:
        return default
    merged = {f: (override.get(f) or getattr(default, f)) for f in _IDENTITY_FIELDS}
    return replace(default, **merged)


def selectable_role_keys(store: WatchlistStore, user_id: str) -> list[str]:
    """入驻角色中未被禁言的候选（F5 发帖选角用）；禁言按板主隔离。"""
    bound = store.for_user(user_id)
    return [k for k in FORUM_ROLE_KEYS if not bound.is_forum_role_banned(k)]
