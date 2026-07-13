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
# 无 | 分隔时，在首个自然停顿处把整句切成「标题 + 摘要」，与既有两段式风格一致。
# 仅用子句停顿（括号/逗号/顿号/分号）；冒号不算分隔（与 | 语义一致，见测试「中文冒号：不拆分」）。
_SPLIT_SEPS = "（(，,、；;"
ROOT = Path(__file__).resolve().parents[1]
JSON_PATH = ROOT / "UPDATE_HISTORY.json"


def _auto_split(text: str) -> tuple[str, str]:
    """无竖线的白话整句 → (标题, 摘要)：在首个自然停顿处切分。纯函数、确定性（保证幂等）。

    - 取首个出现的分隔符（且标题至少 4 字，避免开头就切）。
    - 括号是补充说明 → 并入摘要；逗号/顿号/分号/冒号 → 作为分隔丢弃。
    - 找不到合适停顿 → 整句作标题、摘要空（退化为原行为）。
    """
    cut = next((i for i, ch in enumerate(text) if ch in _SPLIT_SEPS and i >= 4), None)
    if cut is None:
        return text, ""
    title = text[:cut].strip()
    if not title:
        return text, ""
    summary = text[cut:].strip() if text[cut] in "（(" else text[cut + 1:].strip()
    return title, summary


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
            out.append(_auto_split(text))  # 无竖线 → 自动切成标题+摘要
    return out


def merge(existing: list[dict], git_records: list[dict]) -> list[dict]:
    """合并现有条目 + git 提取记录：按 (date, title) 去重（保留已有），按完整时间 ts 倒序。纯函数。

    去重键含 date（天）：同一 title 在不同日期视为不同更新。
    ts（YYYY-MM-DD HH:MM）用于同日内按提交时间精确排序——git 已知的旧条目会被回填 ts，
    修正「同一天新条目沉底」的老问题。无 ts 的（超出扫描窗口/手动条目）按当日 00:00 排。
    """
    def key(e):
        return (str(e.get("date", "")), e.get("title"))

    by_key: dict = {}
    merged: list[dict] = []
    for e in existing:
        if isinstance(e, dict):
            merged.append(e)
            by_key[key(e)] = e
    for r in git_records:
        k = key(r)
        if k in by_key:
            ex = by_key[k]
            if not ex.get("ts") and r.get("ts"):
                ex["ts"] = r["ts"]  # 回填提交时间，修正同日排序
            continue
        by_key[k] = r
        merged.append(r)
    merged.sort(key=lambda x: x.get("ts") or (str(x.get("date", "")) + " 00:00"), reverse=True)
    return merged


def _git_commits(cwd: Path, limit: int = 500) -> list[tuple[str, str]]:
    """返回 [(datetime, body), ...]，最新在前。datetime 为「YYYY-MM-DD HH:MM」（提交时区，即北京）。
    非 git 仓库/无 git → []。
    limit：只扫最近 N 条（历史久远的 📢 已固化进 JSON，无需反复重扫，防大仓库变慢）。"""
    try:
        out = subprocess.run(
            ["git", "log", f"-n{limit}", "--date=format:%Y-%m-%d %H:%M", "--format=%ad%x1f%B%x1e"],
            cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", check=True,
        ).stdout
    except Exception:  # noqa: BLE001
        return []
    commits = []
    for rec in out.split("\x1e"):
        rec = rec.strip()
        if not rec or "\x1f" not in rec:
            continue
        dt, body = rec.split("\x1f", 1)
        commits.append((dt.strip(), body))
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
    for dt, body in _git_commits(root):
        day = dt[:10]
        for title, summary in parse_marker_lines(body):
            git_records.append({"date": day, "ts": dt, "title": title, "summary": summary})
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
