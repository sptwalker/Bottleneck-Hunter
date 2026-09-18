"""互动留言系统 · AI 自主发帖引擎（F5）。

opt-in（forum_settings.ai_enabled）默认关；开启后由 scheduler 低频 job、/forum/ai/run 手动触发，
或用户回帖/点名即时触发（P2）。全程在 set_current_user(板主) 上下文内（scheduler 进入 / API 靠
for_user 绑定，create_task 复制上下文）：读数据只经 for_user(板主)、发言用板主自己的 Key 与预算——
绝无全局 Key（见 [[project_strict_key_isolation]]）。

发言决策走「观察→决策→行动」（P1）：先给角色拼一份留言区摘要（近 N 帖 + 回帖数 + 自己发过什么），
再让它按轻量协议自主选择——[#帖号]=回复该帖 / 直接正文=原创帖 / PASS=这一步不发言（不烧配额）。
取代旧版 50% 抛硬币强定发帖/回帖。单次 LLM 调用，成本与旧版持平；PASS 另设尝试上限防空转。

触发态（P2 · trigger 非空）：用户回帖或 @点名时即时开一轮——被 @ 的角色、被回复帖的原作者优先
发言，其摘要顶部高亮「这条冲你来的」，并放行回到该帖（含自己帖）的评论区；无定向对象则随机一位
回应、单帖封顶。resolve_mentions 供 API 解析正文里 @ 了哪些入驻角色。

长期记忆（P3·#6）：每板每角色一行第一人称「立场备忘」，注入摘要让角色记得自己与同侪的长期观点；
仅调度轮（distill=True）每轮至多蒸馏一位陈旧角色（>24h），8 角色天然 ≤8 次/日、无需计数表。
召集（P3·#8）：仅顶层轮，角色发原创帖以「召集：<议题>」开头即触发扇出，用 P2 触发机制定向请
另外几位角色来聊（受当日剩余配额约束、被邀角色仍自主可 PASS、不递归）。

落库前三闸（去重 → 内容 → 配额，见 forum_moderation）任一不过则跳过且不计配额。
LLM 调用：get_models_for_role 返回的实例已 record-only 包装（喂熔断层），直接 ainvoke——
绝不再套 wrap_record_only，也绝不加 asyncio.wait_for（FallbackChatModel 内部已 wait_for，
外包=自毁，见 [[project-llm-call-layer-unified]]）。
"""

from __future__ import annotations

import logging
import random
import re
from collections import namedtuple
from datetime import datetime, timedelta, timezone

from bottleneck_hunter.watchlist.forum_identity import (
    FOCUS_PROFILES,
    FORUM_ROLE_KEYS,
    get_identity,
    selectable_role_keys,
)
from bottleneck_hunter.watchlist.forum_moderation import (
    check_content,
    check_quota,
    content_hash,
    is_duplicate,
)

logger = logging.getLogger(__name__)

_ROLE_DAILY_CAP = 20      # 每角色每日硬护栏（与 forum_moderation 一致）
_DEFAULT_ROUND_POSTS = 3  # 单轮默认上限（低频控成本）；scheduler 传 3，手动触发不传时同值
_CTX_CAP = 1600           # 背景数据注入上限（字符）
_DIGEST_POSTS = 12        # 摘要展示的近期帖数（观察面比旧版「只看 1 条」宽）
_OWN_RECENT = 3           # 额外回顾自己最近发过的帖（#1 记得自己发过什么）
_DIGEST_CAP = 1600        # 摘要注入上限（字符），与 _CTX_CAP 同量级
_ATTEMPT_SLACK = 2        # ponytail: PASS 不占 budget，允许比 budget 多试几次；再多即收手防空转烧钱
_MEMORY_TTL_H = 24        # 立场备忘陈旧阈值（小时）：超期才重蒸馏，8 角色天然 ≤8 次/日、无需计数表
_MEMORY_DISTILL_POSTS = 8  # 蒸馏时回看该角色近期几条自述
_MEMORY_CAP = 200         # 单条立场备忘字数上限（注入与落库都截断）
_PEER_STANCE_CAP = 40     # 注入同侪立场时每条截断（省 token）
_CONVENE_INVITES = 3      # 一次召集最多请几位其他角色（另受当日剩余配额约束）
_CTX_TAGS = 12            # 观察池标签展示数（B：双市场交错取样，不再只露 top-12 高分那批）
_SATURATION_POSTS = 30    # A：算话题饱和度回看的近期帖数
_SATURATION_MIN = 3       # A：某标的被提及≥该次数即视为「已被反复讨论」
_SATURATION_TOP = 6       # A：饱和提示只列最高频的前几只，别刷屏
_LENS_SNAPSHOT_K = 15     # D：contrarian/uzi/risk 只对前 K 只(按 composite_score)查快照，控成本（调参旋钮）

