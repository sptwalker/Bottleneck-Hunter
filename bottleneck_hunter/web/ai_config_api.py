"""统一 AI 配置 API — 挂载于 /api/ai-config

合并 API Key 管理 + 模型角色分配 + 综合测试 + 自动推荐。
"""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from bottleneck_hunter.auth.dependencies import get_current_user
from bottleneck_hunter.llm_clients.factory import (
    create_llm,
    get_custom_provider,
    list_custom_provider_ids,
    resolve_provider_model,
)
from bottleneck_hunter.llm_clients.role_registry import (
    ROLE_REGISTRY,
)
from bottleneck_hunter.watchlist.store import WatchlistStore

logger = logging.getLogger(__name__)

router = APIRouter(tags=["ai-config"])

_store: WatchlistStore | None = None
_auth_store = None  # AuthStore：custom_providers 唯一真源


def set_store(store: WatchlistStore) -> None:
    global _store
    _store = store


def set_auth_store(store) -> None:
    global _auth_store
    _auth_store = store


def _get_store(user: dict) -> WatchlistStore:
    if _store is None:
        raise HTTPException(500, "Store not initialized")
    return _store.for_user(user.get("sub", ""))




# ── GET /roles ──


@router.get("/roles")
async def get_roles(user: dict = Depends(get_current_user)):
    """返回所有角色定义 + 当前配置 + 可用 Provider 列表。"""
    store = _get_store(user)
    uid = user.get("sub", "")

    db_configs = store.get_role_configs(user_id=uid)
    config_map: dict[str, list[dict]] = {}
    for c in db_configs:
        config_map.setdefault(c["role_key"], []).append(c)

    roles = []
    for role_def in ROLE_REGISTRY.values():
        slots = []
        db_slots = config_map.get(role_def.key, [])
        if db_slots:
            for s in sorted(db_slots, key=lambda x: x["slot_index"]):
                # 回显 model 亦按 provider 当前默认实时解析，不回显冻结快照——与运行时装配口径一致，
                # 避免「系统配置改了默认模型、矩阵仍显示旧值」的误导（与 get_models_for_role 同根因修复）。
                slots.append({
                    "slot_index": s["slot_index"],
                    "provider": s["provider"],
                    "model": resolve_provider_model(s["provider"], uid) or s["model"],
                })
        # 矩阵留空即由智能调度自动选型（DC_MODEL_* 环境影子配置已退役，不再回显）

        roles.append({
            "key": role_def.key,
            "label": role_def.label,
            "group": role_def.group,
            "multi_model": role_def.multi_model,
            "max_slots": role_def.max_slots,
            "default_provider": role_def.default_provider,
            "default_model": role_def.default_model or resolve_provider_model(role_def.default_provider, uid),
            "slot_labels": role_def.slot_labels,
            "slots": slots,
        })

    providers = _build_providers_list(uid)

    return {"roles": roles, "available_providers": providers}


def _build_providers_list(user_id: str = "", include_unconfigured: bool = False) -> list[dict]:
    """统一 provider 列表：provider 定义（catalog）来自 custom_providers 表（全平台共享），
    但「是否已配置 Key / key_hint」严格按**当前用户**判定（严格隔离，Key 从不全局）。
    """
    providers = []
    if _auth_store is None:
        return providers
    try:
        rows = _auth_store.list_custom_providers()
    except Exception:
        return providers

    # 当前用户自己的 Key 提示（provider -> key_hint）+ 用户级主模型 + 用户级付费类型
    user_hints: dict[str, str] = {}
    user_primary = ""
    user_tiers: dict[str, str] = {}
    if user_id:
        try:
            for k in _auth_store.get_user_api_keys(user_id):
                user_hints[k["provider"]] = k.get("key_hint", "") or ""
        except Exception:
            pass
        try:
            from bottleneck_hunter.watchlist.store import WatchlistStore
            wl = WatchlistStore()
            user_primary = (wl.get_primary_provider_config(user_id) or "").lower().strip()
            user_tiers = wl.get_provider_tiers(user_id)  # {provider_id: 'free'|'paid'} 一次性取，免 N 次单查
        except Exception:  # noqa: BLE001
            user_primary = ""

    for cp in rows:
        pid = cp["provider_id"]
        has_key = pid in user_hints
        health = None
        if user_id:
            try:
                from bottleneck_hunter.llm_clients import provider_gate
                health = provider_gate.disabled_info(user_id, pid)  # 认证失效/限流严重禁用态，未禁则 None
            except Exception:  # noqa: BLE001
                pass
        providers.append({
            "id": pid,
            "name": cp.get("display_name") or pid,
            "configured": has_key,               # 按当前用户
            "is_builtin": False,
            "default_model": cp.get("default_model", "") or "",
            "base_url": cp.get("base_url", "") or "",
            "key_hint": user_hints.get(pid, ""),  # 当前用户自己的 hint
            "health": health,                     # {status,reason,detail,disabled_at} 或 None
            "is_primary": pid == user_primary,    # 当前用户自己设定的主模型（严格用户级隔离）
            "tier": user_tiers.get(pid, ""),      # 当前用户自定付费类型 'free'/'paid'/''（徽标唯一真源，退役前端硬编码）
        })
    return providers


