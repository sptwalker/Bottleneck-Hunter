"""互动留言系统 · AI 自主发帖引擎（F5）。

opt-in（forum_settings.ai_enabled）默认关；开启后由 scheduler 低频 job 或 /forum/ai/run 手动触发。
全程在 set_current_user(板主) 上下文内（scheduler 进入 / API 靠 for_user 绑定）：读数据只经
for_user(板主)、发言用板主自己的 Key 与预算——绝无全局 Key（见 [[project_strict_key_isolation]]）。

落库前三闸（去重 → 内容 → 配额，见 forum_moderation）任一不过则跳过且不计配额。
LLM 调用：get_models_for_role 返回的实例已 record-only 包装（喂熔断层），直接 ainvoke——
绝不再套 wrap_record_only，也绝不加 asyncio.wait_for（FallbackChatModel 内部已 wait_for，
外包=自毁，见 [[project-llm-call-layer-unified]]）。
"""

from __future__ import annotations

import logging
import random

from bottleneck_hunter.watchlist.forum_identity import get_identity, selectable_role_keys
from bottleneck_hunter.watchlist.forum_moderation import (
    check_content,
    check_quota,
    content_hash,
    is_duplicate,
)

logger = logging.getLogger(__name__)

_ROLE_DAILY_CAP = 20      # 每角色每日硬护栏（与 forum_moderation 一致）
_DEFAULT_ROUND_POSTS = 3  # 单轮默认上限（低频控成本）；scheduler 传 3，手动触发不传时同值
_REPLY_PROB = 0.5         # 50% 概率回复他人近帖，制造互动感
_CTX_CAP = 1600           # 背景数据注入上限（字符）

_POST_INSTRUCTION = (
    "请围绕当前市场或板主观察池里的某只标的，发表一条你个人视角的原创看法、判断或提问。"
    "150 字以内，观点鲜明、口语化，像在投资论坛发帖。"
    "只输出正文，不要标题、不要署名、不要 markdown 语法。"
)


async def run_forum_ai_round(store, user_id, *, max_posts=None) -> dict:
    """跑一轮 AI 自主发言，返回 {"posts": 新帖数, "replies": 回帖数}。opt-in 门禁不过返回全 0。"""
    bound = store.for_user(user_id)

    # 1) 前置门禁：opt-in 总开关 + 全板当日剩余配额
    settings = bound.get_forum_settings()
    if not settings.get("ai_enabled"):
        return {"posts": 0, "replies": 0}
    daily_cap = int(settings.get("daily_cap", 20) or 20)
    board_today = bound.get_forum_board_daily_total()
    upper = _DEFAULT_ROUND_POSTS if max_posts is None else int(max_posts)
    budget = min(daily_cap - board_today, upper)
    if budget <= 0:
        return {"posts": 0, "replies": 0}

    # 2) 选角色：未禁言且今日 <20，随机（近似「最久未发言」，避免同角色刷屏）
    candidates = [rk for rk in selectable_role_keys(store, user_id)
                  if bound.get_forum_daily_count(rk) < _ROLE_DAILY_CAP]
    if not candidates:
        return {"posts": 0, "replies": 0}
    random.shuffle(candidates)

    # 3) 背景数据（best-effort，只经 for_user(板主)）
    context = _board_context(bound)

    posts = replies = 0
    for role_key in candidates:
        if posts + replies >= budget:
            break
        try:
            body, target = await _generate(store, bound, user_id, role_key, context)
        except Exception as e:  # noqa: BLE001 — 单角色生成失败不拖垮整轮
            logger.warning("Forum AI 生成失败 (role=%s): %s", role_key, e)
            continue
        if not body:
            continue
        # 4) 落库前三闸：任一不过跳过且不计配额（去重/违规/超额不该烧额度）
        if is_duplicate(store, user_id, role_key, body):
            continue
        ok, _ = check_content(body)
        if not ok:
            continue
        ok, _ = check_quota(store, user_id, role_key)
        if not ok:
            continue
        # 5) 持久化 + 计数 + SSE 广播
        h = content_hash(body)
        if target is not None:
            rid = bound.create_forum_reply(target["id"], "ai", body,
                                           author_role_key=role_key, content_hash=h)
            _publish(user_id, "reply_created", post_id=target["id"], reply_id=rid)
            replies += 1
        else:
            pid = bound.create_forum_post("ai", body, author_role_key=role_key, content_hash=h)
            _publish(user_id, "post_created", post=bound.get_forum_post(pid))
            posts += 1
        bound.incr_forum_daily_count(role_key)
    return {"posts": posts, "replies": replies}


