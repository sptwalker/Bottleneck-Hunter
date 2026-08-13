"""行情 DataFrame 落地前的统一清洗：OHLC 合法性 + A股量能单位归一。

单点插在 `FetcherManager.fetch_daily` 的 return 前，覆盖**所有**行情源
（efinance/akshare/pytdx/baostock/yfinance/akshare_us/finnhub），避免每源各写一遍。

背景（真实 bug）：降级链 efinance→akshare→pytdx→baostock 切到 baostock 时，
A股量能单位从「手」(100股) 突变为「股」，信号被静默放大 100×（毁 RSI/量比/打分）。
"""
from __future__ import annotations

import logging

import pandas as pd

logger = logging.getLogger(__name__)

# A股量能 canonical = 「手」(100股)——贴合多数源 + 既有落库 + 境内惯例。
# 下集合是「amount 缺失时」的兜底假设（哪些源以「股」计）；有 amount 时改用数据反推（见 _ashare_volume_in_shares），
# 不再依赖此静态表。ponytail: 正常路径全源都带 amount → 走数据反推；本表仅旧数据/构造样本兜底。
_ASHARE_SHARES_UNIT_SOURCES = {"baostock"}

_OHLC_COLS = ("open", "high", "low", "close")


def _ashare_volume_in_shares(df: pd.DataFrame, source: str) -> bool:
    """判断该 A股 df 的量能是否以「股」计（True 则需 ÷100 归一到「手」）。

    优先用近端 bar 的 r = amount/(close×vol) 反推单位——源无关、每次用真实数据自校准，
    规避「源名→单位」的静态先验（尤其 pytdx 的 vol 单位协议层有歧义、难先验确定）：
      r≈1 → 「股」；r≈100 → 「手」。判据 `r < 10` 取「股」，量级差 100× 远超 qfq 扰动。
    取「近端」5 根：qfq 把 close 锚到最新价，历史 bar 的 close 被拆股/分红压缩会失真，
    近端 close≈实际成交价、比值可靠。amount 缺失/全零（旧数据/构造样本）→ 退回源名假设。
    """
    if {"amount", "volume", "close"} <= set(df.columns):
        recent = df.tail(5)
        close = pd.to_numeric(recent["close"], errors="coerce")
        vol = pd.to_numeric(recent["volume"], errors="coerce")
        amt = pd.to_numeric(recent["amount"], errors="coerce")
        mask = (close > 0) & (vol > 0) & (amt > 0)
        if mask.any():
            return float((amt[mask] / (close[mask] * vol[mask])).median()) < 10.0
    return source in _ASHARE_SHARES_UNIT_SOURCES


def clean_ohlc(df: pd.DataFrame | None, source: str, market: str) -> pd.DataFrame | None:
    """丢弃非法 bar + A股量能单位归一（→手）。

    - 丢弃：`high < low`、任一 OHLC ≤ 0、任一 OHLC 为 NaN 的行。
    - 归一：A股市场下，若数据反推（或源名兜底）判定量能以「股」计，则 `volume //= 100`。
    返回清洗后 df（可能行数变少）；全部非法 → None（让 manager 自动切下一个源）。
    """
    if df is None or df.empty:
        return df
    out = df
    cols = set(out.columns)
    filtered = False

    price_cols = [c for c in _OHLC_COLS if c in cols]
    if price_cols:
        bad = pd.Series(False, index=out.index)
        for c in price_cols:
            v = pd.to_numeric(out[c], errors="coerce")
            bad |= v.isna() | (v <= 0)
        if "high" in cols and "low" in cols:
            bad |= pd.to_numeric(out["high"], errors="coerce") < pd.to_numeric(out["low"], errors="coerce")
        if bad.any():
            logger.debug("clean_ohlc[%s/%s] 丢弃 %d 条非法bar", source, market, int(bad.sum()))
            out = out[~bad]
            filtered = True
            if out.empty:
                return None

    if market == "a_stock" and "volume" in cols and _ashare_volume_in_shares(out, source):
        out = out.copy()
        out["volume"] = (pd.to_numeric(out["volume"], errors="coerce").fillna(0) // 100).astype(int)

    return out.reset_index(drop=True) if filtered else out


def close_divergence(df_a: pd.DataFrame | None, df_b: pd.DataFrame | None) -> float:
    """两源同 ticker 日K 对齐日期后的最大收盘价相对偏差（跨源一致性回归用，>1% 应报警）。

    对齐 `date` 交集后算 `max(|a-b| / |b|)`；无交集/空 → 0.0。
    """
    if df_a is None or df_b is None or df_a.empty or df_b.empty:
        return 0.0
    if "date" not in df_a.columns or "date" not in df_b.columns:
        return 0.0
    a = df_a[["date", "close"]].dropna().set_index("date")["close"].astype(float)
    b = df_b[["date", "close"]].dropna().set_index("date")["close"].astype(float)
    common = a.index.intersection(b.index)
    if len(common) == 0:
        return 0.0
    a, b = a.loc[common], b.loc[common]
    denom = b.abs().where(b.abs() > 0, 1.0)
    return float(((a - b).abs() / denom).max())
