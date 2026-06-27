"""News data pipeline — fetch headlines + LLM summarization & sentiment.

Sources: yfinance Ticker.news + Google Finance RSS (US), akshare (A-stock).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import uuid
from datetime import datetime, timezone

import httpx
import yfinance as yf

try:
    import akshare as ak
except ImportError:
    ak = None  # type: ignore[assignment]

from bottleneck_hunter.watchlist.budget import BudgetTracker
from bottleneck_hunter.watchlist.models import DegradationMode
from bottleneck_hunter.watchlist.retry import with_retry, get_http_client
from bottleneck_hunter.watchlist.store import WatchlistStore

logger = logging.getLogger(__name__)

_SEM: asyncio.Semaphore | None = None


def _get_sem() -> asyncio.Semaphore:
    global _SEM
    if _SEM is None:
        _SEM = asyncio.Semaphore(4)
    return _SEM


# ---------------------------------------------------------------------------
# Raw news fetching
# ---------------------------------------------------------------------------

@with_retry(max_retries=3, base_delay=1.0)
def _fetch_yfinance_news(ticker: str, limit: int = 10) -> list[dict]:
    """Fetch news from yfinance Ticker.news (sync)."""
    t = yf.Ticker(ticker)
    raw = t.news or []
    results = []
    for item in raw[:limit]:
        title = item.get("title", "")
        if not title:
            continue
        pub_ts = item.get("providerPublishTime", 0)
        date_str = datetime.fromtimestamp(pub_ts, tz=timezone.utc).strftime("%Y-%m-%d") if pub_ts else ""
        news_id = hashlib.md5(f"{ticker}:{title}".encode()).hexdigest()[:12]
        results.append({
            "id": news_id,
            "ticker": ticker,
            "date": date_str,
            "title": title,
            "source_url": item.get("link", ""),
            "source_name": item.get("publisher", ""),
        })
    return results


@with_retry(max_retries=3, base_delay=1.0)
async def _fetch_rss_news(ticker: str, limit: int = 5) -> list[dict]:
    """Fetch from Google Finance RSS (async)."""
    url = f"https://news.google.com/rss/search?q={ticker}+stock&hl=en-US&gl=US&ceid=US:en"
    client = get_http_client()
    resp = await client.get(url)
    if resp.status_code != 200:
        return []
    try:
        import feedparser
        feed = feedparser.parse(resp.text)
    except ImportError:
        logger.debug("feedparser not installed, skipping RSS")
        return []

    results = []
    for entry in feed.entries[:limit]:
        title = entry.get("title", "")
        if not title:
            continue
        pub = entry.get("published_parsed")
        date_str = datetime(*pub[:3], tzinfo=timezone.utc).strftime("%Y-%m-%d") if pub else ""
        news_id = hashlib.md5(f"{ticker}:rss:{title}".encode()).hexdigest()[:12]
        results.append({
            "id": news_id,
            "ticker": ticker,
            "date": date_str,
            "title": title,
            "source_url": entry.get("link", ""),
            "source_name": "Google News",
        })
    return results


_ASTOCK_RE = re.compile(r"^(?:SH|SZ|sh|sz)?(\d{6})")


@with_retry(max_retries=3, base_delay=1.0)
def _fetch_astock_news(ticker: str, limit: int = 10) -> list[dict]:
    """Fetch A-stock news from akshare stock_news_em (sync)."""
    if ak is None:
        return []
    code = ticker.split(".")[0].strip()
    m = _ASTOCK_RE.match(code)
    if not m:
        return []
    code_6 = m.group(1)
    df = ak.stock_news_em(symbol=code_6)
    if df is None or df.empty:
        return []
    results = []
    for _, row in df.head(limit).iterrows():
        title = str(row.get("新闻标题", "")).strip()
        if not title:
            continue
        pub_time = str(row.get("发布时间", ""))
        date_str = pub_time[:10] if len(pub_time) >= 10 else ""
        source_name = str(row.get("文章来源", ""))
        source_url = str(row.get("新闻链接", ""))
        news_id = hashlib.md5(f"{ticker}:{title}".encode()).hexdigest()[:12]
        results.append({
            "id": news_id,
            "ticker": ticker,
            "date": date_str,
            "title": title,
            "source_url": source_url,
            "source_name": source_name,
        })
    return results


# ---------------------------------------------------------------------------
# LLM summarization
# ---------------------------------------------------------------------------

_SUMMARY_PROMPT = """你是一个金融新闻分析师。请分析以下关于 {ticker} 的新闻标题，给出：
1. 一句话中文摘要（合并所有新闻的要点）
2. 情感倾向：positive / negative / neutral
3. 情感分数：-1.0（极度利空）到 1.0（极度利好）

