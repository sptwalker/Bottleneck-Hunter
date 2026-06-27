# BottleneckHunter 开发计划

> 基于 Serenity 的"产业链拆解 → 供应商检索 → 交叉验证"方法论

---

## 一、总体目标

构建独立的**产业链瓶颈选股系统**，核心能力：
1. 自动拆解任意产业链 3+ 层深度，识别"卡脖子"瓶颈环节
2. 沿瓶颈环节检索 A 股/美股中被忽视的供应商
3. 多模型交叉验证投资逻辑的稳健性

---

## 二、开发阶段规划

### Phase 1：产业链知识图谱与拆解引擎 ✅ (骨架已完成)

**目标**：构建产业链结构化数据，支持从终端产品到上游原材料的逐层拆解

#### 1.1 产业链数据模型 (`bottleneck_hunter/chain/models.py`) ✅

已定义的数据结构：
- `IndustryNode`：产业链节点（如"光模块"、"磷化铟衬底"）
- `ChainLink`：上下游关系（含依赖度、可替代性评分）
- `ChainGraph`：完整产业链图（有向无环图）
- `BottleneckReport` / `BottleneckScore`：瓶颈分析结果
- `SupplierInfo` / `SupplierScorecard`：供应商信息与评分
- `CrossValidationReport` / `ModelValidation`：交叉验证结果
- `ScreeningResult`：最终选股结果

#### 1.2 LLM 驱动的产业链拆解 (`decomposer.py`) ✅

- 用户输入终端产品（如"GPU"），LLM 逐层向上拆解 3 层以上
- 每层输出：零部件名称、功能描述、关键参数、依赖关系
- 支持预设产业链模板（减少 LLM 调用，提高一致性）
- 结果存储为结构化 JSON，可复用

#### 1.3 瓶颈识别算法 (`bottleneck.py`) ✅

对产业链每个环节评分：
- **稀缺性**（0-10）：供应商数量、市场集中度
- **不可替代性**（0-10）：是否存在替代技术/材料
- **供需缺口**（0-10）：当前及预测供需比
- **定价权**（0-10）：涨价能力、定价权
- **技术壁垒**（0-10）：专利、know-how、认证周期

综合得分 = 加权平均，自动排序输出 Top-N 瓶颈环节

#### 1.4 预设产业链数据 ✅

已创建的产业链模板：
- `gpu_chain.json` — GPU/AI算力产业链
- `robot_chain.json` — 人形机器人产业链
- `aerospace_chain.json` — 商业航天产业链

---

### Phase 2：供应商检索与筛选系统 ✅ (已完成)

**目标**：针对每个瓶颈环节，自动检索并筛选被忽视的优质供应商

#### 2.1 供应商检索工具 (`supplier_search.py`)

新增数据获取工具：
- **A 股**：通过 AKShare 检索同板块/概念的公司列表
  - 按行业分类、概念板块（如"光模块概念"、"磷化铟概念"）
  - 筛选条件：市值 < X 亿、机构持仓比例低、关注度低
- **美股**：通过 yfinance 检索同行业公司
  - 按 sector/industry 分类
  - 筛选条件：Market Cap < $1B、低分析师覆盖

#### 2.2 供应商评估模型 (`supplier_eval.py`)

对候选供应商逐项评估：
- **市场地位**：市占率、是否垄断/寡头
- **客户验证**：是否已有大客户订单/验证
- **产能状况**：产能利用率、扩产计划
- **财务健康**：营收增速、毛利率、现金流
- **估值水平**：PE/PB 相对行业均值偏离

输出 `SupplierScorecard`（结构化评分卡）

---

### Phase 3：多模型交叉验证系统 ✅ (已完成)

**目标**：用多个 LLM 从反面角度拷问投资逻辑，提升判断可靠性

#### 3.1 交叉验证框架 (`cross_validation.py`)

- 支持配置 N 个不同 LLM（如 GPT + Claude + DeepSeek）
- 每个模型从**反面角度**独立审查候选标的：
  - 这个稀缺性是真的唯一吗？
  - 技术会不会被替代？
  - 产能是否真的不足？
  - 客户验证是否可靠？
  - 有无地缘政治风险？
- 汇总多模型意见，生成 `ValidationReport`
- 只有通过多数模型验证的标的才进入最终推荐

---

### Phase 4：选股工作流与报告输出 ✅ (已完成)

#### 4.1 LangGraph 工作流 (`graph.py`) ✅

```
用户输入（产业方向）
    ↓
产业链拆解 (decompose_step)
    ↓
瓶颈识别 (bottleneck_step)
    ↓
供应商检索（Phase 2）
    ↓
交叉验证（Phase 3）
    ↓
最终推荐报告
```

#### 4.2 报告生成 (`report.py`) ✅

支持中文/英文 markdown 报告输出。

---

### Phase 5：CLI 集成 ✅ (已完成)

#### 5.1 CLI 入口 (`cli.py`) ✅

- 交互式选择产业链方向（预设 + 自定义）
- 配置拆解深度、Top-N、语言
- 配置 LLM provider/model
- 自动保存 markdown 报告到 output/

---

## 三、技术实现要点

### 3.1 项目结构

```
BottleneckHunter/
├── bottleneck_hunter/
│   ├── __init__.py
│   ├── cli.py                  # CLI 入口
│   ├── chain/
│   │   ├── models.py           # 数据模型
│   │   ├── decomposer.py       # 产业链拆解引擎
│   │   ├── bottleneck.py       # 瓶颈识别算法
│   │   ├── graph.py            # LangGraph 工作流
│   │   ├── report.py           # 报告生成器
│   │   ├── supplier_search.py  # [Phase 2] 供应商检索
│   │   ├── supplier_eval.py    # [Phase 2] 供应商评估
│   │   ├── cross_validation.py # [Phase 3] 交叉验证
│   │   ├── prompts/            # LLM 提示词
│   │   └── data/               # 预设产业链 JSON
│   ├── llm_clients/
│   │   └── factory.py          # LLM 客户端工厂
│   └── dataflows/              # [Phase 2] 数据获取
├── tests/
├── pyproject.toml
├── .env.example
└── PLAN.md
```

