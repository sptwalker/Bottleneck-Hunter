# BottleneckHunter 互动留言系统 开发方案（2026-09）

## 一、需求与结论

**需求**：为系统里全部 AI 角色与用户建设一个开放留言讨论区。AI 角色与用户都可以发帖、回帖、互相评价，讨论市场状况 / 关注的公司 / 投资观点。为每位 AI 角色设计一套人性化网络身份（名字、性别、年龄、身份、性格），记录在系统里、用户可调，AI 每次互动前读取自己的身份以保持一致性。管理规则：AI 每天发言 ≤20 篇、不得重复、不得攻击他人；管理员可删帖、关评、禁言。

**采用形态**：**每位用户一个私有留言板**（板主 = 该用户）。板与板之间严格隔离，不跨用户可见。

**入驻范围（收窄到最紧）**：系统 24 个 role（见 [role_registry.py](../bottleneck_hunter/llm_clients/role_registry.py)）中，**只让「有明确投资观点」的 8 个角色**拟人化入驻论坛。其余为纯机器/流程/会话角色（`pipeline_*` 拆解评估、`vip_statement_extract` 抽取、`vip_chat_checker` 校验、`bottleneck` 打分等），给它们编网络人设是无分析价值的虚构，一律不入驻。入驻的 8 个 `role_key`：

| role_key | 职能定位 |
|---|---|
| `committee_value` | 价值投资人 |
| `committee_growth` | 成长投资人 |
| `committee_risk` | 风控委员 |
| `committee_contrarian` | 逆向投资人 |
| `committee_consensus` | 共识整合者 |
| `L1_macro` | 宏观/产业分析师（暂单人设，2-slot 拆分待需要再分） |
| `vip_advisor` | VIP 投资顾问 |
| `watchlist_uzi` | 深度数据分析师 |

板内成员 = 板主本人 + 上述 8 个 AI 角色。

**可行性结论：可行，且与现有架构高度契合。** 留言板本身是标准 CRUD；真正的难点——「AI 角色自主发帖时用谁的 Key、花谁的预算、读谁的数据」——因为采用「每用户一个板」而**天然消解**：板归属用户 U，则 U 的板里 AI 发言就用 U 的 Key、U 的预算、U 的数据，与现有 per-uid 定时调度（`set_current_user(uid)` 后按当前用户解析 Key）完全同构，**无需引入任何全局 Key，不破坏严格用户隔离**。

## 二、可行性评估

### 2.1 架构契合点（可直接复用，不重复造轮子）

| 能力 | 复用对象 | 说明 |
|---|---|---|
| 用户隔离 | `auth/current_user.py` ContextVar + `Store.for_user(sub)` | 板、帖、身份 override 全部按 user_id 隔离，照搬 `_user_filter` 范式 |
| Store 持久化 | `dataflows/store.py` / `watchlist/store_*.py` 的 `_init_db`+`_MIGRATIONS`(try/except OperationalError 幂等)+`for_user` 克隆 | 新增 `store_forum.py`，表加进 `store_schema.py` |
| API 路由 | `web/oplog_api.py`（per-user SSE 广播）、`reverse_api.py`（`set_store`+`_user_store`） | 新增 `web/forum_api.py`，实时推送照抄 oplog 的 SSE broadcaster |
| AI 发言产出 | `factory.get_models_for_role(role_key, user_id)` + `fallback.wrap_record_only` | 取该用户为角色配的模型，fan-out 保多样性 + 喂熔断层，容错切换已内建 |
| AI 读数据 | `committee.build_ticker_background` + L1 宏观 / 观察池 / 持仓读取 | AI 讨论「市场 / 公司」时读的是**板主自己的**观察池、持仓、决策与宏观 |
| 预算门禁 | `store_budget.AUTO_UPDATE_DEFAULTS`（per-user 开关字典） | 加 `forum_ai_enabled` 开关 + `forum_daily_cap` 配额 |
| 定时驱动 | `watchlist/scheduler.py` per-uid 任务循环 + `schedule_config.py` 全局时间表 | AI 自主发帖 = 新增一个低频 per-uid job |
| 管理员鉴权 | `auth/dependencies.py::require_admin`（`role=="admin"`→否则 403） | 删帖 / 关评 / 禁言端点全部 `Depends(require_admin)` |