# D 方案：一轮算一次的共享基座 + 逐角色镜片所需的原料，避免 8×list_all/8×饱和扫描。
_BoardCtx = namedtuple("_BoardCtx", ("base", "entries", "saturated", "cat_map"))

# 轻量决策协议：让角色看完摘要后自主选择，而非旧版抛硬币强定发帖/回帖。
_DECIDE_RULES = (
    "\n\n请以你自己的身份和视角，决定这一步怎么参与：\n"
    "· 想回应上面某条帖：正文最开头写 [#帖号]（例：[#12]），再写你的回复，120 字内；\n"
    "· 有新的原创观点想发：先写一行「标题：<一句话概括，20 字内>」，换行后再写正文，"
    "围绕市场或某只标的，150 字内；\n"
    "· 此刻没什么特别想说的：只输出 PASS 四个字母。\n"
    "· 尽量追新：优先聊还没被反复讨论的标的、新角度，或背景里刚冒出来的催化剂/事件；"
    "若只是把已经聊烂的话题再重复一遍，宁可 PASS。"
)
_CONVENE_RULE = (
    "\n· 想召集大家一起讨论某个议题：发原创帖，正文最开头写「召集：<一句话议题>」，"
    "系统会请另外几位分析师来聊这个议题（每天有限额，别滥用）。"
)
_DECIDE_TAIL = "\n对观点不对人、口语化，别硬凑。回帖或 PASS 时不要写标题；不要署名、不要 markdown。"

_PASS_RE = re.compile(r"^\s*pass[\s.。!！]*$", re.IGNORECASE)   # 纯 PASS（容忍尾随标点/空白）
_REF_RE = re.compile(r"^\s*\[?\s*#\s*(\d+)\s*\]?\s*")           # [#12] / #12 / [12] 皆容忍
_MENTION_RE = re.compile(r"@(\w{1,20})")  # @昵称 / @role_key（\w 默认含中日韩，非匹配 token 后续自然落空）
_CONVENE_RE = re.compile(r"^\s*召集[:：]\s*(.+)", re.S)  # 原创帖以「召集：议题」开头 → 触发扇出（P3·#8）
_TITLE_RE = re.compile(r"^\s*标题[:：]\s*([^\n]+)\n(.*)", re.S)  # 原创帖首行「标题：…」→ 拆出标题与正文


def _split_title(text):
    """原创帖拆标题：命中首行「标题：…」→ (标题<=60字, 正文)；无标题行 → ("", 原文)。"""
    m = _TITLE_RE.match(text)
    if not m:
        return "", text.strip()
    return m.group(1).strip()[:60], m.group(2).strip()


async def run_forum_ai_round(store, user_id, *, max_posts=None, trigger=None, distill=False) -> dict:
    """跑一轮 AI 自主发言，返回 {"posts": 新帖数, "replies": 回帖数}。opt-in 门禁不过返回全 0。

    trigger 非空＝由用户回帖/点名即时触发（P2）：被 @ 的角色与被回复帖的原作者优先发言，
    其摘要顶部高亮「这条冲你来的」；无定向对象则随机一位回应、单帖封顶。普通调度轮 trigger=None，
    选角随机、并放行「召集」（#8）与记忆蒸馏（#6，distill=True 时）。
    """
    bound = store.for_user(user_id)

    # 1) 前置门禁：opt-in 总开关 + 全板当日剩余配额
    settings = bound.get_forum_settings()
    if not settings.get("ai_enabled"):
        return {"posts": 0, "replies": 0}
    daily_cap = int(settings.get("daily_cap", 20) or 20)
    board_today = bound.get_forum_board_daily_total()
    upper = _DEFAULT_ROUND_POSTS if max_posts is None else int(max_posts)
    budget = min(daily_cap - board_today, upper)
    if budget <= 0:
        return {"posts": 0, "replies": 0}

    # 2) 选角色：未禁言且今日 <20
    candidates = [rk for rk in selectable_role_keys(store, user_id)
                  if bound.get_forum_daily_count(rk) < _ROLE_DAILY_CAP]
    if not candidates:
        return {"posts": 0, "replies": 0}
    if trigger:
        priority = _priority_roles(bound, trigger, candidates)
        if priority:
            candidates = priority          # 定向轮：只让被点名/被回复的角色决定是否回应
        else:
            random.shuffle(candidates)
            budget = min(budget, 1)        # 回自己帖且没点名：只放一位随机角色，别热闹过头
    else:
        random.shuffle(candidates)         # 随机近似「最久未发言」，避免同角色刷屏

    # 3) 背景数据 + 角色名映射 + 长期记忆（best-effort，只经 for_user(板主)）
    board = _board_context_base(bound)  # D：共享基座一轮算一次；逐角色镜片在 _generate 里套
    names = _name_map(store, user_id)
    try:
        memories = bound.list_forum_memories()  # role_key → {stance, updated_at}：注入自述 + 同侪立场
    except Exception:  # noqa: BLE001
        memories = {}
    can_convene = trigger is None  # 召集仅顶层轮发起：触发轮本身就是扇出，不再二次召集（防递归）

    # 4) 逐角色 观察→决策→行动；PASS 不占 budget 却仍烧一次调用，故另设尝试上限防空转
    posts = replies = 0
    acted: set[str] = set()
    convene: tuple[int, str] | None = None
    attempt_cap = budget + _ATTEMPT_SLACK
    for attempts, role_key in enumerate(candidates):
        if posts + replies >= budget or attempts >= attempt_cap:
            break
        kind, pid, body = await _act_once(store, bound, user_id, role_key,
                                          board, names, memories, trigger, can_convene)
        if kind == "reply":
            replies += 1
            acted.add(role_key)
        elif kind == "post":
            posts += 1
            acted.add(role_key)
            if can_convene and convene is None:  # 每轮至多认一次召集，控扇出成本
                m = _CONVENE_RE.match(body)
                if m:
                    convene = (pid, _oneline(m.group(1))[:120])

    # 5) 召集扇出（#8）：把议题定向请给尚未发言的其他角色（受当日剩余配额约束、不递归）
    if convene:
        cp, cr = await _run_convene(store, bound, user_id, convene, candidates, acted,
                                    board, names, memories)
        posts += cp
        replies += cr

    # 6) 记忆蒸馏（#6）：仅调度轮，每轮至多为一位陈旧角色更新自述备忘（单次 LLM 调用）
    if distill:
        await _maybe_distill_memory(store, bound, user_id, memories)

    return {"posts": posts, "replies": replies}


