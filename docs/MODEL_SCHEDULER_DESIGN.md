# AI 模型智能调度层 · 设计与执行计划

> **版本**：v1.0 — 静态模型分配 → 数据驱动的智能调度
> **日期**：2026-07-10
> **背景**：系统接入了大量不同来源的公开 AI 模型（deepseek/qwen/kimi/glm/openai/anthropic/… + 自定义），这些模型存在不稳定、不可靠、限流、被墙等问题。目标是用一套**后台智能调度机制**取代当前**静态的角色→模型分配**，做到：全系统模型使用稳定、前期设置简化、模型异常时无缝切换、并及时向用户通告各模型使用情况。
>
> **核心理念**：不追求一步到位的"完美调度器"，而是建成一个**能自然平滑过渡的体系**——上线即不劣于现状，随用户真实使用数据积累，调度智能逐渐显现、越用越准。

---

## 一、目标（用户原始诉求）

1. **公共调度层**接入所有模型 API，成为全系统模型使用的统一出入口。
2. 依据**测试 + 用户设置**（付费/免费、质量优先/价格优先、可用性、可靠性）**自动动态分配**模型。
3. **自动监控输出是否正常**，某模型出问题即**无缝切换**其他模型继续完成任务。
4. **取代静态分配、简化前期设置**（角色矩阵可留空、系统自动分配）。
5. **及时通告用户**各模型使用情况（实时切换提示 + 长期健康仪表盘）。

---

## 二、核心判断：底盘已备八成，"油箱"待注

一个反直觉但关键的勘察结论：**调度所需的"机械管道"约 70–80% 已存在于系统**，真正要新写的核心不足 20%。

| 智能调度需要的能力 | 现有基建 | 状态 |
|---|---|---|
| 某模型失败无缝换、任务不中断（sync/async/stream 四路径 + 换成功通知） | `FallbackChatModel`（`fallback.py:81`） | 直接当执行器，30+ 调用点零改动 |
| 候选可用性门禁（去重/禁用/严格Key/可解析/造实例异常） | `build_fallback_candidates`（`fallback.py:180`） | 排序前照用 |
| 唯一裸实例工厂 + 逐用户 Key/端点解析 | `create_llm`/`_create_raw_llm`/`resolve_*`（`factory.py`） | 调度不另解析，直接调 |
| 按可靠性打分排序 | `model_ratings` + `get_calibration_weight` + `recalibrate` 公式 | 现成排序键 + 打分函数 |
| 失败归因 + 前端通知钩子 | `classify_reason` + `push/begin/drain_notices`（`fallback.py:26`） | 每次切换/清单直接接 |
| 定期权重再计算 | `job_model_calibration` + 按用户遍历（`scheduler.py`） | 照搬 |
| 主要/禁用 provider | `is_primary`/`is_active` + `set_provider_status`/`is_provider_active`（已实现） | 调度的用户显式意图输入 |

**但底盘全有，油箱是空的。** 决定"智能"质量的**遥测数据当前几乎为 0**：`record_prediction` 全项目只有"投委会投票"一个真实采集点，只有"准确率"一维、二值（赢/亏）、且滞后（仅模拟平仓回填）、离线周度校准。

> **推论**：不该新造"一层"，该换掉"一处"（静态候选顺序）+ **补上遥测采集面**。若不先补数据，任何方案在冷启动期排序键全是默认 1.0，调度退化为"原静态链 + 熔断"。因此 **Phase 0（补遥测）是不可跳的前置**。这也天然实现了"随数据积累逐渐发挥作用"的平滑过渡。

---

## 三、锁定的设计决策