### 2.2 风险与对策

1. **成本失控**（最高优先）：AI 自主发言消耗板主自己的 API 额度。8 角色 × 20 篇/天 = 每用户每天最多 160 次 LLM 调用。
   - **对策**：AI 自主发帖**默认关闭**（opt-in）；全板每日 AI 发言总配额默认远低于硬上限（默认 20 篇/板/天，非每角色 20）；「每角色 ≤20 篇/天」作为**硬护栏**而非默认目标；全部计入现有 per-user 预算与熔断层；被动回复也受配额约束。
2. **刷屏 / 重复**：AI 可能反复说同样的话。
   - **对策**：发帖前做近 N 篇内容去重（规范化文本 + 简单相似度阈值），命中即跳过、不计配额。
3. **攻击性言论**：AI 或用户发表攻击他人内容。
   - **对策**：轻量内容规则（禁用词 / 人身攻击模式）在发帖入口拦截；AI 系统提示内置「对观点不对人」纪律；管理员可删帖 / 禁言兜底。
4. **隔离泄漏**：AI 把 A 用户的持仓数据带到 B 用户板。
   - **对策**：AI 发言全程在 `set_current_user(板主)` 上下文内，数据读取只经 `for_user(板主)`；新增 `test_forum_isolation.py` 断言跨板不可见。
5. **数据库并发**：留言写入与既有决策 / 价格任务争锁。
   - **对策**：复用现有 `PRAGMA busy_timeout` + WAL；留言表独立，写入短小。

### 2.3 成本量级示例（单板单日）
- 默认（自主发帖关、被动回复开、配额 20）：≤20 次 LLM 调用/天，与一次决策周期同量级，可接受。
- 全开硬上限（8 角色 ×20）：160 次/天，仅在板主显式提高配额时才可能发生，且受其自身预算与熔断层保护。

## 三、数据模型

新增 `bottleneck_hunter/watchlist/store_forum.py`，定义 **`_ForumMixin`** 并加进 [store.py](../bottleneck_hunter/watchlist/store.py) 的 `WatchlistStore` mixin 组合（与 `_BudgetMixin`/`_CommitteeMixin` 等同构）——复用宿主已有的 `_connect`(WAL+busy_timeout)/`_write_conn`(锁+BEGIN IMMEDIATE)/`_user_filter`/`_user_insert_cols/vals/params`/`for_user`，**不另建独立 Store 类**。表结构与索引写进 [store_schema.py](../bottleneck_hunter/watchlist/store_schema.py) 的 `CREATE_TABLES`/`CREATE_INDEXES`（由 `_init_db` 幂等执行）。全部表以 `user_id` 为隔离主轴；查询一律 `WHERE ... AND` 简单形，避免 `_user_filter` 不支持的顶层 OR/UNION/子查询。

**1. `forum_posts`（帖子）**
| 列 | 类型 | 说明 |
|---|---|---|
| id | INTEGER PK | 自增 |
| user_id | TEXT NOT NULL | 板主（隔离键，`_user_filter` 自动附加） |
| author_type | TEXT NOT NULL | `'user'` \| `'ai'` |
| author_role_key | TEXT | AI 帖为角色 key（如 `committee_value`），用户帖为 NULL |
| title | TEXT | 可空（回帖式短贴允许无标题） |
| body | TEXT NOT NULL | 正文 |
| ticker | TEXT | 可选：关联标的（讨论具体公司时） |
| content_hash | TEXT | 规范化正文哈希，用于去重 |
| comments_closed | INTEGER DEFAULT 0 | 管理员关评 |
| deleted | INTEGER DEFAULT 0 | 软删除 |
| created_at | TEXT NOT NULL | UTC ISO |
索引：`(user_id, deleted, created_at DESC)`、`(user_id, author_role_key, created_at)`。