# ── POST /roles ──


class RoleSlotConfig(BaseModel):
    role_key: str
    slot_index: int = 0
    provider: str
    model: str


class RoleConfigSaveRequest(BaseModel):
    configs: list[RoleSlotConfig]


@router.post("/roles")
async def save_roles(req: RoleConfigSaveRequest, user: dict = Depends(get_current_user)):
    """批量保存角色-模型配置。"""
    store = _get_store(user)
    uid = user.get("sub", "")

    saved_keys: set[str] = set()
    for cfg in req.configs:
        role_def = ROLE_REGISTRY.get(cfg.role_key)
        label = role_def.label if role_def else cfg.role_key
        group = role_def.group if role_def else "unknown"

        store.upsert_role_config(
            role_key=cfg.role_key,
            slot_index=cfg.slot_index,
            provider=cfg.provider,
            model=cfg.model,
            role_label=label,
            role_group=group,
            user_id=uid,
        )
        saved_keys.add(cfg.role_key)

    return {"saved": len(req.configs), "roles": list(saved_keys)}


# ── GET /providers ──


@router.get("/providers")
async def get_providers(user: dict = Depends(get_current_user)):
    """获取所有 Provider（统一真源 custom_providers）。"""
    return {"providers": _build_providers_list(user.get("sub", ""))}


# 说明：Provider 的增删改（Key/模型/base_url/显示名）统一走 /api/custom-providers，
# 原 POST /providers/keys、PUT/DELETE /providers/{id}/config 已废弃删除（单轨化）。


class TestOneRequest(BaseModel):
    provider: str
    model: str = ""
    base_url: str = ""
    api_key: str = ""


@router.post("/test/one")
async def test_one(req: TestOneRequest, user: dict = Depends(get_current_user)):
    """测试单个（可未保存）provider/model 配置连通性 —— 供编辑态测试按钮。"""
    from langchain_core.messages import HumanMessage

    uid = user.get("sub", "")
    provider = req.provider.strip()
    # Key 留空时用该用户已存的 Key（加密表），否则内置 provider 测试会因取不到 Key 而误报失败
    api_key = req.api_key.strip()
    if not api_key:
        try:
            from bottleneck_hunter.web.user_api import resolve_user_api_key
            api_key = resolve_user_api_key(uid, provider) or ""
        except Exception:
            api_key = ""
    model = req.model.strip() or resolve_provider_model(provider, uid)
    if not model:
        return {"ok": False, "error": "未指定模型（请填写模型名）"}
    try:
        llm = create_llm(provider, model,
                         api_key=(api_key or None),
                         base_url=(req.base_url.strip() or None),
                         user_id=uid, with_fallback=False)
        await asyncio.wait_for(
            asyncio.to_thread(lambda: llm.invoke([HumanMessage(content="hi")])),
            timeout=60,
        )
        return {"ok": True, "model": model}
    except asyncio.TimeoutError:
        return {"ok": False, "error": "请求超时（60s）"}
    except Exception as e:
        msg = str(e).strip() or e.__class__.__name__
        return {"ok": False, "error": msg[:300]}


# ── POST /provider/{provider}/recover ──


_RECOVER_CALLS = 3  # 流量测试：顺序发 N 次真实调用，全过才算恢复（区别于 /test/one 单发探活）


