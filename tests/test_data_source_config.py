"""付费数据源配置自检 — 加密存储/隔离、probe 探测、key resolver。不打真实外网。"""

import bottleneck_hunter.data_provider.data_source_catalog as cat
from bottleneck_hunter.auth.crypto import decrypt, make_hint
from bottleneck_hunter.auth.store import AuthStore


def _store(tmp_path):
    return AuthStore(db_path=tmp_path / "auth.db")


def test_save_get_encrypt_roundtrip(tmp_path):
    s = _store(tmp_path)
    from bottleneck_hunter.auth.crypto import encrypt
    s.save_data_source_key("u1", "fmp", "", encrypt("secret-key-123"), make_hint("secret-key-123"))
    enc = s.get_data_source_key_encrypted("u1", "fmp")
    assert enc and decrypt(enc) == "secret-key-123"       # 存密文、可解回原文
    lst = s.get_data_source_keys("u1")
    assert len(lst) == 1 and lst[0]["source_id"] == "fmp"
    assert "secret-key-123" not in str(lst)                # list 只回 hint 不回明文
    assert "encrypted_key" not in lst[0]


def test_user_isolation(tmp_path):
    s = _store(tmp_path)
    from bottleneck_hunter.auth.crypto import encrypt
    s.save_data_source_key("u1", "fmp", "", encrypt("k1"), make_hint("k1"))
    assert s.get_data_source_key_encrypted("u2", "fmp") is None   # u2 看不到 u1 的
    assert s.get_data_source_keys("u2") == []


def test_delete(tmp_path):
    s = _store(tmp_path)
    from bottleneck_hunter.auth.crypto import encrypt
    s.save_data_source_key("u1", "fmp", "", encrypt("k1"), make_hint("k1"))
    assert s.delete_data_source_key("u1", "fmp") is True
    assert s.get_data_source_key_encrypted("u1", "fmp") is None
    assert s.delete_data_source_key("u1", "fmp") is False         # 再删无效


def test_make_hint_masks():
    h = make_hint("sk-abcdef1234567890")
    assert "abcdef1234" not in h and h                            # 中间被脱敏


def test_probe_testable_source_success(monkeypatch):
    class _Resp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return [{"price": 190.5}]
    monkeypatch.setattr(cat.requests, "get", lambda *a, **k: _Resp())
    ok, msg = cat.probe_source("fmp", "fakekey")
    assert ok is True and "190.5" in msg


def test_probe_auth_fail(monkeypatch):
    class _Resp:
        status_code = 401
        def raise_for_status(self): pass
        def json(self): return {}
    monkeypatch.setattr(cat.requests, "get", lambda *a, **k: _Resp())
    ok, msg = cat.probe_source("fmp", "badkey")
    assert ok is False and "认证" in msg


def test_probe_timeout(monkeypatch):
    def _boom(*a, **k):
        raise cat.requests.Timeout()
    monkeypatch.setattr(cat.requests, "get", _boom)
    ok, msg = cat.probe_source("finnhub", "k")
    assert ok is False and "超时" in msg


def test_probe_unknown_source(monkeypatch):
    # 未知数据源不应发起任何网络请求，直接返回未知提示
    def _fail(*a, **k):
        raise AssertionError("不该联网")
    monkeypatch.setattr(cat.requests, "get", _fail)
    monkeypatch.setattr(cat.requests, "post", _fail)
    ok, msg = cat.probe_source("nonexistent_source", "whatever")
    assert ok is False and "未知数据源" in msg


def test_probe_empty_key_rejected():
    ok, msg = cat.probe_source("fmp", "")
    assert ok is False and "API Key" in msg


def test_resolve_key_db_then_env(tmp_path, monkeypatch):
    """严格隔离：只认该用户自己的 key；无 key 时返回空（不再回退 env）。"""
    import bottleneck_hunter.auth.store as store_mod
    from bottleneck_hunter.auth.crypto import encrypt
    s = _store(tmp_path)
    s.save_data_source_key("u1", "finnhub", "", encrypt("db-key"), make_hint("db-key"))
    monkeypatch.setattr(store_mod, "AuthStore", lambda *a, **k: s)
    assert cat.resolve_data_source_key("finnhub", "u1") == "db-key"
    # 即使 env 有全局 key，未配置的用户也拿不到（不读 env）
    monkeypatch.setenv("FINNHUB_API_KEY", "env-key")
    assert cat.resolve_data_source_key("finnhub", "nouser") == ""


def test_resolve_key_no_cross_user_borrow(tmp_path, monkeypatch):
    """严格隔离：没配 key 的用户不得借用别人的 key；无 user 上下文也不借用（后台亦严格）。"""
    import bottleneck_hunter.auth.store as store_mod
    from bottleneck_hunter.auth.crypto import encrypt
    from bottleneck_hunter.auth import current_user as CU
    s = _store(tmp_path)
    s.save_data_source_key("owner", "finnhub", "", encrypt("owner-key"), make_hint("owner-key"))
    monkeypatch.setattr(store_mod, "AuthStore", lambda *a, **k: s)
    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
    # 别的用户没配 → 空，绝不借用 owner-key
    assert cat.resolve_data_source_key("finnhub", "stranger") == ""
    # 无 user 上下文（显式空 + ContextVar 空）→ 也不借用
    tok = CU.set_current_user("")
    try:
        assert cat.resolve_data_source_key("finnhub", "") == ""
    finally:
        CU.reset_current_user(tok)


def test_catalog_json_safe():
    c = cat.get_catalog()
    ids = [s["id"] for s in c]
    assert "fmp" in ids and "custom" in ids and "polygon" in ids
    assert all("probe" not in s for s in c)                       # probe 函数不进 JSON


if __name__ == "__main__":
    test_catalog_json_safe()
    test_probe_empty_key_rejected()
    test_make_hint_masks()
    print("smoke ok (run full suite via pytest)")