async def _act_once(store, bound, user_id, role_key, board, names, memories, trigger, can_convene):
    """一个角色走完 观察→决策→行动：生成 → 三闸（去重/内容/配额）→ 落库 → 计数 → SSE。

    返回 (kind, pid, body)：kind ∈ {"post","reply",None}；None＝PASS/空/被闸拦（不发言、不烧配额）。
    body 供上层识别「召集：」以触发扇出。单次 LLM 调用，成本与旧版持平。
    """
    try:
        body, target = await _generate(store, bound, user_id, role_key,
                                       board, names, memories, trigger, can_convene)
    except Exception as e:  # noqa: BLE001 — 单角色生成失败不拖垮整轮
        logger.warning("Forum AI 生成失败 (role=%s): %s", role_key, e)
        return None, None, ""
    if not body:  # PASS 或空输出：不发言、不烧配额
        return None, None, ""
    title = ""
    if target is None:  # 原创帖才拆标题；回帖/召集帖不取（召集「召集：」开头不命中 _TITLE_RE）
        title, body = _split_title(body)
        if not body:  # 只有标题没正文 → 无效，不发
            return None, None, ""
    # 落库前三闸：任一不过跳过且不计配额（去重/违规/超额不该烧额度）
    if is_duplicate(store, user_id, role_key, body):
        return None, None, ""
    ok, _ = check_content(body)
    if not ok:
        return None, None, ""
    ok, _ = check_quota(store, user_id, role_key)
    if not ok:
        return None, None, ""
    h = content_hash(body)
    if target is not None:
        rid = bound.create_forum_reply(target["id"], "ai", body,
                                       author_role_key=role_key, content_hash=h)
        _publish(user_id, "reply_created", post_id=target["id"], reply_id=rid)
        bound.incr_forum_daily_count(role_key)
        return "reply", int(target["id"]), body
    pid = bound.create_forum_post("ai", body, author_role_key=role_key, title=title, content_hash=h)
    _publish(user_id, "post_created", post=bound.get_forum_post(pid))
    bound.incr_forum_daily_count(role_key)
    return "post", int(pid), body


async def _run_convene(store, bound, user_id, convene, candidates, acted,
                       board, names, memories) -> tuple[int, int]:
    """把召集议题定向请给尚未发言的其他角色（每人一次自主决策，可 PASS）。返回 (新增帖, 新增回帖)。

    仅顶层轮触发；被邀角色 can_convene=False，不会再次召集（无递归）。名额 = min(_CONVENE_INVITES,
    当日剩余配额)，故整轮发言量仍以板级 daily_cap 封顶。
    """
    pid, topic = convene
    daily_cap = int(bound.get_forum_settings().get("daily_cap", 20) or 20)
    remaining = daily_cap - bound.get_forum_board_daily_total()
    max_invites = min(_CONVENE_INVITES, remaining)
    if max_invites <= 0:
        return 0, 0
    invitees = [rk for rk in candidates if rk not in acted][:max_invites]
    conv_trigger = {"post_id": pid, "at_roles": (), "reply_excerpt": topic, "convene": True}
    cp = cr = 0
    for rk in invitees:
        kind, _, _ = await _act_once(store, bound, user_id, rk, board, names, memories,
                                     conv_trigger, can_convene=False)
        if kind == "reply":
            cr += 1
        elif kind == "post":
            cp += 1
    return cp, cr


