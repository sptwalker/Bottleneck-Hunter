#!/usr/bin/env python3
"""WAL 安全的 SQLite 备份 + 密钥分离备份。

为什么不能 `cp data/*.db`：WAL 模式下最新已提交事务还在 -wal 边车文件里，裸拷贝
会得到"回滚"了的旧快照，且多个 .db 之间不一致。这里用 sqlite3 `.backup` 在线 API，
对每个库做一致快照。

密钥分离：.encryption_key / .jwt_secret 与密文库同卷时，一次卷损坏两者俱失——
所有用户加密的 API Key 永久不可解。故密钥默认备份到独立目录（--keys-dir）。

用法：
    python scripts/backup.py                        # 备份到 backups/YYYYmmdd_HHMMSS/
    python scripts/backup.py --out /mnt/off/bh      # 库备份到异卷
    python scripts/backup.py --keys-dir /mnt/vault  # 密钥单独去另一处（强烈建议）
    python scripts/backup.py --retain 14            # 只保留最近 14 份库备份

放进 crontab（宿主）：
    17 3 * * * cd /path/to/Bottleneck-Hunter && python scripts/backup.py --out /mnt/off/bh --keys-dir /mnt/vault --retain 14
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
KEY_FILES = [".encryption_key", ".jwt_secret"]


def backup_db(src: Path, dst: Path) -> None:
    """用 sqlite .backup 在线 API 做一致快照（WAL 安全），并 checkpoint 落盘。"""
    src_conn = sqlite3.connect(str(src))
    try:
        src_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")  # 把 -wal 落进主库
        dst_conn = sqlite3.connect(str(dst))
        try:
            src_conn.backup(dst_conn)  # 原子一致快照
        finally:
            dst_conn.close()
    finally:
        src_conn.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "backups"), help="库备份根目录（建议异卷）")
    ap.add_argument("--keys-dir", default="", help="密钥单独备份目录（不填则与库同目录，强烈建议指定异处）")
    ap.add_argument("--retain", type=int, default=0, help="仅保留最近 N 份库备份（0=不清理）")
    args = ap.parse_args()

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.out) / ts
    out_dir.mkdir(parents=True, exist_ok=True)

    dbs = sorted(DATA.glob("*.db"))
    if not dbs:
        print(f"[backup] 未找到任何 .db 于 {DATA}", file=sys.stderr)
        return 1
    for db in dbs:
        dst = out_dir / db.name
        backup_db(db, dst)
        print(f"[backup] {db.name} -> {dst}  ({dst.stat().st_size} bytes)")

    # 密钥：默认与库同处；指定 --keys-dir 则分离（推荐）
    keys_target = Path(args.keys_dir) if args.keys_dir else out_dir
    keys_target.mkdir(parents=True, exist_ok=True)
    for name in KEY_FILES:
        src = DATA / name
        if src.exists():
            dst = keys_target / f"{name}.{ts}"
            shutil.copy2(src, dst)
            print(f"[backup] KEY {name} -> {dst}")
    if not args.keys_dir:
        print("[backup] 警告：密钥与库备份同处；生产请用 --keys-dir 指定异卷/密管，避免一损俱损。",
              file=sys.stderr)

    if args.retain > 0:
        snaps = sorted([p for p in Path(args.out).iterdir() if p.is_dir()], reverse=True)
        for old in snaps[args.retain:]:
            shutil.rmtree(old, ignore_errors=True)
            print(f"[backup] 清理旧备份 {old}")

    print(f"[backup] 完成 -> {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