| 议题 | 决策 | 落到设计 |
|---|---|---|
| 定性 | **先铺全岗位遥测，做真·智能路由** | Phase 0 硬前置，采集每次调用的延迟/成败/原因/病理 |
| "输出是否正常"判定 | **先判格式→稳定性→速度→持续性，质量后置** | 见第四节判定阶梯 |
| 自动分配程度 | **默认智能、用户可覆盖** | 用户手填配置（优先级 1）作覆盖；留空则走遥测自动排序（优先级 3/4） |
| 用户策略 | **每用户可配自己的策略**：优先免费/付费、优先质量/价格 | 严格 Key 隔离下按 `user_id` 存策略 |
| 策略粒度 | **全局默认一档 + 按角色/场景可覆盖** | `RoutingPolicy` 默认全局，角色可选覆盖，简化体验不变 |
| 主模型 vs 遥测 | **主模型给"加成上限"（折中）** | 主模型 `+PRIMARY_BONUS`；别的模型须超过"主+加成"才顶替 → 有粘性非绝对 |
| 免费全熔断时 | **自动回落付费 + 强提示** | 免费不可用即回落付费，必 `push_notice` 强提示"已临时启用付费模型 X" |
| 用户通告 | **两者都做** | ①实时切换弹提示；②健康仪表盘 + 长期输出曲线 |

---

## 四、"输出是否正常"判定阶梯

不同角色输出形态不同，**可判性天差地别**。按用户确定的优先级由易到难分层实现，**质量（语义对错）在线判不了，后置**：

| 优先级 | 维度 | 判据 | 可判性 | 阶段 |
|---|---|---|---|---|
| 1 | **输出格式** | JSON 角色解析失败 / schema 缺字段 / 空返回 = 坏 | 最好判 | Phase 2 |
| 2 | **输出稳定性** | 成功率 / 错误率（超时/报错/限流），随时间是否稳定 | 好判（遥测） | Phase 0→1 |
| 3 | **输出速度** | 延迟、首 token 延迟 | 好判（遥测） | Phase 0→1 |
| 4 | **长时间持续性** | provider 是否长期可用（熔断/健康度滚动窗口） | 好判（熔断） | Phase 1 |
| 5 | **输出质量**（语义对错） | 分析写得对不对、评分离不离谱 | **在线不可判**，靠滞后的选股结果闭环 | 未来 |

**两条硬限制必须写进设计：**
- **中文自由分析**（宏观/委员观点）语义质量在线判不了，`OutputValidator` 对这类角色**只保三条底线**（空/截断/明显拒答话术），**绝不做语义判定**，宁漏勿误杀——否则合法的短答被判坏 → 无谓切换 + 多花钱。
- **流式**：发现输出坏的时刻（首 token 已吐出）恰恰是不能再换模型的时刻。故流式只能靠**开流前**用健康度选定最优 provider，把失败概率前移；首 token 后失败只能记录，无法实时挽救。

---

## 五、可复用的现有基础设施（改造复用，勿重建）

- `llm_clients/fallback.py:81` **FallbackChatModel** — 保留"候选列表→逐个降级→换成功 push_notice" + 四路径包装器形态，**只把静态 candidates 顺序换成调度器排序结果**，对上游 LangChain 接口零改动。
- `llm_clients/fallback.py:180` **build_fallback_candidates** — 过滤流水线（去重/`is_provider_active`/`_user_has_llm_key` 严格 Key/可解析模型/造实例异常）原样作为排序前的候选池筛选。
- `llm_clients/fallback.py:26` **classify_reason + push/begin/drain_notices** — 失败归因 + ContextVar 通知钩子，调度每次切换与"使用清单"直接接。
- `llm_clients/factory.py` **create_llm / _create_raw_llm / resolve_provider_model/base_url** — 逐用户 Key 解析与模型/端点解析唯一入口，调度绝不另起影子配置。
- `llm_clients/factory.py` **get_models_for_role 四级优先链** — 优先级 1（DB 手填）保留为"用户显式覆盖"；优先级 3/4 静态兜底改为遥测排序，即"默认自动"。
- `watchlist/store_ai_models.py` **model_ratings / get_calibration_weight / record_prediction / record_outcome** — 按 `(provider,model,role,user,market)` 隔离的遥测落盘 + 打分读接口；`record_prediction` 从"仅投票"泛化到各岗位即复用同表。
- `watchlist/model_calibrator.py:23` **recalibrate** — "准确率×偏差惩罚×近期衰减"加权公式，直接当调度可靠性打分函数。
- `watchlist/scheduler.py` **job_model_calibration + 按用户遍历** — 照搬用于"调度权重再计算"作业。
- `llm_clients/role_registry.py` **capability_weights / multi_model / max_slots** — 冷启动无遥测时的先验权重与 fan-out 槽数元数据。
- `watchlist/committee.py:116` **_invoke_with_retry** — 已示范"同模型瞬态退避重试 + 换备用"，最后阶段并入统一策略并删除并行实现。

