"""认证 cookie 加固：Secure 判定 + set_auth_cookie 落 Secure 属性。"""
import os

from fastapi import Response

from bottleneck_hunter.auth.jwt_utils import set_auth_cookie, get_cookie_name
from bottleneck_hunter.web.auth_api import _cookie_secure


class _Req:
    def __init__(self, xf=None, scheme="http"):
        self.headers = {"x-forwarded-proto": xf} if xf else {}
        self.url = type("U", (), {"scheme": scheme})()


def test_cookie_secure_forwarded_proto():
    assert _cookie_secure(_Req(xf="https")) is True
    assert _cookie_secure(_Req(xf="http")) is False   # 代理明说 http → 不加，避免 cookie 丢失


def test_cookie_secure_direct_scheme():
    assert _cookie_secure(_Req(scheme="https")) is True


def test_cookie_secure_local_http_default_off():
    os.environ.pop("BH_COOKIE_SECURE", None)
    assert _cookie_secure(_Req()) is False            # 本地 http 默认不加（否则登录后 cookie 不回传）


def test_cookie_secure_env_override():
    os.environ["BH_COOKIE_SECURE"] = "1"
    try:
        assert _cookie_secure(_Req()) is True
    finally:
        os.environ.pop("BH_COOKIE_SECURE", None)


def test_set_auth_cookie_writes_secure_flag():
    r = Response()
    set_auth_cookie(r, "tok", secure=True)
    sc = r.headers.get("set-cookie", "")
    assert get_cookie_name() in sc and "Secure" in sc and "HttpOnly" in sc

    r2 = Response()
    set_auth_cookie(r2, "tok", secure=False)
    assert "Secure" not in r2.headers.get("set-cookie", "")
