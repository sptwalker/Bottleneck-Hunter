"""Gangtise 投研 OpenAPI 生产级直连客户端（零依赖 skill 脚本）。

生产服务器 checkout 不含 .claude/skills/（被 .gitignore 忽略），故本模块独立重写取数逻辑，
只参考 skill 脚本的接口 URL / payload / 字段映射。token 按 (ak,sk) 进程内缓存 + TTL，
不用模块级固化全局，可按调用者切换凭据。所有失败抛异常交 DataHub 熔断，不 print、不落盘。

接口来源（对照 .claude/skills/gangtise-data/scripts/utils.py）：
- auth:      POST /application/auth/oauth/open/loginV2
- financial: POST /application/open-fundamental/financial-report/income-statement/{granularity}
- earnings:  POST /application/open-fundamental/earning-forecast
"""

from __future__ import annotations

import re
import threading
import time
from datetime import date, datetime, timedelta, timezone

import requests

_BJ = timezone(timedelta(hours=8))  # 北京时区（UTC+8）——公告/日历日期归一到北京日历

# ── 接口域名与 URL（照抄 skill utils.py，硬编码生产默认，无 env 依赖）──
_BASE = "https://openapi.gangtise.com/application"
_AUTH_URL = f"{_BASE}/auth/oauth/open/loginV2"
_FUNDAMENTAL = f"{_BASE}/open-fundamental"
_EARNING_FORECAST_URL = f"{_FUNDAMENTAL}/earning-forecast"
# 利润表按市场路由（活体实测三端点均返回真实口径，字段随市场略异，见 _map_gangtise_financials）：
#   A股→累计口径；美股→/us（net 取 netProfitParent）；港股→/hk（net 取 netProfitAttrParent，同 A股）。
# 历史 bug：旧码对所有市场都打 accumulated（A股端点），故美股/港股财务打错端点——这正是
# 「美股财务只能 FMP」误判的代码根因，实为 Gangtise 有 US/HK 三表、只是路由缺失。
_INCOME_URL_BY_MARKET: dict[str, str] = {
    "a_stock": f"{_FUNDAMENTAL}/financial-report/income-statement/accumulated",
    "us_stock": f"{_FUNDAMENTAL}/financial-report/income-statement/us",
    "hk_stock": f"{_FUNDAMENTAL}/financial-report/income-statement/hk",
}
_INCOME_URL = _INCOME_URL_BY_MARKET["a_stock"]  # 向后兼容旧引用（A股累计口径）
# EDB 宏观：全球指标库 getData（2D 表，键 indicatorIdList，非 indicators）
_EDB_GETDATA_URL = f"{_BASE}/open-alternative/EDB/getData"
# 投研洞察域（财报日历 / 公告）——照抄 skill utils.py 的 GANGTISE_INSIGHT_DOMAIN
_INSIGHT = f"{_BASE}/open-insight"
_PERFORMANCE_CALENDAR_URL = f"{_INSIGHT}/schedule/performance-calendar/getList"
_ANNOUNCE_CN_URL = f"{_INSIGHT}/announcement/getList"       # A股公司公告
_ANNOUNCE_HK_URL = f"{_INSIGHT}/announcement-hk/getList"    # 港股（P0 未用，留待第二市场）
_ANNOUNCE_US_URL = f"{_INSIGHT}/announcement-us/getList"    # 美股公告

# 研报（券商中资 / 外资）——照抄 skill broker_report/foreign_report
_BROKER_REPORT_URL = f"{_INSIGHT}/broker-report/getList"
_FOREIGN_REPORT_URL = f"{_INSIGHT}/foreign-report/getList"
# 知识库 RAG（open-data 域，非 open-insight）
_DATA = f"{_BASE}/open-data"
_KB_SEARCH_URL = f"{_DATA}/ai/search/knowledge_base"
# 证券码解析（美股 .O/.N 经 open-reference/securities/search）
_REFERENCE = f"{_BASE}/open-reference"
_SECURITIES_SEARCH_URL = f"{_REFERENCE}/securities/search"
# 历史行情日 K（open-quote，beta/benchmark 用）。
# 实测口径（2026-08 活体验证，见 fetch_quote_history 注释）：
#   · 个股（美/港/A）走统一 `kline/daily` 即可（TSLA.O/00700.HK/A股均返回真实收盘）；
#   · **指数**必须走 `index/kline/daily`——统一端点对指数返回 0 行或报错（SPX.SPI/IXIC.O/HSI.HI
#     在 kline/daily 全空，切 index/kline/daily 后均取到真实收盘）。这正是历史「US/HK 基准
#     beta=0」的根因：旧码只接了统一端点，误判为「Gangtise 无 US/HK 指数」，实为端点路由缺失。
_QUOTE_DAILY_URL = f"{_BASE}/open-quote/kline/daily"
_QUOTE_INDEX_DAILY_URL = f"{_BASE}/open-quote/index/kline/daily"
# 实时快照（FetcherManager 行情路径用）：POST {fieldList: SNAP_FIELD_LIST, securityList:[...]}。
# 端点/字段照抄 skill quote.py（QUOTE_REALTIME_URL + SNAP_FIELD_LIST），响应同为 data.fieldList+data.list 二维表。
_QUOTE_REALTIME_URL = f"{_BASE}/open-quote/quote/realtime"
# 日 K 完整字段（OHLCV+amount，照抄 skill quote.py _FIELD_LIST）——fetch_quote_history 只取 close，
# fetch_ohlcv_daily 取全字段供 FetcherManager；amount 必须透传（clean_ohlc A股量能反推「手/股」依赖它）。
_OHLCV_FIELDS = ["securityCode", "tradeDate", "open", "high", "low", "close",
                 "preClose", "change", "pctChange", "volume", "amount"]
# 实时快照字段（照抄 skill quote.py SNAP_FIELD_LIST）——latestPrice=最新价、pctChange=涨跌幅。
SNAP_FIELD_LIST = ["securityCode", "exchange", "tradeDate", "tradeTime", "latestPrice",
                   "open", "high", "low", "preClose", "change", "pctChange", "volume", "amount", "amplitude"]
# 估值分析（open-fundamental/valuation-analysis，免费）：按 indicator 取时间序列 value + 窗内分位。
# 实测（2026-08）：仅 A股返回数据，美股/港股/指数一律 code=120001（无覆盖）——故 fetch_valuation
# 只对 A股有意义，provider.supports 相应只认领 a_stock，不假装全市场。
_VALUATION_URL = f"{_FUNDAMENTAL}/valuation-analysis"
# A股资金流向（open-quote/fund-flow/daily，免费）：主力/大/中/小单净流入日序列（单位：元）。
# 实测仅 A股覆盖（美股/港股/指数 code=120001）。生产机房 akshare 被墙时，这是主力资金流的可达替代源。
_FUND_FLOW_DAILY_URL = f"{_BASE}/open-quote/fund-flow/daily"
# AI 研报叙事（open-ai/agent/{subpath}；同步 agent POST {securityCode} 即得，异步 agent 不在此接）
_AGENT_BASE = f"{_BASE}/open-ai/agent"
# 指标选股（open-indicator/screener；payload 需预构造好的 universe/expression/indicatorList）
_SCREENER_URL = f"{_BASE}/open-indicator/screener"

# 本系统 market → 日历 marketList 枚举（传 'cn' 报 100005，必须用这些驼峰枚举）
_CAL_MARKET = {
    "a_stock": ["aShares"],
    "us_stock": ["usStocks", "usChinaConcept"],  # 美股含中概（VIP 持仓多为美上市）
}
_PAGE_MAX = 50  # 官方单页上限

_TIMEOUT = 30
_TOKEN_TTL = 1800  # token 有效期保守取 30min，到期重换

# ── token 缓存：{(ak,sk): (token, uid, tenantid, productcode, expire_ts)} ──
_token_cache: dict[tuple[str, str], tuple] = {}
_token_lock = threading.Lock()


class GangtiseError(Exception):
    """Gangtise 接口错误（token 失败 / HTTP 非 200 / body code 异常）。"""


def _now() -> float:
    return time.time()


