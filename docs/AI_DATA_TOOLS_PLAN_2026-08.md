# AI 分析师数据调用能力 — 需求分析与开发规划

> 日期：2026-08-18 · 状态：规划（未实现） · 适用面：决策中心（L1-L4）+ VIP 咨询顾问

## 一、需求分析

用户提出的四项需求，逐条映射到现状：

| # | 需求 | 现状 | 差距 |
|---|------|------|------|
| 1 | AI 分析师了解系统可获取的数据 | 系统有数据财富，但**没有一张"数据能力清单"喂给模型** | 需构建**能力清单（manifest）**：什么能力、什么市场、什么标的、返回什么 |
| 2 | AI 判断欠缺数据、准确提出调用需求 | LLM 层**零 tool-calling**（llm_clients 无任何工具绑定）；所有 LLM 调用都是单次 prompt→JSON | 需新增**结构化数据请求协商环**（工具调用机制） |
| 3 | 系统响应需求、实时补数据 | **执行层已就绪**：`DataHub.fetch(capability, ticker, market, user_id)` 全托管（provider 优先级/熔断/按用户 Key 隔离/记账） | 需把 DataHub 能力**做成工具的确定性执行器**，结果回注 prompt |
| 4 | 取数失败→解释错误→缺数据继续 | 已有"诚实降级"范式（数据缺失即明说、绝不编造），VIP chat 现为"当前数据中没有该信息" | 需把"说没有"升级为"尝试取→失败→解释失败→带着缺失继续" |

**根因诊断：系统不缺数据管道，缺的是"模型向管道提需求"的机制。** 所有决策层（L1-L4）和 VIP chat 的数据都是代码**事前预取**、按调用点写死注入 prompt；模型在推理中对 prompt 之外的任何数据都没有索取通道。`vip/chat.py` 的 `_PROMPT` 里那句"若用户问到 facts 里没有的数据，请明确说'当前数据中没有该信息'"就是这条鸿沟的写照。

### 各咨询面的数据注入现状与缺口

- **L1 宏观**（`run_macro_strategy` / `run_macro_check`）：注入指数/板块/情绪/宏观/新闻/期权PCR+13F聚合（`_collect_market_context`）。缺口：个股级数据一律没有——宏观分析师想核对某权重股（如 NVDA）的实时价、期权活动、财报日期时**无通道**。
- **L2 组合**（`run_strategic_plan`）：注入 L1 + 观察池信号。缺口：按板块配比时想查板块成分股的财务/估值、观察池外大盘股的 SEC/期权数据，无通道。
- **L3 战术**（`run_tactical_plans`）：注入每票快照（价/涨跌/RSI/信号/信心）+ 催化剂。缺口：催化剂临近时想查财报日期（CAP_EARNINGS）、内部人动向（CAP_INSIDER）、机构增减（CAP_INSTITUTIONAL）——这些表都**已落库**（options_pipeline/institutional_pipeline 等），只是从没喂给 L3。
- **L4 执行**：注入持仓/约束/近期成交。缺口：小——执行层看的就是仓位与约束，取数需求少（保留协商环但限制取数预算）。
- **VIP 咨询顾问**（`stream_vip_chat`）：facts 快照（dossier/宏观/经验/台账/研报/候选池）+ 对话内现查行情（`live_quote.fetch_live_quotes`，P1 特性）。缺口：候选池里的新标的只有名字/评分没有现价财务（fact-prefetch 只服务持仓）；用户问"候选池的 XX 财务数据"时只能答"没有"。**这是单点价值最高的面**——用户直接对话，缺数据体验最刺眼。

## 二、总体设计

### 设计取向：结构化 JSON 协商环（首选），原生 tool-calling（备选）

| 方案 | 优点 | 缺点 |
|------|------|------|
| **A. 结构化 JSON 协商环** | 全 provider 统一（openai/anthropic/google/deepseek/qwen/glm/ollama 一视同仁）；零新增 LLM 层复杂度；模型本来就全程输出 JSON，沿用 `extract_json_object` 容错；可测性高（纯函数+假 DataHub 即可测） | 需约定块标记；决策中心天然适合，chat 面需剥离块 |
| B. 原生 tool-calling（`bind_tools`） | 对话体验最自然（工具调用对用户不可见） | 项目 4 种 SDK 各一套 tool schema/流式差异；`FallbackChatModel` 包装层**不透明**（`_generate` 只透传 max_tokens，工具绑定须在裸模型上做，降级/记账/熔断全要重接线）；成本高、价值只在 chat 面 |

**结论：先做 A，全项目一套机制打通两端。** B 留作 P4 可选增强（仅 chat 面、feature-flag 后开），B 与 A 不互斥——原生工具调用在 chat 面可取代"剥离块"这一步，执行层（DataHub）与清单完全复用。

### 协商环结构（A 方案的骨架）