@router.post("/provider/{provider}/recover")
async def recover_provider(provider: str, user: dict = Depends(get_current_user)):
    """流量测试恢复被持久禁用（密钥失效/限流严重/超时频发）的节点：用当前用户 key 顺序发
    _RECOVER_CALLS 次真实调用，全过 → 解除禁用；任一失败 → 保留禁用并回失败原因。"""
    from langchain_core.messages import HumanMessage

    from bottleneck_hunter.llm_clients import provider_gate

    uid = user.get("sub", "")
    provider = provider.strip()
    if not provider_gate.is_disabled(uid, provider):
        return {"ok": True, "recovered": True, "note": "该节点未被禁用"}

    # 解析当前用户自己的 key + 模型（严格隔离，缺 key 即失败）
    try:
        from bottleneck_hunter.web.user_api import resolve_user_api_key
        api_key = resolve_user_api_key(uid, provider) or ""
    except Exception:
        api_key = ""
    model = resolve_provider_model(provider, uid)
    if not model:
        return {"ok": False, "recovered": False, "error": "未配置模型（请先在配置中心填写模型名）"}

    for i in range(_RECOVER_CALLS):
        try:
            llm = create_llm(provider, model, api_key=(api_key or None),
                             user_id=uid, with_fallback=False)
            await asyncio.wait_for(
                asyncio.to_thread(lambda m=llm: m.invoke([HumanMessage(content="ping")])),
                timeout=60,
            )
        except asyncio.TimeoutError:
            return {"ok": False, "recovered": False, "error": f"第 {i + 1}/{_RECOVER_CALLS} 次请求超时(60s)"}
        except Exception as e:
            from bottleneck_hunter.llm_clients.fallback import classify_reason
            msg = str(e).strip() or e.__class__.__name__
            return {"ok": False, "recovered": False,
                    "error": f"第 {i + 1}/{_RECOVER_CALLS} 次失败：{msg[:200]}",
                    "reason": classify_reason(e)}

    provider_gate.clear(uid, provider)  # 全过才解除禁用
    return {"ok": True, "recovered": True, "calls": _RECOVER_CALLS}


# ── POST /provider/{provider}/tier ──


class TierRequest(BaseModel):
    tier: str = ""  # '' 未设 / 'free' 免费 / 'paid' 付费


@router.post("/provider/{provider}/tier")
async def set_provider_tier(provider: str, req: TierRequest, user: dict = Depends(get_current_user)):
    """设当前用户对某 provider 的**付费类型**（用户级，压过 health 静态表；策略「免费/付费优先」据此判定）。
    任何登录用户可设自己的档（tier per-user，非共享定义，故不限管理员）。"""
    uid = user.get("sub", "")
    pid = (provider or "").strip()
    tier = (req.tier or "").strip().lower()
    if tier not in ("", "free", "paid"):
        raise HTTPException(400, "tier 仅接受 '' / 'free' / 'paid'")
    if not pid:
        raise HTTPException(400, "provider 不能为空")
    _get_store(user).set_provider_config_tier(pid, tier, uid)
    return {"ok": True, "provider": pid, "tier": tier}


# ── POST /test/connectivity ──


def _configured_provider_models() -> list[tuple[str, str]]:
    """列出所有已配置 provider 及其默认模型（统一真源 custom_providers 运行时缓存）。含已禁用。"""
    configured: list[tuple[str, str]] = []
    for cp_id in list_custom_provider_ids():
        cp_info = get_custom_provider(cp_id)
        cp_model = cp_info.get("default_model", "") if cp_info else ""
        configured.append((cp_id, cp_model or cp_id))
    return configured


def _active_provider_models() -> list[tuple[str, str]]:
    """仅启用中的 provider（admin 禁用的跳过）——综合测试用，不浪费时间测已禁用模型。"""
    from bottleneck_hunter.llm_clients.factory import is_provider_active
    return [(p, m) for p, m in _configured_provider_models() if is_provider_active(p)]


@router.post("/test/connectivity")
async def test_connectivity(user: dict = Depends(get_current_user)):
    """测试所有已配置 Provider 的连通性。"""
    from bottleneck_hunter.web.model_tester import test_connectivity as _test

    configured = _configured_provider_models()

    async def _run_one(provider, model):
        try:
            result = await _test(provider, model)
            return {"provider": provider, "model": model, **result}
        except Exception as e:
            return {"provider": provider, "model": model, "score": 0, "error": str(e)[:200]}

    results = await asyncio.gather(*[_run_one(p, m) for p, m in configured])
    return {"results": list(results)}