新闻标题：
{headlines}

请严格按以下 JSON 格式回复：
{{"summary": "...", "sentiment": "positive|negative|neutral", "sentiment_score": 0.0}}"""


async def _summarize_with_llm(ticker: str, articles: list[dict], llm, budget: BudgetTracker) -> dict:
    """LLM summarization + sentiment. Returns {summary, sentiment, sentiment_score}."""
    if not articles:
        return {"summary": "", "sentiment": "neutral", "sentiment_score": 0.0}

    mode = budget.get_degradation_mode()
    if mode == DegradationMode.MINIMAL:
        return {"summary": "", "sentiment": "neutral", "sentiment_score": 0.0}

    headlines = "\n".join(f"- {a['title']}" for a in articles[:15])
    prompt = _SUMMARY_PROMPT.format(ticker=ticker, headlines=headlines)

    try:
        from langchain_core.messages import HumanMessage
        response = await llm.ainvoke([HumanMessage(content=prompt)])
        text = response.content.strip()
        # 提取 JSON
        if "```" in text:
            text = text.split("```")[1].strip()
            if text.startswith("json"):
                text = text[4:].strip()
        result = json.loads(text)
        # 记录预算
        in_tok = len(prompt) // 4
        out_tok = len(response.content) // 4
        budget.record(
            provider=getattr(llm, "_llm_type", "unknown"),
            model=getattr(llm, "model_name", "unknown"),
            input_tokens=in_tok,
            output_tokens=out_tok,
            task_type="news_summary",
        )
        return {
            "summary": result.get("summary", ""),
            "sentiment": result.get("sentiment", "neutral"),
            "sentiment_score": float(result.get("sentiment_score", 0.0)),
        }
    except Exception as e:
        logger.warning("News LLM summary failed for %s: %s", ticker, e)
        return {"summary": "", "sentiment": "neutral", "sentiment_score": 0.0}


# ---------------------------------------------------------------------------
# Batch pipeline
# ---------------------------------------------------------------------------

async def _fetch_one(ticker: str, store: WatchlistStore, llm, budget: BudgetTracker, market: str = "us_stock") -> int:
    async with _get_sem():
        if market == "a_stock":
            articles = await asyncio.to_thread(_fetch_astock_news, ticker)
        else:
            articles = await asyncio.to_thread(_fetch_yfinance_news, ticker)
            rss = await _fetch_rss_news(ticker)
            seen = {a["id"] for a in articles}
            for r in rss:
                if r["id"] not in seen:
                    articles.append(r)
                    seen.add(r["id"])

        if not articles:
            return 0

        # LLM 分析
        analysis = await _summarize_with_llm(ticker, articles, llm, budget)

        # 把 LLM 分析结果挂到每条新闻
        for a in articles:
            a["llm_analysis"] = analysis.get("summary", "")
            a["sentiment"] = analysis.get("sentiment", "neutral")
            a["sentiment_score"] = analysis.get("sentiment_score", 0.0)
            a.setdefault("summary", "")

        return store.save_news(articles)


async def fetch_news_batch(
    tickers: list[str],
    store: WatchlistStore,
    llm=None,
    budget: BudgetTracker | None = None,
    market: str = "us_stock",
) -> dict[str, int]:
    """Batch-fetch and summarize news. Returns {ticker: article_count}."""
    if not tickers:
        return {}
    results = {}
    for ticker in tickers:
        try:
            if llm and budget:
                count = await _fetch_one(ticker, store, llm, budget, market=market)
            else:
                if market == "a_stock":
                    articles = await asyncio.to_thread(_fetch_astock_news, ticker)
                else:
                    articles = await asyncio.to_thread(_fetch_yfinance_news, ticker)
                count = store.save_news(articles) if articles else 0
            results[ticker] = count
        except Exception as e:
            logger.error("News pipeline error for %s: %s", ticker, e)
            results[ticker] = -1
    return results