async def _generate(store, bound, user_id, role_key, context):
    """产出 (body, target_post|None)：50% 回复他人近帖，否则原创发帖。"""
    from langchain_core.messages import HumanMessage, SystemMessage

    from bottleneck_hunter.llm_clients import factory

    identity = get_identity(store, user_id, role_key)
    target = _pick_reply_target(bound, role_key) if random.random() < _REPLY_PROB else None
    instruction = _reply_instruction(target) if target is not None else _POST_INSTRUCTION

    system = identity.system_preamble()
    if context:
        system += "\n\n【当前板主的市场背景，供你参考、不要照抄】\n" + context

    models = factory.get_models_for_role(role_key, user_id=user_id, temperature=0.7)
    if not models:
        return "", None
    llm = models[0][0]  # 已 record-only 包装：直接 ainvoke，绝不再套 wait_for / wrap_record_only
    msg = await llm.ainvoke([SystemMessage(content=system), HumanMessage(content=instruction)])
    body = (getattr(msg, "content", "") or "").strip()
    return body[:2000], target  # 2000 硬截；check_content 上限 8000 兜底


def _reply_instruction(target) -> str:
    who = target.get("author_role_key") or "板主"
    body = (target.get("body") or "")[:400]
    return (f"论坛里 {who} 发了一条帖：\n「{body}」\n\n"
            "请以你的身份和视角回复，可以赞同并补充、也可以有理有据地反驳，"
            "120 字以内，对观点不对人、口语化。只输出回复正文，不要署名、不要 markdown。")


def _pick_reply_target(bound, role_key):
    """近 20 帖里挑一条「他人所发、未关评」的帖作回复对象；无则 None（list 已排除软删）。"""
    try:
        posts = bound.list_forum_posts(limit=20)
    except Exception:  # noqa: BLE001
        return None
    cands = [p for p in posts
             if p.get("author_role_key") != role_key and not p.get("comments_closed")]
    return random.choice(cands) if cands else None


def _board_context(bound) -> str:
    """拼板主自己的市场背景：L1 宏观 + 观察池标的清单 + 抽一只的深度背景。全 best-effort。"""
    lines: list[str] = []
    try:
        from bottleneck_hunter.vip.advisory import format_macro_for_prompt
        macro = format_macro_for_prompt(bound)
        if macro and macro.strip():
            lines.append(macro.strip())
    except Exception:  # noqa: BLE001
        pass

    entries: list[dict] = []
    try:
        entries = bound.list_all() or []
    except Exception:  # noqa: BLE001
        entries = []
    if entries:
        tags = []
        for e in entries[:12]:
            tk = e.get("ticker")
            if not tk:
                continue
            sec = (e.get("sector") or "").strip()
            tags.append(f"{tk}（{sec}）" if sec else tk)
        if tags:
            lines.append("板主观察池：" + "、".join(tags))
        seg = _ticker_background(bound, random.choice(entries[:12]))
        if seg:
            lines.append(seg)
    return "\n".join(lines)[:_CTX_CAP]


def _ticker_background(bound, entry) -> str:
    tk = entry.get("ticker")
    if not tk:
        return ""
    try:
        from bottleneck_hunter.watchlist.committee import build_ticker_background
        bg = build_ticker_background(bound, tk, entry.get("id") or "", entry.get("market") or "")
    except Exception:  # noqa: BLE001
        return ""
    if not isinstance(bg, dict):
        return ""
    picks = []
    for k in ("valuation_data", "catalyst_data", "sentiment_data", "sector_trends"):
        v = bg.get(k)
        if (isinstance(v, str) and v and not v.startswith("暂无")) or (isinstance(v, dict) and v):
            picks.append(f"{k}: {v}")
    return (f"{tk} 背景 — " + "；".join(picks)) if picks else ""


def _publish(uid, event, **payload) -> None:
    """向板主 SSE 订阅者广播（lazy import 避开 watchlist→web 硬依赖；无订阅/失败均安全）。"""
    try:
        from bottleneck_hunter.web.forum_api import get_forum_broadcaster
        get_forum_broadcaster().publish(uid, {"type": event, **payload})
    except Exception:  # noqa: BLE001
        pass
