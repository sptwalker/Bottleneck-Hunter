"""论坛管理规则三闸（F3）：去重 / 内容 / 配额，全为纯函数，可单测。

三闸对 AI 与用户的适用面不同（方案 §六）：
- 去重 is_duplicate：只拦 AI 角色反复读同一台词；用户发帖（role_key 空）不判重。
- 内容 check_content：AI 与用户都过（空正文、超长、人身攻击/辱骂一律拒）。
- 配额 check_quota：只约束 AI 角色（角色今日 <20 硬护栏 + 全板今日 SUM < daily_cap）；用户不受限。

数据只经 store.for_user(user_id) 按板主隔离读取；本模块不写库、不加载模型，
判定通过后由调用方（F4/F5）负责落库与计数。
"""

from __future__ import annotations

import hashlib
import re
from difflib import SequenceMatcher
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bottleneck_hunter.watchlist.store import WatchlistStore

# 去重
_DUP_RECENT = 20  # 与该角色最近 N 篇比对
_DUP_THRESHOLD = 0.9  # 归一化后相似度 ≥ 此值视为复读
# 内容
_MAX_BODY = 8000  # 正文字数上限（信任边界输入校验，非风格约束）
# 配额
_ROLE_HARD_CAP = 20  # 每角色每日硬护栏（方案：AI ≤20 篇/天）

# 归一化：去掉所有非「字母/数字/CJK」字符（空白、标点、下划线、emoji），再小写。
# \W 在 Unicode 下不匹配 CJK/字母数字，故中文被保留、标点空白被删。
_NON_WORD = re.compile(r"[\W_]+")

# 人身攻击/辱骂模式（轻量正则，非 ML）。刻意只打「指向人」的辱骂，
# 不封 "垃圾/废" 等在金融语境有正当义的独立词（垃圾债/垃圾股是行业术语）。
_ATTACK_PATTERNS = (
    re.compile(r"你(这个|就是|真是)?[个只]?\s*[傻蠢笨]"),
    re.compile(r"[傻煞沙][逼比屄]"),
    re.compile(r"脑子(有[病坑泡]|进水|瓦特)"),
    re.compile(r"滚(蛋|开|出去|一边|远点)|给我滚"),
    re.compile(r"白痴|蠢货|废物|智障|弱智|脑残|脑瘫|去死|闭嘴|滚犊子"),
    re.compile(r"\bSB\b", re.IGNORECASE),
)


def normalize(body: str) -> str:
    """规范化正文：去空白/标点/下划线 + 小写，供去重比对与哈希。"""
    return _NON_WORD.sub("", body).lower()


def content_hash(body: str) -> str:
    """规范化正文的 sha1（落库到 forum_posts.content_hash，供审计/未来快速判重）。"""
    return hashlib.sha1(normalize(body).encode("utf-8")).hexdigest()


def is_duplicate(
    store: WatchlistStore, user_id: str, author_role_key: str, body: str,
    *, recent: int = _DUP_RECENT, threshold: float = _DUP_THRESHOLD,
) -> bool:
    """该 AI 角色近 N 篇是否已有等价发言（完全同或相似度≥阈值）。用户发帖不判重。"""
    if not author_role_key:
        return False  # ponytail: 去重是 AI 反复刷屏的护栏；用户发帖是其自由
    norm = normalize(body)
    if not norm:
        return False  # 空正文交给 check_content 拦
    bound = store.for_user(user_id)
    # ponytail: O(recent) 次短串 SequenceMatcher，recent=20、正文短足够；量大再上 minhash
    for post in bound.list_forum_posts(role_key=author_role_key, limit=recent):
        other = normalize(post.get("body", ""))
        if other and (other == norm or SequenceMatcher(None, norm, other).ratio() >= threshold):
            return True
    return False


def check_content(body: str) -> tuple[bool, str]:
    """内容规则：空/超长/人身攻击即拒。返回 (是否通过, 原因)。"""
    if not body or not body.strip():
        return False, "正文不能为空"
    if len(body) > _MAX_BODY:
        return False, f"正文超长（>{_MAX_BODY} 字）"
    for pat in _ATTACK_PATTERNS:
        if pat.search(body):
            return False, "疑似人身攻击/辱骂，请对观点不对人"
    return True, ""


def check_quota(store: WatchlistStore, user_id: str, role_key: str) -> tuple[bool, str]:
    """AI 配额闸：角色今日 <20 硬护栏，且全板今日发言 SUM < daily_cap。返回 (是否通过, 原因)。"""
    bound = store.for_user(user_id)
    role_today = bound.get_forum_daily_count(role_key)
    if role_today >= _ROLE_HARD_CAP:
        return False, f"角色今日发言已达硬上限 {_ROLE_HARD_CAP}"
    cap = bound.get_forum_settings()["daily_cap"]
    if bound.get_forum_board_daily_total() >= cap:
        return False, f"全板今日发言已达配额 {cap}"
    return True, ""
