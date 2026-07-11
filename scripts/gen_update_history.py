#!/usr/bin/env python
"""从 git 提交历史的「📢 白话标记」自动生成首页更新记录（UPDATE_HISTORY.json）。

约定：在 commit message 里写一行以 📢 开头的**面向用户的白话**，提交后会自动出现在
首页「🆕 更新历史」，无需手动编辑 JSON。

  格式：  📢 标题 | 详细说明        （| 可省略，省略则只有标题）
  多条：  一条 commit 可写多行 📢，各成一条更新记录
  别名：  [发布] 与 📢 等效（emoji 输入不便时用）

合并策略：git 提取的记录 + 现有 UPDATE_HISTORY.json 手动条目，按 title 去重（保留已有），
按 date 倒序。**幂等**：重复运行结果一致，现有手动条目不丢。

用法：
  python scripts/gen_update_history.py          # 生成/更新 UPDATE_HISTORY.json
  python scripts/gen_update_history.py --quiet   # 静默（供 git hook 用）
自动触发见 .githooks/post-commit（需 git config core.hooksPath .githooks 启用一次）。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

_MARKERS = ("📢", "[发布]")
ROOT = Path(__file__).resolve().parents[1]
JSON_PATH = ROOT / "UPDATE_HISTORY.json"


def parse_marker_lines(body: str) -> list[tuple[str, str]]:
    """从一条 commit message 提取所有 📢 白话行 → [(title, summary), ...]。纯函数。"""
    out: list[tuple[str, str]] = []
    for line in body.splitlines():
        s = line.strip()
        mark = next((m for m in _MARKERS if s.startswith(m)), None)
        if not mark:
            continue
        text = s[len(mark):].strip().lstrip(":：").strip()
        if not text:
            continue
        for sep in ("|", "｜"):          # 半/全角竖线拆 title|summary
            if sep in text:
                t, sm = text.split(sep, 1)
                out.append((t.strip(), sm.strip()))
                break
        else:
            out.append((text, ""))       # 无分隔 → 仅标题
    return out


def merge(existing: list[dict], git_records: list[dict]) -> list[dict]:
    """合并现有条目 + git 提取记录：按 (date, title) 去重（保留已有），按 date 倒序。纯函数。

    去重键含 date：同一 title 在不同日期视为不同更新，允许「改了 commit 里的白话文案」
    在新日期下生效（旧条目仍保留，需要时手动删）。同日同 title 才算重复。
    """
    def key(e):
        return (str(e.get("date", "")), e.get("title"))
    seen = {key(e) for e in existing if isinstance(e, dict)}
    merged = [e for e in existing if isinstance(e, dict)]
    for r in git_records:
        if key(r) in seen:
            continue
        seen.add(key(r))
        merged.append(r)
    merged.sort(key=lambda x: str(x.get("date", "")), reverse=True)
    return merged


def _git_commits(cwd: Path, limit: int = 500) -> list[tuple[str, str]]:
    """返回 [(date, body), ...]，最新在前。非 git 仓库/无 git → []。
    limit：只扫最近 N 条（历史久远的 📢 已固化进 JSON，无需反复重扫，防大仓库变慢）。"""
    try:
        out = subprocess.run(
            ["git", "log", f"-n{limit}", "--date=short", "--format=%ad%x1f%B%x1e"],
            cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", check=True,
        ).stdout
    except Exception:  # noqa: BLE001
        return []
    commits = []
    for rec in out.split("\x1e"):
        rec = rec.strip()
        if not rec or "\x1f" not in rec:
            continue
        date, body = rec.split("\x1f", 1)
        commits.append((date.strip(), body))
    return commits


def generate(root: Path = ROOT) -> list[dict]:
    """从 root 仓库的 git 历史 + 现有 JSON 生成合并后的更新记录列表。"""
    json_path = root / "UPDATE_HISTORY.json"
    existing = []
    if json_path.exists():
        try:
            existing = json.loads(json_path.read_text(encoding="utf-8")) or []
        except Exception:  # noqa: BLE001
            existing = []
    git_records = []
    for date, body in _git_commits(root):
        for title, summary in parse_marker_lines(body):
            git_records.append({"date": date, "title": title, "summary": summary})
    return merge(existing if isinstance(existing, list) else [], git_records)


def main() -> None:
    quiet = "--quiet" in sys.argv
    merged = generate(ROOT)
    new_text = json.dumps(merged, ensure_ascii=False, indent=2) + "\n"
    old_text = JSON_PATH.read_text(encoding="utf-8") if JSON_PATH.exists() else ""
    if new_text != old_text:
        JSON_PATH.write_text(new_text, encoding="utf-8")
        if not quiet:
            print(f"UPDATE_HISTORY.json 已更新（共 {len(merged)} 条）")
    elif not quiet:
        print("UPDATE_HISTORY.json 无变化")


if __name__ == "__main__":
    main()