```
┌─ 请求前 ────────────────────────────────────────┐
│ 1. 构建能力清单（只列当前市场+当前用户真可用的）    │
│ 2. 注入 prompt 开头（规则 + 清单 + 输出约束）      │
├─ 协商（最多 ROUND=2 轮） ────────────────────────┤
│ 3. 模型输出中检测 [[DATA_REQ]] 块                │
│ 4. 无块 → 直接进入最终回答                        │
│ 5. 有块 → 逐条校验（能力/市场/标的/限额）           │
│         → DataHub.fetch 执行（预算内并发）         │
│         → 结果/失败原因回注 prompt                 │
├─ 收尾 ──────────────────────────────────────────┤
│ 6. 模型最终输出（决策中心=JSON；chat=剥离块后流式） │
└─────────────────────────────────────────────────┘
```

### 各部件设计

**① 能力清单（manifest）** — 新模块 `bottleneck_hunter/data_provider/ai_tools.py`
- 构建来源：`get_hub().get_status()`（已注册 provider × capabilities × markets）+ `DATA_SOURCE_CATALOG`（付费源键可用性）+ 市场维度表。
- 输出形如：
  ```json
  [
    {"capability": "quote", "label": "实时行情", "markets": ["us_stock", "a_stock"],
     "ticker_scope": "观察池/持仓标的", "returns": "现价/涨跌/币种"},
    {"capability": "earnings", "label": "财报日期与业绩", ...},
    {"capability": "insider", "label": "内部人交易", ...},
    {"capability": "institutional", "label": "机构持仓(13F)", ...},
    {"capability": "options", "label": "期权活动(成交/PCR)", ...},
    {"capability": "smartmoney", "label": "聪明钱(内部人+机构+期权聚合)", ...},
    {"capability": "sec", "label": "SEC 公告", ...},
    {"capability": "news", "label": "公司新闻", ...}
  ]
  ```
- **只列当前市场 + 当前用户真实可用**（无 Key 的付费源在 `_candidates` 已被排除，清单同口径过滤）——清单即承诺，承诺必须可兑现，否则 AI 提出请求必然失败，违背需求 4。
- 数据块配额：每任务取数预算上限 `MAX_FETCH_CALLS = 8`、每票每能力去重、结果回注时对长文本截断（防上下文膨胀）。

**② 协商环** — 同模块 `negotiate_data_requests(prompt, llm, *, store, market, user_id, surface, budget) -> (final_prompt, fetch_log)`
- 一轮流程：注入清单+规则 → 调模型 → `_extract_data_req(text)`（容忍代码围栏/紧邻文本，剥离 `[[DATA_REQ]]` 标记块）→ 执行 → 拼接结果回注 → 下一轮。
- 无块即返回（一轮成本≈清单 token 增量，可接受）；有块最多 ROUND=2 轮，超限直接进入最终回答并附说明。
- 结果回注格式：`【数据补充 · 能力=earnings · ticker=NVDA】` 成功后跟紧凑 JSON；失败跟 `【取数失败】capability/ticker: 错误摘要（已尝试候选源：fmp→finnhub）`。
- **失败语义（需求 4）**：执行失败**不重试整环**（DataHub 内部已按候选源依次尝试+熔断），单条失败只影响该条；所有失败条目的错误文本与"缺数据已如实呈现"说明一并回注，模型在缺失数据下继续任务并解释原因——对齐现有诚实降级范式（不编造、明说缺失）。

**③ 执行器** — 直接复用 `DataHub.fetch(capability, ticker, market, user_id)`；`CAP_QUOTE/CAP_DAILY` 走 FetcherManager 委托已内置；记账（`record_ds_call`）/熔断/按用户 Key 隔离全部零新增。

**④ 前端 SSE** — 新增事件 `data_fetch_round`（含 `round`/`capability`/`ticker`/`status`/`message`）。现有 `decision.js` 的 `onEvent` 通用渲染（`evt.data.message` → 进度条）已覆盖，无需改前端。

### 接入点

**决策中心（L1-L4）** — 各层在"prompt 构建完成 → LLM 推理"之间插入协商环：
- L1：`run_macro_strategy` 的 `_collect_market_context` 之后（个股级补查的价值）；
- L2：`run_strategic_plan` prompt 构建后；
- L3：`run_tactical_plans` `_collect_watchlist_signals` 之后（催化剂/财报/内幕补查价值最高）；
- L4：`run_execution_plans`（保留环但取数预算收紧）。
- 统一封装：`_run_data_negotiation(store, market, user_id, llm, prompt, surface, budget)`，各层调用点只加 3-5 行。
- L1 交叉验证分支（`use_cross` 双模型）不协商（协商环只挂在单模型最终推理前；交叉验证目的就是多模型互证，加取数会拖慢且收益低）。

