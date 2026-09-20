"""复现 2026-09 那次「入围筛选 168.5 分钟」的取数场景，验证修复后秒级收敛。

场景：253 家美股候选，Yahoo 对每一次调用都返回 429（事故里就是这个形状——闸门间隔被顶到
30s 上限并严格串行，338 次 429 × 30s ≈ 169 分钟）。这里在 yfinance 边界注入 429，
两条取数链（财务 / 聪明钱）并行跑，看四件事：
  1. 是否秒级结束（不再睡满 168 分钟）；
  2. 闸门多久熔断；
  3. 失败是否「可见」（成功 0 / 失败 253，而不是事故里的「0 失败」）；
  4. 熔断后是否快速失败（不再等待）。

运行：python scripts/repro_phase2_429.py           # 默认把闸门间隔压到 0.05s，用于 CI/快速回归
      python scripts/repro_phase2_429.py --prod    # 用生产默认间隔（3→30s 退避），耗时长得多

**--prod 才是生产真实曲线**：默认档把 `_MIN` 压到 0.05s 只是为了让「熔断确实发生」这件事在
CI 里秒级可验；生产默认下冷启动退避到 30s 上限需要真实睡掉一段时间（2026-09 实测 253 家
全 429、熔断前 327s）。两种档都要看：快档测逻辑，慢档测真实最坏等待。
"""
from __future__ import annotations

import asyncio
import sys
import time

sys.path.insert(0, ".")

# Windows 控制台默认 GBK，✅/❌ 直接抛 UnicodeEncodeError 把脚本打死（断言全过了却退出 1）。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from bottleneck_hunter.chain import fetch_budget, quotes_cache  # noqa: E402
from bottleneck_hunter.chain import financial_data as fd  # noqa: E402
from bottleneck_hunter.chain import smart_money as sm  # noqa: E402
from bottleneck_hunter.chain.models import MarketRegion, SupplierInfo  # noqa: E402
from bottleneck_hunter.data_provider import yf_gate  # noqa: E402

N = 253


class _Fake429(Exception):
    """形如 yfinance 撞限流时抛出的 HTTP 429。"""


def _ticker_429(ticker):
    # 每次构造 Ticker（=每次真正发起取数）都撞 429
    raise _Fake429("HTTP Error 429: Too Many Requests")


def _install_fake_yf():
    fake = type("Y", (), {"Ticker": staticmethod(_ticker_429)})()
    fd.yf = fake
    sm.yf = fake


def _suppliers(n: int) -> list[SupplierInfo]:
    return [
        SupplierInfo(
            ticker=f"T{i:04d}",
            name=f"Fake {i}",
            market=MarketRegion.US_STOCK,
            sector="半导体",
            description="压测用假供应商",
        )
        for i in range(n)
    ]


async def main() -> int:
    _install_fake_yf()
    # 与生产同一套起跑动作：清缓存、重置预算、重置闸门
    quotes_cache.reset()
    fetch_budget.reset()
    yf_gate.reset_for_new_run()
    fd.reset_yf_degraded_stats()

    # 默认档：把闸门间隔压小并**立刻生效**，让「熔断触发」在秒级到达。三处都要改：
    #  · `_interval` —— `reset_for_new_run()` 有意保留上一轮退避出来的间隔，若它已是 30s，
    #    整轮仍按 30s 走（只改 _MIN 无效）；
    #  · `_MAX` —— 退避是**翻倍**的，只压低起点不改上限，第 7 次 429 就摸到 30s，累计仍要睡
    #    分钟级（实测只改前两处时 N=40 也跑不完）。上限跟着压到 1s，快档才真的是「秒级验证」；
    #  · 生产曲线不在这条路径上——`--prod` 保持 3→30s 原样。
    if "--prod" not in sys.argv:
        yf_gate._MIN = 0.05  # noqa: SLF001
        yf_gate._interval = 0.05  # noqa: SLF001
        yf_gate._MAX = 1.0  # noqa: SLF001

    suppliers = _suppliers(N)
    fetch_budget.start()

    t0 = time.time()
    trip_at: list[float] = []

    async def _watch():
        while True:
            await asyncio.sleep(0.1)
            if not trip_at and yf_gate.is_tripped():
                trip_at.append(time.time() - t0)

    fin = asyncio.create_task(fd.fetch_batch(suppliers))
    smt = asyncio.create_task(sm.track_batch(suppliers))
    watcher = asyncio.create_task(_watch())
    (fin_res, sm_res) = await asyncio.gather(fin, smt)
    watcher.cancel()
    elapsed = time.time() - t0

    deg = fd.yf_degraded_stats()
    gate = yf_gate.snapshot()
    print(f"候选数          : {N}")
    print(f"耗时            : {elapsed:.2f}s   （事故同一场景实测 168.5 分钟 = 10110s）")
    print(f"熔断于          : {trip_at[0]:.2f}s" if trip_at else "熔断于          : 未触发 ❌")
    print(f"财务 成功/失败  : {len(fin_res[0])} / {len(fin_res[1])}")
    print(f"聪明钱 成功/失败: {len(sm_res[0])} / {len(sm_res[1])}")
    print(f"降级统计        : {deg}   闸门间隔 {yf_gate.current_interval():.2f}s")
    print(f"闸门快照        : {gate}")

    ok = True
    # 快档要求秒级；--prod 走真实退避曲线，上限放宽到取数预算量级，但仍须与事故的 10110s 差一个数量级
    limit = 300 if "--prod" in sys.argv else 60
    if elapsed > limit:
        print(f"❌ 等待过长：{elapsed:.1f}s > {limit}s")
        ok = False
    if not gate.get("tripped"):
        print("❌ 闸门未熔断：253 次全 429 都没触发熔断")
        ok = False
    if not trip_at:
        print("❌ 观察线程没抓到熔断时刻")
        ok = False
    if deg["degraded"] < N:
        print(f"❌ 失败不可见：degraded={deg['degraded']} < {N}")
        ok = False
    if len(fin_res[1]) != N or len(sm_res[1]) != N:
        print(f"❌ 失败未全部可见：财务失败 {len(fin_res[1])}、聪明钱失败 {len(sm_res[1])}，应各为 {N}")
        ok = False

    print()
    print("✅ 事故复现通过：253 家全 429 → 秒级收敛、失败全部可见、不再睡满 168 分钟" if ok
          else "❌ 复现未通过")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