---

## 六、目标架构

请求路径（对上游 30+ 调用点透明，签名不变）：

```
业务调用点（graph/decomposer/L1-L4/committee/pipeline）
   ↓ get_models_for_role(role, user)              ← 签名不动
   ↓ 候选池 = build_fallback_candidates(...)       ← 过滤流水线照用
   ↓ ordered = rank_candidates(候选池, 遥测, 熔断, 策略)   ← 【新】唯一大脑
   ↓ FallbackChatModel(candidates=ordered)          ← 形态不变，仅顺序动态化
   ↓ 执行；失败→classify_reason→熔断记账→换下一候选→push_notice
   ↓ 每次调用旁路 record_call_metric（延迟/成败/原因/病理）  ← 【新】喂养排序
```

**新增组件（尽量小、按需长出）：**

- **`rank_candidates()`** — 唯一新"大脑"，纯函数、无状态、异步友好：
  `score = 可靠性(get_calibration_weight) × 健康度(熔断态/近期成功率) × 速度(延迟) × 策略权重(免费/付费/质量/价格)`；
  主模型额外 `+PRIMARY_BONUS`（可调，实现"加成上限"语义）。**无遥测时退化为现静态顺序**（平滑过渡的关键）。
- **`ProviderHealth`（熔断记忆）** — 进程内 TTL dict，key=`(user_id, provider, market)`。消费 `classify_reason`：认证失败/额度耗尽 → 长冷却；超时/5xx → 短冷却。冷却期内该 provider 在候选中降权/剔除，避免对已知失效模型反复耗一整轮超时。`ponytail:` 先进程内 dict，多 worker 需跨进程时再落 SQLite/Redis。
- **`record_call_metric()`** — per-call 遥测原语，在 `FallbackChatModel` 每候选出口旁路写（失败静默、批量/异步落盘，避开 `_write_lock` 高频瓶颈）。
- **`RoutingPolicy`** — 用户策略（付费/免费 tier + 质量优先/价格优先），全局默认 + 角色可覆盖，挂进 AI 配置中心（唯一入口，不加影子配置）。
- **`OutputValidator`**（Phase 2）— **仅 JSON/评分角色**做保守格式校验触发切换；中文分析只保三条底线。
- **健康仪表盘 + 输出曲线**（Phase 2）— `/model-usage` 端点 + 前端面板。

---

## 七、数据模型改动

- **`model_accuracy` 表加列**（或新建 `model_calls` 明细表，避免污染现有准确率聚合语义 — 实现时二选一）：`latency_ms`、`tokens`、`success`、`error_reason`、`pathology`（空/截断/拒答/JSON失败标记）。天然含 `user_id + market`，隔离不变。
- **`record_prediction` 泛化**：`prediction_type` 从只有 `'vote'` 扩到每个 `role_context`（拆解/瓶颈/宏观/…）。
- **provider tier 元数据**：给 `custom_providers` 或独立小表加 `tier`（free/paid），供免费/付费策略过滤。**当前缺，Phase 2 补。**
- **`RoutingPolicy` 存储**：按 `user_id` 存（严格隔离）；`role_key=''` 表示全局默认，非空为角色覆盖。字段：`prefer_tier`(free/paid/auto)、`optimize_for`(quality/price/availability/reliability)。
- **可调参数**（AI 配置中心）：`PRIMARY_BONUS`、熔断冷却时长、瞬态重试次数。

