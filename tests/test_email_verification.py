"""邮箱验证注册 + 账户管理 —— store 与 email_sender 单元测试。"""

import tempfile
from pathlib import Path

import pytest

from bottleneck_hunter.auth.store import AuthStore
from bottleneck_hunter.auth import email_sender


@pytest.fixture
def store(tmp_path):
    return AuthStore(tmp_path / "auth.db")


class TestVerificationCode:
    def test_create_and_verify_success(self, store):
        store.create_verification("a@e.com", "123456", "register", {"username": "a"})
        ok, msg, payload = store.verify_code("a@e.com", "register", "123456")
        assert ok and payload == {"username": "a"}

    def test_wrong_code_fails_and_counts(self, store):
        store.create_verification("b@e.com", "123456", "register", {})
        ok, msg, _ = store.verify_code("b@e.com", "register", "000000")
        assert not ok and "错误" in msg

    def test_success_consumes_record(self, store):
        store.create_verification("c@e.com", "111111", "register", {})
        assert store.verify_code("c@e.com", "register", "111111")[0]
        # 再验证同码应失败（已消费）
        assert not store.verify_code("c@e.com", "register", "111111")[0]

    def test_expired_code(self, store):
        store.create_verification("d@e.com", "222222", "register", {}, ttl_seconds=-1)
        ok, msg, _ = store.verify_code("d@e.com", "register", "222222")
        assert not ok and "过期" in msg

    def test_attempts_lockout(self, store):
        store.create_verification("e@e.com", "333333", "register", {})
        for _ in range(5):
            store.verify_code("e@e.com", "register", "000000")
        # 第 6 次即使正确也被作废
        ok, msg, _ = store.verify_code("e@e.com", "register", "333333")
        assert not ok

    def test_create_replaces_previous(self, store):
        store.create_verification("f@e.com", "aaaaaa", "register", {"v": 1})
        store.create_verification("f@e.com", "bbbbbb", "register", {"v": 2})
        # 旧码失效，新码有效
        assert not store.verify_code("f@e.com", "register", "aaaaaa")[0]
        ok, _, payload = store.verify_code("f@e.com", "register", "bbbbbb")
        assert ok and payload == {"v": 2}

    def test_resend_cooldown_age(self, store):
        assert store.get_verification_age_seconds("g@e.com", "register") is None
        store.create_verification("g@e.com", "444444", "register", {})
        age = store.get_verification_age_seconds("g@e.com", "register")
        assert age is not None and age >= 0


class TestUserEmail:
    def test_create_user_with_password_hash(self, store):
        h = AuthStore.hash_password("secret123")
        u = store.create_user("alice", email="alice@e.com", password_hash=h, watchlist_limit=24)
        assert u.email == "alice@e.com"
        # 预哈希建号后，原始密码可校验通过
        assert store.verify_password(u, "secret123")

    def test_get_user_by_email_nocase(self, store):
        store.create_user("bob", password="pw12345678", email="Bob@Example.com")
        assert store.get_user_by_email("bob@example.com").username == "bob"

    def test_update_email(self, store):
        u = store.create_user("carol", password="pw12345678", email="c@e.com")
        store.update_email(u.id, "c2@e.com")
        assert store.get_user_by_id(u.id).email == "c2@e.com"


class TestEmailSender:
    def test_no_smtp_fallback_returns_true(self, monkeypatch, caplog):
        """SMTP 未配置：注册仍走通(返回 True)，但默认绝不把验证码写日志(防泄露接管)。"""
        monkeypatch.delenv("SMTP_HOST", raising=False)
        monkeypatch.delenv("BH_DEV_LOG_CODES", raising=False)
        assert email_sender.smtp_configured() is False
        import logging
        with caplog.at_level(logging.WARNING):
            ok = email_sender.send_verification_email("x@e.com", "654321", "register")
        assert ok is True
        assert "654321" not in caplog.text  # 默认绝不把验证码写日志

    def test_dev_opt_in_logs_code(self, monkeypatch, caplog):
        """仅显式 BH_DEV_LOG_CODES=1（本地开发）才打印验证码。"""
        monkeypatch.delenv("SMTP_HOST", raising=False)
        monkeypatch.setenv("BH_DEV_LOG_CODES", "1")
        import logging
        with caplog.at_level(logging.WARNING):
            email_sender.send_verification_email("x@e.com", "654321", "register")
        assert "654321" in caplog.text