### 3.2 依赖

- langgraph / langchain — 工作流编排
- pydantic — 数据模型
- networkx — 产业链图结构（后续可选用）
- yfinance / akshare — 市场数据
- rich / questionary / typer — CLI

### 3.3 关键设计决策

| 决策点 | 选择 | 理由 |
|--------|------|------|
| 产业链存储格式 | JSON（Pydantic 模型） | 兼顾可读性和类型安全 |
| 瓶颈评分方式 | LLM + 规则混合 | LLM 做定性分析，规则做定量约束 |
| 供应商数据来源 | AKShare（A 股）/ yfinance（美股） | 各市场最全面的数据源 |
| 交叉验证模型数 | 3-5 个可配置 | 平衡成本和验证质量 |
| 工作流实现 | LangGraph StateGraph | 与 TradingAgents 架构一致 |

---

## 四、开发优先级与里程碑

| 优先级 | 阶段 | 状态 | 核心交付物 |
|--------|------|------|-----------|
| P0 | Phase 1.2 - 产业链拆解 | ✅ 骨架完成 | LLM 驱动的产业链拆解引擎 |
| P0 | Phase 1.3 - 瓶颈识别 | ✅ 骨架完成 | 瓶颈评分算法 + 报告 |
| P0 | Phase 2.1 - 供应商检索 | ✅ 已完成 | A 股/美股供应商检索工具 |
| P1 | Phase 2.2 - 供应商评估 | ✅ 已完成 | 供应商评分卡 |
| P1 | Phase 3.1 - 交叉验证 | ✅ 已完成 | 多模型交叉验证框架 |
| P2 | Phase 4 - 工作流完善 | ✅ 已完成 | 集成供应商+验证的完整流程 |
| P3 | Phase 5 - CLI 完善 | ✅ 已完成 | 完整的 CLI 选股体验 |
| P3 | Phase 1.1 - 预设数据 | ✅ 3个已创建 | 产业链模板库持续扩充 |
| P0 | Phase 6 - Wizard UI | ✅ 已完成 | 4-Phase Wizard + 圆桌会议 |
| P0 | Phase 7A - 技术债务 | ✅ 已完成 | JSON 统一 + 缓存 TTL + 超时保护 |
| P0 | Phase 7B - 测试补充 | ✅ 已完成 | 74 个新测试，165 项全部通过 |
| P1 | Phase 7D - 体验优化 | ✅ 已完成 | 真流式 + 断连重试 + 模块拆分 |
| P0 | Phase 8A - 观察池骨架 | ✅ 已完成 | 观察池 CRUD + 数据管道 + 策略大脑系统 |
| P0 | Phase 8B.1 - L1/L2 决策引擎 | ✅ 已完成 | 宏观策略 + 组合策略 + 催化剂监控 + 14张DB表 + API端点 |
| P0 | Phase 8B.2 - L3/L4 + 投委会 | ✅ 已完成 | 战术引擎 + 执行引擎 + 4成员投委会 + 完整流程串联 + 26项测试 |
| P0 | Phase 8B.3 - 交易执行 + 前端 | ✅ 已完成 | 模拟交易执行 + 决策中心前端UI + 仓位管理 + AI模型配置 |
| P1 | Phase 8B.4 - 闭环反馈 | ✅ 已完成 | 交易复盘 + 经验卡片 + 催化剂时效 + 反馈闭环 |
| P1 | Phase 8B.5 - 集成测试 + 调优 | ✅ 已完成 | L1/L2/E2E/Budget/数据收集 24 项测试，全套 264 项通过 |
| P1 | Phase 8C - 复盘系统 | ✅ 已合并至 8B.4 | — |
| P1 | Phase 9A - 绩效报告与调优系统 | ✅ 已完成 | 绩效统计+调优建议+前端仪表盘，15 项测试，全套 279 项通过 |
| P1 | Phase 9B - 代码质量优化 | ✅ 已完成 | CSS 模块化(7237→9模块+索引)、JS 拆分(phases.js→4子模块)、DeprecationWarning 修复 |
| P1 | Phase 9C - A股深度适配 | ✅ 已完成 | 观察池双市场支持：price_pipeline(akshare)、news_pipeline(中文新闻)、scheduler(6任务UTC双市场)、store(get_tickers_by_market) |

