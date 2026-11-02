"""文档完整性检查：相对链接可解析、code fence 配平、UTF-8 可读。

判据只看**结构性事实**，不做文风判断：
  1. markdown 里的相对链接/图片目标必须指向真实存在的文件（绝对 URL 与锚点跳过）。
  2. ``` 代码围栏必须成对——配不平意味着渲染错乱，人工很难发现。
  3. 文件必须是合法 UTF-8。

只扫 docs/ 下的 md，以及仓库根的 *.md（README/CLAUDE/AGENTS 等）。
退出码：0 = 干净，1 = 有问题。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# [text](target) 与 ![alt](target)；排除行内代码里出现的伪链接
LINK_RE = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
IMAGE_RE = re.compile(r"!\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
# 行内代码 `...` 里的文字是**示例**（如 `[text](path)`），不是真链接，先剔除再抽链接
INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
REMOTE_PREFIXES = ("http://", "https://", "mailto:", "tel:", "#", "data:")


def fence_balanced(text: str) -> bool:
    """``` 围栏必须成对出现。"""
    return sum(1 for line in text.splitlines() if line.lstrip().startswith("```")) % 2 == 0


def iter_docs(repo_root: Path) -> list[Path]:
    docs = repo_root / "docs"
    files = list(docs.rglob("*.md")) if docs.is_dir() else []
    files.extend(p for p in repo_root.glob("*.md"))
    return sorted(set(files))


def relative_links(text: str) -> list[str]:
    """抽出需要落地检查的链接目标（跳过远程 URL、纯锚点、行内代码示例）。"""
    stripped = INLINE_CODE_RE.sub("", text)
    targets = []
    for regex in (LINK_RE, IMAGE_RE):
        for raw in regex.findall(stripped):
            target = raw.strip()
            if not target or target.startswith(REMOTE_PREFIXES):
                continue
            target = target.split("#", 1)[0]  # 去掉锚点只校验文件
            if target:
                targets.append(target)
    return targets


def check_document(path: Path, repo_root: Path) -> list[str]:
    """返回该文档的问题列表；空列表表示通过。"""
    problems = []
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return ["not valid UTF-8"]
    except OSError as exc:
        return [f"unreadable: {exc}"]

    if not fence_balanced(text):
        problems.append("unbalanced code fence")

    for target in relative_links(text):
        resolved = (path.parent / target).resolve()
        if not resolved.exists():
            problems.append(f"missing link target: {target}")
    return problems


def scan(repo_root: Path) -> list[tuple[Path, list[str]]]:
    bad = []
    for doc in iter_docs(repo_root):
        problems = check_document(doc, repo_root)
        if problems:
            bad.append((doc, problems))
    return bad


def _selftest(tmp_root: Path) -> None:
    """自检：好文档放行，坏链接与坏围栏必须抓到。"""
    good = tmp_root / "docs" / "good.md"
    good.parent.mkdir(parents=True, exist_ok=True)
    good.write_text("# ok\n\n[a](../docs/good.md)\n\n```\ncode\n```\n", encoding="utf-8")
    assert check_document(good, tmp_root) == [], check_document(good, tmp_root)

    bad_link = tmp_root / "docs" / "bad_link.md"
    bad_link.write_text("[x](./nope.md)\n", encoding="utf-8")
    assert any("missing link target" in p for p in check_document(bad_link, tmp_root))

    bad_fence = tmp_root / "docs" / "bad_fence.md"
    bad_fence.write_text("```\nunclosed\n", encoding="utf-8")
    assert "unbalanced code fence" in check_document(bad_fence, tmp_root)

    remote_ok = tmp_root / "docs" / "remote.md"
    remote_ok.write_text("[a](https://example.com) [b](#anchor)\n", encoding="utf-8")
    assert check_document(remote_ok, tmp_root) == []
    print("check_docs selftest passed")


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    if "--selftest" in sys.argv:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            _selftest(Path(tmp))
        return 0

    bad = scan(repo_root)
    if not bad:
        print("docs: clean")
        return 0
    total = sum(len(p) for _, p in bad)
    print(f"docs: {total} problem(s) across {len(bad)} file(s)")
    for path, problems in bad:
        rel = path.relative_to(repo_root)
        for problem in problems:
            print(f"  {rel}: {problem}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
