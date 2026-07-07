"""付费数据源目录 + 连通探测 + Key 解析。

- 目录 DATA_SOURCE_CATALOG：预置有公开自助 REST API 的主流源，每个源带真实探测函数。
- probe_source：用真实端点验证 API Key 可用性（不是 LLM，故不复用 create_llm）。
- resolve_data_source_key：供 fetcher 读取——DB(按 user) → env 兜底。

本次只做「配置 + 测连通」，不把数据接进分析链路。
"""

from __future__ import annotations

import logging
import os

import requests

logger = logging.getLogger(__name__)

_TIMEOUT = 10  # 探测请求超时（秒）
_UA = {"User-Agent": "BottleneckHunter/1.0"}


def _clip(msg: str, n: int = 140) -> str:
    return msg if len(msg) <= n else msg[:n] + "..."


# ── 各源探测函数：返回 (ok, msg) ──────────────────────────

def _probe_fmp(key: str, base_url: str = "") -> tuple[bool, str]:
    # FMP 自 2025-08-31 停用 /api/v3 旧端点，新用户须用 /stable/；仅 ?apikey= query 鉴权有效
    r = requests.get(f"https://financialmodelingprep.com/stable/quote?symbol=AAPL&apikey={key}",
                     timeout=_TIMEOUT, headers=_UA)
    if r.status_code in (401, 403):
        return False, "认证失败：API Key 无效"
    r.raise_for_status()
    data = r.json()
    if isinstance(data, dict) and data.get("Error Message"):
        return False, _clip(str(data["Error Message"]))
    if isinstance(data, list) and data:
        return True, f"连通成功（AAPL=${data[0].get('price', '?')}）"
    return False, "响应为空，Key 可能无权限或额度耗尽"


def _probe_finnhub(key: str, base_url: str = "") -> tuple[bool, str]:
    r = requests.get(f"https://finnhub.io/api/v1/quote?symbol=AAPL&token={key}",
                     timeout=_TIMEOUT, headers=_UA)
    if r.status_code in (401, 403):
        return False, "认证失败：API Key 无效"
    r.raise_for_status()
    data = r.json()
    if isinstance(data, dict) and data.get("c") is not None:
        return True, f"连通成功（AAPL=${data.get('c')}）"
    return False, "响应无行情字段，Key 可能无效"


def _probe_tushare(key: str, base_url: str = "") -> tuple[bool, str]:
    r = requests.post("https://api.tushare.pro",
                      json={"api_name": "trade_cal", "token": key,
                            "params": {"exchange": "SSE", "start_date": "20240101", "end_date": "20240105"},
                            "fields": "cal_date"},
                      timeout=_TIMEOUT, headers=_UA)
    r.raise_for_status()
    data = r.json()
    if data.get("code") == 0:
        return True, "连通成功（Tushare Pro）"
    return False, _clip(str(data.get("msg") or "Token 无效或积分不足"))


def _probe_alphavantage(key: str, base_url: str = "") -> tuple[bool, str]:
    r = requests.get(f"https://www.alphavantage.co/query?function=GLOBAL_QUOTE&symbol=IBM&apikey={key}",
                     timeout=_TIMEOUT, headers=_UA)
    r.raise_for_status()
    data = r.json()
    if data.get("Error Message"):
        return False, _clip(str(data["Error Message"]))
    if data.get("Note") or data.get("Information"):
        return False, _clip(str(data.get("Note") or data.get("Information")))  # 限流/额度
    if data.get("Global Quote", {}).get("05. price"):
        return True, "连通成功（IBM 行情已返回）"
    return False, "响应异常，Key 可能无效"


def _probe_tiingo(key: str, base_url: str = "") -> tuple[bool, str]:
    r = requests.get("https://api.tiingo.com/api/test",
                     headers={"Authorization": f"Token {key}", **_UA}, timeout=_TIMEOUT)
    if r.status_code in (401, 403):
        return False, "认证失败：Token 无效"
    r.raise_for_status()
    return True, "连通成功（Tiingo）"


def _probe_polygon(key: str, base_url: str = "") -> tuple[bool, str]:
    r = requests.get(f"https://api.polygon.io/v3/reference/tickers?apiKey={key}&limit=1",
                     timeout=_TIMEOUT, headers=_UA)
    if r.status_code in (401, 403):
        return False, "认证失败：API Key 无效"
    r.raise_for_status()
    data = r.json()
    if data.get("status") in ("OK", "DELAYED"):
        return True, "连通成功（Polygon.io）"
    return False, _clip(str(data.get("error") or "响应异常"))


