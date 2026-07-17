#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""诊断/清理 ai_role_config 脏配置。

背景：get_models_for_role 优先级1(DB角色矩阵)历史上排在锁定主模型之前，导致
某些角色(如 pipeline_decompose)残留的手配/自动改写记录压过顶栏主模型。代码侧已
治本(prefer_primary 时主模型先于DB矩阵)，本脚本用于清掉这些遗留脏配、恢复干净状态。

用法（在容器内 / 项目根目录）：
    python scripts/clean_role_config.py                 # 只列出所有 ai_role_config（只读）
    python scripts/clean_role_config.py --role pipeline_decompose        # 删该角色全部槽位（所有用户）
    python scripts/clean_role_config.py --role pipeline_decompose --user <uid>   # 仅删某用户该角色
    python scripts/clean_role_config.py --provider siliconflow_nex_n2_pro        # 删所有引用该provider的角色配置

不加删除参数时纯只读，先看清再删。删除按 role_key×slot_index×user_id 精准执行。
"""
import argparse
import sqlite3

from bottleneck_hunter.watchlist.store_base import _DEFAULT_DB


def _rows(where="", params=()):
    conn = sqlite3.connect(str(_DEFAULT_DB))
    conn.row_factory = sqlite3.Row
    try:
        q = ("SELECT role_key, role_label, slot_index, provider, model, user_id, is_active, updated_at "
             "FROM ai_role_config")
        if where:
            q += " WHERE " + where
        q += " ORDER BY user_id, role_key, slot_index"
        return [dict(r) for r in conn.execute(q, params).fetchall()]
    finally:
        conn.close()


def _print(rows):
    if not rows:
        print("  (无匹配记录)")
        return
    for r in rows:
        print(f"  role={r['role_key']:<24} slot={r['slot_index']} "
              f"provider={r['provider']:<26} model={r['model']:<28} "
              f"user={r['user_id'] or '(全局)'} active={r['is_active']} updated={r['updated_at']}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--role", help="按 role_key 删除")
    ap.add_argument("--user", default=None, help="限定 user_id（不填=该角色所有用户）")
    ap.add_argument("--provider", help="按 provider 删除（引用该provider的所有角色配置）")
    args = ap.parse_args()

    print(f"DB: {_DEFAULT_DB}\n")
    print("=== 当前全部 ai_role_config ===")
    _print(_rows())

    if not args.role and not args.provider:
        print("\n（只读模式：未指定 --role / --provider，不做删除。）")
        return

    if args.provider:
        target = _rows("provider = ?", (args.provider,))
        print(f"\n=== 将删除 provider={args.provider} 的 {len(target)} 条 ===")
    else:
        if args.user is not None:
            target = _rows("role_key = ? AND user_id = ?", (args.role, args.user))
        else:
            target = _rows("role_key = ?", (args.role,))
        print(f"\n=== 将删除 role={args.role}"
              f"{f' user={args.user}' if args.user is not None else ' (所有用户)'} 的 {len(target)} 条 ===")
    _print(target)
    if not target:
        return

    conn = sqlite3.connect(str(_DEFAULT_DB))
    try:
        if args.provider:
            cur = conn.execute("DELETE FROM ai_role_config WHERE provider = ?", (args.provider,))
        elif args.user is not None:
            cur = conn.execute("DELETE FROM ai_role_config WHERE role_key = ? AND user_id = ?",
                               (args.role, args.user))
        else:
            cur = conn.execute("DELETE FROM ai_role_config WHERE role_key = ?", (args.role,))
        conn.commit()
        print(f"\n✅ 已删除 {cur.rowcount} 条。请重启容器或调 set_provider_status 让运行时生效"
              f"（角色矩阵每次解析实时读DB，通常无需重启）。")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