---

## 八、硬护栏（必须写成显式约束，不能靠排序自然涌现）

1. **fan-out 角色多样性红线** — 两类机制，勿混：
   - **role 内多槽**（`L1_macro` 2槽 / `bottleneck` 3槽）：`get_models_for_role` 优先级4 用 `seen_prov` **强制槽间 provider 不重复**，`with_fallback=False`、成员失败即丢弃。调度**绝不能对这类做"跨 provider 收敛到同一最优模型"**。
   - **委员会 4 员**（`committee_risk/growth/value/contrarian`）：是 **4 个独立单模型角色**，多样性来自各自的**角色默认 provider**（deepseek/qwen/kimi/glm）+ 用户为它们各配了 KEY；调度器**不跨成员强制去重**（各成员独立解析）。**若用户缺 qwen/kimi/glm 的 KEY，委员会会退化到少数几个 provider（甚至收敛同一个）——这是 KEY 覆盖不足的必然，非调度缺陷**。委员会备用链已跳过熔断中的 provider。
2. **严格 Key 隔离红线**（[[project_strict_key_isolation]]） — 排序/切换/熔断/评分表全部严格按 `(user, market, provider)` 分表，**绝无全局共享健康表/权重**。代价：状态量随用户数膨胀、一个用户的失败经验不惠及他人（合规但低效，接受）。
3. **无第二套影子配置**（[[project_ai_config_unified]]） — 模型/端点解析统一走 `resolve_provider_model/base_url`；策略存 AI 配置中心，不另起 `DC_MODEL_` 影子写。
4. **流式约束** — 首 token 已发出即不可切换模型；调度须在开流前选定 provider。
5. **主 provider 语义保留** — 通过"加成上限"体现用户"其它失效→换回主要"的预期，可被数据超越但有粘性。

---

## 九、分阶段执行计划

每阶段**独立上线、独立回退**，且**上线即不劣于现状**——这是"自然平滑过渡"的落地方式。

### ✅ Phase -1（已完成）：主要/禁用
`is_primary`/`is_active` + `set_provider_status`/`is_provider_active` 已实现，作为调度的用户显式意图输入。

### Phase 0 — 补遥测地基（前置，不可跳 · 约 1 天 + 冷启动积累期）✅ 已完成
- **目标**：开始积累每模型的延迟/成功率/故障画像。**不改任何行为。**
- **改动**：`model_accuracy` 加列（或建 `model_calls` 表）；`record_call_metric` 在 `FallbackChatModel` 四路径出口旁路写（失败静默、批量落盘）；`record_prediction` 泛化到各 `role_context`。
- **文件**：`store_ai_models.py`、`store_schema.py`、`fallback.py`。
- **退出标准**：真实跑几次分析后，能从表里查到各岗位各模型的延迟/成败明细。
- **平滑性**：纯旁路采集，零行为变更，零风险。
- **实现**：建 `model_call_stats` 聚合表（仿 `datasource_stats`，按 日期×用户×provider×model×角色 UPSERT 累加，规避高频写压力）；`record_model_call`/`get_model_call_stats`；四路径旁路接入 + pytest 守卫防测试污染。role/market 列已留位、待 Phase 1+ 贯穿。