| P0 | Phase 10A - 测试加固 | ✅ 已完成 | 4 个测试文件(store/price/news/scheduler)，40 项新测试，全套 319 项通过 |
| P0 | Phase 10B - 决策引擎市场分化 | ✅ 已完成 | 8个提示词注入{market_context}、notice_pipeline(A股公告)、committee市场感知、23项新测试，全套342项通过 |
| P0 | Phase 10C - 前端体验优化 | ✅ 已完成 | 市场筛选器、批量tier操作、笔记编辑、checkbox列，全套342项通过 |
| P0 | Phase 11A - 统一重试框架 | ✅ 已完成 | retry.py(@with_retry装饰器+fetch_with_timeout)、5条管道改造(price/news/sec/options/notice)、9项新测试，全套351项通过 |
| P0 | Phase 11B - 管道健康追踪 | ✅ 已完成 | scheduler精细化状态(partial/error/success)、store.get_stale_tickers()、/health API端点、7项新测试，全套358项通过 |
| P0 | Phase 11C - 前端错误展示 | ✅ 已完成 | 统一Toast组件(替换所有alert)、管道partial状态+hover错误详情、数据过期告警、刷新结果精准反馈 |
| P0 | Phase 12A - Semaphore延迟初始化 | ✅ 已完成 | 5条管道_SEM改为_get_sem()延迟创建，避免跨事件循环崩溃，16项新测试 |
| P0 | Phase 12B - httpx客户端复用 | ✅ 已完成 | retry.py新增get_http_client()共享连接池，news/sec管道改用复用客户端，app.py shutdown关闭，2项新测试 |
| P0 | Phase 12C - scheduler异常日志 | ✅ 已完成 | shutdown_scheduler异常从静默pass改为logger.warning |
| P0 | Phase 12D - 前端健壮性 | ✅ 已完成 | 批量删除API(/batch-delete)、Promise.all加.catch()、搜索debounce(250ms)、策略缓存5分钟TTL |
| P1 | Phase 13A - test_bottleneck.py | ✅ 已完成 | 瓶颈评分算法测试：权重验证、加权计算、上下文构建、分析流程（含排序/失败追踪/进度回调），15项 |
| P1 | Phase 13B - test_graph.py | ✅ 已完成 | ChainGraph遍历测试：节点查找、层过滤、上下游导航、三层链/菱形依赖，14项 |
| P1 | Phase 13C - test_strategy_engine.py | ✅ 已完成 | 策略信号解析测试：bullish/bearish/neutral提取、信心评分（含越界夹紧）、多空论据、条件/风险/目标解析、差异比较，19项。附带修复_compute_strategy_diff的None参数bug |
| P1 | Phase 13D - test_watchlist_api.py | ✅ 已完成 | API端点契约测试(TestClient)：CRUD完整链路(list/add/get/update/delete)、批量删除/批量tier、容量限制(409)、子资源404，23项 |
| P0 | Phase 14 - 决策自动调度闭环 | ✅ 已完成 | 美股4任务定时调度(daily_decision/catalyst_scan/weekly_strategy/auto_review)、_drain_sse消费器、BudgetTracker三级降级、卖出自动复盘、21项新测试(test_scheduler_jobs)、13项新测试(test_trade_executor) |
| P0 | Phase 15 - UZI测试+A股决策调度 | ✅ 已完成 | UZI Runner 44项测试覆盖(从0到100%)、A股4任务决策调度(cn_daily_decision/catalyst_scan/weekly_strategy/auto_review)、前端调度栏双市场分组显示 |
| P0 | Phase 16A - 认证基础设施 | ✅ 已完成 | JWT HttpOnly cookie认证、AuthStore(users/invite_codes/system_config三表)、登录注册页、AuthMiddleware ASGI中间件、默认admin/admin账户 |
| P0 | Phase 16B - 数据隔离 | ✅ 已完成 | WatchlistStore.for_user()工厂+_user_filter()注入、AnalysisStore user_id参数、30+张表加user_id列、老数据迁移归属admin、Scheduler多用户遍历 |
| P0 | Phase 16C - Per-User API KEY | ✅ 已完成 | AES-256-GCM加密存储(user_api_keys表)、用户KEY→.env全局KEY fallback链、Settings面板11 provider配置、KEY hint显示(sk-...xxxx) |
| P0 | Phase 16D - 管理员后台 | ✅ 已完成 | 用户管理(冻结/解冻/删除+级联清理)、邀请码管理(批量生成/作废)、系统配置(开放注册/默认上限)、数据统计、admin.js/admin.css前端面板 |
| P0 | **系统全面评审** | ✅ 已完成 | 5维度深度审计(决策引擎/数据管道/产业链/风控/前端)，38个问题，综合评分5.2/10，详见 [评审报告](docs/SYSTEM_AUDIT_REPORT.md) |
| P0 | Phase 17A - 数据基础修复 | ⬜ 待开始 | market_snapshots加market列、宏观数据接入L1(VIX/美债/DXY/北向资金)、Form 4真实解析、A股基本面增强、机构持仓+分析师评级 |
| P0 | Phase 17B - 技术缺陷修复 | ⬜ 待开始 | L4约束硬验证引擎、夏令时动态处理、SQLite连接安全加固、SSE自动重连、composite_score实际生效 |
| P0 | Phase 17C - 风控量化体系 | ⬜ 待开始 | 回测框架(Sharpe/Sortino/MaxDD/基准对比)、组合风控(VaR/Beta/HHI/相关性矩阵)、仓位算法(Kelly/波动率缩放/风险平价) |
| P1 | Phase 17D - 闭环反馈贯通 | ⬜ 待开始 | 模拟交易流程验证、自动复盘链路修复、经验卡片生成应用、催化剂结果判定、用户偏好学习 |
| P1 | Phase 17E - 规则引擎优化 | ⬜ 待开始 | 可投性过滤(TAM/客户数/毛利率/流动性门槛)、分行业瓶颈权重、LLM评分规则化+锚定、产业链版本管理 |
| P2 | Phase 17F - 体验与可视化 | ⬜ 待开始 | 决策链路追溯、风险仪表盘、催化剂日历、A/B对比分析 |

**当前版本**：60+ Python 模块，32 测试文件，530+ 项测试全部通过

**下一步**：Phase 17A 数据基础修复，详见 [改进完善计划](docs/IMPROVEMENT_PLAN.md)

---

## Phase 6：Wizard UI 重构与 4-Phase 流水线 ✅ (已完成)

**目标**：将一体化选股流程拆分为 4 个可独立刷新的步骤，配合全新 Wizard 式前端

