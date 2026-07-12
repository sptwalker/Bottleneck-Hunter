"""实时操作日志：记录 + 按用户 SSE 广播 + 改动型请求中间件（白话映射）。

- record_operation(user_id, ...)：落库(store_oplog) + 广播给该用户的 SSE 订阅者。
- OpLogMiddleware：拦截 POST/PUT/DELETE/PATCH → 按「路径→白话动作」记一条（2xx=成功，否则=失败）。
- 只记有 user_id 的操作（未登录的登录/注册不入个人日志）。
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


# ── 按用户 SSE 广播器 ──────────────────────────────────────
class _UserBroadcaster:
    def __init__(self):
        self._queues: dict[str, set[asyncio.Queue]] = {}

    def subscribe(self, user_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._queues.setdefault(user_id, set()).add(q)
        return q

    def unsubscribe(self, user_id: str, q: asyncio.Queue) -> None:
        s = self._queues.get(user_id)
        if s:
            s.discard(q)
            if not s:
                self._queues.pop(user_id, None)

    def publish(self, user_id: str, rec: dict) -> None:
        dead = []
        for q in list(self._queues.get(user_id, ())):
            try:
                q.put_nowait(rec)
            except asyncio.QueueFull:
                dead.append(q)   # 卡死的满队列：剔除，避免长期滞留
        for q in dead:
            self.unsubscribe(user_id, q)


_broadcaster = _UserBroadcaster()


def get_broadcaster() -> _UserBroadcaster:
    return _broadcaster


# ── store 注入 + 记录入口 ──────────────────────────────────
_store = None


def set_store(store) -> None:
    global _store
    _store = store


def record_operation(user_id: str, title: str, *, category: str = "user_action",
                     detail: str = "", result: str = "success", market: str = "",
                     meta: dict | None = None) -> None:
    """记一条操作日志并实时广播。无 user_id 或 store 未初始化则静默丢弃。失败不影响主流程。"""
    if not user_id or _store is None:
        return
    try:
        rec = _store.for_user(user_id).record_operation(
            user_id, title, category=category, detail=detail,
            result=result, market=market, meta=meta)
        _broadcaster.publish(user_id, rec)
    except Exception as e:  # noqa: BLE001
        logger.debug("record_operation 失败: %s", e)


# ── 路径 → 白话动作映射（前缀匹配，具体在前）──────────────
# (method, 路径前缀) → (白话标题, 类别默认 user_action)
_ACTION_MAP: list[tuple[str, str, str]] = [
    ("POST", "/api/watchlist/batch-delete", "批量移出观察池"),
    ("POST", "/api/watchlist/refresh-intelligence", "刷新观察池情报"),
    ("POST", "/api/watchlist/refresh-strategy", "刷新观察池策略"),
    ("POST", "/api/watchlist/refresh", "手动刷新观察池数据"),
    ("PUT", "/api/watchlist/batch-tier", "调整观察池分层"),
    ("DELETE", "/api/watchlist/", "移出观察池"),
    ("PATCH", "/api/watchlist/", "修改观察池条目"),
    ("POST", "/api/watchlist", "加入观察池"),
    ("POST", "/api/decision/full-refresh", "全量刷新决策"),
    ("POST", "/api/decision/daily", "运行日常决策"),
    ("POST", "/api/decision/macro/consult/retry", "重试宏观分析师"),
    ("POST", "/api/decision/macro/consult", "宏观咨询"),
    ("POST", "/api/decision/tactical", "生成战术计划"),
    ("POST", "/api/decision/execution", "生成执行方案"),
    ("POST", "/api/trading", "模拟交易操作"),
    ("POST", "/api/reverse/analyze", "反向分析"),
    ("POST", "/api/reverse/cross-analyze", "反向交叉验证"),
    ("POST", "/api/phase1", "产业链拆解分析"),
    ("POST", "/api/phase2", "入围筛选分析"),
    ("POST", "/api/phase4", "多模型交叉验证"),
    ("POST", "/api/ai-report", "生成 AI 报告"),
    ("POST", "/api/screen", "产业链筛选"),
    ("POST", "/api/custom-providers", "新增 AI 接口"),
    ("PUT", "/api/custom-providers/", "修改 AI 接口"),
    ("DELETE", "/api/custom-providers/", "删除 AI 接口"),
    ("POST", "/api/ai-config", "修改 AI 配置"),
    ("POST", "/api/settings", "修改系统设置"),
]

# 不记录的路径（日志流自身/鉴权/心跳/内部测试，避免噪声与自引用/误标）
_SKIP_PREFIXES = ("/api/oplog", "/api/system/logs", "/api/auth/", "/api/ai-config/test")

# 流式(SSE)操作：HTTP 200 只代表"已发起"，真结果在流里，不能据 200 记"完成/成功"
_STREAMING_PREFIXES = (
    "/api/decision/", "/api/phase1", "/api/phase2", "/api/phase4", "/api/screen",
    "/api/ai-report", "/api/reverse/analyze", "/api/reverse/cross-analyze",
    "/api/watchlist/refresh",
)


def _resolve_action(method: str, path: str) -> str:
    for m, prefix, title in _ACTION_MAP:
        if method == m and path.startswith(prefix):
            return title
    return f"{method} {path.split('?')[0]}"  # 未映射 → 通用动作


class OpLogMiddleware:
    """记录改动型请求为操作日志（用户操作 / 失败）。放在 AuthMiddleware 之内以拿到 scope.state.user。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") not in ("POST", "PUT", "DELETE", "PATCH"):
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if not path.startswith("/api/") or any(path.startswith(p) for p in _SKIP_PREFIXES):
            await self.app(scope, receive, send)
            return

        status = {"code": 0}

        async def _send(message):
            if message["type"] == "http.response.start":
                status["code"] = message.get("status", 0)
            await send(message)

        crashed = False
        try:
            await self.app(scope, receive, _send)
        except Exception:
            crashed = True   # 未捕获异常(500 崩溃)：记为失败后原样重抛，不吞
            raise
        finally:
            user = (scope.get("state") or {}).get("user")
            uid = user.get("sub") if isinstance(user, dict) else ""
            if uid:
                code = status["code"] or (500 if crashed else 0)
                ok = (not crashed) and 200 <= code < 400
                title = _resolve_action(scope["method"], path)
                if ok:
                    # 流式操作 200 仅表示已受理，结果在流里 → 不记"完成/成功"，只记"已发起"
                    streaming = any(path.startswith(p) for p in _STREAMING_PREFIXES)
                    record_operation(
                        uid, title, category="user_action",
                        detail="已发起（结果见对应功能页）" if streaming else "操作成功",
                        result="success",
                        meta={"method": scope["method"], "path": path, "status": code})
                else:
                    record_operation(
                        uid, title, category="error",
                        detail=f"操作失败（HTTP {code}）" if code else "操作失败（服务器异常）",
                        result="fail",
                        meta={"method": scope["method"], "path": path, "status": code})