### Phase 1 — 动态排序 + 熔断（= 拿 80% 价值 · 约 1.5 天）✅ 已完成
- **目标**：静态候选顺序 → 遥测排序；角色矩阵可留空即自动分配。
- **改动**：新增 `rank_candidates()`，插进 `build_fallback_candidates` return 前；`get_models_for_role` 优先级 3/4 改调它（优先级 1 手填仍覆盖优先）；主模型加成上限；`ProviderHealth` 进程内熔断接 `classify_reason`。
- **文件**：`fallback.py`、`factory.py`、新增 `llm_clients/health.py`。
- **退出标准**：留空矩阵能自动选模；失效 provider 被熔断跳过；feature flag 按用户灰度。
- **平滑性**：**无遥测时 `rank_candidates` 退化为现静态顺序**；feature flag 可随时关回现状。数据越多，排序越准——"逐渐发挥作用"在此兑现。
- **实现**：`health.py`（`ProviderHealth` 进程内熔断，按 (user,provider) 隔离；`rank_providers` 按 健康度×可靠性+主模型加成 排序，无数据稳定排序退化为原顺序）；接入 `build_fallback_candidates`（备选重排）+ `get_models_for_role` 优先级4（排后选主）+ `_record_call`（成功清除/失败冷却）。全局 flag `BH_SCHEDULER_RANK=0` 可一键关回静态。**熔断优先于主模型加成**（坏了就别硬顶）。per-user 灰度暂用全局 flag（YAGNI）。

### Phase 2 — 策略 + 输出校验 + 通告面板（按需 · 约 2 天）✅ 已完成
- **目标**：用户可配策略；格式校验触发切换；用户可见使用情况。
- **改动**：`RoutingPolicy`（全局默认 + 角色覆盖 + free/paid tier）挂 AI 配置中心；免费→付费自动回落 + 强提示；`OutputValidator`（仅 JSON/评分角色，保守）；`drain_notices` 加"本次各岗位模型使用清单"；`/model-usage` 健康仪表盘 + 输出曲线（长期平均表现）。
- **文件**：`ai_config_api.py`、`custom_provider_api.py`、`admin_api.py`、`fallback.py`、前端 `ai-config.js`/新面板、css。
- **退出标准**：切换弹提示 + 面板可看各模型可用性/延迟/成本/熔断/切换次数与历史曲线。
- **平滑性**：无策略时等权退化；校验保守可按角色关闭。
- **实现**：`validate.py`（角色无关内容启发式：空/伪JSON解析失败/拒答，接入非流式路径，坏格式换模型、末候选兜底，`BH_SCHEDULER_VALIDATE=0` 可关）；`ai_routing_policy` 表 + `rank_providers` 策略感知（免费/付费/价格）+ 免费全熔断→回落付费强提示；`drain_usage` 使用清单 → `with_notices` 发 `model_usage` 事件；`/model-usage`（聚合+日曲线+熔断+策略）、`/routing-policy` GET/POST；前端「🧭 调度看板」页签（策略选择器 + 使用表 + 内联 SVG 成功率曲线）。provider tier 用默认映射（免费:deepseek/qwen/glm/kimi/siliconflow；付费:openai/anthropic/google/…），暂不做逐 provider 标注 UI。

### Phase 2.5 — 模式测试喂入调度器 + 干净切换（取代静态分配）✅ 已完成
- **背景**：智能调度器建成后，静态分配（手填矩阵 priority-1、DC_MODEL_ 环境影子 priority-2、推荐→冻结矩阵）仍**覆盖**调度器，调度形同虚设。此阶段让「模式测试」这唯一的质量信号喂进调度器，并干净切换到调度器驱动。
- **A 能力先验**：`health.load_capability_scores(user, role)` 按角色 `capability_weights` 内联加权算各 provider 的模式测试综合分(0-10)；`rank_providers` 加 `role_key` 参数，`_score` 乘一个 0.7~1.0 的能力乘子（差异化但不压过健康/可靠性；无数据中性）。这补上调度器缺的「质量」维度，取代「推荐→冻结矩阵」。
- **B 多槽自动选型**：`get_models_for_role` 早取 `role_def`；多槽 fan-out 角色（L1_macro 2 槽 / bottleneck 3 槽）跳过 priority-3，priority-4 候选池扩为**全部已注册 provider**，按排序取 **top-N 个不同 provider**（保交叉验证多样性）；单模型位取 top-1。矩阵留空即自动。
- **C 退役推荐**：删 `/recommend`、`/recommend/apply`、`/recommendations` 端点 + 前端「生成推荐/一键应用」按钮/网格/模态/JS。模式测试(`/test/*`)保留——现改喂调度器。
- **D 干净切换**：备份 admin 23 条矩阵 + 9 个 DC_MODEL_ 到 `data/ai_role_config_backup_pre_scheduler.json`；清空矩阵；注释 `.env` 的 `DC_MODEL_*` 影子配置（[[project_ai_config_unified]] 红线）。切换后全部角色由调度器接管：L1_macro→[kimi,deepseek]、bottleneck→3 个多样化、委员会 4 员靠角色默认保持 deepseek/qwen/kimi/glm 多样性。
- **可覆盖不变**：手动矩阵 UI 保留——需固定某角色用某模型时手填即覆盖调度器（"默认智能、用户可覆盖"）。
- **文件**：`health.py`、`factory.py`、`ai_config_api.py`、前端 `index.html`/`ai-config.js`、`.env`（DC_MODEL 注释）。