### 6.1 整体架构

```
起始页（选赛道/历史）
  ↓
Phase 1: 产业链瓶颈（SSE） → 拆解 + 瓶颈评分
  ↓
Phase 2: 入围筛选（SSE）   → 搜索 + 评估 + Alpha + 分层入围 + 手动入选
  ↓
Phase 3: 最终评分（即时+AI） → 几何均值排名 + 横向对比图表 + AI 分析报告
  ↓
Phase 4: 交叉验证（SSE）   → 多模型对抗验证 + AI 圆桌会议（下阶段）
```

级联清除规则：刷新 Phase N → 清除 Phase N..4，保留 1..N-1

### 6.2 已完成 ✅

| 模块 | 说明 |
|------|------|
| `chain/models.py` | FinalScore, FinalScoredCompany 数据模型 |
| `chain/supplier_eval.py` | FinalScorer 几何加权均值评分（quality^w_q × alpha^w_a） |
| `web/phase_cache.py` | 服务端内存缓存（analysis_id → phase 结果） |
| `web/streaming.py` | stream_phase1/2/4 SSE 异步生成器 |
| `web/api.py` | Phase1-4 API 端点 + 缓存读取端点 |
| `tests/test_final_scorer.py` | 10 项单元测试 |
| 前端 JS 骨架 | phases.js + phase-views.js + app.js 路由 |

### 6.3 Step A: Wizard 前端重构（当前）

基于确认的原型 v3（`static/prototype.html`）重新实现前端。

**起始页：**
- 自定义赛道按钮组（文字按钮，最多 8 个），右键配置参数（市场/产业方向/终端产品）
- 全自动分析模式：载入历史数据后一键按当前设置自动完成所有 Phase
- 自定义输入表单：市场/产业方向/终端产品/市值上限
- 历史记录完整表格：市场/产业方向/终端产品/市值规模/分析层数/主分析模型/入围供应商数量/日期
- 赛道配置存储到 localStorage，支持增删改

**Phase 1 — 瓶颈分析：**
- 先设置参数（深度/TopN/市场/市值/LLM），点击"开始分析"再启动
- 产业链图谱保留三图切换（力导向/径向树/D3 Force）
- 图谱窗口内叠加分析进度（spinner + 逐层拆解详细信息）

**Phase 2 — 分层入围 + 手动入选：**
- 按分析层数（子部件层L1/零件层L2/原料层L3）分别设置入围数量
- 表格增加 checkbox 列，手动勾选不超过 3 家加入最终评分
- 表格完整展示五维评分列（地位/客户/产能/财务/估值）+ 综合 + Alpha + 趋势 + 聪明钱 + 护城河
- 所有公司名标注层级标记（L1/L2/L3/L4 彩色 badge）
- 点击可展开行内企业简介和核心产品信息
- 后端 ShortlistConfig 新增 `per_layer_top_n: dict[str, int]` + `manual_picks: list[str]`

**Phase 3 — 综合分析页：**
- 即时部分（拖动滑块实时更新）：排名表 + 4 张对比图表
  - 散点图（质量 vs 预期差，气泡=最终分）
  - 五维雷达对比图（地位/客户/产能/财务/估值）
  - 分组柱状图（各公司 quality/alpha/趋势/聪明钱）
  - 堆叠柱状图（Alpha 因子拆解：瓶颈×信息差+催化剂+趋势+聪明钱）
- AI 报告部分（按钮触发，SSE 流式）：横向对比分析评价
  - 权重变化后提示"排名已更新，是否重新生成报告？"
  - 报告缓存，相同权重不重复生成
- 大尺寸公司详情抽屉（720px 宽，点击行展开），完整覆盖旧系统全部 10 个 section：
  1. 评分概览（最终分/质量分/预期差）
  2. 企业简介
  3. 核心产品（tags）
  4. 五维评分（雷达图 + 条形图）
  5. 优势与风险
  6. 财务快照（8 项指标 + 机构评级/研报覆盖/预期PE/预期EPS）
  7. 财务趋势（加速度/毛利率趋势/连续增长季数）
  8. 瓶颈定位（5 维瓶颈评分）
  9. 竞争护城河（4 维 + 分析要点）
  10. 聪明钱信号（评分/方向/明细）
  11. 催化剂时间线（类型/日期/置信度/影响力）
  12. 预期差分析（6 项因子 + 推理说明）

**Phase 4 — 交叉验证增强：**
- 每家公司可展开详细面板，包含：
  - 10 分制多维评分表（技术壁垒/市场地位/财务健康/成长潜力/估值合理性/风险可控性 × 各AI）
  - 各 AI 独立评语（优势 + 风险点）
- 所有公司名标注层级标记
- 底部预留 AI 圆桌会议区（下阶段开发，含模拟预览）

**全局规范：**
- 所有公司名称后附带层级标记（L1/L2/L3/L4 彩色 badge）
- 数据字段完整覆盖旧系统（dashboard.js 详情抽屉 10 个 section 全部迁移）

### 6.4 Step B: 后端 API 补充

| 端点 | 说明 |
|------|------|
| `POST /api/phase2` | 新增 `per_layer_top_n` 和 `manual_picks` 参数支持 |
| `POST /api/phase3/report` | 新增 AI 横向分析报告生成（SSE 流式） |
| `GET /api/phase3/report/{id}` | 获取已缓存的 AI 报告 |
| `GET /api/history` | 返回完整历史字段（市场/层数/模型/入围数） |
| `POST /api/auto-run` | 前端已实现（自动模式顺序触发 Phase 1-4，无需后端端点） |
| `GET /api/sectors` | 前端已实现（localStorage 管理赛道配置，无需后端端点） |

