"""VIP P0 存储层：财务文档加密存取 / 幂等去重 / 无 PII 泄漏 / 即焚 / 审计 / 删号级联 / require_vip。"""
import pytest

from bottleneck_hunter.auth.store import AuthStore


@pytest.fixture
def store(tmp_path):
    return AuthStore(tmp_path / "auth.db")


def test_financial_doc_encrypt_roundtrip(store):
    did = store.create_financial_doc(
        "u1", content_hash="h1", broker="citi", period_end="2026-06-30",
        file_name="Integrated Statement Jun 2026.pdf",
        parsed_json='{"positions":[{"ticker":"GOOGL","mv":1205022.5}]}',
        recon_flags={"pos_shares": "ok"})
    raw = store.find_financial_doc_by_hash("u1", "h1")
    # 明文绝不落库：密文列里查不到明文
    assert raw and raw["parsed_json_encrypted"] and "GOOGL" not in raw["parsed_json_encrypted"]
    # 需要时可解密取回
    d = store.get_financial_doc("u1", did, decrypt_parsed=True)
    assert "GOOGL" in d["parsed_json"]
    # 对外返回绝不含密文列
    assert "parsed_json_encrypted" not in d and "raw_pdf_encrypted" not in d
    # file_hint 脱敏、file_name 内部保留
    assert raw["file_hint"] and raw["broker"] == "citi"


def test_idempotent_dedup(store):
    store.create_financial_doc("u1", content_hash="h1", parsed_json="{}")
    with pytest.raises(Exception):          # UNIQUE(user_id, content_hash)
        store.create_financial_doc("u1", content_hash="h1", parsed_json="{}")
    store.create_financial_doc("u2", content_hash="h1", parsed_json="{}")  # 异用户同 hash 允许


def test_list_no_pii_leak(store):
    store.create_financial_doc("u1", content_hash="h1", parsed_json='{"secret":"x"}',
                               recon_flags={"portfolio_equity": "fail"})
    rows = store.list_financial_docs("u1")
    assert rows and "parsed_json_encrypted" not in rows[0] and "raw_pdf_encrypted" not in rows[0]
    assert "recon_flags_json" in rows[0] and "status" in rows[0]


def test_update_status_purge_raw(store):
    did = store.create_financial_doc("u1", content_hash="h1", raw_pdf_b64="cGRmYnl0ZXM=", parsed_json="{}")
    assert store.find_financial_doc_by_hash("u1", "h1")["raw_pdf_encrypted"]   # 原始密文在
    assert store.update_financial_doc_status("u1", did, "normalized", purge_raw=True)
    row = store.find_financial_doc_by_hash("u1", "h1")
    assert row["status"] == "normalized" and row["raw_pdf_encrypted"] == "" and row["purged_at"]  # 即焚


def test_advice_audit_and_cascade(store):
    store.create_financial_doc("u1", content_hash="h1", parsed_json="{}")
    store.create_advice_audit("u1", advice_type="report", advice_ref="r1",
                              source_doc_ids=["d1"], disclaimer_version="2026-07-v1",
                              model_provider="deepseek", model_name="deepseek-chat")
    n = store.delete_all_user_financial_docs("u1")
    assert n >= 2
    assert store.find_financial_doc_by_hash("u1", "h1") is None
    assert store.list_financial_docs("u1") == []


def test_require_vip(monkeypatch):
    from bottleneck_hunter.auth import dependencies as dep
    # admin 直通，不查库
    assert dep.require_vip({"sub": "a", "role": "admin"})["role"] == "admin"

    class _Vip:  # settings_json.vip == true
        settings_json = '{"vip": true}'

    class _Plain:
        settings_json = '{}'

    monkeypatch.setattr("bottleneck_hunter.auth.store.AuthStore.__init__", lambda self, *a, **k: None)
    monkeypatch.setattr("bottleneck_hunter.auth.store.AuthStore.get_user_by_id",
                        lambda self, uid: _Vip() if uid == "vip" else _Plain())
    assert dep.require_vip({"sub": "vip", "role": "user"})["sub"] == "vip"
    with pytest.raises(Exception):   # HTTPException 403
        dep.require_vip({"sub": "nope", "role": "user"})