async def _maybe_distill_memory(store, bound, user_id, memories) -> bool:
    """每轮至多为一位「备忘陈旧（>24h 或未建）且近期有发言」的角色蒸馏第一人称长期立场（#6）。

    节流靠 updated_at 陈旧判定：8 角色天然 ≤8 次/日、无需计数表；每轮至多一次 LLM 调用控成本。
    仅调度轮（distill=True）调用；触发轮不蒸馏（省即时开销）。
    """
    for role_key in FORUM_ROLE_KEYS:
        if not _memory_stale(memories.get(role_key)):
            continue
        try:
            own = bound.list_forum_posts(role_key=role_key, limit=_MEMORY_DISTILL_POSTS)
        except Exception:  # noqa: BLE001
            own = []
        if not own:
            continue  # 没发过帖 → 无可蒸馏，看下一位
        corpus = "；".join(_oneline(p.get("body"))[:120] for p in own)
        prev = (memories.get(role_key) or {}).get("stance") or ""
        stance = await _distill_stance(store, user_id, role_key, corpus, prev)
        if stance:
            bound.set_forum_memory(role_key, stance)
        return True  # 每轮至多一位（无论成败，已烧一次调用即收手）
    return False


async def _distill_stance(store, user_id, role_key, corpus, prev) -> str:
    """把角色近期自述提炼成一段第一人称长期立场备忘（≤_MEMORY_CAP 字）。单次 LLM 调用，temperature=0.3。"""
    from langchain_core.messages import HumanMessage, SystemMessage

    from bottleneck_hunter.llm_clients import factory

    models = factory.get_models_for_role(role_key, user_id=user_id, temperature=0.3)
    if not models:
        return ""
    identity = get_identity(store, user_id, role_key)
    prompt = (
        f"下面是你近期在留言区发过的观点，请提炼成一段第一人称『长期立场备忘』（{_MEMORY_CAP} 字以内，"
        "口语化，只写你稳定持有的看法/偏好/关注点，别复述某天的具体行情）：\n" + corpus
    )
    if prev:
        prompt += "\n\n（这是你之前记的备忘，可在此基础上修订，别推翻重来）：" + prev
    llm = models[0][0]  # 已 record-only 包装：直接 ainvoke，绝不再套 wait_for / wrap_record_only
    try:
        msg = await llm.ainvoke([
            SystemMessage(content=identity.system_preamble()),
            HumanMessage(content=prompt),
        ])
    except Exception as e:  # noqa: BLE001 — 蒸馏失败只记日志，下一轮仍会重试
        logger.warning("Forum AI 记忆蒸馏失败 (role=%s): %s", role_key, e)
        return ""
    return _oneline(getattr(msg, "content", "") or "")[:_MEMORY_CAP]


def _memory_stale(mem) -> bool:
    """备忘是否陈旧到该重蒸馏：未建行、无时间、或 updated_at 距今 >_MEMORY_TTL_H 小时。"""
    if not mem or not mem.get("updated_at"):
        return True
    try:
        ts = datetime.fromisoformat(mem["updated_at"])
    except (ValueError, TypeError):
        return True
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - ts) > timedelta(hours=_MEMORY_TTL_H)


def resolve_mentions(store, user_id, text) -> list[str]:
    """从正文解析被 @提及 的入驻角色 → role_key 列表（匹配昵称或 role_key，去重保序）。

    板主/无匹配的 @ 忽略。供 API 在用户回帖/发帖时判定要点名谁、即时触发一轮（P2 · #7）。
    """
    hits = _MENTION_RE.findall(text or "")
    if not hits:
        return []
    by_name = {v: k for k, v in _name_map(store, user_id).items()}  # 昵称 → role_key
    out: list[str] = []
    for h in hits:
        rk = by_name.get(h) or (h if h in FORUM_ROLE_KEYS else "")
        if rk and rk not in out:
            out.append(rk)
    return out


def _priority_roles(bound, trigger, candidates) -> list[str]:
    """触发轮里优先发言的角色：被 @ 的角色 + 被回复帖的原作者（若是 AI）。仅保留可发言候选，去重保序。"""
    want = list(trigger.get("at_roles") or ())
    pid = trigger.get("post_id")
    if pid:
        post = _safe_post(bound, pid)
        if post and post.get("author_type") == "ai":
            ark = post.get("author_role_key") or ""
            if ark:
                want.append(ark)
    seen, out = set(), []
    for rk in want:
        if rk in candidates and rk not in seen:
            seen.add(rk)
            out.append(rk)
    return out