### 6.5 Step C: 集成测试 + 部署

1. 端到端 4-Phase 流程测试（真实 LLM）
2. 分层入围 + 手动入选验证
3. AI 报告生成 + 缓存验证
4. 抽屉详情数据完整性验证（对照旧系统 10 个 section 逐项核对）
5. 级联清除 + 断点续跑验证
6. 全自动分析模式 E2E 测试
7. 自定义赛道 CRUD + localStorage 持久化测试

### 6.6 Step D: AI 圆桌会议（下阶段子模块）

**架构设计（待详细设计）：**

```
POST /api/phase4/meeting — 启动会议（SSE 流式）

会议流程：
  主持人（专用 prompt）控制议程
    ↓
  Round 1: 各 AI 独立发表对 Top-N 排名的看法
    ↓
  Round 2-4: 互相提问 + 质疑 + 回应（上下文传递）
    ↓
  Round 5: 主持人总结共识 + 分歧 + 最终排名
```

关键设计点：
- 主持人 LLM 负责议程控制，从参会 AI 的回复中提取问题转发
- 每轮对话上下文累积传递，模拟真实讨论
- 前端聊天气泡 UI，SSE 逐条推送发言
- 10 分钟超时保护，最多 5 轮 + 总结
- 输出结构化结论：推荐排名 + 风险排名 + 共识度 + 主要分歧点

---

## Phase 7：质量加固与体验优化 ✅ (已完成)

### 7A 技术债务清理 ✅

| 任务 | 说明 |
|------|------|
| `chain/json_utils.py` | 统一 6 处重复的 JSON 提取逻辑为 `extract_json_object` / `extract_json_array` |
| `web/phase_cache.py` | 增加 TTL (4h) + LRU (50 条) 淘汰机制 |
| `web/streaming.py` | 圆桌会议全局超时保护 (600s) |

### 7B 核心测试补充 ✅

新增 74 个单元测试，覆盖 5 个此前零覆盖的核心模块：

| 测试文件 | 目标模块 | 测试数 |
|---------|---------|--------|
| `test_roundtable.py` | roundtable.py | 22 |
| `test_cross_validation.py` | cross_validation.py | 11 |
| `test_catalyst.py` | catalyst.py | 11 |
| `test_smart_money.py` | smart_money.py | 16 |
| `test_meeting_data.py` | meeting_data.py | 15 |

全部 165 个测试通过。

### 7D 体验优化 ✅

| 任务 | 说明 |
|------|------|
| AI 报告真流式 | `api.py` 的 `ainvoke` 改为 `astream` 逐 token 推送，不支持 stream 的模型自动降级 |
| SSE 断连重试 | `phases.js` 提取通用 `readSSEStream()` 工具函数，支持最多 3 次递增间隔重试 + phase_cache 恢复 |
| streaming.py 拆分 | 1,422 行拆为 `streaming/_common.py` / `legacy.py` / `phases.py` / `meeting.py` 四文件包 |
| PLAN.md 更新 | 修正 `/auto-run` 和 `/sectors` 为前端已实现，更新 Phase 6/7 完成状态 |
| 进度百分比 + ETA | Phase 2 供应商评估和催化剂分析步骤增加前端百分比进度条和预估剩余时间 |

---

## Phase 8：跟踪观察池与智能决策引擎

### 8.0 系统设计总览

**核心理念**：从"一次性筛选"进化为"持续跟踪 + 自适应决策"，三大模块形成闭环：

```
┌──────────┐     P4 勾选加入     ┌──────────┐    交易信号     ┌──────────┐
│  分析流程  │ ────────────────► │  观察池   │ ─────────────► │ 交易中心  │
│ Phase1-4  │                   │  跟踪+决策 │  用户确认执行   │ 模拟交易  │
└──────────┘                   └─────┬────┘               └────┬─────┘
                                     │                         │
                                     │◄────────────────────────┘
                                     │  复盘结果 + 经验卡片回流
                                     │  调优建议反馈
```

**前端导航结构**：

| 入口 | 说明 |
|------|------|
| 分析流程 | 现有 Phase 1-4 Wizard（P4 新增"加入观察池"按钮） |
| 观察池 | 独立页面：跟踪信息 + 决策建议 + 经验知识库 |
| 交易中心 | 独立页面：模拟账户 + 操作记录 + 预算监控 |

**设计约束**：
- 一期仅支持美股市场
- 观察池上限 24 只股票
- 数据源全部免费（yfinance + SEC EDGAR + News RSS）
- LLM 成本设日/月上限，超限自动降级
- 模拟交易半自动模式（系统建议 → 用户确认 → 执行）
- 不支持做空，一次性买入/卖出，简化版

### 8.1 数据模型

#### 观察池相关

```
watchlist（观察池）
├── id, ticker, name, market
├── source: "phase3" | "phase4" | "manual"   ← 来源
├── source_analysis_id: nullable              ← 关联的分析记录
├── tier: "focus" | "normal" | "track"        ← 重点(6)/一般(6)/潜力(12)
├── priority_score: float                     ← 自动排序分
├── added_at, last_updated
└── status: "active" | "pending_remove"

market_snapshots（每日快照）
├── ticker, date
├── price, volume, change_pct
├── volume_ratio, price_52w_high/low
└── technical_signals: JSON                   ← RSI/MACD 等

news_digest（每日新闻摘要）
├── ticker, date
├── raw_headlines: JSON
├── llm_summary: text
├── sentiment: float (-1 ~ +1)
└── key_events: JSON                          ← 结构化事件提取

sec_filings（SEC 文件监控）
├── ticker, filing_type (10-K/10-Q/8-K/Form4)
├── filed_date, url
├── llm_summary: text
└── significance: "high" | "medium" | "low"

earnings_reports（财报追踪）
├── ticker, quarter
├── revenue, eps, guidance
├── surprise_pct
└── llm_analysis: text

options_activity（期权异动）
├── ticker, date
├── unusual_volume_calls/puts
├── put_call_ratio
└── notable_trades: JSON

insider_trades（内部人交易）
├── ticker, date, insider_name, title
├── transaction_type: "buy" | "sell"
├── shares, price, value
└── significance_note: text
```

