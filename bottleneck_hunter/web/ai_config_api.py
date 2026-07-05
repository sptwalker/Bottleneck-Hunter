"""统一 AI 配置 API — 挂载于 /api/ai-config

合并 API Key 管理 + 模型角色分配 + 综合测试 + 自动推荐。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

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
    list_roles,
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
                slots.append({
                    "slot_index": s["slot_index"],
                    "provider": s["provider"],
                    "model": s["model"],
                })
        else:
            env_val = os.environ.get(f"DC_MODEL_{role_def.key.upper()}", "").strip()
            if env_val and ":" in env_val:
                p, m = env_val.split(":", 1)
                slots.append({"slot_index": 0, "provider": p, "model": m})

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
    """统一 provider 列表：唯一真源为 custom_providers 表（原内置已迁入）。

    每个 provider 均可编辑/删除/测试，不再区分内置/自定义。
    include_unconfigured 参数保留仅为兼容旧调用签名，现无实际分支（表中即为已配置）。
    """
    providers = []
    if _auth_store is None:
        return providers
    try:
        rows = _auth_store.list_custom_providers()
    except Exception:
        return providers

    for cp in rows:
        if cp.get("is_active") == 0:
            continue
        pid = cp["provider_id"]
        providers.append({
            "id": pid,
            "name": cp.get("display_name") or pid,
            "configured": True,
            "is_builtin": False,
            "default_model": cp.get("default_model", "") or "",
            "base_url": cp.get("base_url", "") or "",
            "key_hint": cp.get("api_key_hint", "") or "",
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
                         user_id=uid)
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


# ── POST /test/connectivity ──


def _configured_provider_models() -> list[tuple[str, str]]:
    """列出所有已配置 provider 及其默认模型（统一真源 custom_providers 运行时缓存）。"""
    configured: list[tuple[str, str]] = []
    for cp_id in list_custom_provider_ids():
        cp_info = get_custom_provider(cp_id)
        cp_model = cp_info.get("default_model", "") if cp_info else ""
        configured.append((cp_id, cp_model or cp_id))
    return configured


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
async def test_comprehensive(request: Request, user: dict = Depends(get_current_user)):
    """SSE 综合能力测试。"""
    store = _get_store(user)
    uid = user.get("sub", "")

    configured = [(p, m) for p, m in _configured_provider_models() if m]

    async def _stream():
        from bottleneck_hunter.web.model_tester import (
            TEST_DIMENSIONS,
            compute_composite_score,
            run_comprehensive_test,
        )
        from bottleneck_hunter.llm_clients.role_registry import _BOTTLENECK_WEIGHTS

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
            model_result = {
                "provider": provider,
                "model": model,
                "composite_score": composite,
                "scores": {d: r.get("score", 0) for d, r in results.items()},
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
    from bottleneck_hunter.web.model_tester import compute_composite_score
    from bottleneck_hunter.llm_clients.role_registry import _BOTTLENECK_WEIGHTS

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


# ── POST /recommend ──


@router.post("/recommend")
async def generate_recommendations(user: dict = Depends(get_current_user)):
    """基于测试结果，为每个角色自动推荐最佳模型。"""
    store = _get_store(user)
    uid = user.get("sub", "")

    test_results = store.get_test_results(user_id=uid)
    if not test_results:
        raise HTTPException(400, "请先运行综合测试")

    model_scores: dict[str, dict[str, float]] = {}
    for r in test_results:
        key = f"{r['provider']}:{r['model']}"
        if key not in model_scores:
            model_scores[key] = {"_provider": r["provider"], "_model": r["model"]}
        model_scores[key][r["test_type"]] = r["score"]

    from bottleneck_hunter.web.model_tester import compute_composite_score

    # 跨角色 provider 负载：用于打破单一模型通吃（每个 provider 已被分配的角色数）
    provider_load: dict[str, int] = {}
    # 单模型角色的负载惩罚系数（按 0-10 分制，0.5 表示每多占一个槽扣 0.5 分）
    LOAD_PENALTY = 0.5
    # 投委会刻意种子（qwen/kimi/glm）的让步阈值：种子分差在此值内即优先采用，保留多样性设计
    DEFAULT_SEED_EPS = 1.5

    recommendations = []
    for role_def in ROLE_REGISTRY.values():
        weights = role_def.capability_weights
        if not weights:
            continue

        ranked = []
        for key, scores in model_scores.items():
            score_data = {k: {"score": v} for k, v in scores.items()
                          if isinstance(v, (int, float))}
            cs = compute_composite_score(score_data, weights)
            ranked.append((key, cs, scores["_provider"], scores["_model"]))
        ranked.sort(key=lambda x: x[1], reverse=True)

        if role_def.multi_model:
            used_providers: set[str] = set()
            slots_assigned = []
            for key, cs, provider, model in ranked:
                if len(slots_assigned) >= role_def.max_slots:
                    break
                if provider in used_providers and len(slots_assigned) < role_def.max_slots:
                    if len([s for s in slots_assigned if s["provider"] != provider]) > 0:
                        continue
                used_providers.add(provider)
                slots_assigned.append({
                    "slot_index": len(slots_assigned),
                    "provider": provider,
                    "model": model,
                    "composite_score": cs,
                })

            for slot in slots_assigned:
                provider_load[slot["provider"]] = provider_load.get(slot["provider"], 0) + 1
                store.save_recommendation(
                    role_key=role_def.key,
                    slot_index=slot["slot_index"],
                    provider=slot["provider"],
                    model=slot["model"],
                    composite_score=slot["composite_score"],
                    score_breakdown=json.dumps({d: model_scores.get(f"{slot['provider']}:{slot['model']}", {}).get(d, 0)
                                                for d in weights}),
                    reason=f"综合分 {slot['composite_score']:.1f}",
                    user_id=uid,
                )
                recommendations.append({
                    "role_key": role_def.key,
                    "role_label": role_def.label,
                    **slot,
                })
        else:
            if ranked:
                # 1) 跨角色负载均衡：综合分 - 该 provider 已占角色数 × 惩罚，打破单一模型通吃
                def _adjusted(item):
                    _k, _cs, _prov, _m = item
                    return _cs - LOAD_PENALTY * provider_load.get(_prov, 0)
                best_key, best_cs, best_provider, best_model = max(ranked, key=_adjusted)

                # 2) 尊重注册表「刻意分散」的种子默认（仅投委会 growth=qwen/value=kimi/contrarian=glm
                #    等非 deepseek 默认；deepseek 是通用兜底默认，不参与让步以免抵消负载均衡）
                seed = role_def.default_provider
                if seed and seed != "deepseek" and best_provider != seed:
                    for key, cs, provider, model in ranked:
                        if provider == seed and (best_cs - cs) < DEFAULT_SEED_EPS:
                            best_key, best_cs, best_provider, best_model = key, cs, provider, model
                            break

                provider_load[best_provider] = provider_load.get(best_provider, 0) + 1
                store.save_recommendation(
                    role_key=role_def.key,
                    slot_index=0,
                    provider=best_provider,
                    model=best_model,
                    composite_score=best_cs,
                    score_breakdown=json.dumps({d: model_scores.get(best_key, {}).get(d, 0)
                                                for d in weights}),
                    reason=f"综合分 {best_cs:.1f}",
                    user_id=uid,
                )
                recommendations.append({
                    "role_key": role_def.key,
                    "role_label": role_def.label,
                    "slot_index": 0,
                    "provider": best_provider,
                    "model": best_model,
                    "composite_score": best_cs,
                })

    return {"recommendations": recommendations}


# ── POST /recommend/apply ──


@router.post("/recommend/apply")
async def apply_recommendations(user: dict = Depends(get_current_user)):
    """将推荐结果一键应用到角色配置。"""
    store = _get_store(user)
    uid = user.get("sub", "")

    recs = store.get_recommendations(user_id=uid)
    if not recs:
        raise HTTPException(400, "没有可用的推荐结果")

    applied = 0
    for rec in recs:
        role_def = ROLE_REGISTRY.get(rec["role_key"])
        if not role_def:
            continue

        store.upsert_role_config(
            role_key=rec["role_key"],
            slot_index=rec["slot_index"],
            provider=rec["recommended_provider"],
            model=rec["recommended_model"],
            role_label=role_def.label,
            role_group=role_def.group,
            user_id=uid,
        )

        applied += 1

    return {"applied": applied}


# ── GET /recommendations ──


@router.get("/recommendations")
async def get_recommendations(user: dict = Depends(get_current_user)):
    """获取当前推荐结果。"""
    store = _get_store(user)
    uid = user.get("sub", "")
    recs = store.get_recommendations(user_id=uid)

    result = []
    for rec in recs:
        role_def = ROLE_REGISTRY.get(rec["role_key"])
        result.append({
            "role_key": rec["role_key"],
            "role_label": role_def.label if role_def else rec["role_key"],
            "slot_index": rec["slot_index"],
            "provider": rec["recommended_provider"],
            "model": rec["recommended_model"],
            "composite_score": rec["composite_score"],
            "reason": rec["reason"],
        })

    return {"recommendations": result}
