"""首页更新记录自动生成脚本（scripts/gen_update_history.py）的单元测试。

覆盖：📢 标记解析（各格式/别名/多行）、合并去重排序、以及真实临时 git repo 的端到端提取。
"""
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# 脚本不是包，按路径加载为模块
_spec = importlib.util.spec_from_file_location("guh", ROOT / "scripts" / "gen_update_history.py")
guh = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guh)


# ── 标记解析 ───────────────────────────────────────────
@pytest.mark.parametrize("body,expected", [
    ("feat: x\n\n📢 标题A | 说明A", [("标题A", "说明A")]),
    ("📢 仅标题", [("仅标题", "")]),
    ("📢 全角竖线｜说明B", [("全角竖线", "说明B")]),
    ("[发布] 别名标记 | 也生效", [("别名标记", "也生效")]),
    ("📢：紧跟冒号", [("紧跟冒号", "")]),                # 标记后紧跟冒号被剥离
    ("📢 中文冒号：不拆分", [("中文冒号：不拆分", "")]),   # 中文冒号非分隔符（仅 | ｜）
    ("一行\n📢 A|a\n📢 B|b\n尾", [("A", "a"), ("B", "b")]),  # 多行
    ("feat: 纯技术提交，无标记", []),
    ("📢    ", []),   # 空标记不产生记录
])
def test_parse_marker_lines(body, expected):
    assert guh.parse_marker_lines(body) == expected


# ── 合并去重排序 ───────────────────────────────────────
def test_merge_dedup_by_date_title_and_sorts():
    existing = [{"date": "2026-07-05", "title": "老条目", "summary": "手动写的"}]
    git_records = [
        {"date": "2026-07-10", "title": "新条目", "summary": "自动"},
        {"date": "2026-07-05", "title": "老条目", "summary": "git版本"},   # 同(date,title) → 跳过，保留 existing
        {"date": "2026-07-08", "title": "老条目", "summary": "改了文案"},  # 同 title 异 date → 保留(算新更新)
    ]
    merged = guh.merge(existing, git_records)
    assert [m["date"] for m in merged] == ["2026-07-10", "2026-07-08", "2026-07-05"]  # date 倒序
    old0705 = next(m for m in merged if m["date"] == "2026-07-05")
    assert old0705["summary"] == "手动写的"   # 同(date,title)保留现有，不被 git 覆盖


# ── 真实 git repo 端到端 ───────────────────────────────
def _git(repo, *args):
    subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True)


def _init_repo(repo):
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")


def _commit(repo, message):
    (repo / "f.txt").write_text(str(len(message)))   # 每次内容不同以产生 commit
    _git(repo, "add", ".")
    msg = repo / "_m.txt"
    msg.write_text(message, encoding="utf-8")
    _git(repo, "commit", "-F", str(msg))


def test_generate_from_real_git(tmp_path):
    _init_repo(tmp_path)
    _commit(tmp_path, "feat: 新功能\n\n📢 观察池支持批量导入 | 一次粘贴多个代码即可添加\n")
    recs = guh.generate(tmp_path)
    hit = [r for r in recs if r["title"] == "观察池支持批量导入"]
    assert hit and hit[0]["summary"] == "一次粘贴多个代码即可添加"
    assert hit[0]["date"]   # 有日期


def test_generate_tolerates_corrupt_json(tmp_path):
    """现有 UPDATE_HISTORY.json 损坏时 generate 不崩（回退空 existing）。"""
    _init_repo(tmp_path)
    _commit(tmp_path, "feat: x\n\n📢 标题A | 说明A")
    (tmp_path / "UPDATE_HISTORY.json").write_text("{ 坏的 json ,,,", encoding="utf-8")
    recs = guh.generate(tmp_path)   # 不抛异常
    assert isinstance(recs, list)
    assert any(r["title"] == "标题A" for r in recs)


def test_main_writes_only_on_change(tmp_path, monkeypatch):
    """main() 仅在内容真变时写盘（幂等，避免每次 commit 产生空 diff）。"""
    import time
    _init_repo(tmp_path)
    _commit(tmp_path, "feat: x\n\n📢 幂等测试 | 说明")
    jp = tmp_path / "UPDATE_HISTORY.json"
    monkeypatch.setattr(guh, "ROOT", tmp_path)
    monkeypatch.setattr(guh, "JSON_PATH", jp)

    guh.main()                              # 首次：写入
    assert jp.exists() and "幂等测试" in jp.read_text(encoding="utf-8")
    mtime1 = jp.stat().st_mtime
    time.sleep(0.02)
    guh.main()                              # 二次：无变化 → 不写
    assert jp.stat().st_mtime == mtime1     # mtime 不变 = 未写盘（幂等）


def test_generate_idempotent_no_marker(tmp_path):
    """无 📢 的 commit → 不产生记录（幂等，不误伤）。"""
    repo = tmp_path
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "f.txt").write_text("x")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "feat: 纯技术提交")
    assert guh.generate(repo) == []   # 无标记 → 空