#### 决策引擎相关

```
composite_score（综合评分 — 每日更新）
├── ticker, date
├── fundamental_score: float                  ← 财务面
├── technical_score: float                    ← 技术面
├── catalyst_score: float                     ← 催化剂
├── sentiment_score: float                    ← 市场情绪
├── smart_money_score: float                  ← 聪明钱
├── overall_score: float                      ← 加权总分
├── signal: "strong_buy"|"buy"|"hold"|"reduce"|"sell"
├── llm_reasoning: text
└── confidence: float (0-1)
```

#### 交易中心相关

```
sim_account（模拟账户 — 用户可自定义初始资金和仓位上限）
├── initial_capital, current_cash
├── total_value, total_pnl, total_pnl_pct
├── max_position_pct
└── created_at

sim_positions（持仓）
├── ticker, shares, avg_cost
├── current_price, unrealized_pnl
└── weight_pct

sim_trades（交易记录）
├── id, ticker, action: "buy"|"sell"
├── shares, price, total_value
├── signal_source: JSON                       ← 触发信号
├── llm_reasoning: text                       ← 决策理由
├── status: "pending"|"executed"|"rejected"   ← 半自动确认
└── executed_at
```

#### 复盘系统相关

```
trade_review（交易复盘）
├── trade_id → sim_trades
├── entry_price, exit_price, holding_days
├── pnl, pnl_pct
├── what_went_right: text
├── what_went_wrong: text
├── lesson_learned: text
└── reviewed_at

experience_cards（经验卡片 — LLM 压缩的可复用知识）
├── id, scope: "global"|"sector"|"ticker"
├── scope_key: nullable
├── category: "pattern"|"lesson"|"rule"
├── content: text
├── evidence: JSON                            ← 支撑案例
├── confidence: float
├── applied_count: int
└── created_at, updated_at

tuning_log（调优记录）
├── id, type: "weight"|"threshold"|"prompt"|"rule"
├── parameter_name
├── old_value, new_value
├── reason: text
├── evidence: JSON
├── status: "proposed"|"approved"|"rejected"
└── proposed_at, decided_at

llm_budget（LLM 成本控制）
├── date
├── tokens_used, cost_estimate
├── daily_limit, monthly_limit
└── alert_threshold_pct
```

### 8.2 LLM 成本控制策略

```
三级降级机制：
├── 正常模式：主模型处理所有分析
├── 节约模式（日用量 > 80%）：潜力区股票跳过 LLM 分析，仅更新量化数据
└── 熔断模式（日用量 > 100%）：停止所有 LLM 调用，仅更新价格数据

默认上限：
├── 日上限：$3（约 300K tokens @ GPT-4o）
└── 月上限：$60

每日预估消耗（24 只股票）：
├── 新闻摘要分析：24 × ~2K tokens ≈ 48K
├── 综合评分更新：24 × ~3K tokens ≈ 72K
├── 异常检测判断：24 × ~1K tokens ≈ 24K
├── 每日简报生成：1 × ~5K tokens ≈ 5K
└── 合计 ≈ 150K tokens/天（约 $0.5-1）
```

### 8.3 技术选型

| 组件 | 选择 | 理由 |
|------|------|------|
| 定时调度 | APScheduler 内嵌 FastAPI | 一期最简，二期可替换为 Celery/cron |
| 数据存储 | SQLite（扩展现有 store.py） | 无额外依赖，二期可迁移 PostgreSQL |
| 浏览器推送 | Web Push API + Service Worker | 标准方案，免费，离线可达 |
| SEC 数据 | EDGAR REST API（免费） | 官方接口，Form 4 / 8-K / 10-Q |
| 期权数据 | yfinance options chain | 免费快照，够用 |
| 新闻抓取 | yfinance news + Google News RSS | 零成本，LLM 补质量 |
| 经验存储 | SQLite + JSON 字段 | 一期够用，二期可迁移向量库 |

### 8.4 Phase 8A：核心骨架 + 策略大脑系统 ✅ (已完成)

**目标**：观察池 CRUD + 数据管道 + 策略大脑系统（收集→分析→策略→执行→回顾循环）

#### 已完成清单 ✅

| # | 模块 | 说明 |
|---|------|------|
| 1 | 数据库设计 | 11 张表（watchlist/snapshots/news/sec/filings/insider/options/earnings/uzi/intelligence/strategy/budget） |
| 2 | 观察池 CRUD | 完整 API：添加/查询/更新/删除/分层管理，前端三区卡片布局 |
| 3 | 数据管道 | 价格/新闻/SEC/期权 4 条管道，支持手动刷新 + APScheduler 定时调度 |
| 4 | UZI 深度分析 | 6 种分析类型（关键驱动因素/风险/催化剂/护城河/估值/综合），SSE 流式 UI |
| 5 | 策略大脑系统 | `stock_intelligence` + `strategy_records` 表 + `strategy_engine.py` 完整实现 |
| 6 | 情报聚合 | "刷新信息" — 聚合所有数据源生成情报简报（SSE 流式） |
| 7 | 策略生成 | "刷新策略" — LLM 生成 8 板块结构化策略（多空分析/操作建议/风险控制/目标时间线/信心评级） |
| 8 | 前端 UI | 观察池主页 + 股票详情抽屉 + 策略 Tab + 历史版本时间线 + 徽章信号显示 |
| 9 | 预算控制 | LLM 日/月用量跟踪 + 上限设置 + 实时监控 API |
| 10 | Phase 4 集成 | 交叉验证页面增加"加入观察池"按钮，支持批量导入 |