async def _generate(store, bound, user_id, role_key, board, names,
                    memories=None, trigger=None, can_convene=False):
    """观察→决策→行动：拼角色专属摘要（含长期记忆注入），模型自主选择回帖 [#号] / 原创 / PASS。

    返回 (body, target_post|None)：body 空表示 PASS（不发言、不烧配额）；
    target 非空表示回复该帖，否则原创发帖。can_convene=True 时额外提供「召集」协议项。单次 LLM 调用。
    board 是 _BoardCtx（共享基座 + entries/饱和集/催化剂映射）：此处套逐角色镜片（D 方案软分工）。
    """
    from langchain_core.messages import HumanMessage, SystemMessage

    from bottleneck_hunter.llm_clients import factory

    identity = get_identity(store, user_id, role_key)
    deep_seg, focus_line = _role_lens(bound, role_key, board.entries, board.saturated, board.cat_map)
    context = board.base + ("\n" + deep_seg if deep_seg else "")
    digest, targets = _build_role_digest(bound, role_key, names, trigger, memories, focus_line)

    system = identity.system_preamble()
    if context:
        system += "\n\n【当前板主的市场背景，供你参考、不要照抄】\n" + context

    models = factory.get_models_for_role(role_key, user_id=user_id, temperature=0.7)
    if not models:
        return "", None
    llm = models[0][0]  # 已 record-only 包装：直接 ainvoke，绝不再套 wait_for / wrap_record_only
    msg = await llm.ainvoke([
        SystemMessage(content=system),
        HumanMessage(content=_decide_instruction(digest, can_convene)),
    ])
    return _parse_decision(getattr(msg, "content", "") or "", targets)


def _decide_instruction(digest: str, can_convene: bool = False) -> str:
    body = digest.strip() or "（留言区暂时还没有帖子）"
    rules = _DECIDE_RULES + (_CONVENE_RULE if can_convene else "") + _DECIDE_TAIL
    return "【留言区最近动态】\n" + body + rules


def _parse_decision(raw, targets):
    """把模型输出解析成 (body, target|None)。

    PASS → ("", None) 不发言；开头 [#号] 且号可回帖 → 回复该帖（剥掉标记）；
    号非法或剥标记后为空 → 退化为原创帖（上层 body 空则自然跳过）。容错优先，解析失败当发帖。
    """
    raw = (raw or "").strip()
    if not raw or _PASS_RE.match(raw):
        return "", None
    m = _REF_RE.match(raw)
    if not m:
        return raw[:2000], None
    body = raw[m.end():].strip()
    target = targets.get(int(m.group(1)))
    if target is not None and body:
        return body[:2000], target
    # ponytail: 引用了不可回帖的号(或剥标记后空了) → 当原创帖；再上层空 body 会跳过
    return body[:2000], None


def _build_role_digest(bound, role_key, names, trigger=None, memories=None, focus_line=None):
    """拼给该角色看的留言区摘要 + 可回帖目标表 {帖号: 帖}。

    展示近 _DIGEST_POSTS 条帖（带回帖数、标注「你自己」/「已关评」），注入长期立场备忘
    （自己的 + 同侪的，#6），再附本角色最近 _OWN_RECENT 条自述回顾（#1 记得自己发过什么）。
    targets 只含「他人所发、未关评」的帖——即可被 [#帖号] 回复的对象；自己的帖与已关评帖只作上下文。
    触发态下若本角色正是被点名/被回复/被召集对象，顶部加一句高亮，并把该帖塞进 targets（放行回到自己帖）。
    focus_line（D 方案）：非空则追加「你的关注域」一行软分工提示，把话题往本角色镜片拉。
    """
    try:
        posts = bound.list_forum_posts(limit=_DIGEST_POSTS)
    except Exception:  # noqa: BLE001
        posts = []
    targets: dict[int, dict] = {}
    lines: list[str] = []
    for p in posts:
        pid = int(p["id"])
        mine = p.get("author_type") == "ai" and p.get("author_role_key") == role_key
        closed = bool(p.get("comments_closed"))
        rc = int(p.get("reply_count") or 0)
        tag = "·已关评" if closed else (f"·{rc}回帖" if rc else "")
        who = "你自己" if mine else _who(names, p)
        lines.append(f"[#{pid}]（{who}{tag}）{_oneline(p.get('body'))[:90]}")
        if not mine and not closed:
            targets[pid] = p
    digest = "\n".join(lines)
    mem = _memory_block(role_key, names, memories)  # 自己的长期立场 + 同侪立场（#6）
    if mem:
        digest = mem + "\n\n" + digest
    head = _trigger_head(bound, role_key, trigger, targets)  # 被点名/被回复/被召集时顶置高亮 + 放行该帖
    if head:
        digest = head + "\n\n" + digest
    try:
        own = bound.list_forum_posts(role_key=role_key, limit=_OWN_RECENT)
    except Exception:  # noqa: BLE001
        own = []
    if own:
        digest += "\n\n【你最近发过】" + "；".join(_oneline(p.get("body"))[:50] for p in own)
    if focus_line:  # D：软分工提示，追加在最后一行
        digest += "\n\n" + focus_line
    return digest[:_DIGEST_CAP], targets