def _probe_custom(key: str, base_url: str = "") -> tuple[bool, str]:
    """自定义源：GET 用户填写的探测 URL（{KEY} 占位符替换为实际 Key）。"""
    if not base_url:
        return False, "请填写探测 URL（base_url）"
    url = base_url.replace("{KEY}", key)
    r = requests.get(url, timeout=_TIMEOUT, headers=_UA)
    if r.status_code in (401, 403):
        return False, f"认证失败（HTTP {r.status_code}）"
    r.raise_for_status()
    return True, f"连通成功（HTTP {r.status_code}）"


# ── 目录：id → 元信息 + 探测函数 ─────────────────────────
# testable=False 的源（Morningstar/OpenBB）无公开自助 REST，仅存凭证不自动测连通。

DATA_SOURCE_CATALOG: list[dict] = [
    {"id": "fmp", "name": "Financial Modeling Prep", "env": "FMP_API_KEY",
     "site": "https://financialmodelingprep.com/developer/docs",
     "note": "季度财报/一致预期/深度财务，性价比高（推荐首选）", "testable": True, "probe": _probe_fmp},
    {"id": "finnhub", "name": "Finnhub", "env": "FINNHUB_API_KEY",
     "site": "https://finnhub.io/docs/api",
     "note": "美股行情/财务/新闻，免费档 60次/分", "testable": True, "probe": _probe_finnhub},
    {"id": "tushare", "name": "Tushare Pro", "env": "TUSHARE_TOKEN",
     "site": "https://tushare.pro/document/2",
     "note": "A股行情/财务/财报日历（积分制）", "testable": True, "probe": _probe_tushare},
    {"id": "alphavantage", "name": "Alpha Vantage", "env": "ALPHAVANTAGE_API_KEY",
     "site": "https://www.alphavantage.co/documentation/",
     "note": "美股行情/基本面，免费档限流较严", "testable": True, "probe": _probe_alphavantage},
    {"id": "tiingo", "name": "Tiingo", "env": "TIINGO_API_KEY",
     "site": "https://www.tiingo.com/documentation/general/overview",
     "note": "美股行情/财务/新闻", "testable": True, "probe": _probe_tiingo},
    {"id": "polygon", "name": "Polygon.io", "env": "POLYGON_API_KEY",
     "site": "https://polygon.io/docs",
     "note": "美股行情/期权/参考数据", "testable": True, "probe": _probe_polygon},
    {"id": "custom", "name": "自定义数据源", "env": "",
     "site": "", "note": "填写完整探测 URL（用 {KEY} 作 API Key 占位符）+ API Key",
     "testable": True, "probe": _probe_custom},
]

_CATALOG_BY_ID = {s["id"]: s for s in DATA_SOURCE_CATALOG}


def get_catalog() -> list[dict]:
    """返回目录（不含 probe 函数对象，供前端 JSON 序列化）。"""
    return [{k: v for k, v in s.items() if k != "probe"} for s in DATA_SOURCE_CATALOG]


def get_source_meta(source_id: str) -> dict | None:
    return _CATALOG_BY_ID.get(source_id)


def probe_source(source_id: str, api_key: str, base_url: str = "") -> tuple[bool, str]:
    """真实探测数据源连通性。异常/超时优雅降级为 (False, msg)。"""
    meta = _CATALOG_BY_ID.get(source_id)
    if not meta:
        return False, f"未知数据源：{source_id}"
    if not meta.get("testable") or meta.get("probe") is None:
        return False, "该数据源无公开自助 API，不支持自动测连通"
    if not api_key and source_id != "custom":
        return False, "请先填写 API Key"
    try:
        return meta["probe"](api_key, base_url)
    except requests.Timeout:
        return False, "请求超时（10s）"
    except requests.RequestException as e:
        return False, _clip(f"请求失败：{e}")
    except Exception as e:  # noqa: BLE001
        return False, _clip(f"探测异常：{e}")


def resolve_data_source_key(source_id: str, user_id: str = "") -> str:
    """供 fetcher 读取数据源 Key。

    - 有 user_id（用户请求）：只认该用户自己配置的 key，无则回退 env。
      **不借用他人 key** —— 否则会造成跨用户额度泄漏。
    - 无 user_id（后台全局采集，无 user 上下文）：取任一用户配置的 key，仍无则回退 env。
    """
    try:
        from bottleneck_hunter.auth.crypto import decrypt
        from bottleneck_hunter.auth.store import AuthStore
        store = AuthStore()
        if user_id:
            enc = store.get_data_source_key_encrypted(user_id, source_id)
        else:
            enc = store.any_data_source_key_encrypted(source_id)  # 仅后台全局采集
        if enc:
            return decrypt(enc)
    except Exception as e:  # noqa: BLE001
        logger.debug("resolve_data_source_key(%s) DB 读取失败: %s", source_id, e)
    meta = _CATALOG_BY_ID.get(source_id)
    env_var = (meta or {}).get("env", "")
    return os.environ.get(env_var, "") if env_var else ""
