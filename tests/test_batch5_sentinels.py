"""Batch 5（P2-6 催化剂日期结构化）回归哨兵。

生产库实证：`catalyst_tracking` 之外，chain 侧 `expected_date` 的取值里
`2025Q3` x338、`2025Q4` x272、`2025H2` x76、`2025年下半年` x6 —— 这些写法
在下游（`_days_until_date` / `_date_diff` / 各种 `[:10]` 切片）**一律解析失败**，
等同于「这家没有催化剂」，且失败是静默的。

两处收口：
- 写入侧 `CatalystEvent._normalize_date`（chain 侧）
- 写入侧 `WatchlistStore.create_catalyst`（观察池侧，下游是字符串比较）
"""

from __future__ import annotations

import pytest

from bottleneck_hunter.chain.models import CatalystEvent, _normalize_expected_date


class TestNormalizeExpectedDate:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            # 季度 → 期末（用期末更保守：不会把未到期的误判成已过期）
            ("2025Q3", "2025-09-30"),
            ("2026Q1", "2026-03-31"),
            ("2025q4", "2025-12-31"),
            # 半年度
            ("2025H2", "2025-12-31"),
            ("2026H1", "2026-06-30"),
            ("2025年下半年", "2025-12-31"),
            ("2025年上半年", "2025-06-30"),
            # 中文季度写法
            ("2025年第3季度", "2025-09-30"),
            ("2025年四季度", "2025-12-31"),
            # 年月 → 月末
            ("2025-08", "2025-08-31"),
            ("2025年5月", "2025-05-31"),
            # 已是 ISO → 原样（不得被改写，否则会造出「日期悄悄漂移」）
            ("2026-08-15", "2026-08-15"),
            ("2025-12-31", "2025-12-31"),
            # 区间 → 取最晚（「未来 6-18 个月」的语义）
            ("2025Q4-2026Q1", "2026-03-31"),
            ("2025Q3-Q4", "2025-12-31"),
            ("2025H2-2026H1", "2026-06-30"),
            ("2024Q4-2025Q2", "2025-06-30"),
            # 2 月月末按闰年算
            ("2024年2月", "2024-02-29"),
            ("2025年2月", "2025-02-28"),
            # ── 以下取自生产库 148 个真实取值里的写法（2026-09 实测） ──
            ("2025年Q3", "2025-09-30"),      # 338 条的 2025Q3 加个「年」字
            ("2026年Q1", "2026-03-31"),
            ("2025年H2", "2025-12-31"),
            ("2025年H1", "2025-06-30"),
            ("2025年Q3-Q4", "2025-12-31"),   # 中英混写的区间，同样要吃住
            ("2025年Q4至2026年Q1", "2026-03-31"),
            ("2025年7-8月", "2025-08-31"),   # 月区间取后一月
            ("2025年7月至8月", "2025-08-31"),
            ("2025年7~8月", "2025-08-31"),
            # 整年写法（末位兜底）
            ("2025全年", "2025-12-31"),
            ("2025年内", "2025-12-31"),
            ("2026年", "2026-12-31"),
            ("2025-2026", "2026-12-31"),
        ],
    )
    def test_parses_and_normalizes(self, raw, expected):
        assert _normalize_expected_date(raw) == expected

    def test_year_fallback_does_not_override_precise_periods(self):
        """兜底的「整年 → 12-31」**只在更精确的写法一个都没匹配上时**才生效。

        否则 `2025Q3` 会多出一个 2025-12-31 的候选并被 max 选中，
        季度末被整年末盖掉 —— 到期日凭空推迟一个季度。
        """
        assert _normalize_expected_date("2025Q3") == "2025-09-30"
        assert _normalize_expected_date("2025-08") == "2025-08-31"
        assert _normalize_expected_date("2025年5月") == "2025-05-31"

    @pytest.mark.parametrize("raw", ["未来6-12个月", "未来1-3个月", "未来3-6个月"])
    def test_relative_window_is_not_resolved(self, raw):
        """「未来 N 个月」是相对表述，**解析需要写入时刻**，此处拿不到 →
        必须返回 ""，不能拿今天去代入（那会把「6 个月后」写成今天附近的某天）。
        """
        assert _normalize_expected_date(raw) == ""

    @pytest.mark.parametrize(
        "raw", ["订单金额2026万元", "新增产能5000万台", "营收增长2025%", "持股2026万股"]
    )
    def test_quantity_is_not_parsed_as_year(self, raw):
        """整年兜底必须排除「4 位数 + 单位」的数量写法。

        否则「订单金额 2026 万元」会被读成「2026 年兑现」—— 凭空造出一个
        从未存在过的到期日，比返回「无日期」坏得多。
        """
        assert _normalize_expected_date(raw) == ""

    @pytest.mark.parametrize("raw", [None, "", "   ", "未定", "待确认", "N/A", "TBD", "即将"])
    def test_unparseable_becomes_empty_not_guess(self, raw):
        """解析不了 → ""（= 无日期），**绝不臆造一个日期**。

        宁可下游看到「无日期」，也不能凭空造一个 —— 那会让「不知道什么时候」
        伪装成「已知在某天」。
        """
        assert _normalize_expected_date(raw) == ""

    def test_catalyst_event_validator_applies(self):
        """validator 挂在模型上，任何构造路径都会归一，不依赖调用方自觉。"""
        ev = CatalystEvent(event_type="capacity", description="新产线投产", expected_date="2025Q3")
        assert ev.expected_date == "2025-09-30"

    def test_catalyst_event_without_date_stays_empty(self):
        ev = CatalystEvent(event_type="order", description="大客户订单")
        assert ev.expected_date == ""

    def test_normalized_date_is_parseable_by_downstream(self):
        """这是本条修复的**判定核心**：归一后的值必须能被下游真正解析。

        旧值 "2025Q3" 在 `_days_until_date` 里 fromisoformat 抛 ValueError →
        返回 None → 等同于「无催化剂」。
        """
        from datetime import datetime

        from bottleneck_hunter.watchlist.decision_engine import _days_until_date

        old = CatalystEvent(event_type="capacity", description="d", expected_date="2025Q3")
        new = CatalystEvent(event_type="capacity", description="d", expected_date="2026-09-30")
        # 旧写法会归一成 2025-09-30（已过期）；新写法是未来日期
        assert _days_until_date(new.expected_date) is not None
        assert _days_until_date(new.expected_date) > 0
        # 证明「不归一」确实会解析失败：直接把原始串喂进去
        assert _days_until_date("2025Q3") is None
        assert datetime.fromisoformat(old.expected_date)  # 归一后至少是可解析的 ISO


