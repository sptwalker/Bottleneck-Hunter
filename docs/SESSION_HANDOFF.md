# 会话交接记录 — BottleneckHunter（用于更换对话后接续）

> 新对话开始时：先读本文件 + 记忆 `project_model_scheduler.md`。项目路径 `c:\Users\walker\Documents\walker\Vibecode\Bottleneck-Hunter`。
> 本会话主线：**AI 模型智能调度系统**（取代静态分配）。设计文档 `docs/MODEL_SCHEDULER_DESIGN.md`。

---

## 一、当前系统状态

- **服务器**：`http://127.0.0.1:8000` 后台运行中。启动命令**必须** `bottleneck-hunter serve`（`python -m web.app` 不启动）。
  - 重启前先杀 :8000：`pid=$(netstat -ano | grep LISTENING | grep ":8000 " | awk '{print $NF}'); powershell -Command "Stop-Process -Id $pid -Force"`
  - 后台起：`(bottleneck-hunter serve >/tmp/bh.log 2>&1 &)`，等 `curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8000/` 返回非 000。
- **后端/schema 改动需重启**才生效；**前端静态资源只需浏览器硬刷新 Ctrl+Shift+R**（no-cache 已完整覆盖 index.html + 所有 JS/CSS，见 app.py:248-303，无 Service Worker）。
- **主线已全部提交推送**（`git status` 干净，`main` = `origin/main`）。本会话最后提交 `c768bbe`。
- **数据库绝不清空**：所有 schema 改动（`model_call_stats`/`ai_routing_policy` 表、`custom_providers.is_primary` 列）都是**幂等自动迁移**（启动时自动完成，已迁好）。清空 = 丢所有用户加密 Key/观察池/分析历史/调度遥测。
- **Feature flags**：`BH_SCHEDULER_RANK=0` 关调度排序回静态；`BH_SCHEDULER_VALIDATE=0` 关输出格式校验。默认都开。

## 二、本会话已完成（智能调度系统，全部上线 + 审查验收）

**核心成果：用数据驱动的智能调度取代静态角色→模型分配。** 关键洞察——调度所需基建约七八成已存在（FallbackChatModel/model_ratings/classify_reason/校准scheduler），缺的是排序函数+遥测+熔断。设计理念：**自然平滑过渡**，无数据时退化为现状，随使用数据积累越来越准。

分阶段（全部完成）：
1. **Phase -1 主要/禁用**（`custom_providers.is_primary`/`is_active`）：admin 可设全局主要模型/禁用模型，禁用时联动替换角色配置。`auth/store.py`、`custom_provider_api.py`、`factory.py`。
2. **Phase 0 遥测地基**：`model_call_stats` 表（按 日期×用户×provider×model×角色 聚合，仿 datasource_stats）；`FallbackChatModel` 四路径旁路 `record_model_call`。纯增量零行为变更。`store_schema.py`、`store_ai_models.py`、`fallback.py`。
3. **Phase 1 动态排序+熔断**：`llm_clients/health.py`（`ProviderHealth` 进程内熔断按(user,provider)隔离；`rank_providers` 按健康度×可靠性+主模型加成排序；**熔断是最终乘子 ×0.05 压过一切加成**——W1审查修的高危bug）。接入 `build_fallback_candidates` + `get_models_for_role` 优先级4。无数据退化为原顺序。
4. **Phase 2 策略+格式校验+看板**：`llm_clients/validate.py`（角色无关内容启发式，只判空/伪JSON/拒答，raw_decode 接受JSON+尾注）；`ai_routing_policy` 表（每用户 免费/付费·质量/价格，全局默认+角色覆盖）；免费全熔断→自动回落付费+强提示；`drain_usage` 使用清单；`/model-usage`+`/routing-policy` 端点；前端「🧭 调度看板」页签。
5. **Phase 2.5 干净切换**：模式测试能力分喂进 `rank_providers`（`load_capability_scores`，补质量维度、取代"推荐→冻结矩阵"，**推荐端点/前端已删**）；多槽 fan-out（L1_macro/bottleneck）优先级4 自动选 N 个多样化 provider；**已清空 admin 矩阵 + 注释 .env 的 DC_MODEL_ 影子配置**（备份 `data/ai_role_config_backup_pre_scheduler.json`），全部角色由调度器接管，手动矩阵仅可选覆盖。
6. **验收审查修正**（`/code-review`+多智能体对抗核验）：3高危（熔断被加成击穿/优先级3不查熔断/看板esc未定义）+5中（DC_MODEL退役/末候选开熔断/跨用户遥测泄漏/免费提示误报/JSON误杀）+死代码清理。零回归。