**VIP chat** — `stream_vip_chat` 在 facts 构建完成后插入：
- 提示词里"若用户问到 facts 里没有的数据，请明确说'当前数据中没有该信息'"改为"若确需 facts 之外的数据，可在回答前发出 `[[DATA_REQ]]` 块申请补充，系统会实时取数；取数失败则说明原因并在缺失下继续分析"。
- 输出剥离：检测 `[[DATA_REQ]]` 块，剥掉后流式；**剥离失败（模型没按格式）则跳过取数、直接走原回答**（fail-open，不阻塞对话）。
- 复用 `live_quote` 的取数路径（其 fetch 已走 hub），把 quote 请求并入统一协商执行器，删除/收敛其单独格式。
- 结果块追加进 `guard_corpus` 防 number_guard 误标（现价等合法数字）。

## 三、分阶段开发规划

> 每阶段含：产出物、验收点（可用真实数据证明）、测试。P0-P1 共享协商环，一次建好两端复用。

### P0 — 协商环底座（核心，1 个模块 + 测试）
- `data_provider/ai_tools.py`：manifest 构建（hub status + catalog + market 过滤 + 用户 Key 过滤）+ 协商环 + 请求校验 + 结果回注格式化 + 取数预算。
- 测试（`tests/test_ai_data_tools.py`，假 DataHub 注入，沿 hub 单例注入法）：清单只含可用项；无 DATA_REQ 块零额外调用；单轮取数回注；多轮收敛（≤2 轮）；失败条目回注错误文本；超预算截断；ticker/capability 非法校验拒绝。
- 验收：`python tests/test_ai_data_tools.py` 全绿 + `__main__` 自检。

### P1 — VIP 咨询顾问接入（单点价值最高，先落地见效）
- `vip/chat.py`：facts 后协商环 + 块剥离 + guard_corpus 并入 + 提示词升级 + `live_quote` 收敛。
- 测试：fake hub 下"问候选池标的财务 → 触发 quote/earnings 取数 → 结果进回答"；"取数全失败 → 回答含失败说明且无编造数字"；"模型不按格式发块 → fail-open 原回答"。
- 验收：真机（dev 服务）对 VIP 顾问问一个 facts 里没有的标的，观察数据补充与失败解释。

### P2 — 决策中心 L1-L4 接入
- `decision_engine.py`：统一 `_run_data_negotiation`，L1（单模型路径）/L2/L3/L4 各插接入点；L1 交叉验证分支跳过；SSE `data_fetch_round` 事件。
- 测试：L3 用假 store+假 hub 验证"催化剂临近票被请求 earnings → 结果进 L3 prompt"；预算不足/协商异常时降级为原流程（不阻断决策）。
- 验收：真机跑一次完整决策（l3l4 范围），进度条出现数据补充事件，L3 结果引用取到的数据。

### P3 — 守门与加固
- 每任务取数预算、每票每能力去重、观察池外 ticker 拒绝（除非在持仓/候选池）、长文本截断、清单上限（≤8 能力，超出截断）。
- 请求方安全：取数只读不写库（与 live_quote 同语义）、**绝不把取数结果持久化进决策产出**（结果只进 prompt，provenance 里记录 fetch_log 备查）。
- 每面可开关（配置项/环境变量）`BH_AI_TOOLS_ENABLED`，默认开、事故一键关。
- 回归：全量现有测试不破坏（重点：`test_data_freshness_gate`、VIP chat 流式顺序 session→disclaimer→chunk→done）。

### P4 —（可选增强，YAGNI 前默认不建）原生 tool-calling
- 仅 chat 面、feature-flag 后开：`bind_tools` 需在裸模型（`_create_raw_llm`）上做，协商环与 manifest/执行器完全复用，用原生工具调用取代块剥离。
- 前置条件：对 `FallbackChatModel` 做工具调用兼容评估（`_generate` 透传策略），成本收益不划算则保持 A 方案不建。

## 四、风险与对策

| 风险 | 对策 |
|------|------|
| 模型乱发 DATA_REQ 块 / 不按格式 | 校验层拒绝非法请求；剥离失败 fail-open 原回答；round 上限 |
| 协商拖慢决策总时长 | 每轮仅一轮无块即过；取数预算 8 次/任务；整体 `asyncio.wait_for` 超时 |
| 上下文膨胀（回注大量数据） | 结果截断（条数/字段长度）、按能力白名单返回字段 |
| 取数失败被模型编造成"查到了" | 失败条目回注错误文本+“该数据缺失”显式标注，沿用 number_guard 防数字幻觉 |
| 多用户 Key 泄露 | 执行走 `DataHub.fetch(user_id=...)` 用户上下文，无全局 Key（沿用既有隔离） |
| 前端进度条不认识新事件 | `onEvent` 通用渲染已覆盖；事件字段只用 message |

## 五、待确认

1. **落地顺序**：建议 VIP chat（P1）先于决策中心（P2）——直接对话场景缺数据体验最刺眼、且能尽早实战检验协商环。若更看重决策中心可对调。
2. **取数预算**：每任务 8 次、最多 2 轮，是否符合预期（可按层差异化）？
3. **候选池标的**：VIP 顾问对"观察池候选、非持仓"标的允许取数（现 live_quote 只覆盖持仓+问题提及），默认放行，可收紧。
