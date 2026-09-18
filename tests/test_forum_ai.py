"""F5 · 论坛 AI 发帖引擎测试（stub model，不真调 provider）。

覆盖：ai_enabled=0 空转 / 开启后落库+计数 / 全板配额上限 / 内容闸不计配额 /
禁言角色排除 / 回复既有帖 / PASS 不发言 / 决策协议解析。全部显式 tmp db_path
（见 [[project-watchlist-db-path-not-env]]），sub 用真实用户名 alice（forum 无全局板）。
stub 通过 monkeypatch factory.get_models_for_role 注入：返回纯正文 → 原创帖、
返回「[#帖号] …」→ 回复该帖、返回「PASS」→ 不发言；固定洗牌以去角色顺序的非确定性。
"""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from langchain_core.messages import AIMessage

from bottleneck_hunter.llm_clients import factory
from bottleneck_hunter.watchlist import forum_ai
from bottleneck_hunter.watchlist.forum_identity import FORUM_ROLE_KEYS
from bottleneck_hunter.watchlist.store import WatchlistStore

_OK_BODY = "我觉得这只票的现金流被低估了，估值有修复空间，值得再看一眼。"


class _StubLLM:
    def __init__(self, text):
        self._text = text

    async def ainvoke(self, messages, **kwargs):  # 与 FallbackChatModel.ainvoke 同签名（消息列表）
        return AIMessage(content=self._text)


def _patch_model(monkeypatch, text=_OK_BODY):
    monkeypatch.setattr(factory, "get_models_for_role",
                        lambda role_key, **kw: [(_StubLLM(text), "stub", "stub-model")])


def _fix_order(monkeypatch):
    monkeypatch.setattr(forum_ai.random, "shuffle", lambda seq: None)  # 固定角色顺序，确定性


@pytest.fixture
def bound(tmp_path):
    return WatchlistStore(tmp_path / "forum.db").for_user("alice")


def test_disabled_by_default_returns_zero(bound, monkeypatch):
    _patch_model(monkeypatch)
    _fix_order(monkeypatch)
    r = asyncio.run(forum_ai.run_forum_ai_round(bound, "alice"))
    assert r == {"posts": 0, "replies": 0}
    assert bound.list_forum_posts() == []  # 默认 ai_enabled=0：空转不落库


def test_posts_and_counts_when_enabled(bound, monkeypatch):
    _patch_model(monkeypatch)  # 纯正文 stub → 原创帖
    _fix_order(monkeypatch)
    bound.set_forum_settings(ai_enabled=True)
    r = asyncio.run(forum_ai.run_forum_ai_round(bound, "alice", max_posts=2))
    assert r == {"posts": 2, "replies": 0}  # 无 [#号] → 只发帖
    posts = bound.list_forum_posts()
    assert len(posts) == 2
    assert all(p["author_type"] == "ai" and p["author_role_key"] in FORUM_ROLE_KEYS for p in posts)
    assert sum(bound.get_forum_daily_count(rk) for rk in FORUM_ROLE_KEYS) == 2
    assert bound.get_forum_board_daily_total() == 2


def test_board_daily_cap_limits_round(bound, monkeypatch):
    _patch_model(monkeypatch)
    _fix_order(monkeypatch)
    bound.set_forum_settings(ai_enabled=True, daily_cap=1)
    r = asyncio.run(forum_ai.run_forum_ai_round(bound, "alice", max_posts=5))
    assert r == {"posts": 1, "replies": 0}  # budget=min(cap-已发, 上限)=1
    assert bound.get_forum_board_daily_total() == 1


def test_content_gate_skips_without_counting(bound, monkeypatch):
    _patch_model(monkeypatch, text="你就是个傻子")  # 命中人身攻击正则
    _fix_order(monkeypatch)
    bound.set_forum_settings(ai_enabled=True)
    r = asyncio.run(forum_ai.run_forum_ai_round(bound, "alice", max_posts=3))
    assert r == {"posts": 0, "replies": 0}
    assert bound.list_forum_posts() == []
    assert bound.get_forum_board_daily_total() == 0  # 违规不计配额


def test_pass_produces_no_post(bound, monkeypatch):
    _patch_model(monkeypatch, text="PASS")  # 协议：只输出 PASS = 这一步不发言
    _fix_order(monkeypatch)
    bound.set_forum_settings(ai_enabled=True)
    r = asyncio.run(forum_ai.run_forum_ai_round(bound, "alice", max_posts=3))
    assert r == {"posts": 0, "replies": 0}
    assert bound.list_forum_posts() == []
    assert bound.get_forum_board_daily_total() == 0  # PASS 不烧配额