def _login(ak: str, sk: str) -> tuple:
    """ak/sk 换 token，返回 (token, uid, tenantid, productcode)。失败抛 GangtiseError。"""
    resp = requests.post(_AUTH_URL, json={"accessKey": ak, "secretKey": sk}, timeout=_TIMEOUT)
    if resp.status_code != 200:
        raise GangtiseError(f"auth HTTP {resp.status_code}")
    body = resp.json()
    if not body.get("state", True):
        raise GangtiseError(f"auth failed: {body.get('message', 'unknown')}")
    data = body.get("data") or {}
    token = data.get("accessToken")
    if not token:
        raise GangtiseError("auth: no accessToken in response")
    auth = token if str(token).startswith("Bearer ") else f"Bearer {token}"
    uid = None if data.get("uid") is None else str(data.get("uid"))
    tenantid = None if data.get("tenantId") is None else str(data.get("tenantId"))
    productcode = None if data.get("productCode") is None else str(data.get("productCode"))
    return auth, uid, tenantid, productcode


def _headers(ak: str, sk: str) -> dict:
    """取（缓存的）token 并构造请求头。TTL 内复用，过期或首次则登录。"""
    key = (ak, sk)
    with _token_lock:
        cached = _token_cache.get(key)
        if cached and cached[4] > _now():
            auth, uid, tenantid, productcode = cached[0], cached[1], cached[2], cached[3]
        else:
            auth, uid, tenantid, productcode = _login(ak, sk)
            _token_cache[key] = (auth, uid, tenantid, productcode, _now() + _TOKEN_TTL)
    h = {"Authorization": auth}
    if uid:
        h["uid"] = uid
    if tenantid:
        h["tenantid"] = tenantid
    if productcode:
        h["productcode"] = productcode
    return h


def _sec_code(ticker: str, market: str) -> str:
    """本系统 ticker → Gangtise securityCode。A股加 .SH/.SZ/.BJ 后缀（与 Tushare 同格式）。

    ponytail: 起步只做 A股（6位数字直通）。港股(.HK)/美股(.US) 的码制留待接第二市场时补，
    上升路径：接 open-reference/securities/search 做名称→码解析。
    """
    t = ticker.strip().upper()
    if "." in t:
        return t
    if market == "a_stock" and len(t) == 6 and t.isdigit():
        if t[0] == "6":
            return f"{t}.SH"
        if t[0] in ("4", "8") or t.startswith("920"):
            return f"{t}.BJ"
        return f"{t}.SZ"
    return t


# 美股 gtsCode 后缀（NASDAQ=.O / NYSE=.N，另有 .A/.PK/.OB/.US）——判「已是美股码」
_US_SUFFIXES = (".O", ".N", ".A", ".PK", ".OB", ".US")
# 证券码解析缓存：{(ticker_upper, market): gtsCode}（进程内，码制稳定不设 TTL）
_gts_code_cache: dict[tuple[str, str], str] = {}
_gts_code_lock = threading.Lock()


def _resolve_gts_code(ak: str, sk: str, ticker: str, market: str) -> str:
    """本系统 ticker → Gangtise gtsCode。A股走纯 `_sec_code`（不联网）；
    美股经 open-reference/securities/search 解析 .O/.N（进程内缓存）。

    解析失败/查无匹配 → 回退 `_sec_code`（裸 ticker），交由下游接口报错、不静默造码。
    """
    t = ticker.strip().upper()
    if market != "us_stock":
        return _sec_code(ticker, market)
    if t.endswith(_US_SUFFIXES):   # 已是美股码，原样
        return t
    key = (t, market)
    with _gts_code_lock:
        hit = _gts_code_cache.get(key)
    if hit:
        return hit
    payload = {"keyword": t, "top": 10, "category": ["stock", "dr"]}
    resp = requests.post(_SECURITIES_SEARCH_URL, headers=_headers(ak, sk), json=payload, timeout=_TIMEOUT)
    if resp.status_code != 200:
        raise GangtiseError(f"securities/search HTTP {resp.status_code}")
    body = resp.json()
    if not (str(body.get("code", "")) == "000000" and body.get("status") is True):
        return _sec_code(ticker, market)   # 接口拒绝 → 回退裸 ticker
    items = (body.get("data") or {}).get("list") or []
    # 优先取「码前缀 == ticker 且后缀为美股」的项；否则取首个美股码项
    best = None
    for it in items:
        gts = str(it.get("gtsCode") or "").strip().upper()
        if not gts.endswith(_US_SUFFIXES):
            continue
        if gts.rsplit(".", 1)[0] == t:
            best = gts
            break
        if best is None:
            best = gts
    resolved = best or _sec_code(ticker, market)
    if best:
        with _gts_code_lock:
            _gts_code_cache[key] = resolved
    return resolved


def _parse_report_body(body: dict) -> list[dict]:
    """解析财报接口 body（fieldList + list 二维表）→ list[dict]。照抄 skill _parse_income_statement_body。"""
    if not body or str(body.get("code", "")) != "000000" or body.get("status") is False:
        return []
    block = body.get("data") or {}
    fields = block.get("fieldList") or []
    rows = block.get("list") or []
    if not fields or not rows:
        return []
    out = []
    for row in rows:
        if not isinstance(row, (list, tuple)):
            continue
        n = min(len(fields), len(row))
        if n:
            out.append({fields[i]: row[i] for i in range(n)})
    return out


def fetch_financials(ak: str, sk: str, ticker: str, market: str) -> dict | None:
    """取利润表（累计/最近报告期），按市场路由端点。返回 {ticker, report_date, rows:[...]} 或 None。

    美股经 `_resolve_gts_code` 解析 gtsCode（.O/.N）；A股走纯 `_sec_code`（不联网）。
    端点按 market 选：美股→/us、港股→/hk、A股→累计口径。未知市场退 A股端点（保守）。
    """
    code = _resolve_gts_code(ak, sk, ticker, market)
    income_url = _INCOME_URL_BY_MARKET.get(market, _INCOME_URL)
    payload = {
        "securityCode": code,
        "period": ["latest"],            # 官方 period 枚举：latest=最近报告期（CLI 的 Q0 映射到此）
        "reportType": ["consolidated"],  # 合并报表
        "fieldList": [],                 # 空 = 取全部科目
        "startDate": None,
        "endDate": None,
        "fiscalYear": None,
    }
    resp = requests.post(income_url, headers=_headers(ak, sk), json=payload, timeout=_TIMEOUT)
    if resp.status_code != 200:
        raise GangtiseError(f"financials HTTP {resp.status_code}")
    rows = _parse_report_body(resp.json())
    if not rows:
        return None
    latest = rows[0]
    return {
        "ticker": ticker,
        "security_code": code,
        "report_date": latest.get("endDate") or latest.get("reportDate") or "",
        "rows": rows,
    }


def _parse_edb_body(body: dict, indicator_ids: list[str]) -> dict[str, dict]:
    """解析 EDB getData 二维表 → {indicator_id: {latest, prev, date}}。

    表结构：data.fieldList=['date', id1, id2...]，data.dataList=[[yyyymmdd, v1, v2...], ...]。
    值为字符串小数、可空（日频/月频序列交错，多数格是空串）。取每个 id 的**最新非空值**与
    其**前一个非空值**（供算变动），及最新值对应日期。空/错误码 → 空 dict。
    """
    if not body or str(body.get("code", "")) != "000000" or body.get("status") is False:
        return {}
    block = body.get("data") or {}
    fields = block.get("fieldList") or []
    rows = block.get("dataList") or []
    if not fields or not rows:
        return {}
    col = {fid: fields.index(fid) for fid in indicator_ids if fid in fields}
    out: dict[str, dict] = {}
    for fid, ci in col.items():
        seq = []  # (date, value) 非空，按表内顺序（升序）
        for row in rows:
            if not isinstance(row, (list, tuple)) or ci >= len(row):
                continue
            raw = row[ci]
            if raw in (None, "", "-"):
                continue
            try:
                seq.append((str(row[0]), float(raw)))
            except (ValueError, TypeError):
                continue
        if not seq:
            continue
        latest_date, latest_val = seq[-1]
        prev_val = seq[-2][1] if len(seq) >= 2 else None
        out[fid] = {"latest": latest_val, "prev": prev_val, "date": latest_date}
    return out


