"""文本编码污染检测：抓 U+FFFD 替换符、私用区码点、以及已知的 GBK/CP1252 乱码串。

判据优先级：**只看结构性信号**，不做字符猜测。
  - U+FFFD 是解码失败的确定证据，绝不可能是作者本意。
  - 私用区 U+E000-F8FF 只会出现在编码恢复失败后的产物里。
  - GBK 双字节解码产生的固定乱码串（如 '锛' '鈥'），是 UTF-8 被当 GBK 解的指纹。
    '锛' / '鈥' 单独出现时可能是正常中文，因此只匹配**成对/成串**的组合。

只扫 git 跟踪的文本文件，跳过二进制与已知白名单。
退出码：0 = 干净，1 = 有污染。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

TEXT_SUFFIXES = frozenset({
    ".py", ".md", ".json", ".toml", ".yaml", ".yml",
    ".js", ".mjs", ".ts", ".html", ".css", ".sh", ".txt",
})

# UTF-8 被当作 GBK/CP936 解码后的特征串。
# '锛' 后接替换符 = UTF-8 的 3 字节标点被截断；'鈥' 同理（U+2010-2015 族）。
MOJIBAKE_MARKERS = {
    "锛�": "UTF-8 punctuation decoded as GBK (truncated 3-byte char)",
    "鈥�": "UTF-8 dash/quote decoded as GBK (truncated 3-byte char)",
    "ï¿½": "UTF-8 replacement char decoded as Latin-1",
    "â\u0080\u0094": "em dash decoded as Latin-1",
}

WHITELIST = frozenset({
    # 本脚本自身含乱码字面量用于匹配
    "scripts/check_mojibake.py",
})


def iter_text_files(repo_root: Path) -> list[Path]:
    """列出 git 跟踪的文本文件；不在 git 仓库时返回空列表。"""
    try:
        out = subprocess.run(
            ["git", "ls-files"],
            cwd=repo_root, capture_output=True, text=True, encoding="utf-8", check=True,
        ).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []
    files = []
    for rel in out.splitlines():
        rel = rel.strip()
        if not rel or Path(rel).suffix.lower() not in TEXT_SUFFIXES:
            continue
        if rel in WHITELIST:
            continue
        files.append(repo_root / rel)
    return files


def has_private_use(text: str) -> bool:
    """私用区码点：编码恢复失败后的典型残留。"""
    return any("" <= ch <= "" for ch in text)


def find_violations(text: str) -> list[str]:
    """返回该文本里的污染描述列表；空列表表示干净。"""
    hits = []
    if "�" in text:
        hits.append(f"U+FFFD x{text.count(chr(0xfffd))}")
    if has_private_use(text):
        hits.append("private-use codepoint")
    for marker, why in MOJIBAKE_MARKERS.items():
        if marker in text:
            hits.append(f"{why} ({marker!r})")
    return hits


def scan(repo_root: Path) -> list[tuple[Path, list[str]]]:
    bad = []
    for path in iter_text_files(repo_root):
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            bad.append((path, ["not valid UTF-8"]))
            continue
        hits = find_violations(text)
        if hits:
            bad.append((path, hits))
    return bad


def _selftest() -> None:
    """自检：干净的放行，污染的必须抓到。"""
    assert find_violations("正常中文 clean text") == []
    assert find_violations("坏的�字符")
    assert find_violations("私用区")
    assert find_violations("x = 1  # 锛�")
    print("check_mojibake selftest passed")


def main() -> int:
    if "--selftest" in sys.argv:
        _selftest()
        return 0
    repo_root = Path(__file__).resolve().parent.parent
    bad = scan(repo_root)
    if not bad:
        print("mojibake: clean")
        return 0
    print(f"mojibake: {len(bad)} file(s) affected")
    for path, hits in bad:
        print(f"  {path.relative_to(repo_root)}: {'; '.join(hits)}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