#### 核心设计特性

**策略大脑循环**：
```
"刷新数据"(数据管道)  →  "刷新信息"(情报聚合)  →  "刷新策略"(LLM策略)
      ↓                        ↓                        ↓
 外部数据采集              情报简报生成              8板块结构化策略
 price/news/sec/           LLM汇总+摘要             信号+信心+理由+对比
 options/earnings          key_signals提取          signal/confidence/reasoning
      ↓                        ↓                        ↓
      └─────写入 DB ──►────────┴──────────►────────────┘
```

**8 板块策略输出**：
1. 情报摘要（3-5 要点）
2. 多空分析（✅多头论据 / ❌空头论据）
3. 核心逻辑（2-3 句投资主线）
4. 操作策略（信号 + 买入/加仓/减仓/卖出条件）
5. 风险控制（止损位/仓位比例/对冲建议）
6. 目标与时间（价格目标 + 时间窗口 + 概率）
7. 与上次策略对比（信号变化/假设调整）
8. 信心评级（1-10 分 + 理由）

**前端交互流程**：
- 三按钮分离：刷新数据（数据管道）/ 刷新信息（情报） / 刷新策略（LLM），用户可控频率和成本
- 表格增加"策略"列：显示信号徽章（看多/中性/看空）+ 信心分
- 抽屉新增"策略"Tab：完整展示 8 板块内容 + 版本历史时间线
- 批量策略查询：`/strategy-summaries` 端点避免 N+1 查询

### 8.5 Phase 8B：LLM 驱动交易决策系统（7.5 周）

> 完整设计文档：[`docs/TRADING_DECISION_SYSTEM.md`](docs/TRADING_DECISION_SYSTEM.md)

**目标**：从当前"策略大脑"（单股策略建议）升级为完整的 LLM 驱动交易决策系统

#### 核心架构升级

```
当前（Phase 8A）                            目标（Phase 8B）
┌──────────────┐                    ┌──────────────────────┐
│  单股策略建议   │                    │  四层决策体系          │
│  8 板块分析    │       ───►        │  L1 市场宏观策略        │
│  信号+信心分   │                    │  L2 组合配置策略        │
└──────────────┘                    │  L3 战术执行计划        │
                                    │  L4 账户执行策略        │
                                    ├──────────────────────┤
                                    │  多 LLM 投委会         │
                                    │  交易闭环 + 复盘       │
                                    │  用户交互接口          │
                                    └──────────────────────┘
```

#### 实施路线图

| 子阶段 | 时间 | 内容 | 交付物 |
|--------|------|------|--------|
| 8B.1 信息输入 + L1-L2 | 2 周 | MarketContextCollector + ChartGenerator + SignalPerception + 宏观策略管理器 + 组合策略定位器 | macro_strategies + strategic_plans 表 |
| 8B.2 战术执行 + 模拟交易 | 2 周 | TacticalEngine + AccountExecutor + SimulatedTrading | tactical_plans + account_strategies + sim_* 表 |
| 8B.3 投委会 + 用户接口 | 1.5 周 | InvestmentCommittee（4 成员独立审查+共识）+ WebSocket 实时对话 | committee_reviews + user_preferences 表 |
| 8B.4 闭环反馈系统 | 1 周 | 交易确认/拒绝跟踪 + 催化剂时效管理 + 自动复盘 + 经验卡片 | trade_feedback + catalyst_tracking + auto_reviews + experience_cards 表 ✅ |
| 8B.5 集成测试 + 调优 | 1 周 | E2E 测试 + 性能优化 + LLM 成本优化 | 全流程验证通过 |

#### 关键设计决策

| 决策 | 方案 | 理由 |
|------|------|------|
| 决策模式 | LLM 判断 + 规则信号辅助 | 规则只输出参考信号，LLM 做最终决策 |
| 策略更新 | 增量模式（不重建） | L1 每周 + 日检 / L2 每周 + 偏差检 / L3 每日 / L4 每日 |
| 多模型验证 | 4 成员投委会 | 风控官/成长型/价值型/逆向投资者独立审查 |
| 用户参与 | WebSocket 实时对话 | 可修改策略参数/持仓偏好/风险偏好，LLM 学习用户偏好 |
| K 线分析 | Vision LLM 读取 PNG 图 | mplfinance 生成 → base64 编码 → 多模态 LLM 分析 |

### 8.6 Phase 8C：复盘系统（2 周）

**目标**：交易复盘 + 经验卡片 + 参数调优 + 闭环

#### Week 5：复盘引擎

| # | 任务 | 说明 |
|---|------|------|
| 1 | 交易复盘框架 | 卖出后自动触发 → LLM 归因分析（对/错 + 原因） |
| 2 | 归因分析引擎 | 对比入场时各维度评分 vs 实际走势，定位判断偏差来源 |
| 3 | 经验卡片生成 | LLM 从复盘中提炼可复用知识 → scope(global/sector/ticker) |
| 4 | 经验卡片管理 UI | 查看/编辑/删除/标记有效性 + 按行业/股票筛选 |
| 5 | 决策上下文注入 | 综合评分和交易建议生成时，自动检索相关经验卡片注入 prompt |
| 6 | 行业级经验聚合 | 同行业多只股票的共性规律 → 行业级经验卡片 |

