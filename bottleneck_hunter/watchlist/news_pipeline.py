"""News data pipeline — fetch headlines + LLM summarization & sentiment.

Sources: yfinance Ticker.news + Google Finance RSS (US), akshare (A-stock).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import datetime, timezone
from urllib.parse import quote

import yfinance as yf

try:
    import akshare as ak
except ImportError:
    ak = None  # type: ignore[assignment]

from bottleneck_hunter.watchlist.budget import BudgetTracker
from bottleneck_hunter.watchlist.models import DegradationMode
from bottleneck_hunter.watchlist.retry import with_retry, get_http_client, fetch_with_timeout
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

async def _news_in_thread(fn, ticker: str, timeout_sec: float = 20) -> list[dict]:
    """在线程里跑阻塞新闻抓取并加超时兜底：挂起到点即放回线程/协程，返回空列表。

    yfinance/akshare 新闻抓取阻塞无超时——不加会一路占住线程池令新闻批次死挂。
    """
    try:
        return await fetch_with_timeout(asyncio.to_thread(fn, ticker), timeout_sec=timeout_sec)
    except asyncio.TimeoutError:
        logger.warning("抓取 %s 新闻超时(%ss)，跳过本轮", ticker, timeout_sec)
        return []


@with_retry(max_retries=3, base_delay=1.0)
def _fetch_yfinance_news(ticker: str, limit: int = 10) -> list[dict]:
    """Fetch news from yfinance Ticker.news (sync)."""
    t = yf.Ticker(ticker)
    raw = t.news or []
    results = []
    for item in raw[:limit]:
        content = item.get("content", {}) if isinstance(item.get("content"), dict) else {}
        title = content.get("title") or item.get("title", "")
        if not title:
            continue

        pub_date = content.get("pubDate") or content.get("displayTime", "")
        if pub_date:
            date_str = pub_date[:10]
        else:
            pub_ts = item.get("providerPublishTime", 0)
            date_str = datetime.fromtimestamp(pub_ts, tz=timezone.utc).strftime("%Y-%m-%d") if pub_ts else ""

        link = ""
        canonical = content.get("canonicalUrl")
        if isinstance(canonical, dict):
            link = canonical.get("url", "")
        elif isinstance(canonical, str):
            link = canonical
        if not link:
            click = content.get("clickThroughUrl")
            if isinstance(click, dict):
                link = click.get("url", "")
        if not link:
            link = item.get("link", "")

        provider = content.get("provider", {})
        publisher = provider.get("displayName", "") if isinstance(provider, dict) else ""
        if not publisher:
            publisher = item.get("publisher", "")

        summary = content.get("summary", "")

        news_id = hashlib.md5(f"{ticker}:{title}".encode()).hexdigest()[:12]
        results.append({
            "id": news_id,
            "ticker": ticker,
            "date": date_str,
            "title": title,
            "summary": summary,
            "source_url": link,
            "source_name": publisher,
        })
    return results


@with_retry(max_retries=3, base_delay=1.0)
async def _fetch_rss_news(query: str, limit: int = 5, tag: str = "") -> list[dict]:
    """Fetch from Google News RSS by free-text query (async).

    query: 任意检索词（个股用 f"{ticker} stock"，主题用 "AI stocks" 等）。
    tag:   用于返回项的 ticker 字段与去重 id；省略则用 query 本身。
    """
    tag = tag or query
    url = f"https://news.google.com/rss/search?q={quote(query)}&hl=en-US&gl=US&ceid=US:en"
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
        news_id = hashlib.md5(f"{tag}:rss:{title}".encode()).hexdigest()[:12]
        results.append({
            "id": news_id,
            "ticker": tag,
            "date": date_str,
            "title": title,
            "source_url": entry.get("link", ""),
            "source_name": "Google News",
        })
    return results


@with_retry(max_retries=3, base_delay=1.0)
def _fetch_astock_news(ticker: str, limit: int = 10) -> list[dict]:
    """Fetch A-stock news from akshare stock_news_em (sync)."""
    if ak is None:
        return []
    from bottleneck_hunter.watchlist.store_base import extract_astock_code
    code_6 = extract_astock_code(ticker)  # 全系统唯一 A股代码提取器（见 store_base）
    if not code_6:
        return []
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


_MARKET_ITEMS_PROMPT = """你是金融新闻编辑。请分析下列市场新闻标题，返回严格 JSON：
1. sentiment：整体情绪，取值 positive / negative / neutral
2. summaries：与输入等长的数组，每个元素是对应序号标题的一句**中文**摘要（≤40字，保留主体/事件/方向；输入若为英文或其它外文，请翻译成中文）