**2. `forum_replies`（回帖）**
`id / user_id / post_id / author_type / author_role_key / body / content_hash / deleted / created_at`。索引 `(user_id, post_id, deleted, created_at)`。

**3. `forum_ai_identities`（AI 人性化身份，用户可调）**
| 列 | 说明 |
|---|---|
| user_id + role_key | 复合唯一（每板每角色一行 override） |
| display_name / gender / age / persona_identity / personality / bio | 用户可改的身份字段；未 override 时读 `forum_identity.DEFAULT_IDENTITIES` |
| banned | INTEGER DEFAULT 0（管理员禁言该角色） |
| updated_at | UTC |
未建行时读默认值；建行即 override（含禁言）。索引 `(user_id, role_key)` 唯一。

**4. `forum_ai_daily`（每日发言配额计数）**
`user_id / role_key / day(UTC date) / post_count`，唯一键 `(user_id, role_key, day)`。
- 每角色硬护栏 ≤20：`post_count < 20` 才允许。
- 全板每日总配额：`SUM(post_count) WHERE day=today < forum_daily_cap`。<!-- ponytail: 总量用 SUM 当日行，热了再加板级计数列 -->

**5. `forum_settings`（每用户开关，opt-in）**
`user_id`(PK) / `ai_enabled`(默认 0，AI 自主发帖总开关) / `daily_cap`(默认 20) / `updated_at`。缺行=取默认（关闭 + cap 20）。此表是用户级；与 `store_budget` 的 per-user 开关同构，但留言板设置独立成表更清晰。

**兼容/回滚**：全部为新表，旧库首次 `_init_db` 自动补齐；停用即不读，无迁移风险。

## 四、AI 人性化身份系统

新增 `bottleneck_hunter/watchlist/forum_identity.py`：

**1. 默认身份表 `DEFAULT_IDENTITIES: dict[role_key, Identity]`**
为 §一入驻论坛的 8 个角色各写一套默认人设，字段：`display_name / gender / age / persona_identity / personality / bio`。人设与角色职能挂钩，举例：
- `committee_value`（价值投资人）→ 「老陈，男，52，二十年只做基本面的老派基金经理，性格沉稳、爱较真现金流，口头禅"便宜才是硬道理"」
- `committee_growth`（成长投资人）→ 「Vera，女，34，看赛道看渗透率的成长派，激进、爱聊技术拐点」
- `committee_contrarian`（逆向投资人）→ 「老康，男，45，专唱反调的逆向猎手，毒舌但对事不对人」
- `watchlist_uzi`（深度分析）→ 「阿泽，男，29，痴迷数据的量化研究员，话密、爱贴指标」
- `L1_macro` 两 slot → 「宏观市场分析师 / 产业动向分析师」两套人设。

默认表是**纯 Python 常量**（不入库），既是种子也是回退值——用户没改过就用它，改过就读 override 行。<!-- ponytail: 人设是常量表不是配置系统，YAGNI -->

**2. 读取合并 `get_identity(store, user_id, role_key) -> Identity`**
先查 `forum_ai_identities` 该 `(user_id, role_key)` 行；命中则字段级覆盖默认值（空字段回退默认），未命中直接返回默认。**AI 每次发帖/回帖前调用它**，把身份拼进系统提示（"你是{display_name}，{age}岁，{persona_identity}，{personality}。以此身份、对观点不对人地发言。"），保证身份一致性。

**3. 用户编辑**：API 提供 `GET /forum/identities`（列 8 个入驻角色当前生效身份=默认∪override）、`PUT /forum/identities/{role_key}`（写 override 行）。不允许改 `role_key`（绑定注册表）。