#### Week 6：调优闭环

| # | 任务 | 说明 |
|---|------|------|
| 7 | 参数调优建议 | 分析历史表现 → 建议评分权重/买卖阈值调整 + 证据展示 |
| 8 | Prompt 调优建议 | 检测系统性偏差（如催化剂判断过于乐观）→ 建议 prompt 修改 |
| 9 | 调优确认流程 | 展示建议 + 证据 → 用户批准/拒绝 → 生效并记录 |
| 10 | 调优效果追踪 | 调整前后的决策准确率对比（A/B 视图） |
| 11 | 定期复盘报告 | 月度/季度自动生成系统表现总结（胜率/收益率/最大回撤） |
| 12 | 历史分析对比 | 同赛道不同时间的分析结果 → 排名变化追踪 |

### 8.7 后端新增 API 一览

```
观察池：
  POST   /api/watchlist                ← 添加股票（from P4 或手动）
  GET    /api/watchlist                ← 获取列表（含分层排序）
  PUT    /api/watchlist/{ticker}       ← 更新（层级/状态）
  DELETE /api/watchlist/{ticker}       ← 移除
  GET    /api/watchlist/{ticker}/detail ← 股票详情（快照+新闻+SEC+评分）

数据管道：
  POST   /api/pipeline/trigger         ← 手动触发数据更新
  GET    /api/pipeline/status          ← 管道状态（上次运行/下次调度）
  GET    /api/watchlist/{ticker}/news   ← 新闻摘要历史
  GET    /api/watchlist/{ticker}/filings ← SEC 文件列表
  GET    /api/watchlist/{ticker}/insider ← 内部人交易
  GET    /api/watchlist/{ticker}/options ← 期权异动

决策引擎：
  GET    /api/watchlist/{ticker}/score  ← 综合评分历史
  GET    /api/signals                  ← 待处理信号列表
  GET    /api/briefing/{date}          ← 每日简报

交易中心：
  POST   /api/sim/account              ← 创建/重置模拟账户
  GET    /api/sim/account              ← 账户概览
  GET    /api/sim/positions            ← 当前持仓
  GET    /api/sim/trades               ← 交易历史
  POST   /api/sim/trades/{id}/confirm  ← 确认执行交易
  POST   /api/sim/trades/{id}/reject   ← 拒绝交易建议

复盘系统：
  GET    /api/reviews                  ← 复盘列表
  GET    /api/reviews/{trade_id}       ← 单笔复盘详情
  GET    /api/experience               ← 经验卡片列表
  PUT    /api/experience/{id}          ← 编辑经验卡片
  GET    /api/tuning                   ← 调优建议列表
  POST   /api/tuning/{id}/approve      ← 批准调优
  POST   /api/tuning/{id}/reject       ← 拒绝调优
  GET    /api/report/monthly           ← 月度复盘报告

预算：
  GET    /api/budget                   ← 当前用量 + 上限
  PUT    /api/budget                   ← 修改上限
```

### 8.8 前端页面结构

```
顶部导航栏：[ 分析流程 ] [ 观察池 ] [ 交易中心 ]

观察池页面：
├── 三区卡片布局（重点6 / 一般6 / 潜力12）
│   └── 每张卡片：名称 + 当前价 + 涨跌幅 + 信号灯 + 综合评分
├── 手动添加入口（搜索框 + ticker 验证）
├── 数据管道状态栏（上次更新 + 手动刷新按钮）
├── 每日简报面板（当日要点 + 建议操作）
└── 股票详情页（点击卡片展开）
    ├── 价格走势图（K线 + 成交量 + 技术指标）
    ├── 综合评分趋势图
    ├── 新闻时间线
    ├── SEC 文件列表
    ├── 内部人交易记录
    ├── 期权异动
    └── 经验卡片（与该股票相关的历史经验）

交易中心页面：
├── 账户概览（总资产 / 现金 / 持仓市值 / 总盈亏 / 收益率曲线）
├── 当前持仓表格（股票 / 数量 / 成本 / 现价 / 盈亏 / 仓位占比）
├── 待确认交易（系统建议列表 + 确认/拒绝按钮）
├── 交易历史（完整操作记录 + 决策理由）
├── LLM 预算监控（日/月用量仪表盘 + 上限设置）
└── 复盘面板
    ├── 已完成交易复盘列表（盈亏 + 归因 + 教训）
    ├── 经验卡片库（全局/行业/个股三级）
    ├── 调优建议列表（待审批 + 已执行 + 效果对比）
    └── 月度/季度绩效报告
```

---

## 五、风险与注意事项

1. **LLM 幻觉风险**：产业链拆解结果可能包含不准确信息，需要与预设数据交叉核对
2. **数据源限制**：A 股供应商数据依赖 AKShare 接口稳定性，需做好降级处理
3. **API 成本**：多模型交叉验证会显著增加 API 调用成本，建议设置模型数量上限；Phase 8 每日持续调用需设日/月硬上限 + 三级降级
4. **回测验证**：开发完成后，应选取历史案例（如光模块产业链）验证系统输出质量
5. **合规风险**：输出报告需明确标注"仅供参考，不构成投资建议"
6. **模拟交易风险**：模拟环境无滑点/流动性限制，实际交易结果可能偏差较大，UI 需醒目标注"模拟数据"
7. **免费数据源不稳定**：yfinance/EDGAR 可能限流或接口变更，数据管道需做好降级和重试
8. **经验卡片质量**：LLM 总结的经验可能包含错误归因，需用户定期审核经验库有效性
9. **调优过拟合**：基于少量交易的参数调优可能过拟合，建议积累 20+ 笔交易后再启动自动调优建议