def fetch_edb(ak: str, sk: str, indicator_ids: list[str],
              start_date: str, end_date: str) -> dict[str, dict]:
    """取 EDB 宏观指标序列，返回 {indicator_id: {latest, prev, date}}。

    indicator_ids ≤10/次（官方批量上限）；start/end 传 'yyyy-MM-dd'。失败抛 GangtiseError 交熔断。
    """
    if not indicator_ids:
        return {}
    payload = {
        "indicatorIdList": list(indicator_ids),   # 关键：indicatorIdList，不是 indicators
        "startDate": start_date,
        "endDate": end_date,
    }
    resp = requests.post(_EDB_GETDATA_URL, headers=_headers(ak, sk), json=payload, timeout=_TIMEOUT)
    if resp.status_code != 200:
        raise GangtiseError(f"edb HTTP {resp.status_code}")
    return _parse_edb_body(resp.json(), indicator_ids)


def _parse_forecast_body(body: dict) -> list[dict]:
    """解析一致预期 body（与财报的二维表不同：data.updateList[].fieldList[] 按 forecastYear）。

    照抄 skill earning_forecast._parse_earning_forecast_body 的结构：外层按发布日 date，
    内层每条 field 是某 forecastYear 的一致预期（eps/pe/netIncome/roe…）。摊平成 list[dict]，
    每行含 date + forecastYear + 各指标英文键。
    """
    if not body or str(body.get("code", "")) != "000000" or body.get("status") is False:
        return []
    block = body.get("data") or {}
    update_list = block.get("updateList") or []
    out = []
    for item in update_list:
        if not isinstance(item, dict):
            continue
        d = str(item.get("date") or "").strip()
        for f in (item.get("fieldList") or []):
            if not isinstance(f, dict):
                continue
            fy = str(f.get("forecastYear") or "").strip()
            if not fy:
                continue
            row = dict(f)
            row["date"] = d
            out.append(row)
    return out


def fetch_earnings_forecast(ak: str, sk: str, ticker: str, market: str,
                            start_date: str, end_date: str) -> dict | None:
    """取券商一致预期（EPS/PE/归母净利/ROE 等）。返回 {ticker, forecasts:[...]} 或 None。"""
    code = _sec_code(ticker, market)
    payload = {
        "securityCode": code,
        "startDate": start_date,
        "endDate": end_date,
        # consensusList 必须显式列指标：实测传 [] 只回 forecastYear+date、无任何数值列
        "consensusList": ["eps", "pe", "netIncome", "netIncomeYoy", "roe"],
    }
    resp = requests.post(_EARNING_FORECAST_URL, headers=_headers(ak, sk), json=payload, timeout=_TIMEOUT)
    if resp.status_code != 200:
        raise GangtiseError(f"earnings HTTP {resp.status_code}")
    rows = _parse_forecast_body(resp.json())
    if not rows:
        return None
    return {"ticker": ticker, "security_code": code, "forecasts": rows}


# ── 财报日历 + 公告（催化剂源，见 GANGTISE_INTEGRATION_MASTER_PLAN §4.2）──────
# 语义与财报/EDB 不同：按市场段/单只 security 拉，时间窗有硬上限 → 按月分段再合并。


def _iter_month_segments(start: str, end: str):
    """把 [start, end]（'yyyy-MM-dd'）按月切成 ≤1 个月的闭区间子段（升序 yield）。

    治本 `110003 TIME_RANGE_EXCEEDED`（实测跨 6 月即报），非重试。子段首尾无缝衔接、不漏日。
    """
    s = datetime.strptime(start.strip()[:10], "%Y-%m-%d").date()
    e = datetime.strptime(end.strip()[:10], "%Y-%m-%d").date()
    cur = s
    while cur <= e:
        y, m = (cur.year, cur.month + 1) if cur.month < 12 else (cur.year + 1, 1)
        try:
            nxt = date(y, m, cur.day)
        except ValueError:            # cur.day 超下月最大天（1/31→2）→ 取下月首日
            nxt = date(y, m, 1)
        seg_end = min(e, nxt - timedelta(days=1))
        yield (cur.isoformat(), seg_end.isoformat())
        cur = seg_end + timedelta(days=1)


def _fetch_paged(url: str, headers: dict, base_payload: dict, parser,
                 *, parser_args: tuple = (), label: str = "") -> list[dict]:
    """from/size 翻页拉取并合并全部页；每页调 parser(body, *parser_args)。不足一页即止。"""
    out: list[dict] = []
    frm = 0
    while True:
        payload = dict(base_payload, **{"from": frm, "size": _PAGE_MAX})
        resp = requests.post(url, headers=headers, json=payload, timeout=_TIMEOUT)
        if resp.status_code != 200:
            raise GangtiseError(f"{label} HTTP {resp.status_code}")
        rows = parser(resp.json(), *parser_args)
        out.extend(rows)
        if len(rows) < _PAGE_MAX:
            break
        frm += _PAGE_MAX
    return out


def _format_time_range_ms(start: str, end: str) -> tuple[int, int]:
    """A股公告 startTime/endTime：毫秒整型时间戳（按北京时区解释；end 到当日 23:59:59.999）。

    ponytail: 显式挂 _BJ（skill 用裸 local time）——容器 tz 非北京时会偏一天，挂 _BJ 治本。
    """
    s = int(datetime.strptime(start.strip()[:10], "%Y-%m-%d").replace(tzinfo=_BJ).timestamp() * 1000)
    e_next = (datetime.strptime(end.strip()[:10], "%Y-%m-%d") + timedelta(days=1)).replace(tzinfo=_BJ)
    return s, int(e_next.timestamp() * 1000) - 1


def _format_time_range_str(start: str, end: str) -> tuple[str, str]:
    """US/HK 公告 startTime/endTime：字符串 'yyyy-MM-dd 00:00:00' / '... 23:59:59'。"""
    return f"{start.strip()[:10]} 00:00:00", f"{end.strip()[:10]} 23:59:59"


def _ms_to_bj_date(v) -> str:
    """公告时间戳（13位ms / 10位s / 'yyyy-MM-dd...' 串）→ 北京日历 'yyyy-MM-dd'；无法解析回空。"""
    if v in (None, ""):
        return ""
    s = str(v).strip()
    if s.isdigit():
        n = int(s)
        if len(s) >= 13:
            n //= 1000
        return datetime.fromtimestamp(n, _BJ).strftime("%Y-%m-%d")
    return s[:10]


def _parse_calendar_body(body: dict) -> list[dict]:
    """解析 performance-calendar getList body → list[dict]（宽松码：非明确错误即成功）。"""
    if not body:
        return []
    code = body.get("code", 200)
    if code not in (200, "000000") and body.get("status", True) is not True:
        return []
    rows = (body.get("data") or {}).get("list") or []
    out = []
    for it in rows:
        if not isinstance(it, dict):
            continue
        out.append({
            "report_id": it.get("performanceReportId"),
            "security_codes": it.get("securityCodeList") or [],
            "security_name": it.get("securityName") or "",
            "category": it.get("category") or "",
            "publish_date": str(it.get("publishDate") or "").strip()[:10],
            "title": (it.get("title") or "").strip(),
            "has_attachment": bool(it.get("hasAttachment")),
        })
    return out


def _parse_announcement_body(body: dict, strict: bool) -> list[dict]:
    """解析 announcement getList body → list[dict]。

    strict=False（A股）：非明确错误即成功；strict=True（US/HK）：须 code=='000000' 且 status is True。
    """
    if not body:
        return []
    if strict:
        ok = str(body.get("code", "")) == "000000" and body.get("status") is True
    else:
        code = body.get("code", 200)
        ok = not (code not in (200, "000000") and body.get("status", True) is not True)
    if not ok:
        return []
    rows = (body.get("data") or {}).get("list") or []
    out = []
    for it in rows:
        if not isinstance(it, dict):
            continue
        pc = it.get("primaryCategory") if isinstance(it.get("primaryCategory"), dict) else {}
        sc = it.get("secondaryCategory") if isinstance(it.get("secondaryCategory"), dict) else {}
        out.append({
            "announcement_id": it.get("announcementId"),
            "title": (it.get("title") or "").strip(),
            "publish_date": _ms_to_bj_date(it.get("publishTime") or it.get("announcementDate")),
            "security_code": it.get("securityCode") or "",
            "security_name": it.get("securityName") or "",
            "primary_category": (pc.get("categoryName") or ""),
            "secondary_category": (sc.get("categoryName") or ""),
            "source_name": it.get("sourceName") or "",
            "file_count": it.get("fileCount") or 0,
        })
    return out