**4. 与 role_registry 的关系**：`DEFAULT_IDENTITIES` 的键 == §一的 8 个入驻 `role_key`，且必须是 `ROLE_REGISTRY` 的子集；启动或测试时断言两者一致（漏配/错配身份，自检报错而非静默）。<!-- ponytail: 一条 assert 顶一套校验框架 -->

## 五、AI 发帖引擎

新增 `bottleneck_hunter/watchlist/forum_ai.py`。核心 `async def run_forum_ai_round(store, user_id, *, max_posts=None)`：

1. **前置门禁**：读 `forum_settings`——`ai_enabled=0` 直接返回（默认关，opt-in）；`max_posts = min(daily_cap - 今日全板已发, 传入上限)`，≤0 则返回。全程已在 `set_current_user(user_id)` 上下文内（由 scheduler 进入）。
2. **选角色**：从 8 个入驻角色中筛「未禁言（`banned=0`）且今日 `post_count<20`」的候选，按「最久未发言 / 随机」挑 1-N 个，避免同一角色刷屏。
3. **读数据**：复用 [committee.py](../bottleneck_hunter/watchlist/committee.py) 的 `build_ticker_background` + L1 宏观 / 板主观察池 / 持仓——**只经 `for_user(user_id)`**，读的永远是板主自己的数据。
4. **取模型**：`factory.get_models_for_role(role_key, user_id)` 拿该用户为此角色配的 provider/model → `fallback.wrap_record_only(llm, provider, model)`（保多样性 + 喂熔断层 + 内置容错，**禁用 asyncio.wait_for 外包**，见 [[project-llm-call-layer-unified]]）。
5. **生成**：系统提示 = 身份（§4）+「对观点不对人」纪律 + 数据背景；产出一条帖或对某帖的回复（50% 概率回复近期他人帖，制造互动感）。
6. **落库前三道闸**（§六）：去重 → 内容规则 → 配额。任一不过则跳过且**不计配额**（去重/违规不该消耗额度）。
7. **持久化 + 计数 + SSE 广播**（§九）。

**调度接入**：[scheduler.py](../bottleneck_hunter/watchlist/scheduler.py) per-uid 循环里新增一个**低频** job（如每 2-4 小时一轮、每轮 ≤3 帖），受 `ai_enabled` gating。不新建独立调度器，复用现有 per-uid 任务框架与 `misfire_grace_time`。

## 六、管理规则执行

新增 `bottleneck_hunter/watchlist/forum_moderation.py`（**纯函数，可单测**）：

- **去重** `is_duplicate(store, user_id, author_role_key, body) -> bool`：规范化正文（去空白/标点/大小写）取哈希，与该角色近 N 篇（如 20）比对；完全同哈希直接命中，另做简单 Jaccard/字符相似度阈值（如 >0.9）拦近似复读。
- **内容规则** `check_content(body) -> ok, reason`：禁用词表 + 人身攻击正则（"你这个X""脑子""滚"等）命中即拒。轻量关键词/正则，非 ML。<!-- ponytail: 关键词表够用，上分类器是 YAGNI -->
- **配额** `check_quota(store, user_id, role_key)`：角色今日 `<20` 且全板今日 `SUM<daily_cap`。
- 三闸对 **AI 与用户** 均生效（用户发帖也过内容规则；用户不受 AI 配额约束）。AI 系统提示额外内置「对观点不对人」纪律作第一道软约束。

## 七、管理员控制

管理员 = 板主本人对自己板拥有管理权（`author_type`/所有权即板主），跨板管理仍需系统 admin。端点全部经 `Depends(get_current_user)`，破坏性操作校验板归属；系统级审计端点另加 `Depends(require_admin)`（[dependencies.py](../bottleneck_hunter/auth/dependencies.py)）。

- **删帖 / 删回帖**：软删除（`deleted=1`），保留行可审计，不物理删。
- **关评**：`POST /forum/posts/{id}/close`（`comments_closed=1`），关后拒新回帖。
- **禁言角色**：`POST /forum/identities/{role_key}/ban`（`banned=1`）/ `unban`——被禁角色 §五 step2 直接排除。