def test_banned_roles_excluded(bound, monkeypatch):
    _patch_model(monkeypatch)
    _fix_order(monkeypatch)
    bound.set_forum_settings(ai_enabled=True)
    for rk in FORUM_ROLE_KEYS:
        bound.set_forum_role_banned(rk, True)
    r = asyncio.run(forum_ai.run_forum_ai_round(bound, "alice", max_posts=3))
    assert r == {"posts": 0, "replies": 0}  # 全禁言 → 无候选


def test_replies_to_existing_post(bound, monkeypatch):
    bound.set_forum_settings(ai_enabled=True)
    pid = bound.create_forum_post("user", "我看多这只票，逻辑是产能瓶颈缓解。")
    # 协议：正文开头 [#帖号] = 回复该帖。stub 用 [#pid] 前缀强制角色回复板主这条帖。
    _patch_model(monkeypatch, text=f"[#{pid}] 补充一点，这块业务的护城河其实还在，别急着看空。")
    _fix_order(monkeypatch)
    r = asyncio.run(forum_ai.run_forum_ai_round(bound, "alice", max_posts=1))
    assert r == {"posts": 0, "replies": 1}  # [#号] → 回帖，不新增帖
    replies = bound.list_forum_replies(pid)
    assert len(replies) == 1 and replies[0]["author_type"] == "ai"
    assert "护城河" in replies[0]["body"] and "#" not in replies[0]["body"]  # [#号] 标记已剥离
    posts = bound.list_forum_posts()
    assert len(posts) == 1 and posts[0]["reply_count"] == 1  # 未新增帖，且该帖 reply_count 计到 1


def test_parse_decision_variants():
    """决策协议解析器：PASS / [#号] / 非法号退化 / 纯原创，四路都对（新逻辑唯一门禁）。"""
    targets = {7: {"id": 7}}
    assert forum_ai._parse_decision("PASS", targets) == ("", None)
    assert forum_ai._parse_decision("  pass. ", targets) == ("", None)  # 容忍大小写/尾标点
    body, tgt = forum_ai._parse_decision("[#7] 我同意这点", targets)
    assert tgt == {"id": 7} and body == "我同意这点"
    body, tgt = forum_ai._parse_decision("#7 也行", targets)  # 容忍无方括号
    assert tgt == {"id": 7} and body == "也行"
    body, tgt = forum_ai._parse_decision("[#99] 引用了不存在的号", targets)
    assert tgt is None and body == "引用了不存在的号"  # 号非法 → 退化为原创帖
    body, tgt = forum_ai._parse_decision("这是一条原创观点", targets)
    assert tgt is None and body == "这是一条原创观点"


# ── P2：即时触发 / @提及 / 关注自己帖的回帖 ─────────────────────────────
def test_resolve_mentions(bound):
    """@提及解析：role_key 直呼与昵称都命中、同角色去重、未知@与板主忽略（#7 触发前置）。"""
    rk = FORUM_ROLE_KEYS[0]
    name = forum_ai.get_identity(bound, "alice", rk).display_name
    hits = forum_ai.resolve_mentions(bound, "alice", f"@{rk} 你怎么看 @{name} 再说下 @查无此人 @板主")
    assert hits == [rk]  # 两种写法去重为一；未匹配的 @token 自然落空
    assert forum_ai.resolve_mentions(bound, "alice", "没有艾特任何人") == []


def test_trigger_targets_only_mentioned_role(bound, monkeypatch):
    """被 @ 点名 → 定向轮只让该角色发言，其余角色不被卷入（#5 即时触发 + #7 点名）。"""
    bound.set_forum_settings(ai_enabled=True)
    pid = bound.create_forum_post("user", "大家看看这只票的逻辑，有没有人接一下。")
    target_rk = FORUM_ROLE_KEYS[1]
    _patch_model(monkeypatch, text=_OK_BODY)  # 纯正文 → 原创帖（此处只验角色定向）
    _fix_order(monkeypatch)
    trig = {"post_id": pid, "at_roles": (target_rk,), "reply_excerpt": "@某人 你怎么看"}
    r = asyncio.run(forum_ai.run_forum_ai_round(bound, "alice", max_posts=3, trigger=trig))
    assert r["posts"] + r["replies"] == 1  # 定向轮只放一位（被点名者）
    assert len(bound.list_forum_posts(role_key=target_rk)) == 1  # 唯一发言者正是被点名角色