def _memory_block(role_key, names, memories) -> str:
    """注入长期立场备忘：本角色自己的（提醒它记得自己的观点）+ 同侪的（记得别人的长期观点，#6）。"""
    if not memories:
        return ""
    parts: list[str] = []
    own = memories.get(role_key)
    if own and (own.get("stance") or "").strip():
        parts.append("【你的长期立场（你自己记的）】" + _oneline(own["stance"])[:_MEMORY_CAP])
    peers = []
    for rk, mem in memories.items():
        if rk == role_key:
            continue
        s = (mem.get("stance") or "").strip()
        if s:
            peers.append(f"{names.get(rk) or rk}：{_oneline(s)[:_PEER_STANCE_CAP]}")
    if peers:
        parts.append("【其他分析师的长期立场】" + "；".join(peers))
    return "\n".join(parts)


def _trigger_head(bound, role_key, trigger, targets) -> str:
    """若本角色是被点名/被回复/被召集对象，返回一句顶置高亮，并把该帖塞进 targets（含回到自己帖的评论区）。"""
    if not trigger:
        return ""
    at_roles = set(trigger.get("at_roles") or ())
    pid = trigger.get("post_id")
    post = _safe_post(bound, pid) if pid else None
    is_convene = bool(trigger.get("convene"))
    is_author = bool(post and post.get("author_type") == "ai"
                     and post.get("author_role_key") == role_key)
    if role_key not in at_roles and not is_author and not is_convene:
        return ""  # 本轮虽被触发，但该角色不是被点名/被召集对象 → 按常规摘要处理
    if post and not bool(post.get("comments_closed")):
        targets[int(post["id"])] = post  # 确保能 [#号] 回到该帖（自己帖/被召集帖也可回评论区）
    excerpt = _oneline(trigger.get("reply_excerpt"))[:80]
    where = f"[#{pid}]" if pid else "留言区"
    if is_convene:
        return f"📣 有人在 {where} 发起召集，想请大家一起聊：「{excerpt}」。有想法就 [#{pid}] 回帖参与，没有就 PASS。"
    if is_author:
        return f"⚡ 刚有人在你的帖 {where} 下回复：「{excerpt}」。这是冲你来的，优先想想要不要回应。"
    return f"⚡ 有人在 {where} 点名 @了你：「{excerpt}」。优先想想要不要回应。"


def _safe_post(bound, post_id):
    try:
        return bound.get_forum_post(int(post_id))
    except Exception:  # noqa: BLE001
        return None


def _who(names, post) -> str:
    if post.get("author_type") != "ai":
        return "板主"
    rk = post.get("author_role_key") or ""
    return names.get(rk) or rk or "AI"


def _oneline(s) -> str:
    return " ".join((s or "").split())


def _name_map(store, user_id) -> dict:
    """role_key → 昵称，一轮建一次（供摘要标注帖子作者，省去逐帖查身份）。"""
    out = {}
    for rk in FORUM_ROLE_KEYS:
        try:
            out[rk] = get_identity(store, user_id, rk).display_name
        except Exception:  # noqa: BLE001
            out[rk] = rk
    return out


def _count_mentions(blob: str, needle: str) -> int:
    """latin/数字代码用词边界，CJK 名用子串——避免 "GM" 命中 "programming"、中文名少字误命中。"""
    if not needle:
        return 0
    if re.fullmatch(r"[A-Za-z0-9.\-]+", needle):
        return len(re.findall(rf"\b{re.escape(needle)}\b", blob, re.IGNORECASE))
    return blob.count(needle)


def _saturated_tickers(bound, entries) -> dict[str, int]:
    """A：近期被反复讨论的标的 → {ticker: 提及次数}，只留出现≥_SATURATION_MIN 的前 _SATURATION_TOP 只。

    拉近 _SATURATION_POSTS 帖，对观察池已知 ticker + 中英文名做匹配计数。
    # ponytail: 启发式计数（子串/词边界），非主题级 NLP 聚类；真要按「问题/话题」去重再上 embedding。
    """
    try:
        posts = bound.list_forum_posts(limit=_SATURATION_POSTS)
    except Exception:  # noqa: BLE001
        return {}
    if not posts or not entries:
        return {}
    known: list[tuple[str, str]] = []  # (ticker, 匹配用 needle)
    for e in entries:
        tk = (e.get("ticker") or "").strip()
        if not tk:
            continue
        needles = {tk}  # set 去重：A 股 company_name 常与 company_name_cn 相同，避免同名双计成假饱和
        for nm in (e.get("company_name_cn"), e.get("company_name")):
            nm = (nm or "").strip()
            if len(nm) >= 2:
                needles.add(nm)
        known.extend((tk, n) for n in needles)
    if not known:
        return {}
    blob = " ".join(_oneline(p.get("body")) for p in posts)
    counts: dict[str, int] = {}
    for tk, needle in known:
        n = _count_mentions(blob, needle)
        if n:
            counts[tk] = counts.get(tk, 0) + n
    ranked = sorted(
        ((tk, n) for tk, n in counts.items() if n >= _SATURATION_MIN),
        key=lambda kv: kv[1], reverse=True,
    )
    return dict(ranked[:_SATURATION_TOP])


