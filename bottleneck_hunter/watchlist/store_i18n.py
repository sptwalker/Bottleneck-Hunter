"""翻译缓存 store mixin —— 全局复用的按需翻译缓存（新闻中英对照等）。

key = md5(源文 + '\\x1f' + 目标语言)。翻译是纯文本→文本，跨用户可共享，故不按 user 隔离。
"""

from __future__ import annotations

import hashlib

from bottleneck_hunter.watchlist.store_base import _now_iso


def translation_key(text: str, target: str) -> str:
    return hashlib.md5(f"{text}\x1f{target}".encode()).hexdigest()


class _I18nMixin:
    def get_cached_translations(self, texts: list[str], target: str) -> dict[str, str]:
        """返回 {源文: 译文}，仅含命中缓存的。"""
        if not texts:
            return {}
        keys = {translation_key(t, target): t for t in texts}
        out: dict[str, str] = {}
        conn = self._connect()
        try:
            qmarks = ",".join("?" * len(keys))
            rows = conn.execute(
                f"SELECT cache_key, translated FROM translation_cache WHERE cache_key IN ({qmarks})",
                tuple(keys.keys()),
            ).fetchall()
            for r in rows:
                src = keys.get(r["cache_key"])
                if src is not None:
                    out[src] = r["translated"]
            return out
        finally:
            conn.close()

    def save_translations(self, pairs: dict[str, str], target: str) -> int:
        """批量写入 {源文: 译文}。返回写入条数。"""
        if not pairs:
            return 0
        now = _now_iso()
        rows = [(translation_key(src, target), target, src, tr, now)
                for src, tr in pairs.items() if src and tr]
        with self._write_conn() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO translation_cache "
                "(cache_key, target, source_text, translated, created_at) VALUES (?,?,?,?,?)",
                rows,
            )
        return len(rows)