def fetch_performance_calendar(ak: str, sk: str, markets, start: str, end: str,
                               categories=None, securities=None) -> list[dict]:
    """取财报日历/业绩预告快报。markets：本系统 market 列表（a_stock/us_stock，映射到 marketList 枚举）；
    start/end 'yyyy-MM-dd'；categories 默认三类全取；securities 传证券码则只取这些标的（缺省全市场）。
    内部按月分段 + 翻页合并。
    """
    market_list: list[str] = []
    for m in markets:
        market_list.extend(_CAL_MARKET.get(m, []))
    if not market_list:
        return []
    cats = list(categories) if categories else [
        "performanceForecast", "performanceExpress", "performanceAnnouncement"]
    sec_list = [str(s).upper() for s in securities] if securities else []
    headers = _headers(ak, sk)
    out: list[dict] = []
    for seg_s, seg_e in _iter_month_segments(start, end):
        base = {"startDate": seg_s, "endDate": seg_e,
                "categoryList": cats, "marketList": market_list, "securityList": sec_list}
        out.extend(_fetch_paged(_PERFORMANCE_CALENDAR_URL, headers, base,
                                _parse_calendar_body, label="calendar"))
    return out


def fetch_announcements(ak: str, sk: str, market: str, security: str,
                        start: str, end: str, categories=None) -> list[dict]:
    """取单只标的公告。market：a_stock/us_stock（港股留待第二市场）；security：单只 ticker；
    start/end 'yyyy-MM-dd'。A股用毫秒时间戳；美股用字符串时间戳 + 必填 searchType/rankType。
    内部按月分段 + 翻页合并。
    """
    code = _sec_code(security, market)
    headers = _headers(ak, sk)
    us = market == "us_stock"
    url = _ANNOUNCE_US_URL if us else _ANNOUNCE_CN_URL
    out: list[dict] = []
    for seg_s, seg_e in _iter_month_segments(start, end):
        base = {"securityList": [code], "categoryList": list(categories) if categories else []}
        if us:
            st, et = _format_time_range_str(seg_s, seg_e)
            base.update({"startTime": st, "endTime": et, "searchType": 1, "rankType": 1})
        else:
            st, et = _format_time_range_ms(seg_s, seg_e)
            base.update({"startTime": st, "endTime": et})
        out.extend(_fetch_paged(url, headers, base, _parse_announcement_body,
                                parser_args=(us,), label="announcement"))
    return out


# ── 研报（券商中/外资）+ 知识库 KB + 历史行情（beta/benchmark）────────────
# 研报语义同公告：批量列表、时间窗按月分段；证据注入 chain 交叉验证 + 投委会 + VIP advisory。


def _strip_html(s) -> str:
    """剥离研报 title/brief 里的高亮 HTML 标签（<em>…</em> 等），压缩空白。"""
    if not s:
        return ""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", str(s))).strip()


def _parse_research_body(body: dict, foreign: bool) -> list[dict]:
    """解析 broker/foreign-report getList body → list[dict]（宽松码：code∈{200,'000000'} 或 status is True）。"""
    if not body:
        return []
    code = body.get("code", 200)
    if code not in (200, "000000") and body.get("status") is not True:
        return []
    rows = (body.get("data") or {}).get("list") or []
    out = []
    for it in rows:
        if not isinstance(it, dict):
            continue
        pub = it.get("publisher") if isinstance(it.get("publisher"), dict) else {}
        secs = [
            {"code": s.get("securityCode") or "", "name": s.get("securityName") or ""}
            for s in (it.get("securityList") or []) if isinstance(s, dict)
        ]
        inds = [
            (i.get("industryName") or "") for i in (it.get("industryList") or [])
            if isinstance(i, dict)
        ]
        row = {
            "report_id": it.get("reportId"),
            "title": _strip_html(it.get("title")),
            "brief": _strip_html(it.get("brief")),
            "publish_date": _ms_to_bj_date(it.get("publishTime") or it.get("reportDate")),
            "securities": secs,
            "industries": [x for x in inds if x],
            "category": it.get("category") or "",
            "broker": pub.get("brokerName") or "",
            "analyst": pub.get("author") or "",
            "llm_tags": it.get("llmTagList") or [],
            "page_number": it.get("pageNumber") or 0,
            "foreign": foreign,
        }
        if foreign:                       # 外资研报的中文翻译标题/摘要（若有）
            row["title_zh"] = _strip_html(it.get("titleTranslate"))
            row["brief_zh"] = _strip_html(it.get("briefTranslate"))
        out.append(row)
    return out


def fetch_research(ak: str, sk: str, *, securities=None, industries=None,
                   category_list=None, llm_tag_list=None,
                   start: str, end: str, foreign: bool = False) -> list[dict]:
    """取券商研报（foreign=False 中资 broker-report / True 外资 foreign-report）。

    两条分类轴：category_list（macro/strategy/industry/company/…）+ llm_tag_list
    （inDepth 深度 / earningsReview 业绩点评 / industryStrategy 行业策略）。
    时间窗按月分段（治本潜在窗口上限）+ from/size 翻页合并。start/end 'yyyy-MM-dd'。
    """
    url = _FOREIGN_REPORT_URL if foreign else _BROKER_REPORT_URL
    sec_list = [str(s).upper() for s in securities] if securities else []
    headers = _headers(ak, sk)
    out: list[dict] = []
    for seg_s, seg_e in _iter_month_segments(start, end):
        st, et = _format_time_range_ms(seg_s, seg_e)
        base: dict = {"startTime": st, "endTime": et}
        if sec_list:
            base["securityList"] = sec_list
        if industries:
            base["industryList"] = list(industries)
        if category_list:
            base["categoryList"] = list(category_list)
        if llm_tag_list:
            base["llmTagList"] = list(llm_tag_list)
        out.extend(_fetch_paged(url, headers, base, _parse_research_body,
                                parser_args=(foreign,), label="research"))
    return out


def _parse_kb_body(body: dict) -> list[dict]:
    """解析 knowledge_base body → list[dict]。片段在**顶层 data**（非 data.list）。"""
    if not body:
        return []
    code = body.get("code", 200)
    if code not in (200, "000000") and body.get("status") is not True:
        return []
    rows = body.get("data") or []
    if not isinstance(rows, list):
        return []
    out = []
    for it in rows:
        if not isinstance(it, dict):
            continue
        out.append({
            "content": (it.get("content") or "").strip(),
            "title": (it.get("title") or "").strip(),
            "time": str(it.get("time") or "").strip(),
            "resource_type": it.get("resourceType") or "",
            "source_id": it.get("sourceId") or "",
        })
    return out


def fetch_kb(ak: str, sk: str, query: str, top: int = 10) -> list[dict]:
    """知识库 RAG 语义检索：query 必填，top 取回片段数。返回 [{content,title,time,...}]。"""
    if not query or not query.strip():
        return []
    payload = {"query": query.strip(), "top": int(top)}
    resp = requests.post(_KB_SEARCH_URL, headers=_headers(ak, sk), json=payload, timeout=_TIMEOUT)
    if resp.status_code != 200:
        raise GangtiseError(f"kb HTTP {resp.status_code}")
    return _parse_kb_body(resp.json())


def _parse_quote_body(body: dict) -> dict[str, list]:
    """解析 kline/daily 二维表 → {securityCode: [(tradeDate, close), ...]}（按表内顺序，值 str→float）。

    表结构：data.fieldList=[列名...]，data.list=[行数组...]（位置对齐 fieldList）。
    码校验严格：str(code)=='000000' 且 status is not False。close 为字符串，本地转 float。
    """
    if not body or str(body.get("code", "")) != "000000" or body.get("status") is False:
        return {}
    block = body.get("data") or {}
    fields = block.get("fieldList") or []
    rows = block.get("list") or []
    if not fields or not rows:
        return {}
    try:
        ci_code = fields.index("securityCode")
        ci_date = fields.index("tradeDate")
        ci_close = fields.index("close")
    except ValueError:
        return {}
    out: dict = {}
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) <= max(ci_code, ci_date, ci_close):
            continue
        code = str(row[ci_code] or "").strip()
        d = str(row[ci_date] or "").strip()
        try:
            cl = float(row[ci_close])
        except (ValueError, TypeError):
            continue
        if code and d:
            out.setdefault(code, []).append((d, cl))
    return out