def _board_context_base(bound) -> _BoardCtx:
    """一轮算一次的共享基座：L1 宏观 + 双市场平衡观察池 + 近期催化剂 + 反重复提示（不含深挖）。

    深挖种子改为逐角色（_role_lens）——D 方案的软分工。返回 _BoardCtx，把 entries/饱和集/
    催化剂标的→impact 映射一并带出，供 _role_lens 复用，避免 8×list_all / 8× 饱和扫描。
    B：观察池按 market 交错取样。C：注入随时间变化的催化剂/事件。A：显式点出已被反复讨论的票。
    全 best-effort，任何一步失败都不影响其余。
    """
    lines: list[str] = []
    try:
        from bottleneck_hunter.vip.advisory import format_macro_for_prompt
        macro = format_macro_for_prompt(bound)
        if macro and macro.strip():
            lines.append(macro.strip())
    except Exception:  # noqa: BLE001
        pass

    entries: list[dict] = []
    try:
        entries = bound.list_all() or []
    except Exception:  # noqa: BLE001
        entries = []

    saturated = _saturated_tickers(bound, entries)  # A

    cat_map: dict[str, str] = {}  # C：催化剂标的 → impact_level，回喂 B/D 深挖加权与 risk 镜片
    try:
        cats = bound.get_recent_catalysts()
    except Exception:  # noqa: BLE001
        cats = []
    if cats:
        cbits = []
        for c in cats[:6]:
            tk = (c.get("ticker") or "").strip()
            lvl = c.get("impact_level") or ""
            if tk:
                cat_map[tk] = lvl
            title = _oneline(c.get("title"))[:24]
            date = (c.get("expected_date") or "")[:10]
            cbits.append(" ".join(x for x in (tk, date, title) if x) + (f"（{lvl}）" if lvl else ""))
        if cbits:
            lines.append("近期催化剂/事件（可据此发起新话题，别都盯着老几只）：" + "；".join(cbits))

    if entries:
        # B：按 market 分组交错取样，双市场都露出，不再只取前 12 高分（多为美股）
        by_market: dict[str, list[dict]] = {}
        for e in entries:
            by_market.setdefault(e.get("market") or "", []).append(e)
        pools = [lst[:] for lst in by_market.values()]  # 各市场已按分数倒序，round-robin 取各自最优
        tags: list[str] = []
        while pools and len(tags) < _CTX_TAGS:
            for pool in pools:
                if not pool or len(tags) >= _CTX_TAGS:
                    continue
                e = pool.pop(0)
                tk = e.get("ticker")
                if not tk:
                    continue
                sec = (e.get("sector") or "").strip()
                tags.append(f"{tk}（{sec}）" if sec else tk)
            pools = [pl for pl in pools if pl]
        if tags:
            lines.append("板主观察池：" + "、".join(tags))

    if saturated:  # A：显式点出已聊烂的标的，配合 _DECIDE_RULES 让 AI 换角度或 PASS
        bits = "、".join(f"{tk}×{n}" for tk, n in saturated.items())
        lines.append("近期已被反复讨论（换个角度或换只票，别扎堆）：" + bits)

    return _BoardCtx("\n".join(lines)[:_CTX_CAP], entries, saturated, cat_map)


def _fresh_pool(entries, saturated) -> list[dict]:
    """有 ticker 且未被反复讨论的候选；全被饱和则退回全部有 ticker 的（别选空）。"""
    pool = [e for e in entries if e.get("ticker") and e.get("ticker") not in saturated]
    return pool or [e for e in entries if e.get("ticker")]


def _role_lens(bound, role_key, entries, saturated, cat_map):
    """D 方案·逐角色镜片：返回 (deep_dive_seg, focus_line)。

    focus_line 追加到 digest 末尾（软分工提示）；deep_dive_seg 是该角色专属深挖票的背景段
    （拼到共享基座后面）。deep_dive=False 的角色（macro/consensus）不给单股种子——macro 改给
    板块聚合。选票缺数据时按用户拍板的降级路径兜底，绝不在此拉实时行情。
    """
    prof = FOCUS_PROFILES.get(role_key)
    pool = _fresh_pool(entries, saturated)
    if prof is None:  # 未配镜片：退回 B 的「全池排除饱和 + 催化剂优先」
        hot = [e for e in pool if e.get("ticker") in cat_map]
        pick = random.choice(hot or pool) if pool else None
        return (_ticker_background(bound, pick) if pick else ""), ""
    focus_line = "【你的关注域】" + prof.focus
    if not prof.deep_dive:
        seg = _sector_digest(entries) if role_key == "L1_macro" else ""
        return seg, focus_line
    entry = _pick_for_role(bound, prof.pick, pool, cat_map)
    seg = _ticker_background(bound, entry) if entry else ""
    return seg, focus_line


