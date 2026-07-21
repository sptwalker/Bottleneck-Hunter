#!/usr/bin/env python3
"""一次性数据清洗：把反向分析历史遗留的非 canonical ticker 归一化对齐。

背景：反向分析此前只 strip、不 normalize，落库存了原文——`aapl`（非 AAPL）、
`600519`（缺 .SS 后缀）。观察池/决策/快照全用 normalize_ticker 后的 canonical 形，
导致 company_archive 建的档按原文 key、下游按归一化 key 去查，对不上、档案失联。
代码侧已在 reverse.py 入口补了归一化；此脚本清存量。

处理两张表（复用 app 的 normalize_ticker，A股后缀等逻辑与线上完全一致）：
  · reverse_analyses (watchlist.db)  —— ticker 非唯一，直接 UPDATE 改名
  · company_archive  (analyses.db)   —— (user_id,ticker) 唯一，改名若撞已有行则去重：
        保留 updated_at 较新的一条，删另一条（不静默丢，打印每一步）

安全：默认 dry-run 只打印将改什么；加 --apply 才落库。幂等——已 canonical 的跳过，
重复运行无副作用。运行前建议先 scripts/backup.py 备份。

用法（服务器容器内）：
    python scripts/normalize_tickers.py               # 预演（不写）
    python scripts/normalize_tickers.py --apply       # 执行
    python scripts/normalize_tickers.py --self-check   # 逻辑自检（临时库）
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

# 表 → (归一化所需的 market 列是否存在始终为真；此处仅登记要扫的表)
_TARGET_TABLES = ("reverse_analyses", "company_archive")


def _norm(ticker: str, market: str) -> str:
    from bottleneck_hunter.watchlist.store_base import normalize_ticker
    return normalize_ticker(ticker, market or "")


def _has_table(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def _clean_reverse_analyses(conn: sqlite3.Connection, apply: bool) -> int:
    """ticker 非唯一：逐行改名即可。"""
    changed = 0
    rows = conn.execute(
        "SELECT id, ticker, COALESCE(market,'') AS market FROM reverse_analyses"
    ).fetchall()
    for r in rows:
        norm = _norm(r["ticker"], r["market"])
        if norm == r["ticker"]:
            continue
        changed += 1
        print(f"  [reverse_analyses] {r['ticker']!r} -> {norm!r}  (id={r['id']})")
        if apply:
            conn.execute("UPDATE reverse_analyses SET ticker=? WHERE id=?", (norm, r["id"]))
    return changed


def _clean_company_archive(conn: sqlite3.Connection, apply: bool) -> int:
    """(user_id,ticker) 唯一：改名撞已有行则保留 updated_at 较新者。"""
    changed = 0
    rows = conn.execute(
        "SELECT rowid, user_id, ticker, COALESCE(market,'') AS market, "
        "COALESCE(updated_at,'') AS updated_at FROM company_archive"
    ).fetchall()
    for r in rows:
        norm = _norm(r["ticker"], r["market"])
        if norm == r["ticker"]:
            continue
        uid = r["user_id"]
        dup = conn.execute(
            "SELECT rowid, COALESCE(updated_at,'') AS updated_at "
            "FROM company_archive WHERE user_id=? AND ticker=?",
            (uid, norm),
        ).fetchone()
        changed += 1
        if dup is None:
            print(f"  [company_archive] {r['ticker']!r} -> {norm!r}  (user={uid[:8] if uid else ''})")
            if apply:
                conn.execute("UPDATE company_archive SET ticker=? WHERE rowid=?", (norm, r["rowid"]))
        else:
            # 冲突：canonical 行已存在，保留较新，删较旧
            if r["updated_at"] > dup["updated_at"]:
                print(f"  [company_archive] 冲突 {r['ticker']!r}->{norm!r}: 本行较新，覆盖旧 canonical 行")
                if apply:
                    conn.execute("DELETE FROM company_archive WHERE rowid=?", (dup["rowid"],))
                    conn.execute("UPDATE company_archive SET ticker=? WHERE rowid=?", (norm, r["rowid"]))
            else:
                print(f"  [company_archive] 冲突 {r['ticker']!r}->{norm!r}: 已有 canonical 更新，删本原文行")
                if apply:
                    conn.execute("DELETE FROM company_archive WHERE rowid=?", (r["rowid"],))
    return changed


def run(apply: bool) -> int:
    if not DATA.exists():
        print(f"[normalize] 未找到 data 目录: {DATA}", file=sys.stderr)
        return 1
    total = 0
    dbs = sorted(DATA.glob("*.db"))
    for db in dbs:
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        try:
            for table in _TARGET_TABLES:
                if not _has_table(conn, table):
                    continue
                print(f"[{db.name}] 扫描 {table} ...")
                if table == "reverse_analyses":
                    total += _clean_reverse_analyses(conn, apply)
                else:
                    total += _clean_company_archive(conn, apply)
            if apply:
                conn.commit()
        finally:
            conn.close()
    verb = "已归一化" if apply else "将归一化（dry-run，未写入）"
    print(f"[normalize] {verb} {total} 行" + ("" if apply else "；加 --apply 执行"))
    return 0


def _self_check() -> int:
    """临时库验证：改名、A股补后缀、唯一键冲突去重（保留较新）。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE company_archive (user_id TEXT, ticker TEXT, market TEXT, "
                 "updated_at TEXT, UNIQUE(user_id, ticker))")
    # u1: 'aapl'(旧) 与已有 'AAPL'(新) 冲突 → 应保留 AAPL 那条、删 aapl
    conn.execute("INSERT INTO company_archive VALUES ('u1','aapl','us_stock','2026-01-01')")
    conn.execute("INSERT INTO company_archive VALUES ('u1','AAPL','us_stock','2026-06-01')")
    # u2: '600519' 无冲突 → 改名为 600519.SS
    conn.execute("INSERT INTO company_archive VALUES ('u2','600519','a_stock','2026-01-01')")
    _clean_company_archive(conn, apply=True)
    left = {(r["user_id"], r["ticker"]) for r in conn.execute("SELECT user_id,ticker FROM company_archive")}
    assert left == {("u1", "AAPL"), ("u2", "600519.SS")}, f"清洗结果不符: {left}"
    # 幂等：再跑一次应 0 改动
    again = _clean_company_archive(conn, apply=True)
    assert again == 0, f"非幂等，第二次仍改 {again} 行"
    print("normalize_tickers 自检通过：改名 + A股补后缀 + 冲突去重（留新）+ 幂等")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正写库（默认 dry-run 只打印）")
    ap.add_argument("--self-check", action="store_true", help="临时库跑逻辑自检")
    args = ap.parse_args()
    if args.self_check:
        return _self_check()
    return run(args.apply)


if __name__ == "__main__":
    raise SystemExit(main())