class TestWatchlistCreateCatalystNormalizes:
    """观察池侧的收口：下游是**字符串比较**，非 ISO 会让那些判断静默不成立。"""

    def _store(self, tmp_path):
        from bottleneck_hunter.watchlist.store import WatchlistStore
        return WatchlistStore(db_path=str(tmp_path / "t.db"))

    @staticmethod
    def _entry(store, ticker: str) -> str:
        return store.add({"ticker": ticker, "market": "a_stock", "name": ticker})

    @pytest.mark.parametrize(
        "raw,expected",
        [("2026Q1", "2026-03-31"), ("2026H2", "2026-12-31"), ("2026-07", "2026-07-31"),
         ("2026-07-15", "2026-07-15")],
    )
    def test_created_catalyst_stores_iso_date(self, tmp_path, raw, expected):
        store = self._store(tmp_path)
        eid = self._entry(store, "600001.SH")
        store.create_catalyst(entry_id=eid, ticker="600001.SH", title="产能投产", expected_date=raw)
        cats = store.get_catalysts_for_entry(eid)
        assert [c["expected_date"] for c in cats] == [expected]

    def test_unparseable_stored_as_null(self, tmp_path):
        """存 NULL 而非 "待定" —— 上层用 `expected_date IS NOT NULL` 筛有效日期，
        存一个不可解析的串会被当成「有效日期」捞出来，是更坏的失败方式。"""
        store = self._store(tmp_path)
        eid = self._entry(store, "600001.SH")
        store.create_catalyst(entry_id=eid, ticker="600001.SH", title="待定事件", expected_date="待定")
        cats = store.get_catalysts_for_entry(eid)
        assert cats[0]["expected_date"] is None

    def test_none_stays_none(self, tmp_path):
        store = self._store(tmp_path)
        eid = self._entry(store, "600002.SH")
        store.create_catalyst(entry_id=eid, ticker="600002.SH", title="无日期", expected_date=None)
        assert store.get_catalysts_for_entry(eid)[0]["expected_date"] is None