新闻标题：
{headlines}

严格按以下 JSON 回复：
{{"sentiment": "neutral", "summaries": ["中文摘要1", "中文摘要2"]}}"""


async def _summarize_market_items(items: list[dict], llm, budget: BudgetTracker) -> dict:
    """一次 LLM 调用：整体情绪 + 逐条中文摘要（按输入顺序对齐）。失败优雅降级为空。"""
    if not items or llm is None or budget is None:
        return {"sentiment": "neutral", "summaries": []}
    if budget.get_degradation_mode() == DegradationMode.MINIMAL:
        return {"sentiment": "neutral", "summaries": []}

    subset = items[:15]
    headlines = "\n".join(f"{i + 1}. {it.get('title', '')}" for i, it in enumerate(subset))
    prompt = _MARKET_ITEMS_PROMPT.format(headlines=headlines)
    try:
        from langchain_core.messages import HumanMessage
        response = await llm.ainvoke([HumanMessage(content=prompt)])
        text = response.content.strip()
        if "```" in text:
            text = text.split("```")[1].strip()
            if text.startswith("json"):
                text = text[4:].strip()
        result = json.loads(text)
        budget.record(
            provider=getattr(llm, "_llm_type", "unknown"),
            model=getattr(llm, "model_name", "unknown"),
            input_tokens=len(prompt) // 4,
            output_tokens=len(response.content) // 4,
            task_type="market_news_summary",
        )
        sums = result.get("summaries", [])
        return {
            "sentiment": result.get("sentiment", "neutral"),
            "summaries": [str(x) for x in sums] if isinstance(sums, list) else [],
        }
    except Exception as e:
        logger.warning("市场新闻批量中文摘要失败: %s", e)
        return {"sentiment": "neutral", "summaries": []}


# ---------------------------------------------------------------------------
# Batch pipeline
# ---------------------------------------------------------------------------

async def _fetch_one(ticker: str, store: WatchlistStore, llm, budget: BudgetTracker, market: str = "us_stock",
                     cache: dict | None = None) -> int:
    async with _get_sem():
        # 免费源文章拉取(RSS/yfinance/akshare)可共享；DataHub keyed 新闻 + LLM 情绪按用户
        _ck = ("news_full", market, ticker)
        if cache is not None and _ck in cache:
            articles = [dict(a) for a in cache[_ck]]
        else:
            if market == "a_stock":
                articles = await _news_in_thread(_fetch_astock_news, ticker)
            else:
                articles = await _news_in_thread(_fetch_yfinance_news, ticker)
                rss = await _fetch_rss_news(f"{ticker} stock", tag=ticker)
                seen = {a["id"] for a in articles}
                for r in rss:
                    if r["id"] not in seen:
                        articles.append(r)
                        seen.add(r["id"])
            if cache is not None:
                cache[_ck] = [dict(a) for a in articles]

        # 并入 DataHub 多源 keyed 新闻（finnhub/tiingo/fmp/av 按质量梯队+额度均衡；无 key 返 None）
        try:
            from bottleneck_hunter.data_provider.hub import CAP_NEWS, get_hub
            uid = getattr(store, "_user_id", "")  # 用本用户自己的 key，避免跨用户借用
            hubrec = await get_hub().fetch(CAP_NEWS, ticker, market, uid)
            seen = {a["id"] for a in articles}
            for a in (hubrec or {}).get("articles", []):
                if a.get("id") and a["id"] not in seen:
                    articles.append(a)
                    seen.add(a["id"])
        except Exception as e:  # noqa: BLE001
            logger.debug("DataHub 多源新闻并入跳过 (%s): %s", ticker, e)

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
    cache: dict | None = None,
) -> dict[str, int]:
    """Batch-fetch and summarize news. Returns {ticker: article_count}.

    cache: 周期内多用户共享免费源新闻拉取（RSS/yfinance/akshare）；按用户的 LLM 情绪不共享。
    """
    if not tickers:
        return {}
    results = {}
    for ticker in tickers:
        src = "akshare" if market == "a_stock" else "yfinance"
        try:
            from bottleneck_hunter.data_provider.hub import CAP_NEWS, get_hub
            async with get_hub().track(src, CAP_NEWS, market) as _sink:
                if llm and budget:
                    count = await _fetch_one(ticker, store, llm, budget, market=market, cache=cache)
                else:
                    # 调度器无 LLM 路径：纯免费源拉取，可周期内去重
                    _ck = ("news_sched", market, ticker)
                    if cache is not None and _ck in cache:
                        articles = [dict(a) for a in cache[_ck]]
                    else:
                        if market == "a_stock":
                            articles = await _news_in_thread(_fetch_astock_news, ticker)
                        else:
                            articles = await _news_in_thread(_fetch_yfinance_news, ticker)
                        if cache is not None:
                            cache[_ck] = [dict(a) for a in (articles or [])]
                    count = store.save_news(articles) if articles else 0
                _sink["rows"] = max(count, 0)
            results[ticker] = count
        except Exception as e:
            logger.error("News pipeline error for %s: %s", ticker, e)
            results[ticker] = -1
    return results


# ---------------------------------------------------------------------------
# 市场/主题级新闻（供 L1 宏观决策感知大盘与热点事件）
# ---------------------------------------------------------------------------

# 每市场的主题检索词表：(RSS 查询词, 展示标签)。热点漂移时在此维护。
_MARKET_NEWS_TOPICS: dict[str, list[tuple[str, str]]] = {
    "us_stock": [
        ("AI stocks", "AI"),
        ("Federal Reserve rate decision", "美联储"),
        ("stock market outlook", "大盘"),
    ],
    "a_stock": [
        ("人工智能 股市", "AI"),
        ("央行 货币政策", "央行"),
        ("A股 大盘 走势", "大盘"),
    ],
    "hk_stock": [
        ("Hong Kong stock market", "港股"),
        ("China AI technology", "AI"),
    ],
}


def _fetch_cn_market_news(limit: int = 12) -> list[dict]:
    """国内可访问的市场财经快讯（akshare：财联社/东财/同花顺），Google News 被墙时兜底。

    列名随源/版本变化，故用「含关键字」方式弹性识别标题/日期列。
    """
    try:
        import akshare as ak
    except ImportError:
        return []
    sources = [
        ("财联社", lambda: ak.stock_info_global_cls(symbol="全部")),
        ("东方财富", lambda: ak.stock_info_global_em()),
        ("同花顺", lambda: ak.stock_info_global_ths()),
    ]
    for name, fn in sources:
        try:
            df = fn()
            if df is None or getattr(df, "empty", True):
                continue
            cols = list(df.columns)
            title_col = next((c for c in cols if "标题" in str(c)), None) or cols[0]
            date_col = (next((c for c in cols if "日期" in str(c)), None)
                        or next((c for c in cols if "时间" in str(c)), None))
            items = []
            for _, r in df.head(limit).iterrows():
                title = str(r.get(title_col, "") or "").strip()
                if not title:
                    continue
                d = str(r.get(date_col, "") or "")[:10] if date_col else ""
                items.append({"topic": "", "title": title, "date": d, "source_name": name})
            if items:
                logger.info("市场新闻改用国内源「%s」兜底，%d 条", name, len(items))
                return items
        except Exception as e:  # noqa: BLE001
            logger.debug("国内新闻源 %s 失败: %s", name, e)
    return []


async def fetch_market_news(market: str = "us_stock", llm=None, budget: BudgetTracker | None = None,
                            per_topic: int = 4) -> list[dict]:
    """抓取市场/主题级近期新闻，供 L1 宏观决策的 {market_news} 使用。

    - 按 market 的主题词表并发抓 Google News RSS，按 id 去重。
    - Google News 抓不到（如国内服务器被墙）→ 用国内可访问的财经快讯兜底。
    - 有 llm+budget 时用 _summarize_with_llm 得整体市场情绪，附到每条。
    - 任何失败均优雅降级：返回已拿到的部分或空列表，绝不让 L1 因新闻抓取而中断。
    """
    topics = _MARKET_NEWS_TOPICS.get(market, _MARKET_NEWS_TOPICS["us_stock"])
    try:
        fetched = await asyncio.gather(
            *[_fetch_rss_news(q, limit=per_topic, tag=tag) for q, tag in topics],
            return_exceptions=True,
        )
    except Exception as e:
        logger.warning("市场新闻抓取失败: %s", e)
        return []

    items: list[dict] = []
    seen: set[str] = set()
    for group in fetched:
        if isinstance(group, Exception) or not group:
            continue
        for r in group:
            if r["id"] in seen:
                continue
            seen.add(r["id"])
            items.append({
                "topic": r.get("ticker", ""),   # tag 落在 ticker 字段
                "title": r.get("title", ""),
                "date": r.get("date", ""),
                "source_name": r.get("source_name", ""),
            })

    if not items:
        # Google News 抓不到（如国内服务器被墙）→ 用国内可访问的财经快讯兜底
        items = await asyncio.to_thread(_fetch_cn_market_news, max(per_topic * 4, 12))

    if not items:
        return []

    # 整体情绪 + 逐条中文摘要（一次 LLM 调用）
    if llm and budget:
        try:
            enriched = await _summarize_market_items(items, llm, budget)
            sentiment = enriched.get("sentiment", "neutral")
            summaries = enriched.get("summaries", [])
            for i, it in enumerate(items):
                it["sentiment"] = sentiment
                if i < len(summaries) and summaries[i]:
                    it["summary"] = summaries[i]
        except Exception as e:
            logger.warning("市场新闻分析失败: %s", e)

    return items


# 借道 news_digest 表存市场级新闻的哨兵 ticker（个股查询皆精确匹配，永不误捞）
_MARKET_SENTINELS = {
    "us_stock": "__MARKET_US__",
    "a_stock": "__MARKET_CN__",
    "hk_stock": "__MARKET_HK__",
}


def market_sentinel(market: str) -> str:
    return _MARKET_SENTINELS.get(market, "__MARKET_US__")


async def refresh_market_news(store: WatchlistStore, market: str = "us_stock",
                              llm=None, budget: BudgetTracker | None = None) -> int:
    """抓市场/主题新闻并落库（借道 news_digest 哨兵 ticker），供 L1 读库。返回写入条数。"""
    items = await fetch_market_news(market, llm, budget)
    if not items:
        return 0
    sentinel = market_sentinel(market)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    articles = []
    for it in items:
        title = it.get("title", "")
        if not title:
            continue
        articles.append({
            "id": hashlib.md5(f"{sentinel}:{title}".encode()).hexdigest()[:12],
            "ticker": sentinel,
            "date": it.get("date") or today,
            "title": title,
            "summary": it.get("summary", ""),
            "sentiment": it.get("sentiment", ""),
            "sentiment_score": 0.0,
            "source_url": "",
            "source_name": it.get("source_name", ""),
            "llm_analysis": it.get("topic", ""),  # 主题标签存这里
        })
    return store.save_news(articles) if articles else 0