def fetch_quote_history(ak: str, sk: str, securities, start: str, end: str,
                        limit: int = 10000, *, is_index: bool = False) -> dict[str, list]:
    """取日频收盘序列（beta/benchmark 用）。securities：gtsCode 列表；
    start/end 'yyyy-MM-dd'；返回 {gtsCode: [(date, close)...]}。不复权（beta 两侧同口径即可）。

    is_index：True 走 `index/kline/daily`（宽基指数专用端点），False 走统一 `kline/daily`（个股）。
    个股（美/港/A）统一端点即可；指数**必须**用 index 端点，否则统一端点返回 0 行——实测
    SPX.SPI/IXIC.O/HSI.HI/000300.SH 在 index 端点均取到真实收盘，是修 US/HK 基准 beta=0 的关键。
    """
    sec_list = [str(s).upper() for s in securities] if securities else []
    if not sec_list:
        return {}
    payload = {
        "securityList": sec_list,
        "startDate": start.strip()[:10],
        "endDate": end.strip()[:10],
        "limit": int(limit),
        "fieldList": ["securityCode", "tradeDate", "close"],
    }
    url = _QUOTE_INDEX_DAILY_URL if is_index else _QUOTE_DAILY_URL
    resp = requests.post(url, headers=_headers(ak, sk), json=payload, timeout=_TIMEOUT)
    if resp.status_code != 200:
        raise GangtiseError(f"quote HTTP {resp.status_code}")
    return _parse_quote_body(resp.json())


# ── 行情/日K OHLCV + 实时快照（FetcherManager 路径，master plan 未覆盖的第二条 dispatch）──────
# fetch_quote_history 保持 close-only（VIP beta 消费者依赖），以下为**加法式**扩展：取全 OHLCV+amount。


def _parse_ohlcv_body(body: dict) -> list[dict]:
    """解析 kline/daily 二维表 → [{date,open,high,low,close,volume,amount}...]（升序）。

    严格码 000000 且 status is not False；缺 tradeDate/OHLC 任一列 → []；单行任一价格非数 → 跳过。
    volume/amount 缺失 → 0.0（下游 clean_ohlc 用 amount 反推 A股量能单位，故必须带出）。
    """
    if not body or str(body.get("code", "")) != "000000" or body.get("status") is False:
        return []
    block = body.get("data") or {}
    fields = block.get("fieldList") or []
    rows = block.get("list") or []
    if not fields or not rows:
        return []
    idx = {name: i for i, name in enumerate(fields)}
    if any(k not in idx for k in ("tradeDate", "open", "high", "low", "close")):
        return []

    def _num(row, name):
        i = idx.get(name)
        if i is None or i >= len(row):
            return None
        try:
            return float(row[i])
        except (ValueError, TypeError):
            return None

    out = []
    i_d = idx["tradeDate"]
    for row in rows:
        if not isinstance(row, (list, tuple)) or i_d >= len(row):
            continue
        d = str(row[i_d] or "").strip()[:10]
        o, h, low_, c = _num(row, "open"), _num(row, "high"), _num(row, "low"), _num(row, "close")
        if not d or None in (o, h, low_, c):
            continue
        out.append({
            "date": d, "open": o, "high": h, "low": low_, "close": c,
            "volume": _num(row, "volume") or 0.0,
            "amount": _num(row, "amount") or 0.0,
        })
    out.sort(key=lambda r: r["date"])  # kline/daily 已升序；显式排序保证确定性
    return out


def fetch_ohlcv_daily(ak: str, sk: str, ticker: str, market: str, days: int = 180) -> list[dict]:
    """取日频 OHLCV（含 amount）供 FetcherManager 行情路径。个股统一走 `kline/daily`。

    A股 6位直通、美股经 securities/search 解析 .O/.N（`_resolve_gts_code`）。窗口按 days 放宽
    （交易日≈日历日×5/7，取 ×1.6+15 缓冲确保覆盖 days 根 bar，下游 tail 截取）。空/错误码 → []。
    """
    code = _resolve_gts_code(ak, sk, ticker, market)
    days = max(int(days), 1)
    span = int(days * 1.6) + 15
    end = date.today()
    start = end - timedelta(days=span)
    payload = {
        "securityList": [code],
        "startDate": start.isoformat(),
        "endDate": end.isoformat(),
        "limit": max(days * 2, 500),
        "fieldList": list(_OHLCV_FIELDS),
    }
    resp = requests.post(_QUOTE_DAILY_URL, headers=_headers(ak, sk), json=payload, timeout=_TIMEOUT)
    if resp.status_code != 200:
        raise GangtiseError(f"ohlcv HTTP {resp.status_code}")
    return _parse_ohlcv_body(resp.json())


def _parse_realtime_body(body: dict) -> list[dict]:
    """解析实时快照二维表（data.fieldList+data.list）→ 逐行 dict（原始英文键→原值）。严格码。"""
    if not body or str(body.get("code", "")) != "000000" or body.get("status") is False:
        return []
    block = body.get("data") or {}
    fields = block.get("fieldList") or []
    rows = block.get("list") or []
    if not fields or not rows:
        return []
    out = []
    for row in rows:
        if not isinstance(row, (list, tuple)):
            continue
        n = min(len(fields), len(row))
        if n:
            out.append({fields[i]: row[i] for i in range(n)})
    return out


def fetch_realtime_quote(ak: str, sk: str, ticker: str, market: str) -> dict | None:
    """取单只实时快照（latestPrice/pctChange/volume/amount/OHLC）→ 规范 dict 或 None。

    A股 6位直通、美股经 securities/search 解析码。取匹配 securityCode 的行（无匹配退首行）；
    latestPrice 为空/0 → None（未开市/无效行情）。失败抛 GangtiseError 交熔断。
    """
    code = _resolve_gts_code(ak, sk, ticker, market)
    payload = {"fieldList": list(SNAP_FIELD_LIST), "securityList": [code]}
    resp = requests.post(_QUOTE_REALTIME_URL, headers=_headers(ak, sk), json=payload, timeout=_TIMEOUT)
    if resp.status_code != 200:
        raise GangtiseError(f"realtime HTTP {resp.status_code}")
    rows = _parse_realtime_body(resp.json())
    if not rows:
        return None
    up = code.upper()
    snap = next((r for r in rows if str(r.get("securityCode", "")).upper() == up), rows[0])

    def _f(k):
        try:
            return float(snap.get(k))
        except (ValueError, TypeError):
            return None

    price = _f("latestPrice")
    if not price:
        return None
    return {
        "security_code": code,
        "price": price,
        "change_pct": _f("pctChange") or 0.0,
        "volume": _f("volume") or 0.0,
        "amount": _f("amount") or 0.0,
        "open": _f("open"), "high": _f("high"), "low": _f("low"),
        "pre_close": _f("preClose"),
        "trade_date": str(snap.get("tradeDate") or "").strip()[:10],
    }


def _parse_valuation_body(body: dict) -> tuple[float | None, float | None, str]:
    """解析 valuation-analysis 单指标 body → (latest_value, latest_percentile, as_of_date)。

    表结构：data.fieldList=['tradeDate','value','percentileRank']，data.list=[行...]（升序）。
    取最后一行（最新交易日）的 value 与窗内分位（percentileRank，0~100）。空/错误码 → (None,None,'')。
    """
    if not body or str(body.get("code", "")) != "000000" or body.get("status") is False:
        return None, None, ""
    block = body.get("data") or {}
    fields = block.get("fieldList") or []
    rows = block.get("list") or []
    if not fields or not rows:
        return None, None, ""
    try:
        ci_d = fields.index("tradeDate")
        ci_v = fields.index("value")
        ci_p = fields.index("percentileRank")
    except ValueError:
        return None, None, ""
    for row in reversed(rows):  # 最新交易日在末尾；跳过尾部空值行
        if not isinstance(row, (list, tuple)) or len(row) <= max(ci_d, ci_v, ci_p):
            continue
        try:
            v = float(row[ci_v])
            p = float(row[ci_p])
        except (ValueError, TypeError):
            continue
        d = str(row[ci_d] or "").strip()[:10]
        return round(v, 4), round(p, 2), d
    return None, None, ""


# 估值分位默认取的指标（peTtm 市盈率TTM / pbMrq 市净率 / peg）；官方 indicator 枚举，勿改拼写。
_VALUATION_INDICATORS = ("peTtm", "pbMrq", "peg")


