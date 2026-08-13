"""P1-⑥ 单通道 IM 推送：三渠道 payload 结构 + 空url/未知渠道 no-op（零副作用，全程不打真实网络）。

- build_push_payload：bark(json title/body) / serverchan(data title/desp) / feishu(json msg_type text)。
- 未知渠道 → 空 dict；push_event 空 url 或未知渠道 → 连 http client 都不碰（monkeypatch 成炸弹自证）。
- 有 url+已知渠道 → 调一次 post、kwargs 正确；post 抛异常被吞、返回 False，不冒泡。
"""
import pytest

from bottleneck_hunter.watchlist import push
from bottleneck_hunter.watchlist.push import build_push_payload, push_event


def test_payload_bark():
    assert build_push_payload("bark", "标题", "正文") == {"json": {"title": "标题", "body": "正文"}}


def test_payload_serverchan():
    assert build_push_payload("serverchan", "标题", "正文") == {"data": {"title": "标题", "desp": "正文"}}


def test_payload_feishu():
    p = build_push_payload("feishu", "标题", "正文")
    assert p["json"]["msg_type"] == "text"
    assert p["json"]["content"]["text"] == "标题\n正文"


def test_payload_unknown_channel_empty():
    assert build_push_payload("telegram", "t", "b") == {}


async def test_push_event_empty_url_noop(monkeypatch):
    """空 url → 不构造 client、不发请求（误触 get_http_client 就炸，以证明未被调用）。"""
    monkeypatch.setattr(push, "get_http_client",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("空 url 不应触碰 http client")))
    assert await push_event("", "bark", "t", "b") is False


async def test_push_event_unknown_channel_noop(monkeypatch):
    monkeypatch.setattr(push, "get_http_client",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("未知渠道不应触碰 http client")))
    assert await push_event("https://api.day.app/KEY", "telegram", "t", "b") is False


async def test_push_event_posts(monkeypatch):
    """有 url+已知渠道 → 调 get_http_client().post 一次，带正确 kwargs。"""
    calls = []

    class _FakeClient:
        async def post(self, url, **kwargs):
            calls.append((url, kwargs))

    monkeypatch.setattr(push, "get_http_client", lambda: _FakeClient())
    ok = await push_event("https://api.day.app/KEY", "bark", "标题", "正文")
    assert ok is True
    assert calls == [("https://api.day.app/KEY", {"json": {"title": "标题", "body": "正文"}})]


async def test_push_event_swallows_error(monkeypatch):
    """post 抛网络异常 → 被吞、返回 False，不冒泡（主流程不因推送失败中断）。"""
    class _BoomClient:
        async def post(self, url, **kwargs):
            raise RuntimeError("network down")

    monkeypatch.setattr(push, "get_http_client", lambda: _BoomClient())
    assert await push_event("https://x", "bark", "t", "b") is False


async def test_patch_stores_push_config_and_validates(tmp_path, monkeypatch):
    """settings patch：DEFAULTS 含两键；合法渠道/http(s) url 存入；非法渠道/非 http scheme 落空(trust boundary)。"""
    from bottleneck_hunter.watchlist.store import WatchlistStore
    from bottleneck_hunter.watchlist.store_budget import AUTO_UPDATE_DEFAULTS
    from bottleneck_hunter.web import settings_api
    from bottleneck_hunter.web.settings_api import AutoUpdatePatch, patch_auto_update

    assert AUTO_UPDATE_DEFAULTS["push_webhook_url"] == "" and AUTO_UPDATE_DEFAULTS["push_channel"] == ""

    monkeypatch.setattr(settings_api, "_store", WatchlistStore(str(tmp_path / "s.db")))
    user = {"sub": "u1"}

    # 合法：bark + https → 原样存
    r = await patch_auto_update(
        AutoUpdatePatch(push_channel="bark", push_webhook_url="https://api.day.app/KEY"), user=user)
    assert r["config"]["push_channel"] == "bark"
    assert r["config"]["push_webhook_url"] == "https://api.day.app/KEY"

    # 非法渠道 → 落空
    r = await patch_auto_update(AutoUpdatePatch(push_channel="telegram"), user=user)
    assert r["config"]["push_channel"] == ""

    # 非 http scheme(file://) → 落空，挡掉 scheme 滥用
    r = await patch_auto_update(AutoUpdatePatch(push_webhook_url="file:///etc/passwd"), user=user)
    assert r["config"]["push_webhook_url"] == ""

    # 用户主动清空 → 允许空串
    r = await patch_auto_update(AutoUpdatePatch(push_webhook_url=""), user=user)
    assert r["config"]["push_webhook_url"] == ""


async def test_oplog_pushes_auto_update_only(tmp_path, monkeypatch):
    """oplog 挂钩：配了 webhook 的用户，auto_update 触发一次 POST；user_action(手动)不推；未配用户零副作用。"""
    import asyncio

    from bottleneck_hunter.watchlist import push
    from bottleneck_hunter.watchlist.store import WatchlistStore
    from bottleneck_hunter.web import oplog

    store = WatchlistStore(str(tmp_path / "op.db"))
    monkeypatch.setattr(oplog, "_store", store)   # monkeypatch → 测后自动还原，不污染全局 _store
    su = store.for_user("u1")
    su.set_auto_update_config("push_webhook_url", "https://api.day.app/KEY")
    su.set_auto_update_config("push_channel", "bark")

    captured = []

    async def fake_push(url, channel, title, body):
        captured.append((url, channel, title, body))
        return True

    monkeypatch.setattr(push, "push_event", fake_push)   # _maybe_push 内 from ...push import push_event 取到它

    # auto_update → 推一次，body 带 market
    oplog.record_operation("u1", "决策已更新", category="auto_update", detail="L3 完成", market="us_stock")
    await asyncio.sleep(0.05)
    assert len(captured) == 1
    assert captured[0][0] == "https://api.day.app/KEY" and captured[0][1] == "bark"
    assert "us_stock" in captured[0][3]

    # user_action(手动操作) → 不推
    captured.clear()
    oplog.record_operation("u1", "手动加仓", category="user_action", detail="ok")
    await asyncio.sleep(0.05)
    assert captured == []

    # 未配 webhook 的 u2 → auto_update 也零副作用
    oplog.record_operation("u2", "决策已更新", category="auto_update", detail="x")
    await asyncio.sleep(0.05)
    assert captured == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