def test_trigger_author_replies_into_own_thread(bound, monkeypatch):
    """用户在某 AI 帖下回帖、未点名 → 原作者被顶到评论区回自己帖（#3 特别关注自己帖的回帖）。"""
    bound.set_forum_settings(ai_enabled=True)
    author_rk = FORUM_ROLE_KEYS[2]
    pid = bound.create_forum_post("ai", "我发个原创观点：这只票产能瓶颈缓解，看多。",
                                  author_role_key=author_rk)
    _patch_model(monkeypatch, text=f"[#{pid}] 谢谢关注，我再补一句：需求端也在回暖。")
    _fix_order(monkeypatch)
    trig = {"post_id": pid, "at_roles": (), "reply_excerpt": "楼主这个逻辑靠谱吗"}
    r = asyncio.run(forum_ai.run_forum_ai_round(bound, "alice", max_posts=3, trigger=trig))
    assert r == {"posts": 0, "replies": 1}  # 原作者回到自己帖的评论区（非触发态本会被 targets 排除）
    replies = bound.list_forum_replies(pid)
    assert len(replies) == 1 and replies[0]["author_role_key"] == author_rk
    assert "#" not in replies[0]["body"]  # [#号] 标记已剥离


# ── P3：长期记忆（自述备忘 #6）/ 召集其他角色讨论（#8）────────────────────
def test_distill_writes_memory_when_stale(bound, monkeypatch):
    """调度轮 distill=True：为「有发言但备忘陈旧」的角色蒸馏出第一人称长期立场并落库（#6）。"""
    bound.set_forum_settings(ai_enabled=True)
    rk = FORUM_ROLE_KEYS[0]
    bound.create_forum_post("ai", "我长期看好高端制造里的国产替代主线。", author_role_key=rk)  # 蒸馏语料
    _patch_model(monkeypatch, text="我偏好被低估的现金牛，长期看多高端制造的国产替代。")
    _fix_order(monkeypatch)
    asyncio.run(forum_ai.run_forum_ai_round(bound, "alice", max_posts=1, distill=True))
    mem = bound.get_forum_memory(rk)
    assert mem and mem["stance"] and "国产替代" in mem["stance"]  # 首位陈旧角色被蒸馏落库


def test_no_distill_on_triggered_round(bound, monkeypatch):
    """触发轮（trigger 非空、未传 distill）不蒸馏：省即时开销，记忆只在调度轮更新（#6）。"""
    bound.set_forum_settings(ai_enabled=True)
    rk = FORUM_ROLE_KEYS[0]
    pid = bound.create_forum_post("user", "大家看看这只票。")
    _patch_model(monkeypatch, text=f"[#{pid}] 我觉得这逻辑还行。")
    _fix_order(monkeypatch)
    trig = {"post_id": pid, "at_roles": (rk,), "reply_excerpt": "@某人 你怎么看"}
    asyncio.run(forum_ai.run_forum_ai_round(bound, "alice", max_posts=3, trigger=trig))
    assert bound.list_forum_memories() == {}  # 触发轮不写记忆


def test_convene_invites_other_roles(bound, monkeypatch):
    """某角色发「召集：议题」原创帖 → 系统定向请其他尚未发言的角色来回帖参与（#8）。"""
    bound.set_forum_settings(ai_enabled=True)
    calls = {"n": 0}
    convene_body = "召集：大家怎么看半导体设备的国产化拐点？"

    class _SeqLLM:
        async def ainvoke(self, messages, **kwargs):
            calls["n"] += 1  # 第 1 次＝召集帖，其后＝被邀角色回帖
            text = convene_body if calls["n"] == 1 else "[#1] 我认为拐点临近，设备订单在回暖。"
            return AIMessage(content=text)

    monkeypatch.setattr(factory, "get_models_for_role",
                        lambda role_key, **kw: [(_SeqLLM(), "stub", "stub-model")])
    _fix_order(monkeypatch)
    r = asyncio.run(forum_ai.run_forum_ai_round(bound, "alice", max_posts=1))
    assert r["posts"] == 1                 # 召集帖本身（顶层轮 budget=1）
    assert r["replies"] >= 1               # 至少一位被邀角色回帖参与（扇出受当日剩余配额约束）
    posts = bound.list_forum_posts()
    assert len(posts) == 1 and posts[0]["body"].startswith("召集")
    assert posts[0]["reply_count"] >= 1    # 召集帖下已聚起讨论


def test_convene_not_triggered_on_directed_round(bound, monkeypatch):
    """触发轮里角色即便写「召集：…」也不扇出：触发轮本身就是扇出，防二次召集/递归（#8）。"""
    bound.set_forum_settings(ai_enabled=True)
    rk = FORUM_ROLE_KEYS[1]
    seed = bound.create_forum_post("user", "问一句。")
    _patch_model(monkeypatch, text="召集：大家来聊聊这个板块。")  # 定向轮下应仅当普通原创帖
    _fix_order(monkeypatch)
    trig = {"post_id": seed, "at_roles": (rk,), "reply_excerpt": "@某人 你怎么看"}
    r = asyncio.run(forum_ai.run_forum_ai_round(bound, "alice", max_posts=3, trigger=trig))
    assert r["posts"] + r["replies"] == 1  # 只有被点名者发了一帖，无召集扇出


# ---------- 话题多样化：A 反重复 / B 双市场采样 / C 催化剂驱动 ----------