def fetch_valuation(ak: str, sk: str, ticker: str, market: str,
                    indicators=_VALUATION_INDICATORS, years: int = 3) -> dict | None:
    """取估值分位：每指标返回 {value, percentile, as_of}（percentile=近 `years` 年窗内分位 0~100）。

    仅 A股有覆盖（实测美股/港股/指数 code=120001）。窗口取近 `years` 年，分位为窗内相对位置——
    yfinance 只给当前 PE，给不出「贵/便宜」的历史锚，这是该能力唯一增量价值。
    返回 {indicator: {value, percentile, as_of}}；全指标皆空 → None（交 hub 视为无数据，不落桩）。
    """
    code = _resolve_gts_code(ak, sk, ticker, market)
    from datetime import date as _date
    end = _date.today()
    try:
        start = end.replace(year=end.year - int(years))
    except ValueError:  # 2/29 等边界
        start = end.replace(year=end.year - int(years), day=28)
    out: dict = {}
    for ind in indicators:
        payload = {
            "securityCode": code,
            "indicator": ind,
            "startDate": start.isoformat(),
            "endDate": end.isoformat(),
            "limit": 2000,
            "fieldList": ["value", "percentileRank"],
        }
        resp = requests.post(_VALUATION_URL, headers=_headers(ak, sk), json=payload, timeout=_TIMEOUT)
        if resp.status_code != 200:
            raise GangtiseError(f"valuation HTTP {resp.status_code}")
        v, p, as_of = _parse_valuation_body(resp.json())
        if v is not None:
            out[ind] = {"value": v, "percentile": p, "as_of": as_of}
    return out or None


def _parse_fund_flow_body(body: dict) -> list[dict]:
    """解析 fund-flow/daily 二维表 → [{date, main_net, large_net, xlarge_net}...]（升序，单位：元）。

    表结构：data.fieldList=[列名...]，data.list=[行...]。取主力/大单/特大单净流入（元）。
    空/错误码 → []。缺列则该列缺省 None。
    """
    if not body or str(body.get("code", "")) != "000000" or body.get("status") is False:
        return []
    block = body.get("data") or {}
    fields = block.get("fieldList") or []
    rows = block.get("list") or []
    if not fields or not rows:
        return []
    idx = {name: i for i, name in enumerate(fields)}
    i_d = idx.get("tradeDate")
    if i_d is None:
        return []

    def _cell(row, name):
        i = idx.get(name)
        if i is None or i >= len(row):
            return None
        try:
            return float(row[i])
        except (ValueError, TypeError):
            return None

    out = []
    for row in rows:
        if not isinstance(row, (list, tuple)) or i_d >= len(row):
            continue
        d = str(row[i_d] or "").strip()[:10]
        if not d:
            continue
        out.append({
            "date": d,
            "main_net": _cell(row, "mainNetInflow"),
            "large_net": _cell(row, "largeNetInflow"),
            "xlarge_net": _cell(row, "xlargeNetInflow"),
        })
    return out


def fetch_fund_flow(ak: str, sk: str, ticker: str, start: str, end: str) -> list[dict]:
    """取 A股资金流向日序列（主力/大单/特大单净流入，单位：元，升序）。

    仅 A股覆盖。返回 [{date, main_net, large_net, xlarge_net}...]，失败抛 GangtiseError 交熔断，空 → []。
    生产机房 akshare 被墙时的可达替代源（聪明钱 A股主力净流入兜底）。
    """
    code = _sec_code(ticker, "a_stock")  # A股 6位直通 → 交易所后缀（不联网）
    payload = {
        "securityList": [code],
        "startDate": start.strip()[:10],
        "endDate": end.strip()[:10],
        "limit": 100,
        "fieldList": ["securityCode", "tradeDate", "mainNetInflow", "largeNetInflow", "xlargeNetInflow"],
    }
    resp = requests.post(_FUND_FLOW_DAILY_URL, headers=_headers(ak, sk), json=payload, timeout=_TIMEOUT)
    if resp.status_code != 200:
        raise GangtiseError(f"fund-flow HTTP {resp.status_code}")
    return _parse_fund_flow_body(resp.json())


def _parse_screener_body(body: dict) -> list[dict]:
    """解析 screener body → [{code, name}]（只取命中名单，忽略指标矩阵值）。

    data.securityCodeList / securityNameList 位置对齐。空集返回 []（各数组空）。
    """
    if not body or str(body.get("code", "")) != "000000" or body.get("status") is False:
        return []
    block = body.get("data") or {}
    codes = block.get("securityCodeList") or []
    names = block.get("securityNameList") or []
    out = []
    for i, code in enumerate(codes):
        c = str(code or "").strip()
        if not c:
            continue
        out.append({"code": c, "name": str(names[i]).strip() if i < len(names) else ""})
    return out


def screen(ak: str, sk: str, *, universe: list[str], expression: str,
           indicator_list: list[dict]) -> list[dict]:
    """指标选股（A股）：传已构造好的 universe / expression / indicatorList → 命中 [{code,name}]。

    ponytail: 不做「口语→payload」的指标/板块 NL 解析（那是 gangtise-screener skill 的 ~3000 行活）。
      本函数只做 API 直连；调用方给结构化条件。上升路径：需口语选股时接 skill 的三段式解析器。
    缺 universe/expression/indicatorList 任一 → API 报 100001，故此处先行短路返回 []。
    """
    universe = [str(u).strip() for u in (universe or []) if str(u).strip()]
    if not universe or not (expression or "").strip() or not indicator_list:
        return []
    payload = {"universe": universe, "expression": expression.strip(),
               "indicatorList": indicator_list}
    resp = requests.post(_SCREENER_URL, headers=_headers(ak, sk), json=payload, timeout=_TIMEOUT)
    if resp.status_code != 200:
        raise GangtiseError(f"screener HTTP {resp.status_code}")
    return _parse_screener_body(resp.json())


def _parse_narrative_body(body: dict) -> str:
    """解析同步 agent body → Markdown 文本（data.content；data 为 list 时首个非空 content）。"""
    if not body or str(body.get("code", "")) != "000000" or body.get("status") is not True:
        return ""
    data = body.get("data")
    if isinstance(data, dict):
        return str(data.get("content") or "").strip()
    if isinstance(data, list):
        for it in data:
            if isinstance(it, dict) and str(it.get("content") or "").strip():
                return str(it["content"]).strip()
    return str(data or "").strip() if data else ""


# 同步 agent 子路径（POST {securityCode} 直接返回 content）。异步 agent（earnings-review/
# viewpoint-debate 需 getId→轮询 600s）默认不接——调用重，按 §10 边界默认关。
_NARRATIVE_SUBPATH = {
    "one-pager": "/one-pager",
    "investment-logic": "/investment-logic",
    "peer-comparison": "/peer-comparison",
}


def fetch_narrative(ak: str, sk: str, agent_type: str, security: str) -> str:
    """AI 研报叙事：一页通 / 投资逻辑 / 同业对比（同步 agent，POST securityCode 即得 Markdown）。

    security 传 gtsCode（A股 6位加后缀、美股 .O/.N）；agent_type 见 _NARRATIVE_SUBPATH。
    ponytail: 只接 3 个同步 agent；异步 agent（earnings-review 等）需 600s 轮询，默认关（§10），
      上升路径：需要时抄 skill agents.py 的 getId→getContent 轮询封装。
    """
    sub = _NARRATIVE_SUBPATH.get(agent_type)
    if not sub or not (security or "").strip():
        return ""
    url = f"{_AGENT_BASE}{sub}"
    resp = requests.post(url, headers=_headers(ak, sk),
                         json={"securityCode": security.strip().upper()}, timeout=_TIMEOUT)
    if resp.status_code != 200:
        raise GangtiseError(f"narrative HTTP {resp.status_code}")
    return _parse_narrative_body(resp.json())


