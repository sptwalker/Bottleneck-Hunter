"""单通道 IM 推送：把自动更新/错误事件推到用户配置的一个 IM webhook（bark / serverchan / feishu）。

设计（ponytail）：一个函数 + if 分支即可覆盖三渠道，故意不建适配器框架、不做双向控制、不接
Telegram/Slack（YAGNI，需要时再加一个 if）。fire-and-forget——任何网络异常一律吞掉，绝不阻断
主流程（推送只是锦上添花，宁可漏推也不能因推送失败拖垮记录/调度）。
"""
import logging

from bottleneck_hunter.watchlist.retry import get_http_client

logger = logging.getLogger(__name__)

# 支持的渠道（前端下拉、settings 校验、payload 构造三处共用此单一事实来源）
PUSH_CHANNELS = ("bark", "serverchan", "feishu")


def build_push_payload(channel: str, title: str, body: str) -> dict:
    """按渠道构造 httpx.post 的 kwargs（json= 或 data=）。未知渠道 → 空 dict（调用方据此跳过，不发）。

    - bark：POST 基址(api.day.app/{key})，JSON {title, body}
    - serverchan：POST {key}.send(sctapi.ftqq.com)，表单 {title, desp}
    - feishu：POST 自定义机器人 webhook，JSON {msg_type:text, content:{text}}
    """
    if channel == "bark":
        return {"json": {"title": title, "body": body}}
    if channel == "serverchan":
        return {"data": {"title": title, "desp": body}}
    if channel == "feishu":
        return {"json": {"msg_type": "text", "content": {"text": f"{title}\n{body}"}}}
    return {}


async def push_event(webhook_url: str, channel: str, title: str, body: str) -> bool:
    """向单个 IM webhook 推一条（fire-and-forget）。返回是否实际发出（供测试/日志断言）。

    空 url 或未知渠道 → 直接 no-op 返回 False（零副作用，连 http client 都不碰）。
    任何异常吞掉、debug 日志、返回 False——绝不冒泡打断调用方（oplog 记录/调度）。
    """
    kwargs = build_push_payload(channel, title, body)
    if not webhook_url or not kwargs:
        return False
    try:
        await get_http_client().post(webhook_url, **kwargs)
        return True
    except Exception as e:  # noqa: BLE001 — 推送失败绝不能冒泡打断主流程
        logger.debug("IM 推送失败(%s): %s", channel, e)
        return False
