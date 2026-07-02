"""观察池分档容量推导 —— 单一真源，杜绝硬编码。

三档容量由「总上限 × 可配置比例」推导：
- focus（重点关注）= round(total × focus_pct)
- normal（一般关注）= round(total × normal_pct)
- track（跟踪）= 剩余（total - focus - normal），保证三档之和恒等于 total，不因舍入丢股。
"""

from __future__ import annotations

DEFAULT_TOTAL = 24
DEFAULT_FOCUS_PCT = 0.25
DEFAULT_NORMAL_PCT = 0.25

TIERS = ("focus", "normal", "track")


def derive_tier_caps(
    total: int = DEFAULT_TOTAL,
    focus_pct: float = DEFAULT_FOCUS_PCT,
    normal_pct: float = DEFAULT_NORMAL_PCT,
) -> dict[str, int]:
    """由总上限 + 比例推导三档容量。

    护栏：
    - total 下限 0；比例裁剪到 [0, 1]，且 focus+normal 不超过 1（超出按比例缩放）。
    - focus/normal 各自不超过 total；track 取剩余且不为负。
    - 三档之和恒等于 total。
    """
    total = max(0, int(total))
    focus_pct = max(0.0, float(focus_pct))
    normal_pct = max(0.0, float(normal_pct))
    # focus+normal 超 100% 时等比缩放，给 track 留 0
    if focus_pct + normal_pct > 1.0:
        scale = 1.0 / (focus_pct + normal_pct)
        focus_pct *= scale
        normal_pct *= scale

    focus = min(total, round(total * focus_pct))
    normal = min(total - focus, round(total * normal_pct))
    track = total - focus - normal  # 剩余，守恒
    return {"focus": focus, "normal": normal, "track": track}


def demo() -> None:
    """assert 自检：默认值、缩放、守恒、无负。"""
    # 默认 24 → 6/6/12（与历史硬编码一致）
    assert derive_tier_caps(24, 0.25, 0.25) == {"focus": 6, "normal": 6, "track": 12}
    # 20 → 5/5/10
    assert derive_tier_caps(20, 0.25, 0.25) == {"focus": 5, "normal": 5, "track": 10}
    # 30 / 30% / 20% → 9 / 6 / 15
    assert derive_tier_caps(30, 0.30, 0.20) == {"focus": 9, "normal": 6, "track": 15}
    # 单用户小上限 8 → 2/2/4
    assert derive_tier_caps(8, 0.25, 0.25) == {"focus": 2, "normal": 2, "track": 4}
    # 守恒 + 无负：随机比例总和恒等于 total
    for total in (0, 1, 3, 7, 25, 100):
        for fp, npct in ((0.0, 0.0), (0.5, 0.5), (0.9, 0.9), (1.0, 0.0), (0.33, 0.33)):
            caps = derive_tier_caps(total, fp, npct)
            assert sum(caps.values()) == total, (total, fp, npct, caps)
            assert all(v >= 0 for v in caps.values()), (total, fp, npct, caps)
    print("tier_limits.demo OK")


if __name__ == "__main__":
    demo()