def _demo() -> None:
    """自检：token 缓存分支 + 证券码映射 + body 解析（全 mock，不打真实网络）。"""
    # ① 证券码映射
    assert _sec_code("600519", "a_stock") == "600519.SH"
    assert _sec_code("000001", "a_stock") == "000001.SZ"
    assert _sec_code("830799", "a_stock") == "830799.BJ"
    assert _sec_code("600519.SH", "a_stock") == "600519.SH"  # 已带后缀原样
    assert _sec_code("AAPL", "us_stock") == "AAPL"

    # ② body 解析：二维表 → list[dict]
    body = {"code": "000000", "data": {
        "fieldList": ["reportDate", "revenue", "netProfit"],
        "list": [["2025-03-31", 100.0, 20.0], ["2024-12-31", 400.0, 80.0]]}}
    rows = _parse_report_body(body)
    assert len(rows) == 2 and rows[0]["revenue"] == 100.0, rows
    assert _parse_report_body({"code": "999", "data": {}}) == []   # 错误码 → 空
    assert _parse_report_body({}) == []

    # ②b 一致预期解析：updateList[].fieldList[] 按 forecastYear 摊平
    fbody = {"code": "000000", "data": {"securityCode": "600519.SH", "updateList": [
        {"date": "2025-08-01", "fieldList": [
            {"forecastYear": "2025", "eps": 70.5, "pe": 20.1, "netIncome": 880.0},
            {"forecastYear": "2026", "eps": 80.2, "pe": 17.7, "netIncome": 1001.0}]}]}}
    frows = _parse_forecast_body(fbody)
    assert len(frows) == 2 and frows[0]["forecastYear"] == "2025" and frows[0]["eps"] == 70.5, frows
    assert frows[0]["date"] == "2025-08-01"
    assert _parse_forecast_body({"code": "999", "data": {}}) == []

    # ②c EDB 二维稀疏表解析：取每个 id 的最新非空 + 前一个非空 + 最新日期
    edb_body = {"code": "000000", "data": {
        "fieldList": ["date", "M00012461", "M00012340"],
        "dataList": [
            ["20260531", "4.2", ""],       # CPI 有值、利率空
            ["20260601", "", "3.62"],      # 利率有值、CPI 空
            ["20260630", "3.5", "3.63"]]}}  # 两者最新值
    ev = _parse_edb_body(edb_body, ["M00012461", "M00012340"])
    assert ev["M00012461"] == {"latest": 3.5, "prev": 4.2, "date": "20260630"}, ev
    assert ev["M00012340"]["latest"] == 3.63 and ev["M00012340"]["prev"] == 3.62, ev
    assert _parse_edb_body({"code": "999"}, ["M1"]) == {}          # 错误码 → 空
    assert _parse_edb_body(edb_body, ["M_NONE"]) == {}            # id 不在表 → 空

    # ②d 财报日历解析 + 按月分段
    cal_body = {"code": "000000", "data": {"list": [
        {"performanceReportId": "R1", "securityCodeList": ["600519.SH"], "securityName": "贵州茅台",
         "category": "performanceExpress", "publishDate": "2026-09-15 08:00", "title": "业绩快报",
         "hasAttachment": True}]}}
    cal = _parse_calendar_body(cal_body)
    assert len(cal) == 1 and cal[0]["security_codes"] == ["600519.SH"], cal
    assert cal[0]["publish_date"] == "2026-09-15" and cal[0]["has_attachment"] is True, cal
    assert _parse_calendar_body({"code": "100005"}) == []            # 明确错误码 → 空
    assert _parse_calendar_body({"data": {"list": []}}) == []

    # ②e 公告解析：A股宽松 vs 美股严格；毫秒时间戳 → 北京日历
    ann_body = {"code": "000000", "status": True, "data": {"list": [
        {"announcementId": "A1", "title": "关于回购股份的公告",
         "publishTime": 1757894400000,          # 2025-09-15 00:00 UTC → 北京 08:00 同日
         "primaryCategory": {"categoryName": "公司治理"},
         "secondaryCategory": {"categoryName": "回购"},
         "securityCode": "600519.SH", "securityName": "贵州茅台", "fileCount": 2}]}}
    a_cn = _parse_announcement_body(ann_body, False)
    assert len(a_cn) == 1 and a_cn[0]["primary_category"] == "公司治理", a_cn
    assert a_cn[0]["publish_date"] == "2025-09-15", a_cn
    a_us = _parse_announcement_body(ann_body, True)                  # 严格：code+status 满足 → 通过
    assert len(a_us) == 1, a_us
    # 美股严格码：缺 status 即失败；A股宽松码：缺 status 仍通过
    assert _parse_announcement_body({"code": "000000", "data": {"list": [{"title": "x"}]}}, True) == []
    assert len(_parse_announcement_body({"code": 200, "data": {"list": [{"title": "x"}]}}, False)) == 1

    # ②f 按月分段：跨 6 月拆成 ≤1 月子段，首尾无缝、全覆盖
    segs = list(_iter_month_segments("2026-01-15", "2026-07-14"))
    assert segs[0][0] == "2026-01-15" and segs[-1][1] == "2026-07-14", segs
    assert len(segs) == 6, segs
    for (_, e0), (s1, _) in zip(segs, segs[1:], strict=False):       # 相邻段无缝衔接（差 1 天）
        d0 = datetime.strptime(e0, "%Y-%m-%d").date()
        d1 = datetime.strptime(s1, "%Y-%m-%d").date()
        assert (d1 - d0).days == 1, (e0, s1)
    assert list(_iter_month_segments("2026-03-10", "2026-03-20")) == [("2026-03-10", "2026-03-20")]

    # ②g 时间戳格式化：A股毫秒 int，美股字符串
    ms_s, ms_e = _format_time_range_ms("2026-01-01", "2026-01-01")
    assert isinstance(ms_s, int) and ms_e - ms_s == 86400000 - 1, (ms_s, ms_e)
    str_s, str_e = _format_time_range_str("2026-01-01", "2026-01-01")
    assert str_s == "2026-01-01 00:00:00" and str_e == "2026-01-01 23:59:59"

    # ②h 研报解析：中资 publisher/securityList/llmTagList；外资含翻译字段；HTML 剥离
    rb = {"code": "000000", "data": {"list": [
        {"reportId": "R9", "title": "<em>贵州茅台</em>深度报告", "brief": "<p>维持买入</p>",
         "publishTime": 1757894400000, "category": "company",
         "publisher": {"brokerName": "国海证券", "author": "张三"},
         "securityList": [{"securityCode": "600519.SH", "securityName": "贵州茅台"}],
         "industryList": [{"industryName": "食品饮料"}], "llmTagList": ["inDepth"], "pageNumber": 32}]}}
    rr = _parse_research_body(rb, False)
    assert len(rr) == 1 and rr[0]["broker"] == "国海证券" and rr[0]["analyst"] == "张三", rr
    assert rr[0]["title"] == "贵州茅台深度报告" and rr[0]["brief"] == "维持买入", rr   # HTML 剥离
    assert rr[0]["securities"][0]["code"] == "600519.SH" and rr[0]["llm_tags"] == ["inDepth"], rr
    assert rr[0]["publish_date"] == "2025-09-15" and "title_zh" not in rr[0], rr
    fb = {"code": "000000", "data": {"list": [
        {"reportId": "F1", "title": "AAPL Deep Dive", "titleTranslate": "<em>苹果</em>深度",
         "brief": "Buy", "briefTranslate": "买入", "publishTime": "2026-08-01",
         "publisher": {"brokerName": "Morgan Stanley"}, "securityList": [], "llmTagList": []}]}}
    fr = _parse_research_body(fb, True)
    assert fr[0]["foreign"] is True and fr[0]["title_zh"] == "苹果深度" and fr[0]["brief_zh"] == "买入", fr
    assert _parse_research_body({"code": "500"}, False) == []            # 明确错误码 → 空
    assert len(_parse_research_body({"status": True, "data": {"list": [{"title": "x"}]}}, False)) == 1  # 仅 status

    # ②i KB 解析：片段在顶层 data（非 data.list）
    kbb = {"code": "000000", "data": [
        {"content": "半导体设备国产化率提升", "title": "行业纪要", "time": "2026-08-20",
         "resourceType": "report", "sourceId": "S1"}]}
    kb = _parse_kb_body(kbb)
    assert len(kb) == 1 and kb[0]["content"].startswith("半导体") and kb[0]["source_id"] == "S1", kb
    assert _parse_kb_body({"code": "000000", "data": {"list": []}}) == []   # data 非 list → 空
    assert _parse_kb_body({"code": "999", "data": []}) == []

    # ②j 行情解析：二维表 → {code: [(date, close)]}，close str→float，严格码
    qb = {"code": "000000", "data": {
        "fieldList": ["securityCode", "tradeDate", "close"],
        "list": [["AAPL.O", "2026-08-28", "230.5"], ["AAPL.O", "2026-08-29", "232.1"],
                 ["NDX", "2026-08-28", "19000.0"]]}}
    q = _parse_quote_body(qb)
    assert q["AAPL.O"] == [("2026-08-28", 230.5), ("2026-08-29", 232.1)], q
    assert q["NDX"][0][1] == 19000.0, q
    assert _parse_quote_body({"code": "000000", "status": False}) == {}     # status False → 空
    assert _parse_quote_body({"code": "000000", "data": {"fieldList": ["x"], "list": [["1"]]}}) == {}  # 缺列 → 空

    # ②j-1 OHLCV 解析：全字段二维表 → [{date,ohlcv,amount}]，str→float，缺 OHLC/日期行跳过，升序
    ob = {"code": "000000", "data": {
        "fieldList": _OHLCV_FIELDS,
        "list": [["600519.SH", "2026-08-29", "1500", "1520", "1495", "1512", "1490", "22", "1.48",
                  "3.1e4", "4.6e7"],
                 ["600519.SH", "2026-08-28", "1490", "1505", "1485", "1500", "1480", "20", "1.35",
                  "2.9e4", "4.3e7"]]}}
    ohlcv = _parse_ohlcv_body(ob)
    assert len(ohlcv) == 2 and ohlcv[0]["date"] == "2026-08-28", ohlcv          # 升序：28 在前
    assert ohlcv[1]["close"] == 1512.0 and ohlcv[1]["amount"] == 4.6e7, ohlcv    # amount 透传
    assert ohlcv[0]["volume"] == 2.9e4, ohlcv
    bad_ob = {"code": "000000", "data": {"fieldList": ["tradeDate", "open", "high", "low", "close"],
              "list": [["2026-08-28", "10", "", "9", "9.5"]]}}                    # high 空 → 该行跳过
    assert _parse_ohlcv_body(bad_ob) == [], _parse_ohlcv_body(bad_ob)
    assert _parse_ohlcv_body({"code": "120001"}) == []                           # 错误码 → 空
    assert _parse_ohlcv_body({"code": "000000", "data": {
        "fieldList": ["tradeDate", "close"], "list": [["2026-08-28", "9.5"]]}}) == []  # 缺 OHLC 列 → 空

    # ②j-2 实时快照解析：SNAP 二维表 → 逐行 dict；取匹配码行
    rtb = {"code": "000000", "status": True, "data": {
        "fieldList": SNAP_FIELD_LIST,
        "list": [["AAPL.O", "NASDAQ", "2026-08-31", "16:00", "232.5", "230.0", "233.0", "229.5",
                  "231.0", "1.5", "0.65", "5.2e7", "1.2e10", "1.52"]]}}
    rrows = _parse_realtime_body(rtb)
    assert len(rrows) == 1 and rrows[0]["latestPrice"] == "232.5", rrows
    assert rrows[0]["securityCode"] == "AAPL.O" and rrows[0]["pctChange"] == "0.65", rrows
    assert _parse_realtime_body({"code": "000000", "status": False}) == []       # status False → 空
    assert _parse_realtime_body({"code": "999"}) == []

    # ②k 证券码：美股已带 .O/.N 后缀原样（不联网）；A股走纯映射
    assert _resolve_gts_code("ak", "sk", "AAPL.O", "us_stock") == "AAPL.O"
    assert _resolve_gts_code("ak", "sk", "600519", "a_stock") == "600519.SH"

    # ③ token 缓存：首次 login，TTL 内复用（_login 只应被调一次）
    calls = {"n": 0}
    global _login
    orig = _login
    def fake_login(ak, sk):
        calls["n"] += 1
        return "Bearer TESTTOK", "u1", "t1", "p1"
    _login = fake_login
    try:
        _token_cache.clear()
        h1 = _headers("ak", "sk")
        _headers("ak", "sk")   # 命中缓存（不新登录）
        assert calls["n"] == 1, f"token 应只 login 一次，实得 {calls['n']}"
        assert h1["Authorization"] == "Bearer TESTTOK"
        assert h1["uid"] == "u1" and h1["tenantid"] == "t1" and h1["productcode"] == "p1"
        # 过期 → 重新 login
        _token_cache[("ak", "sk")] = ("Bearer OLD", None, None, None, _now() - 1)
        _headers("ak", "sk")
        assert calls["n"] == 2, "过期后应重新 login"
    finally:
        _login = orig
        _token_cache.clear()

    # ②l 选股解析：securityCodeList/securityNameList 对齐 → [{code,name}]，忽略指标矩阵
    scr = {"code": "000000", "status": True, "data": {
        "securityCodeList": ["002371.SZ", "688012.SH", ""],
        "securityNameList": ["北方华创", "中微公司"],
        "values": [[15.2], [12.1], [9.9]]}}
    ss = _parse_screener_body(scr)
    assert len(ss) == 2 and ss[0] == {"code": "002371.SZ", "name": "北方华创"}, ss
    assert ss[1]["code"] == "688012.SH" and ss[1]["name"] == "中微公司", ss   # 空码被跳过
    assert _parse_screener_body({"code": "100001"}) == []                      # 缺条件错误码 → 空
    assert _parse_screener_body({"code": "000000", "status": False}) == []
    assert screen("ak", "sk", universe=[], expression="F1>0",
                  indicator_list=[{"field": "F1"}]) == []                       # 缺 universe 短路

    # ②l-1 估值分位解析：取末行（最新交易日）value+percentileRank；尾部空值行跳过
    valb = {"code": "000000", "status": True, "data": {
        "fieldList": ["tradeDate", "value", "percentileRank"],
        "list": [["2026-08-20", "18.5", "12.0"],
                 ["2026-08-21", "19.2", "15.3"],
                 ["2026-08-22", "", ""]]}}   # 末行空 → 回退到前一行
    v, p, as_of = _parse_valuation_body(valb)
    assert v == 19.2 and p == 15.3 and as_of == "2026-08-21", (v, p, as_of)
    assert _parse_valuation_body({"code": "120001"}) == (None, None, "")   # 无覆盖码 → 空
    assert _parse_valuation_body({"code": "000000", "data": {"fieldList": [], "list": []}}) == (None, None, "")

    # ②l-2 资金流解析：二维表 → [{date, main_net, large_net, xlarge_net}]（元、升序，缺列缺省）
    ffb = {"code": "000000", "status": True, "data": {
        "fieldList": ["securityCode", "tradeDate", "mainNetInflow", "largeNetInflow", "xlargeNetInflow"],
        "list": [["600519.SH", "2026-08-21", 3.69e7, 3.26e7, 4.25e6],
                 ["600519.SH", "2026-08-22", -1.2e7, -8.0e6, -4.0e6]]}}
    ff = _parse_fund_flow_body(ffb)
    assert len(ff) == 2 and ff[0]["date"] == "2026-08-21" and ff[0]["main_net"] == 3.69e7, ff
    assert ff[1]["main_net"] == -1.2e7, ff
    assert _parse_fund_flow_body({"code": "120001"}) == []                  # 无覆盖码 → 空
    assert _parse_fund_flow_body({"code": "000000", "data": {"fieldList": [], "list": []}}) == []

    # ②m 叙事解析：data.content 直取；data 为 list 取首个非空；错误/非 True 状态 → 空串
    nb = {"code": "000000", "status": True,
          "data": {"date": "2026-08-30", "content": "## 贵州茅台一页通\n瑞银目标价1572"}}
    assert _parse_narrative_body(nb).startswith("## 贵州茅台"), _parse_narrative_body(nb)
    nlist = {"code": "000000", "status": True, "data": [{"content": ""}, {"content": "投资逻辑正文"}]}
    assert _parse_narrative_body(nlist) == "投资逻辑正文", _parse_narrative_body(nlist)
    assert _parse_narrative_body({"code": "999", "status": True, "data": {"content": "x"}}) == ""
    assert _parse_narrative_body({"code": "000000", "status": False, "data": {"content": "x"}}) == ""
    assert fetch_narrative("ak", "sk", "unknown-agent", "600519.SH") == ""      # 未知 agent 短路

    print("gangtise_client demo: OK")


if __name__ == "__main__":
    _demo()
