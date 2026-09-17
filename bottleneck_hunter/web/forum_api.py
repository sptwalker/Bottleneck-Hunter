"""互动留言系统 API（F4）— 挂载在 /api/forum。

每用户一个私有板：全部端点 Depends(get_current_user)，读写只经 _user_store(user)=for_user(sub)，
跨板不可见（软删/关评/改身份的「板归属校验」即由 for_user + _user_filter 天然完成：
删不到别人板的帖 → rowcount 0 → 404）。管理员=板主本人（方案 §七），故删帖/关评/禁言
无需 require_admin，靠隔离即可。SSE 广播复用 oplog 的 per-user _UserBroadcaster（另起实例）。

用户发帖只过内容闸（check_content）；不判重、不受配额（去重/配额是 AI 护栏，见 forum_moderation）。
POST /ai/run：ai_enabled=0（默认）直接空转返回，开启后才惰性导入 F5 引擎——F4 不硬依赖 F5。
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from bottleneck_hunter.auth.dependencies import get_current_user
from bottleneck_hunter.llm_clients.role_registry import ROLE_REGISTRY
from bottleneck_hunter.watchlist.forum_identity import FORUM_ROLE_KEYS, get_identity
from bottleneck_hunter.watchlist.forum_moderation import check_content, content_hash
from bottleneck_hunter.watchlist.store import WatchlistStore
from bottleneck_hunter.web.oplog import _UserBroadcaster  # 复用 per-user SSE 广播器

logger = logging.getLogger(__name__)

router = APIRouter(tags=["forum"])

_store: WatchlistStore | None = None
_broadcaster = _UserBroadcaster()  # 论坛自己的一份实例（与 oplog 事件流隔离）


def set_store(store: WatchlistStore) -> None:
    global _store
    _store = store


def get_forum_broadcaster() -> _UserBroadcaster:
    return _broadcaster


def _user_store(user: dict) -> WatchlistStore:
    if _store is None:
        raise HTTPException(status_code=500, detail="WatchlistStore not initialized")
    return _store.for_user(user["sub"])


def _publish(uid: str, event: str, **payload) -> None:
    """向该板主的 SSE 订阅者广播一条事件（无订阅者/满队列均安全）。"""
    _broadcaster.publish(uid, {"type": event, **payload})


def _maybe_trigger_ai_round(store: WatchlistStore, uid: str, *, post_id: int | None,
                            body: str, require_mention: bool) -> None:
    """用户回帖/点名即时触发一轮 AI 自主发言（P2 · fire-and-forget，结果经 SSE 回推）。

    AI 未开启则空转；F5 惰性导入（F4 不硬依赖 F5）。触发轮预算小、硬顶仍是每日 daily_cap；
    AI 发言经 store 直写、不回打本 API，故无「AI 触发 AI」自激环。create_task 复制当前请求
    上下文（含板主身份 ContextVar），后台轮仍用板主自己的 Key 与预算。
    """
    if not store.is_forum_ai_enabled():
        return
    from bottleneck_hunter.watchlist.forum_ai import resolve_mentions, run_forum_ai_round  # F5，惰性
    at_roles = resolve_mentions(store, uid, body)
    if require_mention and not at_roles:
        return  # 新帖只有点名了角色才惊动 AI；普通新帖不触发
    trigger = {"post_id": post_id, "at_roles": tuple(at_roles), "reply_excerpt": body}
    task = asyncio.create_task(run_forum_ai_round(store, uid, max_posts=3, trigger=trigger))
    task.add_done_callback(_log_round_task)


def _log_round_task(task: asyncio.Task) -> None:
    try:
        task.result()
    except Exception as e:  # noqa: BLE001 — 后台触发轮异常只记日志，不影响已返回的用户请求
        logger.warning("Forum AI 即时触发轮失败: %s", e)


# ── 请求体 ────────────────────────────────────────────────
class PostCreate(BaseModel):
    body: str
    title: str = ""
    ticker: str = ""


class ReplyCreate(BaseModel):
    body: str


class IdentityUpdate(BaseModel):
    display_name: str | None = None
    gender: str | None = None
    age: str | None = None
    persona_identity: str | None = None
    personality: str | None = None
    bio: str | None = None


class SettingsUpdate(BaseModel):
    ai_enabled: bool | None = None
    daily_cap: int | None = None


# ── 帖子 ──────────────────────────────────────────────────
@router.get("/posts")
async def list_posts(limit: int = 50, offset: int = 0, role_key: str = "",
                     user: dict = Depends(get_current_user)):
    store = _user_store(user)
    return {"posts": store.list_forum_posts(limit=limit, offset=offset, role_key=role_key)}


@router.post("/posts")
async def create_post(req: PostCreate, user: dict = Depends(get_current_user)):
    ok, reason = check_content(req.body)
    if not ok:
        raise HTTPException(status_code=400, detail=reason)
    store = _user_store(user)
    pid = store.create_forum_post("user", req.body, title=req.title, ticker=req.ticker,
                                  content_hash=content_hash(req.body))
    post = store.get_forum_post(pid)
    _publish(user["sub"], "post_created", post=post)
    _maybe_trigger_ai_round(store, user["sub"], post_id=pid, body=req.body, require_mention=True)
    return post


@router.get("/posts/{post_id}")
async def get_post(post_id: int, user: dict = Depends(get_current_user)):
    store = _user_store(user)
    post = store.get_forum_post(post_id)
    if not post or post["deleted"]:
        raise HTTPException(status_code=404, detail="帖子不存在")  # 软删行 store 仍返回(审计)，API 视图当 404
    return {"post": post, "replies": store.list_forum_replies(post_id)}


@router.delete("/posts/{post_id}")
async def delete_post(post_id: int, user: dict = Depends(get_current_user)):
    if not _user_store(user).soft_delete_forum_post(post_id):
        raise HTTPException(status_code=404, detail="帖子不存在")  # 含跨板：删不到即 404
    _publish(user["sub"], "post_deleted", post_id=post_id)
    return {"status": "deleted"}


@router.post("/posts/{post_id}/close")
async def close_comments(post_id: int, user: dict = Depends(get_current_user)):
    if not _user_store(user).set_forum_comments_closed(post_id, True):
        raise HTTPException(status_code=404, detail="帖子不存在")
    _publish(user["sub"], "comments_closed", post_id=post_id, closed=True)
    return {"status": "closed"}


@router.post("/posts/{post_id}/open")
async def open_comments(post_id: int, user: dict = Depends(get_current_user)):
    if not _user_store(user).set_forum_comments_closed(post_id, False):
        raise HTTPException(status_code=404, detail="帖子不存在")
    _publish(user["sub"], "comments_closed", post_id=post_id, closed=False)
    return {"status": "opened"}


# ── 回帖 ──────────────────────────────────────────────────
@router.post("/posts/{post_id}/replies")
async def create_reply(post_id: int, req: ReplyCreate, user: dict = Depends(get_current_user)):
    store = _user_store(user)
    post = store.get_forum_post(post_id)
    if not post or post["deleted"]:
        raise HTTPException(status_code=404, detail="帖子不存在")  # 软删行 store 仍返回(审计)，API 视图当 404
    if post["comments_closed"]:
        raise HTTPException(status_code=403, detail="该帖已关闭评论")
    ok, reason = check_content(req.body)
    if not ok:
        raise HTTPException(status_code=400, detail=reason)
    rid = store.create_forum_reply(post_id, "user", req.body, content_hash=content_hash(req.body))
    _publish(user["sub"], "reply_created", post_id=post_id, reply_id=rid)
    _maybe_trigger_ai_round(store, user["sub"], post_id=post_id, body=req.body, require_mention=False)
    return {"reply_id": rid}


@router.delete("/replies/{reply_id}")
async def delete_reply(reply_id: int, user: dict = Depends(get_current_user)):
    if not _user_store(user).soft_delete_forum_reply(reply_id):
        raise HTTPException(status_code=404, detail="回帖不存在")
    _publish(user["sub"], "reply_deleted", reply_id=reply_id)
    return {"status": "deleted"}


# ── AI 身份 ───────────────────────────────────────────────
@router.get("/identities")
async def list_identities(user: dict = Depends(get_current_user)):
    store = _user_store(user)
    uid = user["sub"]
    out = []
    for rk in FORUM_ROLE_KEYS:
        row = asdict(get_identity(store, uid, rk))  # 默认∪override 生效身份
        row["label"] = ROLE_REGISTRY[rk].label
        row["banned"] = store.is_forum_role_banned(rk)
        row["overridden"] = store.get_forum_identity_override(rk) is not None
        out.append(row)
    return {"identities": out}


@router.put("/identities/{role_key}")
async def update_identity(role_key: str, req: IdentityUpdate, user: dict = Depends(get_current_user)):
    if role_key not in FORUM_ROLE_KEYS:
        raise HTTPException(status_code=404, detail="非论坛入驻角色")
    store = _user_store(user)
    fields = req.model_dump(exclude_none=True)
    if fields:
        store.set_forum_identity(role_key, **fields)
    return asdict(get_identity(store, user["sub"], role_key))


@router.post("/identities/{role_key}/ban")
async def ban_role(role_key: str, user: dict = Depends(get_current_user)):
    if role_key not in FORUM_ROLE_KEYS:
        raise HTTPException(status_code=404, detail="非论坛入驻角色")
    _user_store(user).set_forum_role_banned(role_key, True)
    _publish(user["sub"], "role_banned", role_key=role_key, banned=True)
    return {"role_key": role_key, "banned": True}


@router.post("/identities/{role_key}/unban")
async def unban_role(role_key: str, user: dict = Depends(get_current_user)):
    if role_key not in FORUM_ROLE_KEYS:
        raise HTTPException(status_code=404, detail="非论坛入驻角色")
    _user_store(user).set_forum_role_banned(role_key, False)
    _publish(user["sub"], "role_banned", role_key=role_key, banned=False)
    return {"role_key": role_key, "banned": False}


# ── 板设置 ────────────────────────────────────────────────
@router.get("/settings")
async def get_settings(user: dict = Depends(get_current_user)):
    return _user_store(user).get_forum_settings()


@router.put("/settings")
async def update_settings(req: SettingsUpdate, user: dict = Depends(get_current_user)):
    store = _user_store(user)
    store.set_forum_settings(ai_enabled=req.ai_enabled, daily_cap=req.daily_cap)
    return store.get_forum_settings()


# ── AI 发言（手动触发；F5 引擎惰性导入）──────────────────
@router.post("/ai/run")
async def run_ai(user: dict = Depends(get_current_user)):
    store = _user_store(user)
    if not store.is_forum_ai_enabled():  # 默认关：直接空转，不导入 F5
        return {"posted": 0, "reason": "AI 自主发帖未开启（forum_settings.ai_enabled=0）"}
    from bottleneck_hunter.watchlist.forum_ai import run_forum_ai_round  # F5，惰性
    r = await run_forum_ai_round(store, user["sub"])
    return {"posted": r["posts"] + r["replies"], **r}  # posted=总数（兼容旧前端），另拆 posts/replies


# ── 实时推送 ──────────────────────────────────────────────
@router.get("/stream")
async def forum_stream(request: Request, user: dict = Depends(get_current_user)):
    uid = user["sub"]
    q = _broadcaster.subscribe(uid)

    async def gen():
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    rec = await asyncio.wait_for(q.get(), timeout=30)
                    yield {"event": "forum", "data": json.dumps(rec, ensure_ascii=False)}
                except asyncio.TimeoutError:
                    yield {"event": "ping", "data": "{}"}
        finally:
            _broadcaster.unsubscribe(uid, q)

    return EventSourceResponse(gen())