# ── POST /test/comprehensive (SSE) ──


@router.post("/test/comprehensive")
async def test_comprehensive(request: Request, incremental: bool = False,
                             user: dict = Depends(get_current_user)):
    """SSE 综合能力测试。incremental=True 只测「尚无测试结果」的接口(新增接口增量补测，省时)。"""
    store = _get_store(user)
    uid = user.get("sub", "")

    configured = [(p, m) for p, m in _active_provider_models() if m]
    if incremental:
        # 增量：只测「所有维度都还没测过」的接口；半途中断(仅部分维度)的仍会重测补齐
        from bottleneck_hunter.web.model_tester import TEST_DIMENSIONS
        need = len(TEST_DIMENSIONS)
        dims_by_pm: dict[tuple, set] = {}
        for r in store.get_test_results(user_id=uid):
            dims_by_pm.setdefault((r["provider"], r["model"]), set()).add(r["test_type"])
        fully_tested = {pm for pm, dims in dims_by_pm.items() if len(dims) >= need}
        configured = [(p, m) for p, m in configured if (p, m) not in fully_tested]

    async def _stream():
        from bottleneck_hunter.llm_clients.role_registry import _BOTTLENECK_WEIGHTS
        from bottleneck_hunter.web.model_tester import (
            compute_composite_score,
            run_comprehensive_test,
        )

        total = len(configured)
        all_results = []

        yield {"event": "test_start", "data": json.dumps({"total": total})}

        for i, (provider, model) in enumerate(configured):
            if await request.is_disconnected():
                break

            yield {
                "event": "test_model_start",
                "data": json.dumps({"provider": provider, "model": model, "index": i}),
            }

            results = await run_comprehensive_test(provider, model)

            for dim, result in results.items():
                store.save_test_result(provider, model, dim, result.get("score", 0),
                                       json.dumps(result), user_id=uid)
                yield {
                    "event": "test_dimension_done",
                    "data": json.dumps({
                        "provider": provider, "model": model,
                        "dimension": dim, **result,
                    }),
                }

            composite = compute_composite_score(results, _BOTTLENECK_WEIGHTS)
            # scores 同时带 {dim}_detail（含 fail_reason/error），让前端 0 分 tooltip 实时可用，
            # 与 GET /test/results 的数据形状一致（无需重刷页面才看到失败原因）。
            scores = {d: r.get("score", 0) for d, r in results.items()}
            for d, r in results.items():
                scores[f"{d}_detail"] = r
            model_result = {
                "provider": provider,
                "model": model,
                "composite_score": composite,
                "scores": scores,
            }
            all_results.append(model_result)

            yield {
                "event": "test_model_done",
                "data": json.dumps(model_result),
            }

        yield {"event": "test_all_done", "data": json.dumps({"results": all_results})}

    return EventSourceResponse(_stream())


# ── GET /test/results ──


@router.get("/test/results")
async def get_test_results(user: dict = Depends(get_current_user)):
    """获取最近的综合测试结果。"""
    from bottleneck_hunter.llm_clients.role_registry import _BOTTLENECK_WEIGHTS
    from bottleneck_hunter.web.model_tester import compute_composite_score

    store = _get_store(user)
    uid = user.get("sub", "")
    results = store.get_test_results(user_id=uid)

    grouped: dict[str, dict] = {}
    for r in results:
        key = f"{r['provider']}:{r['model']}"
        if key not in grouped:
            grouped[key] = {
                "provider": r["provider"],
                "model": r["model"],
                "scores": {},
                "tested_at": r["tested_at"],
            }
        grouped[key]["scores"][r["test_type"]] = r["score"]
        raw = {}
        try:
            raw = json.loads(r.get("raw_result", "{}"))
        except Exception:
            pass
        grouped[key]["scores"][f"{r['test_type']}_detail"] = raw

    for entry in grouped.values():
        score_dict = {k: {"score": v} for k, v in entry["scores"].items()
                      if isinstance(v, (int, float))}
        entry["composite_score"] = compute_composite_score(score_dict, _BOTTLENECK_WEIGHTS)

    return {"results": list(grouped.values())}


