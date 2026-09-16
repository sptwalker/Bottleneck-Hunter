# 互动留言系统 开发日志（2026-09）

配套方案：[FORUM_SYSTEM_PLAN_2026-09.md](FORUM_SYSTEM_PLAN_2026-09.md)。形态＝**每用户一个私有留言板**（板主＝该用户），板间严格隔离。入驻拟人化角色收窄至 8 个「有明确投资观点」的 `role_key`：`committee_value / committee_growth / committee_risk / committee_contrarian / committee_consensus / L1_macro / vip_advisor / watchlist_uzi`。

## 分期交付与门禁

| 阶段 | 交付 | 专项测试 |
|---|---|---|
| F0 | 方案文档（收窄至 8 角色） | — |
| F1 | `store_forum.py`（`_ForumMixin` 接入 `WatchlistStore`）+ `store_schema.py` 5 表 3 索引 | `test_forum_store.py` 14 passed |
| F2 | `forum_identity.py`：8 默认人设常量表 + `get_identity` 默认∪override 合并 + `selectable_role_keys` | `test_forum_identity.py` 11 passed |
| F3 | `forum_moderation.py`：去重 / 内容 / 配额三纯函数 | `test_forum_moderation.py` 14 passed |
| F4 | `forum_api.py` 全端点 + app 装配 + SSE broadcaster | `test_forum_api.py` 15 + `test_forum_isolation.py` 6 passed |
| F5 | `forum_ai.py` 发帖引擎 + scheduler 低频 job + stub-model 测试 | `test_forum_ai.py` 6 passed |
| F6 | 全量 pytest + 范围 ruff + 本日志 | 见下 |

论坛专项聚合 **66 passed**。

## F5 实现要点（本轮）

**`bottleneck_hunter/watchlist/forum_ai.py` · `run_forum_ai_round(store, user_id, *, max_posts=None) -> int`** 七步：

1. **前置门禁**：`for_user(板主)` 绑定 → 读 `forum_settings`，`ai_enabled=0`（默认）直接返回 0（opt-in）；`budget = min(daily_cap − 全板今日已发, 传入/默认上限)`，≤0 返回。
2. **选角色**：`selectable_role_keys`（未禁言）∩ 今日 `post_count < 20`（每角色硬护栏），`random.shuffle` 去同角色刷屏。
3. **背景数据**：`_board_context` 只经 `for_user(板主)` 拼 L1 宏观 + 观察池标的 + 抽一只深度背景，全 best-effort（try/except，1600 字上限）。
4. **生成**：`get_identity` 拼身份系统提示；50% 概率回复他人近帖、否则原创。LLM 走 `factory.get_models_for_role(role_key, user_id)` 返回的**已 record-only 包装**实例，直接 `ainvoke([SystemMessage, HumanMessage])`——**绝不再套 `wrap_record_only`、绝不加 `asyncio.wait_for`**（熔断层内部已 `wait_for`，外包即自毁，见 [[project-llm-call-layer-unified]]）。
5. **落库前三闸**：`is_duplicate` → `check_content` → `check_quota`，任一不过则跳过且**不计配额**（去重/违规不该烧额度）。
6. **持久化 + 计数**：回帖走 `create_forum_reply`、原创走 `create_forum_post`，随后 `incr_forum_daily_count`。
7. **SSE 广播**：`_publish` lazy import `web.forum_api.get_forum_broadcaster`（避 watchlist→web 硬依赖），无订阅/失败均安全。

**调度接入**（`scheduler.py`）：新增 `job_forum_ai_round` interval job——

- 双层门禁：`is_global_enabled(_auth_store)`（管理员级 kill-switch）+ 每板 `is_forum_ai_enabled()`；
- 经 `_iter_users()`（`category=None`：无 kill-switch/无 eligibility/无分类耦合，honoring §十三「不引入配置系统」）遍历 per-uid；
- **单用户模式**（`_auth_store` 为 None）`_iter_users` 产出 `uid=""` + 未绑定 store，`if not uid: continue` 跳过（论坛天生多用户/每板，无全局板）；
- `_JOB_SPECS` 6 元组 + `list_job_categories`/`list_job_labels` 登记；`schedule_config.GLOBAL_SCHEDULE_DEFAULTS["forum_ai_round"] = {"interval_hours": 3}`（低频每 3h 一轮、每轮 ≤3 帖）。

## 隔离与安全不变量（守）

- AI 发言全程 `set_current_user(板主)` 上下文内，数据只经 `for_user(板主)`；**绝无全局 Key**，缺 Key 抛 `MissingUserKeyError`（[[project_strict_key_isolation]]）。`test_forum_isolation.py` 断言跨板不可见。
- 测试显式传 `db_path`，绝不依赖 `WATCHLIST_DB`（[[project-watchlist-db-path-not-env]]）；stub-model 经 monkeypatch `factory.get_models_for_role` 注入，不真调 provider。
- 未改前端 `index.html`、未动 `.agents/`/`.codex/`/`AGENTS.md`/Gangtise/PDF。

## F6 门禁结果

- **全量**：`python -m pytest -q` → **2097 passed, 4 skipped**（基线 2091 + 本轮 6 个 F5 新测试）。
- **范围只读 ruff `check`**（`forum_ai.py` / `scheduler.py` / `schedule_config.py` / `test_forum_ai.py`，不 `--fix`/`format`）：
  - 新文件 `forum_ai.py`、`test_forum_ai.py`：**All checks passed**（修掉 `forum_ai.py` 唯一 1 条 SIM114，行为等价合并 `if/elif` 同体分支）。
  - 改动文件 vs HEAD 基线：净增 **3 条 E501**（`_JOB_SPECS`/`list_job_categories`/`list_job_labels` 各 1 行），均落在**全文件既有的中文对齐配置表**内（同文件基线已有 74 条同类 E501，我新增行的邻居无一例外 141–194 字符）；只 wrap 我这 3 行会破坏整表列对齐、且与周边风格相悖，故按「match surrounding code / 不重排无关既有代码」保留。`B007`(6)/`F821`(2)/`I001`(1) 与 HEAD 基线**完全一致**，非本次引入，未触。

## 提交状态

**代码提交/推送待用户显式授权**（本方案实现工作不沿用旧量化任务的合并推送授权）。授权后：commit message 用行首独立 `📢` 白话行 + `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`；暂存前逐文件审查，仅纳入论坛必需变更，排除前端 `index.html`、`.agents/`、`.codex/`、`AGENTS.md`、Gangtise 文档与 PDF。
