"""CLAUDE.md / AGENTS.md 的路径引用必须真实存在。

CLAUDE.md 是每次会话最被信任的输入，却没有任何机制保证它没腐化：
一次重命名、一次文件移动，注解就悄悄指向不存在的路径，之后所有会话都在用过期事实。

判据：
  - 反引号里的路径（含 / 且带常见后缀）必须存在
  - markdown 链接的相对目标必须存在
  - @path 形式的引用必须存在

故意不校验纯文件名（如 `store.py`）——它可能只是行文举例，不是路径断言。
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCUMENTS = ("CLAUDE.md", "AGENTS.md")

# 反引号内、含目录分隔符且带已知后缀的路径
BACKTICK_PATH_RE = re.compile(r"`([A-Za-z0-9_./-]+/[A-Za-z0-9_./-]+\.(?:py|md|json|toml|yml|yaml|js|ts|html|css|sh))`")
MARKDOWN_LINK_RE = re.compile(r"\]\(([^)\s]+)\)")
AT_PATH_RE = re.compile(r"(?m)^@([^\s]+)$")
REMOTE_PREFIXES = ("http://", "https://", "mailto:", "#")


def referenced_paths(text: str) -> set[str]:
    """抽出文档里所有"应当存在"的路径引用。"""
    found: set[str] = set()
    found.update(BACKTICK_PATH_RE.findall(text))
    for target in MARKDOWN_LINK_RE.findall(text):
        if not target.startswith(REMOTE_PREFIXES):
            found.add(target.split("#", 1)[0])
    for target in AT_PATH_RE.findall(text):
        found.add(target)
    return {p for p in found if p}


def missing_references(path: Path) -> list[str]:
    """返回该文档中指向不存在文件的引用列表。"""
    text = path.read_text(encoding="utf-8")
    missing = []
    for ref in sorted(referenced_paths(text)):
        if not ref.strip():
            continue
        resolved = (path.parent / ref).resolve()
        if not resolved.exists():
            missing.append(ref)
    return missing


def test_documents_exist() -> None:
    for name in DOCUMENTS:
        assert (REPO_ROOT / name).is_file(), f"{name} missing"


def test_references_resolve() -> None:
    """所有路径引用必须落地。"""
    problems = []
    for name in DOCUMENTS:
        doc = REPO_ROOT / name
        if not doc.is_file():
            continue
        for ref in missing_references(doc):
            problems.append(f"{name} -> {ref}")
    assert not problems, "CLAUDE.md/AGENTS.md 引用了不存在的路径:\n" + "\n".join(problems)


def test_extractor_actually_detects(tmp_path: Path) -> None:
    """自检：抽取器必须真能抓到坏引用，否则上面那条测试等于空跑。"""
    # 在 tmp 下造一个真实存在的文件，用它证明"存在的引用不报"
    real = tmp_path / "bottleneck_hunter" / "watchlist" / "evidence.py"
    real.parent.mkdir(parents=True)
    real.write_text("", encoding="utf-8")

    doc = tmp_path / "CLAUDE.md"
    doc.write_text(
        "- `bottleneck_hunter/does_not_exist.py`\n"
        "- [x](./also_missing.md)\n"
        "- `bottleneck_hunter/watchlist/evidence.py`\n",
        encoding="utf-8",
    )
    missing = missing_references(doc)
    assert "bottleneck_hunter/does_not_exist.py" in missing
    assert "./also_missing.md" in missing
    assert "bottleneck_hunter/watchlist/evidence.py" not in missing
