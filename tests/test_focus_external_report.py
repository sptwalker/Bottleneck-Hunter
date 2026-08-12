"""焦点块外部研究报告注入 `_external_report_text` + 焦点块接线自检：

- FOCUS_REPORT_DIR 下有 {ticker}.pdf → 读出文本，焦点块含"外部研究报告"段
- env 未配 / 空 ticker → ""（静默降级）
- 路径穿越（../secret）被净化 → ""，绝不逃逸配置目录

真研报解析走 pymupdf（已装），本测用 fitz 现造 PDF，不依赖外部文件。
"""
import fitz

from bottleneck_hunter.watchlist import macro_consultation as mc


def _make_pdf(path, text):
    doc = fitz.open()
    doc.new_page().insert_text((72, 72), text)
    doc.save(str(path))
    doc.close()


class _EmptyStore:
    """空库：证明仅有外部研报时焦点块也点亮。"""
    def get_company_profile(self, t): return None
    def get_latest_snapshot(self, t): return None
    def get_institutional_holders(self, t, limit=50): return []
    def get_analyst_ratings(self, t, limit=50): return []
    def get_news(self, t, limit=8): return []
    def get_earnings(self, t): return []
    def get_options(self, t, limit=1): return []
    def list_all(self): return []
    def get_catalysts_for_entry(self, e, active_only=True): return []
    def get_focus_report(self, t): return None


class _UploadStore(_EmptyStore):
    """有用户上传研报（DB）的库：证明 DB 优先于磁盘目录。"""
    def get_focus_report(self, t):
        return {"report_text": "DB_UPLOADED Morgan Stanley target 720", "filename": "x.pdf", "char_len": 34}


def test_external_report_read(tmp_path, monkeypatch):
    _make_pdf(tmp_path / "SNPS.pdf", "CFRA Synopsys FY26 Revenue 9690 EPS 14.84")
    monkeypatch.setenv("FOCUS_REPORT_DIR", str(tmp_path))
    txt = mc._external_report_text("SNPS")
    assert "Synopsys" in txt and "14.84" in txt
    assert mc._external_report_text("snps") == txt   # 大小写归一，命中同一文件


def test_focus_block_includes_external_report(tmp_path, monkeypatch):
    _make_pdf(tmp_path / "SNPS.pdf", "CFRA Synopsys Revenue 9690 EPS 14.84")
    monkeypatch.setenv("FOCUS_REPORT_DIR", str(tmp_path))
    block = mc._focus_ticker_block(_EmptyStore(), "SNPS")
    assert "外部研究报告" in block and "14.84" in block


def test_missing_env_and_blank(monkeypatch):
    monkeypatch.delenv("FOCUS_REPORT_DIR", raising=False)
    assert mc._external_report_text("SNPS") == ""


def test_path_traversal_blocked(tmp_path, monkeypatch):
    _make_pdf(tmp_path / "SNPS.pdf", "should not leak via traversal")
    monkeypatch.setenv("FOCUS_REPORT_DIR", str(tmp_path))
    assert mc._external_report_text("../../secret") == ""   # 净化后无对应文件
    assert mc._external_report_text("") == ""


def test_db_upload_wins_over_disk(tmp_path, monkeypatch):
    """用户上传（DB）优先：即便磁盘目录也有同股 PDF，焦点块引用 DB 文本。"""
    _make_pdf(tmp_path / "SNPS.pdf", "DISK_FALLBACK old broker note")
    monkeypatch.setenv("FOCUS_REPORT_DIR", str(tmp_path))
    block = mc._focus_ticker_block(_UploadStore(), "SNPS")
    assert "DB_UPLOADED" in block and "720" in block
    assert "DISK_FALLBACK" not in block   # DB 命中即不读盘


def test_extract_report_text_from_bytes(tmp_path):
    """上传端点用的 extract_report_text 接受 bytes（内存 PDF）并抽出文本。"""
    p = tmp_path / "m.pdf"
    _make_pdf(p, "Cadence upgrade to Buy")
    txt = mc.extract_report_text(p.read_bytes(), pages=6)
    assert "Cadence" in txt and "Buy" in txt


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