**收尾迭代（S1-S3 + 配置UX + 更新记录）：**
- **S1** 认证测试夹具：4 个 API 测试文件注入已登录用户，52→20 失败（剩 20 是既有交易执行/feedback 问题，真·基线就有，与调度无关）。
- **S2** 调度看板增强：`/model-assignments` 端点（各角色实际选型+source标注，asyncio.to_thread）；前端「当前各角色实际选型」+告警条；**新建 `tests/test_model_scheduler.py`**（补审查指出的"调度器无覆盖测试"缺口，24项）。
- **S3** 模式测试月度自动化：`job_model_capability_refresh`（每月1号，仅过期模型>45天，每次≤10，多重成本护栏：全局总开关fail-closed+用户开关+预算门控+封顶）。`scheduler.py`+`schedule_config.py`（monthly 触发器）。
- **S4 已评估跳过**：非投委会角色的 outcome 无客观标准（催化剂 judge 是价格代理启发式、L1/拆解无 ground truth），硬接会给调度加噪声。模式测试（受控基准）才是干净的质量信号，已够用。
- **配置中心 UX 第一批**：首屏引导卡（未配 Key 时引导+高亮国内免费模型）+ 已就绪状态条 + 模型卡状态可视（已连接/未配置+健康点+免费徽标）+ 空状态黑洞修复。加 `_rolesLoaded` 门闩防竞态误弹。`ai-config.js`/`index.html`/`aiconfig.css`。
- **首页更新记录自动生成**：`scripts/gen_update_history.py`（从 commit 的 `📢 标题|说明` 提取，合并现有、按 日期+标题 去重）+ `.githooks/post-commit`（文件锁防递归+rebase护栏+rev-parse定位）。**已自举验证端到端**。

## 三、未完成 / 下一步（按优先级）

1. **启用更新记录 hook**（每台开发机一次）：`git config core.hooksPath .githooks`。之后 commit 里写 `📢 白话` 就自动上首页。见 `.githooks/README.md`。
2. **配置中心 UX 第二批**（设计已在 `docs/MODEL_SCHEDULER_DESIGN.md` 之外的对话里，用户倾向"高价值低风险渐进"）：页签重组 6→2 主题（AI 模型 / 数据源）、"AI 模型分配"降级为"高级折叠"、术语改名（Provider→AI 模型）。**动结构、风险较高，需先和用户确认范围**。
3. **S5 / Phase 3 统一两套容错**（最高风险）：把 `committee._invoke_with_retry` 的同模型退避重试并入统一策略层，删并行实现。**必须带投委会回归测试**（`test_decision_8b2` TestCommittee）。
4. **S6 生产硬化**（按需，多worker/高并发才需）：C1 熔断跨进程持久化、C2 遥测异步缓冲、C3 provider tier 标注 UI。
5. **既有 20 个测试失败**（非本线引入）：`test_decision_8b3` TestTradeExecutor（交易执行缺市价快照守卫）+ `test_decision_8b4` feedback 404。真·基线 25e871b 就有，属交易/决策模块历史维护，另议。

## 四、关键约束（改调度相关代码必读）

- **严格 Key 按用户隔离**（`project_strict_key_isolation`）：熔断/评分/遥测/能力分全部按 user_id，**绝无全局共享表**。`_load_stats` 空 user_id 返回空（不借跨用户聚合）。
- **fan-out 多样性红线**：role 内多槽（L1_macro 2/bottleneck 3）用 `seen_prov` 强制槽间 provider 不重复；委员会 4 员是独立单模型角色，多样性靠角色默认（deepseek/qwen/kimi/glm）+ 用户各配 Key，缺 Key 时退化不保证（文档 §八.1 已如实说明）。
- **不加影子配置**（`project_ai_config_unified`）：DC_MODEL_ 已退役；统一走 AI 配置中心。
- **流式首 token 后不可切**：调度只能开流前用健康度选型。
- **测试遥测隔离**：`_record_call` 在 pytest 下不写真实库（防假模型污染）；health 是内存态可测。

## 五、通用运维/风格约定（跨会话长期有效）

- **前端库禁用公共 CDN**（jsdelivr/unpkg/cdnjs/staticfile 国内全不可达），必须本地 vendor，用 `registry.npmmirror.com` 下载（记忆 `project_frontend_cdn_blocked`）。
- **ruff**：`B008`(FastAPI `Depends()`) 和 `I001` 是全项目既有风格，新代码只剩这些可接受；不要为清既有 ruff 债扩大 diff。
- **TodoWrite 有环境 bug**：status 字段常被注入 `\r` 导致校验失败——可少用/忽略，用 plan 文件跟踪进度更稳。
- **平台**：Windows（Git Bash + PowerShell）。控制台 GBK，打印 emoji/中文到 stdout 会 UnicodeEncodeError——脚本内部 utf-8 读写没事，只是别 print emoji 到控制台。
- 用户偏好**中文**输出；反感 over-promise（验收要真实数据证明，别用代码自证）；风格 lazy/精简（ponytail）；大改动前先规划/确认范围，不擅自扩大。

## 六、参考

- 设计文档：`docs/MODEL_SCHEDULER_DESIGN.md`（12 节，锁定决策 + 数据模型 + 分阶段 + 硬护栏）
- 记忆：`project_model_scheduler`、`project_strict_key_isolation`、`project_ai_config_unified`、`project_server_start_command`、`project_frontend_cdn_blocked`、`feedback_chinese_output`
- 核心文件：`llm_clients/health.py`（调度大脑）、`factory.py get_models_for_role`（四级优先）、`fallback.py`（执行+遥测+校验）、`validate.py`、`web/decision_api.py`（看板端点）、`watchlist/scheduler.py job_model_capability_refresh`
- 调度器测试：`tests/test_model_scheduler.py`（24项）、`tests/test_update_history.py`（14项）
