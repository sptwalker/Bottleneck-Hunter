"""全球宏观背景研报注入 `_macro_disk_report_text` + `_macro_report_block` 自检：

- MACRO_REPORT_DIR（未配回退 FOCUS_REPORT_DIR）下 _macro.pdf → 读出文本；缺文件/未配 → ""
- 路径固定文件名，仍验直属目录（纵深防护）
- `_macro_report_block`：无 DB+无盘 → ""；仅磁盘 → 块含「全球宏观背景研报」+正文；DB 优先于磁盘
- 跨真实市场全局：以 us_stock 存、a_stock 读仍命中（内部固定 __macro__ 分区），仍按用户隔离

真研报解析走 pymupdf（已装），本测用 fitz 现造 PDF，不依赖外部文件。
"""
import fitz

from bottleneck_hunter.watchlist import macro_consultation as mc
from bottleneck_hunter.watchlist.store import WatchlistStore


def _make_pdf(path, text):
    doc = fitz.open()
    doc.new_page().insert_text((72, 72), text)
    doc.save(str(path))
    doc.close()


def _store(tmp_path):
    return WatchlistStore(str(tmp_path / "wl.db")).for_user("u1").for_market("us_stock")


def test_macro_disk_read(tmp_path, monkeypatch):
    _make_pdf(tmp_path / "_macro.pdf", "Nomura Vantage Point CPI 3.4 payrolls -23k")
    monkeypatch.setenv("MACRO_REPORT_DIR", str(tmp_path))
    txt = mc._macro_disk_report_text()
    assert "Nomura" in txt and "3.4" in txt


def test_macro_disk_falls_back_to_focus_dir(tmp_path, monkeypatch):
    """未配 MACRO_REPORT_DIR 时回退 FOCUS_REPORT_DIR。"""
    _make_pdf(tmp_path / "_macro.pdf", "fallback macro weekly")
    monkeypatch.delenv("MACRO_REPORT_DIR", raising=False)
    monkeypatch.setenv("FOCUS_REPORT_DIR", str(tmp_path))
    assert "fallback macro" in mc._macro_disk_report_text()


def test_macro_disk_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("MACRO_REPORT_DIR", str(tmp_path))   # 目录在但无 _macro.pdf
    assert mc._macro_disk_report_text() == ""
    monkeypatch.delenv("MACRO_REPORT_DIR", raising=False)
    monkeypatch.delenv("FOCUS_REPORT_DIR", raising=False)
    assert mc._macro_disk_report_text() == ""


def test_block_empty_when_no_source(tmp_path, monkeypatch):
    monkeypatch.delenv("MACRO_REPORT_DIR", raising=False)
    monkeypatch.delenv("FOCUS_REPORT_DIR", raising=False)
    assert mc._macro_report_block(_store(tmp_path)) == ""


def test_block_from_disk(tmp_path, monkeypatch):
    _make_pdf(tmp_path / "_macro.pdf", "MACRO_DISK Fed dovish silver +10.4")
    monkeypatch.setenv("MACRO_REPORT_DIR", str(tmp_path))
    block = mc._macro_report_block(_store(tmp_path))
    assert "全球宏观背景研报" in block and "MACRO_DISK" in block and "10.4" in block


def test_block_db_wins_over_disk(tmp_path, monkeypatch):
    """用户上传（DB，__macro__ 分区）优先于磁盘预置。"""
    _make_pdf(tmp_path / "_macro.pdf", "MACRO_DISK stale note")
    monkeypatch.setenv("MACRO_REPORT_DIR", str(tmp_path))
    st = _store(tmp_path)
    st.for_market(mc.MACRO_REPORT_MARKET).save_focus_report(
        mc.MACRO_REPORT_KEY, "宏观背景研报.pdf", "MACRO_DB Nomura Aug-11 CPI decisive")
    block = mc._macro_report_block(st)
    assert "MACRO_DB" in block and "Aug-11" in block
    assert "MACRO_DISK" not in block   # DB 命中即不读盘


def test_block_global_across_markets(tmp_path, monkeypatch):
    """以 us_stock 存，切 a_stock 读仍命中同一行（跨真实市场全局）；换用户则读不到（仍按用户隔离）。"""
    monkeypatch.delenv("MACRO_REPORT_DIR", raising=False)
    monkeypatch.delenv("FOCUS_REPORT_DIR", raising=False)
    st = _store(tmp_path)
    st.for_market(mc.MACRO_REPORT_MARKET).save_focus_report(
        mc.MACRO_REPORT_KEY, "m.pdf", "MACRO_DB global one row")
    # 同用户、切到 A股 分区调用 → 仍读到（block 内部固定 __macro__）
    assert "MACRO_DB" in mc._macro_report_block(st.for_market("a_stock"))
    # 换用户 → 读不到（用户隔离）
    assert mc._macro_report_block(st.for_user("u2")) == ""


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