def test_saturated_tickers_flags_repeated_latin(bound):
    """A：同一 latin ticker 被刷够 _SATURATION_MIN 次 → 判定为已被反复讨论。"""
    bound.add({"ticker": "NVDA", "company_name": "Nvidia", "market": "US", "tier": "track"})
    for _ in range(forum_ai._SATURATION_MIN):
        bound.create_forum_post("ai", "NVDA 又新高了，估值真的贵。", author_role_key="committee_value")
    sat = forum_ai._saturated_tickers(bound, bound.list_all())
    assert sat.get("NVDA", 0) >= forum_ai._SATURATION_MIN


def test_saturated_tickers_matches_cjk_name(bound):
    """A：帖子只提中文名（不写代码）也能命中该标的（CJK 子串匹配）。"""
    bound.add({"ticker": "600519", "company_name": "Kweichow Moutai",
               "company_name_cn": "贵州茅台", "market": "A", "tier": "track"})
    for _ in range(forum_ai._SATURATION_MIN):
        bound.create_forum_post("ai", "贵州茅台的护城河还在吗？", author_role_key="committee_value")
    sat = forum_ai._saturated_tickers(bound, bound.list_all())
    assert "600519.SS" in sat


def test_saturated_tickers_ignores_sparse(bound):
    """A：提及不足阈值不算饱和，别误伤正常讨论。"""
    bound.add({"ticker": "NVDA", "company_name": "Nvidia", "market": "US", "tier": "track"})
    bound.create_forum_post("ai", "NVDA 看一眼。", author_role_key="committee_value")
    assert forum_ai._saturated_tickers(bound, bound.list_all()) == {}


def test_board_context_balances_both_markets(bound, monkeypatch):
    """B：即便美股分数全面更高，A 股仍应在观察池露出（双市场交错，不再只取 top 高分那批）。"""
    monkeypatch.setattr(forum_ai, "_ticker_background", lambda *a, **k: "")
    monkeypatch.setattr(forum_ai, "_CTX_TAGS", 2)  # 收窄展示位，逼出「若按分数截断 A 股会被挤掉」
    bound.add({"ticker": "NVDA", "company_name": "Nvidia", "market": "US", "tier": "track", "composite_score": 99})
    bound.add({"ticker": "AAPL", "company_name": "Apple", "market": "US", "tier": "track", "composite_score": 98})
    bound.add({"ticker": "MSFT", "company_name": "Microsoft", "market": "US", "tier": "track", "composite_score": 97})
    bound.add({"ticker": "600519", "company_name": "Moutai", "company_name_cn": "贵州茅台",
               "market": "A", "tier": "track", "composite_score": 50})
    ctx = forum_ai._board_context(bound)
    assert "板主观察池" in ctx
    assert "600519.SS" in ctx  # 分数最低的 A 股靠交错采样进入，证明市场平衡
    assert "NVDA" in ctx


def test_board_context_injects_catalysts(bound, monkeypatch):
    """C：近期催化剂注入背景块，供 AI 追新话题。"""
    monkeypatch.setattr(forum_ai, "_ticker_background", lambda *a, **k: "")
    eid = bound.add({"ticker": "TSLA", "company_name": "Tesla", "market": "US", "tier": "track"})
    future = (datetime.now(timezone.utc) + timedelta(days=3)).strftime("%Y-%m-%d")
    bound.create_catalyst(eid, "TSLA", "产销数据发布", expected_date=future, impact_level="high")
    ctx = forum_ai._board_context(bound)
    assert "近期催化剂/事件" in ctx
    assert "TSLA" in ctx and "产销数据发布" in ctx


def test_board_context_shows_saturation_hint(bound, monkeypatch):
    """A+背景：饱和标的显式点名提示，配合决策规则让 AI 换角度。"""
    monkeypatch.setattr(forum_ai, "_ticker_background", lambda *a, **k: "")
    bound.add({"ticker": "NVDA", "company_name": "Nvidia", "market": "US", "tier": "track"})
    for _ in range(forum_ai._SATURATION_MIN):
        bound.create_forum_post("ai", "NVDA 估值太高了。", author_role_key="committee_value")
    ctx = forum_ai._board_context(bound)
    assert "换个角度或换只票" in ctx and "NVDA" in ctx


def test_get_recent_catalysts_cross_market(bound):
    """C：论坛店 _market='' → get_recent_catalysts 跨市场取到 A 股催化剂。"""
    eid = bound.add({"ticker": "600519", "company_name_cn": "贵州茅台", "market": "A", "tier": "track"})
    future = (datetime.now(timezone.utc) + timedelta(days=5)).strftime("%Y-%m-%d")
    bound.create_catalyst(eid, "600519.SS", "分红除权", expected_date=future)
    cats = bound.get_recent_catalysts()
    assert any(c["ticker"] == "600519.SS" for c in cats)