### Phase 3（最后）— 统一两套容错
- **目标**：把 `committee._invoke_with_retry` 的"同模型瞬态退避重试"并入统一策略层，删除并行实现。
- **风险最高**：迁移不彻底会回退投委会稳定性。**必须带投委会回归测试，单独一步做。**

---

## 十、通知与可观测（用户通告，两者都做）

- **实时切换提示**：沿用 `push_notice`——每次自动切换弹"已自动替换为 X（原因：限流）"；免费回落付费时强提示"已临时启用付费模型 X"。
- **本次使用清单**：请求结束 `drain_notices` 追加"本次各岗位模型：拆解=deepseek、瓶颈=qwen/kimi/glm、投委会=…"。
- **健康仪表盘 + 输出曲线**：`/model-usage` 面板展示各模型可用性/成功率/延迟/成本/熔断状态、切换次数，以及**长期平均表现曲线**（把静态评级面板升级成实时调度看板）。

---

## 十一、风险与缓解

| 风险 | 缓解 |
|---|---|
| **冷启动无数据** → 早期≈静态链 | `rank_candidates` 退化为现顺序 + `role_registry` 能力档位先验；这是**特性不是缺陷**（平滑过渡） |
| per-call 高频写撞 SQLite `_write_lock` | 内存滑窗聚合 / 批量异步落盘，绝不每次同步写 |
| 进程内熔断多 worker 不共享 | 接受最终一致；需要时再落 SQLite/Redis（勿一开始上分布式） |
| 免费回落付费=真花钱 | 显式确认策略 + 强通知 |
| 流式首 token 后不可 failover | 开流前用健康度选型前移失败概率，不根治 |
| fan-out 被排序收敛破坏多样性 | 硬护栏：每槽独立选、槽间 provider 不重复 |
| 统一容错删 committee 老路的回归 | 放最后 + 投委会回归测试 |
| `OutputValidator` 误杀短答 | 仅 JSON/评分角色 + 保守 + 可按角色关 |

---

## 十二、"自然平滑过渡"如何保证（核心理念落地）

用户强调：系统要**自然正常运行、平滑过渡、随数据积累逐渐发挥作用**。本设计通过四条机制保证：

1. **上线即不劣于现状**：无遥测 → `rank_candidates` 退化为现静态顺序；无策略 → 等权；矩阵可留空但手填仍优先。任何阶段第一天的行为都 ⊇ 现状。
2. **数据驱动渐显**：Phase 0 先默默采集，排序质量随样本积累单调提升，无需"切换开关"式的突变。
3. **灰度可回退**：feature flag 按用户开启；每阶段独立上线、随时关回现状。
4. **决策留人**：默认智能但用户可覆盖，主模型有加成粘性，花钱路径强提示——自动化不夺走用户的控制感。

> **一句话**：先补数据（Phase 0）、再上排序+熔断（Phase 1 拿 80% 价值）、策略与看板按需长出（Phase 2）、最后统一容错（Phase 3）。不预建大调度器，让它随真实痛点和数据一件件长出来。