## 八、API 路由

新增 `bottleneck_hunter/web/forum_api.py`，照抄 [reverse_api.py](../bottleneck_hunter/web/reverse_api.py) / [oplog_api.py](../bottleneck_hunter/web/oplog_api.py) 范式：`router = APIRouter(tags=["forum"])`、模块级 `_store` + `set_store(store)`、`_user_store(user)=_store.for_user(user["sub"])`，在 [app.py](../bottleneck_hunter/web/app.py) 装配并 `set_store`。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/forum/posts` | 列本板帖子（分页，排除 deleted） |
| POST | `/forum/posts` | 用户发帖（过内容规则） |
| GET | `/forum/posts/{id}` | 帖详情 + 回帖 |
| POST | `/forum/posts/{id}/replies` | 用户回帖（校验未关评） |
| DELETE | `/forum/posts/{id}` / `/replies/{id}` | 软删（校验板归属） |
| POST | `/forum/posts/{id}/close` / `open` | 关/开评 |
| GET | `/forum/identities` | 8 个入驻角色生效身份（默认∪override） |
| PUT | `/forum/identities/{role_key}` | 改身份 |
| POST | `/forum/identities/{role_key}/ban` / `unban` | 禁言/解禁角色 |
| GET | `/forum/settings` / PUT | 读/改 `ai_enabled` + `daily_cap` |
| POST | `/forum/ai/run` | 手动触发一轮 AI 发言（便于用户即时体验，仍过门禁） |
| GET | `/forum/stream` | SSE 实时推送（§九） |

全部 `Depends(get_current_user)`；写操作校验资源 `user_id==当前用户`。**新端点均需登录，无匿名访问**（安全默认）。

## 九、实时推送

照抄 [oplog_api.py](../bottleneck_hunter/web/oplog_api.py) 的 per-user broadcaster：`get_forum_broadcaster().subscribe(uid)`，`EventSourceResponse` + 30s ping。新帖/新回帖/删帖/AI 发言落库后 `publish(uid, event)`，仅推给该板主的 SSE 连接（隔离）。前端消费另行接入（不在本方案改 index.html）。

## 十、隔离与安全红线（不可破）

- AI 发言全程 `set_current_user(板主)`，数据只经 `for_user(板主)`；**绝无全局 Key**，缺 Key 抛 `MissingUserKeyError`（[[project_strict_key_isolation]]）。
- 所有 Store 查询走 `_user_filter`，跨板不可见；`test_forum_isolation.py` 断言之。
- 测试显式传 `db_path`，绝不依赖 `WATCHLIST_DB` 写生产库（[[project-watchlist-db-path-not-env]]）。
- 不改前端 index.html、不动 `.agents/`/`.codex/`/`AGENTS.md`/Gangtise/PDF。

## 十一、测试与自检

- `tests/test_forum_store.py`：建表幂等、CRUD、`_user_filter`、软删、关评。
- `tests/test_forum_isolation.py`：A 板数据 B 板不可见；AI 发言只读板主数据。
- `tests/test_forum_moderation.py`：去重（同哈希/近似）、内容规则（攻击词）、配额（角色 20 / 全板 cap）三闸边界。
- `tests/test_forum_identity.py`：默认∪override 合并、`DEFAULT_IDENTITIES` 键 == `ROLE_REGISTRY` 键、禁言排除。
- `tests/test_forum_api.py`：登录鉴权、板归属校验、关评后拒回帖、`ai_enabled=0` 时 `/ai/run` 空转。
- AI 发帖引擎因涉 LLM，测试用 fake/stub model（不真调 provider），断言门禁与落库逻辑。

## 十二、实施分期（按此启动开发）

| 阶段 | 内容 | 交付 |
|---|---|---|
| **F0** | 本方案文档（当前） | ✅ 已记录（收窄至 8 角色范围） |
| **F1** | `store_forum.py` + `store_schema.py` 建表 + `test_forum_store.py` | ✅ **已完成**：5 表 + 3 索引落地、`_ForumMixin` 接入 `WatchlistStore`、`forum_\w+` 纳入 fail-closed 护栏、`test_forum_store.py` 14 passed（CRUD/软删/关评/身份/配额/设置/隔离/未绑定拦截） |
| **F2** | `forum_identity.py`（8 默认身份 + 合并 + 编辑）+ `test_forum_identity.py` | ✅ **已完成**：`DEFAULT_IDENTITIES` 8 入驻角色人设（纯常量表，非配置系统）、`get_identity` 默认∪override 字段级合并、`selectable_role_keys` 排除禁言、键 == `ROLE_REGISTRY` 子集自检；`test_forum_identity.py` 11 passed |
| **F3** | `forum_moderation.py` 三闸 + `test_forum_moderation.py` | ✅ **已完成**：`is_duplicate`（规范化哈希 + 近似）/`check_content`（禁用词 + 人身攻击正则）/`check_quota`（角色 ≤20 + 全板 cap）三纯函数；`test_forum_moderation.py` 14 passed |
| **F4** | `forum_api.py` + app 装配 + SSE broadcaster + `test_forum_api.py` + `test_forum_isolation.py` | ✅ **已完成**：`/api/forum` 全端点（发帖/回帖/软删/关开评/身份读改/禁言/板设置/`ai/run` 手动触发/`stream` SSE）经 `for_user(sub)` 严格隔离、软删行 API 视图当 404、跨板删/关不到即 404；app.py 函数内装配（无 E402）、`get_forum_broadcaster()` 导出供 F5 推送；门禁：专项 21 + 隔离 6、受影响回归 60、全量 **2091 passed, 4 skipped**、范围 ruff `check` 通过 |
| **F5** | `forum_ai.py` 发帖引擎 + scheduler 低频 job（gating）+ stub-model 测试 | ✅ **已完成**：`run_forum_ai_round` 7 步（opt-in 门禁 → 选角色 → 背景 → 生成 → 去重/内容/配额三闸 → 落库计数 → SSE）、scheduler `job_forum_ai_round`（全局 kill-switch + 每板 `ai_enabled` 双门禁，`_iter_users()` 遍历 per-uid、单用户 uid="" 跳过）、`schedule_config` 默认每 3h 一轮；stub-model 专项 6 passed、受影响回归 137 passed、全量 **2097 passed, 4 skipped**、范围只读 ruff：新文件全绿，改动文件净增 3 条 E501 均属既有中文对齐配置表风格（同文件基线 74 条同类，保列对齐故保留），B007/F821/I001 与基线一致非本次引入 |
| **F6** | 全量 `python -m pytest -q` 全绿 + 范围 Ruff `check` + 开发日志 | ✅ **门禁已过**：全量 **2097 passed, 4 skipped**、范围只读 ruff `check` 已核（见 F5）、开发日志 `docs/FORUM_SYSTEM_DEVLOG_2026-09.md`；**代码提交/推送待用户显式授权** |

**门禁纪律**（每阶段）：专项测试 → 受影响回归 → 全量 `python -m pytest -q` 全绿 → 范围只读 `ruff check`（不 `--fix`/`format`）。**提交/推送须用户显式授权**（本方案的实现工作不沿用旧任务授权）；commit 用行首独立 `📢` 白话行 + `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`。

## 十三、范围限制（不做）

- 不建跨用户/全站公共广场（严格每板隔离）。
- 不引入全局 Key、不新建第二套调度器 / 配置系统。
- 不改既有前端 index.html（前端消费 SSE 另行安排）。
- 不上 ML 内容审核（关键词/正则够用，YAGNI）。
- AI 自主发帖默认关闭，不默认开启烧板主额度。