def _pick_for_role(bound, strategy, pool, cat_map):
    """按角色策略从候选池挑一只深挖票；缺数据即降级，返回 entry 或 None。"""
    if not pool:
        return None
    if strategy == "value":  # 估值/防御：偏低 composite_score
        ranked = sorted(pool, key=lambda e: e.get("composite_score") or 0.0)
        return random.choice(ranked[:max(3, len(ranked) // 2)])
    if strategy == "growth":  # 成长赛道：科技/新能源 sector
        kws = ("半导体", "芯片", "新能源", "电子", "科技", "AI", "算力", "光伏", "电池", "软件")
        hot = [e for e in pool if any(k in (e.get("sector") or "") for k in kws)]
        return random.choice(hot or pool)
    if strategy == "risk":  # 下行风险：高冲击催化剂优先 → 大跌 → 全池
        crit = [e for e in pool if cat_map.get(e.get("ticker")) in ("critical", "high")]
        if crit:
            return random.choice(crit)
        drop = _extreme_by_snapshot(bound, pool, "change_pct", want_max=False)
        return drop or random.choice(pool)
    if strategy == "contrarian":  # 情绪反面：涨跌幅/RSI 极值；缺快照→池本就已排除饱和(天然反选冷门)
        ext = _extreme_by_snapshot(bound, pool, "change_pct", want_abs=True)
        return ext or random.choice(pool)
    if strategy == "technical":  # 技术/资金流：放量优先；缺快照→催化剂→全池
        vol = _extreme_by_snapshot(bound, pool, "volume", want_max=True)
        if vol:
            return vol
        hot = [e for e in pool if e.get("ticker") in cat_map]
        return random.choice(hot or pool)
    if strategy == "holdings":  # 顾问：板主实际持仓票；空仓→高分优质
        try:
            held = {(p.get("ticker") or "") for p in bound.get_sim_positions()}
        except Exception:  # noqa: BLE001
            held = set()
        mine = [e for e in pool if e.get("ticker") in held]
        if mine:
            return random.choice(mine)
        ranked = sorted(pool, key=lambda e: e.get("composite_score") or 0.0, reverse=True)
        return ranked[0] if ranked else None
    return random.choice(pool)  # default / 未知策略


def _extreme_by_snapshot(bound, pool, field, *, want_max=True, want_abs=False):
    """在前 _LENS_SNAPSHOT_K 只(按 composite_score)里按快照 field 取极值的 entry；全缺快照→None。

    # ponytail: 只查前 K 只，避免每轮对全池逐票查快照；A 股常缺快照→自然跳过，缺价降级见调用方。
    """
    cand = sorted(pool, key=lambda e: e.get("composite_score") or 0.0, reverse=True)[:_LENS_SNAPSHOT_K]
    best = None
    best_val = None
    for e in cand:
        try:
            snap = bound.get_latest_snapshot(e.get("ticker") or "")
        except Exception:  # noqa: BLE001
            snap = None
        if not snap or snap.get(field) is None:
            continue
        val = snap[field]
        val = abs(val) if want_abs else val
        if best_val is None or (val > best_val if want_max else val < best_val):
            best_val, best = val, e
    return best


def _sector_digest(entries) -> str:
    """macro 镜片：观察池按 sector 聚合分布（不给单股种子），供其从板块轮动切入。"""
    counts: dict[str, int] = {}
    for e in entries:
        sec = (e.get("sector") or "").strip()
        if sec:
            counts[sec] = counts.get(sec, 0) + 1
    if not counts:
        return ""
    top = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:8]
    return "板块分布（供你看轮动，不必盯单只）：" + "、".join(f"{s}×{n}" for s, n in top)


def _ticker_background(bound, entry) -> str:
    tk = entry.get("ticker")
    if not tk:
        return ""
    try:
        from bottleneck_hunter.watchlist.committee import build_ticker_background
        bg = build_ticker_background(bound, tk, entry.get("id") or "", entry.get("market") or "")
    except Exception:  # noqa: BLE001
        return ""
    if not isinstance(bg, dict):
        return ""
    picks = []
    for k in ("valuation_data", "catalyst_data", "sentiment_data", "sector_trends"):
        v = bg.get(k)
        if (isinstance(v, str) and v and not v.startswith("暂无")) or (isinstance(v, dict) and v):
            picks.append(f"{k}: {v}")
    return (f"{tk} 背景 — " + "；".join(picks)) if picks else ""


def _publish(uid, event, **payload) -> None:
    """向板主 SSE 订阅者广播（lazy import 避开 watchlist→web 硬依赖；无订阅/失败均安全）。"""
    try:
        from bottleneck_hunter.web.forum_api import get_forum_broadcaster
        get_forum_broadcaster().publish(uid, {"type": event, **payload})
    except Exception:  # noqa: BLE001
        pass
