# QuantDinger 开源量化平台研究报告

> 研究对象：https://github.com/brokermr810/QuantDinger（上游 `OpenByteInc/QuantDinger`，Apache-2.0）
> 方法：clone 全量仓库（含完整 git 历史）逐模块代码精读 + 关键机制实跑验证，**不采信 README 宣传**
> 日期：2026-11-02 ｜ 版本：源码 `VERSION=5.6.1`，最新 tag `v5.8.1`

---

## 0. 一句话结论

**这是一个"工程纪律远强于量化方法论"的项目。** 它的架构分层、并发/幂等/对账设计、文档与 AI 编码代理约束，值得直接借鉴；但它的因子研究、回测撮合、风控强制力都存在明显缺口，且对 A 股支持是"数据能取、回测半通、实盘没有"。对你（BottleneckHunter）而言，**值得抄的是骨骼和纪律，不是它的量化内核**。

---

## 1. 项目形态

| 维度 | 实测值 |
|---|---|
| 仓库规模 | 1242 文件 / 49 MB，其中 965 个 Python |
| 后端代码 | 189,674 行（`app/`），测试 70,310 行 |
| 测试 | 379 个测试文件，**2,989 个用例**，含 Postgres 真库集成测试 |
| 提交历史 | 757 commits，2025-12-29 起，2026-09/10 各 130+ 次，仍高频活跃 |
| 技术栈 | Flask 3.1 + Gunicorn、PostgreSQL 18、Redis ×2、Kafka、Celery、pandas、TA-Lib；40 个依赖，`requirements.lock` 全 `==` 锁 |
| 部署面 | docker-compose **18 个服务**（env-sync / PG / Redis×2 / Kafka / kafka-init / migration / backend / mcp / trading / dispatcher / evaluator / audit / scheduler / celery-worker / celery-beat / frontend / mobile） |
| 数据模型 | `init.sql` 105 张表、206 个索引；33 个 `YYYYMMDD_*.sql` 增量迁移 |
| 最重文件 | `routes/ai_chat.py` 4,726 行；`trading_executor.py` 3,265；`pending_order_worker.py` 2,989 |

**开源边界（重要）**：后端 Apache-2.0；**前端 `QuantDinger-Vue` 与移动端 `QuantDinger-Mobile` 是"source-available"独立许可，且不在本仓库**。自部署实际拉的是 GHCR 预构建镜像。想改 UI 需要另行授权——这不是完整开源产品。

---

## 2. 架构：最值得借鉴的部分

### 2.1 一镜像多进程角色

```python
# app/runtime/roles.py:9-18
QD_PROCESS_ROLE ∈ {api, trading, scheduler, celery, kafka-audit,
                   strategy-dispatcher, strategy-evaluator, legacy}
# 未知角色直接 raise（:20-26），不静默退化成 legacy
```

`app/startup.py:213-232` 的 `run_startup_hooks` 按角色决定起哪些后台线程；API 与 Celery 角色**明确不启任何进程内后台服务**。持久交易循环只活在 `trading-worker`。

这不到 40 行，却根治了"HTTP 进程里偷偷跑交易线程"这一类最难查的生产事故。**BottleneckHunter 的 `scheduler.py`（2,112 行）跑在 FastAPI 进程内，这是同类隐患。**

### 2.2 并发与幂等：写成了可执行的规约

`docs/architecture/CONCURRENCY_MODEL.md` 把并发域、序列化键、保护手段列成表格（策略生命周期 / 单标的执行 / 快速下单 / 待成交派发 / 网格单元 / 账户镜像 / 回测 / Agent 任务 / 支付 / 缓存 / SSE），每条给出 `建议键形`：

```
actor_id:operation:target:client_request_id
strategy:{strategy_id}:symbol:{symbol}:side:{side}
credential:{credential_id}:instrument:{inst_id}:order
```

并强制 "**Claim Before Work**"：选行 → 原子转 `pending→processing` → 提交 → 才调外部。没有 worker 能在拥有该行之前调交易所。

落地证据：
- 跨进程锁用 `pg_try_advisory_lock(hashtextextended('trade-execution:{user}:{cred}:{market}:{symbol}'))`，3s 超时（`live_trading/execution_lock.py:110-142`）
- 租约心跳用**独立 psycopg2 连接**续租，理由是"不被应用连接池与交易所 I/O 阻塞"（`lease_heartbeat.py:30-41`）
- `executor.runtime_guard = lease_heartbeat.strategy_valid` —— 租约失效直接拒绝下单，fencing 落到执行点

### 2.3 回测自证机制（`_reconcile`）—— 我认为这是全项目最该抄的一处

`strategy_v2/runtime.py:2286-2356`：回测结束后**逐笔重放**现金、费用、成交价、成交时机、持仓，任何一项与 `final equity` 差异超 1e-6 就 `passed=False`。

```python
expected_fee  = notional * self.broker.commission      # 手续费重算
expected_price = 按 fill_reference 反推（limit / gap_open / bar_open×滑点）
if fill_time < signal_time: timing_mismatches.append(index)   # 未来函数断言
```

**把"结果可信"从承诺变成可断言的输出**。这一条与你 CLADE.md 里"验收必用真实数据证明不靠代码完成"完全同构，可以直接搬到 L1-L4 每轮决策的台账上。

### 2.4 防未来函数：时序硬、数据访问软

时序不变量是真的（实测 day1 收盘信号 → day2 开盘价成交，`signal_time=01-01 / time=01-02`）：

```
set_clock(t, include_current=False) → execute(上轮挂单) → 策略产生信号
→ 信号进 pending → 下一根 bar open 才成交
```

**但数据访问路径有洞，且已实测跑通**：

```python
def handle_data(context, data):
    fr = list(context.portal.frames.values())[0]
    log(str(fr["close"].iloc[-1]))   # 第 1 根 bar 就打印出最后一根收盘价
```

`_bind_runtime_api`（runtime.py:1938）把整个 `context` 交给用户代码，而 `context.portal.frames` 持有**未截断的完整 DataFrame**；`visible_frame` 的截断只在读取 API 层生效。

**为什么这条对你重要**：QD 有付费策略市场（`community_service.py`，2,870 行），用户写策略读未来数据刷出"完美回测"→ 上架卖。这是**经济激励驱动的攻击面**，不是理论风险。修法很小：给 handler 一个窄门面（`__getattr__` 白名单），或把 `frames` 改成私有名。

---

## 3. 量化方法论：名实对照

### 3.1 做对了的（有完整实现，可借鉴）

| 机制 | 位置 | 实现质量 |
|---|---|---|
| Benjamini-Hochberg FDR | `strategy_v2/factor_benchmark.py:42` `_benjamini_hochberg`，α 钳 [0.001, 0.5] | 真实现，q 值参与因子分类 |
| PBO（CSCV） | `strategy_evolution/statistics.py:110`，组合上限 2000 防爆炸，`periods<4` 返回 insufficient | 真 CSCV，退化有明确 reason |
| Deflated Sharpe | 同上 `:75`，用样本偏度 + 峰度，`effective_trials==1` 时 benchmark=0 | 公式完整，含非正态校正 |
| Walk-forward | `walk_forward.py`，按**真实 bar 数**切分，带 `embargo_bars`（禁运期防泄漏），保留 `blind_start/blind_end` 最终盲测区 | 规范 |
| 因子生命周期 | 六态 `alive/reversed/candidate/decaying/dead/insufficient`，`insufficient` 排最后不参与排名 | 诚实的缺失处理 |
| 成本感知评分 | `factor_research.py:130,220-223` `cost_rate = commission + slippage`，按换手 × 切换倍数（建仓 1×、换手 2×）从分组毛收益扣到净收益，再进评分 | 真实现，非宣传 |
| 回测指标 | `backtest/metrics.py` 1,346 行：Sortino/Calmar/Omega/Tail Ratio/VaR/CVaR/偏度峰度/双 Beta/捕获率/M²/Jensen α | 覆盖完整，年化因子按频率与市场动态取（`frequencies.py`） |
| 状态语义 | 逐指标返回 `available/partial/insufficient_history` + `status` 字段，样本不足返 `null` **绝不填 0** | 值得抄的口径纪律 |

内置因子 63 个（技术类 sma/ema/macd/kdj/cci/adx/aroon/trix/mfi… + 基本面类 market_cap/earnings_yield/book_to_price/ROE/revenue_growth/debt_to_equity/FCF_yield），统一 `FactorDefinition` 数据类，带 `direction_hint` 与 `parameter_schema`（供参数搜索直接用）。

### 3.2 做砸了的（回测引擎的系统性偏差）

| 缺陷 | 证据 | 后果 |
|---|---|---|
| **maker 算法在回测里是死的** | `runtime.py:806` `is_limit_order = order_type=="limit" or execution_algo=="limit"`；`maker_then_market` 走 else 当市价单。实测：设 `maker_then_market + 5s 等待 + 10bps 偏移`，成交仍是 `fill_reference=bar_open` | 回测吃掉"挂单未成交"风险，**系统性偏乐观** |
| 成本模型粗糙 | 单一 `commission` 费率，无 maker/taker、无最低佣金、无印花税/过户费 | A 股回测偏乐观 |
| 合约乘数未使用 | `contract_size` 在 `strategy_v2/` 零引用 | 期货/永续按 1:1 计价 |
| 爆仓口径过宽 | 仅 `equity<=0` 才强平（`liquidate_if_insolvent`），**无维持保证金率**（`maintenance|mmr` 零命中） | 高杠杆下严重低估穿仓频率 |
| 资金费率未建模 | `executionAssumptions.fundingMode = "not_modeled"` | 永续长周期必偏 |
| 无 A 股专属规则 | T+1 卖出限制、涨跌停、停牌在引擎层**零处理** | A 股回测不可用 |
| 年化硬编码 | 非 crypto 一律 252 天 × 390 分钟；A 股是 240 分钟、港股 330 | Sharpe/波动率偏差约 1.6% |
| 指标 O(n²) | `_compute_builtin_indicator` 对每根 bar `frame.iloc[:index+1]` 重算 | 长历史慢 |

### 3.3 因子实验室的规模限制

- **"最多 40 因子"是真的**：`strategy_v2/service.py:357` `maximum_factors = max(2, int(os.getenv("FACTOR_BENCHMARK_MAX_FACTORS", "40")))`，可配
- 面板构造是真 point-in-time（`fundamental_data.py` 用 `available_at<=as_of` 过滤，`fundamental_coverage.py` 定义 `ready/partial/stale/no_data` 四态）
- 前向收益用 `open.shift(-1)` 入场、`open.shift(-(holding+1))` 出场（`factor_research.py:126-128`），即**次日开盘入场**，无未来泄漏；研究日按 `holding_period` 步进避免重叠
- 但**历史基本面同步只支持 USStock / HKStock**：`fundamental_sync.py:56,88` 显式拒绝 `mode=='history'` 且市场含 CNStock
- 旗舰 AI 研报的市场白名单 `SUPPORTED_MARKETS = {USStock, HKStock, Crypto, Forex, Futures}` —— **不含 A 股**

---

## 4. 实盘执行：好的极好，坏的极坏

### 4.1 值得抄的三点

1. **提交前落 client id**（`pending_orders/submission_recovery.py:70-98`）：POST 前先把 `client_order_id` 写进行，用 `WHERE status='processing'` 的 rowcount≠1 判定"我丢了这批活"。比"发完再存"强一个数量级。
2. **未知态不重试，改为反查**：transport 异常 → 重分类为 `submit_unknown` → 行标 `sent` + `submit_outcome_unknown`，靠 client id 反查。**盲目重试 = 开两次仓**，这是实盘最贵的教训。
3. **风控 fail-closed 的预算快照**（`order_budget.py:32-50`）：`capacity<=0 or price<=0` 直接拒（而非当 0），且用"现有持仓 + 本单"的 projected notional 判断。

### 4.2 必须警惕的三点

1. **网格有回滚窗口**：`grid/engine.py:1824` 先向交易所下单，`:1859` 才 INSERT 本地行。中间崩溃 → 交易所留有本地不可见的孤儿单 → 重启对账只遍历本地行（`:266-315`）→ **永不发现** → 同价位重复建仓。且 client id 带随机 nonce（`exchange_orders.py:274-291`），重试也不幂等。
2. **风控非全局强制**：名义预算只在 `_execute_signal` 链路上；**网格（`grid/exchange_orders.py:294`）和 Quick Trade（`quick_trade.py:477`）都是旁路**。账户级敞口风控 `account_risk.py` 写好却**零调用**（只被测试 import）。**单日亏损限制全仓零命中**。权益止损是 opt-in，不配就不跑。
3. **状态机无终态、无 TTL、无超时撤单**：`pending_orders` 无 CHECK 约束；`_mark_sent` 能把 `cancelled` 改回 `sent`（`worker.py:2829`）；"查不到就留在 sent 再查"（`order_quantities.py:154-158`）→ 永久空转。

### 4.3 虚拟账户（signal 模式）

`virtual_trading.py` 是**确定性瞬间全成**：`价格 × (1±0.05%)`、限价穿价即全量成交、无排队无部分成交；费率写死 0.05% + 0.05% 滑点、恒 taker；**`cash_balance` 只做记账，没有 insufficient_cash 分支**。

给你的启示：模拟盘必须与实盘的成交假设对齐，否则"模拟赚、实盘亏"无法归因。

### 4.4 JEV（AI 交易前决策门）

- 是**第三方**（TypeSafe `api.typesafe.ai/v1`），非本地规则引擎
- 五个 typed choice：data_quality / signal_alignment / market_regime / risk_check / execution_quality，置信度阈值 0.55
- **只拦开仓**，且**整类排除网格/DCA/马丁**；平仓、止损、止盈永远绕过
- **fail-open 到底**：JEV 挂 → 退 LLM → LLM 挂 → `allowed=True, decision="error_allowed"`
- 审计表 `qd_ai_decisions` 含 `probabilities_json / latency_ms / checks_json`，但 `_persist` **吞掉所有异常**

**这个设计取向值得认同**：AI 只做"要不要开仓"的否决闸，永不影响风险退出；失败放行而非阻断交易。与 BottleneckHunter 投委会"多 persona 评审 + 硬约束"同向。

---

## 5. AI 层：分层判断

### 5.1 值得抄

1. **结论必须绑证据 ID**（`professional_report/llm_contract.py`）：`NarrativeClaim.evidence_refs` 里不认识的 ID 被剔除并记 warning，**剔完为空则整条 claim 丢弃**；pydantic `extra="forbid"`，未知字段裁掉并留痕。
2. **自然语言 → 策略代码的四段闸**：意图路由（9 个 capability pack，**只拼命中的片段**）→ 隔离编译（10s / 512MB / 512KiB）→ AST 语义断言（方向与下单调用一致、禁 `len(bars)` 当时钟）→ 合成数据行为校验 → 有界修复（`max_repair_attempts=2`, temp=0.15）。
3. **Agent Gateway 的分档安全**：6 档 scope（R/W/B/N/C/T）、token 只存 sha256、双键限流（token 60/min + tenant 600/min）、幂等表 `(token,method,route,key)+request_hash`、append-only 审计、**名义额预留** `qd_agent_notional_reservations`、实盘三重开关（token 有 T + `paper_only=false` + 服务端 `AGENT_LIVE_TRADING_ENABLED=true`）+ kill switch。

### 5.2 不要抄

1. **`llm.py` 的 provider 矩阵**：9 家 × 3 候选模型 × 硬编码换家优先级；每次请求新建 `requests.Session()` 无连接池；**`usage` 字段整个丢弃，零 token 计数**；无并发上限。你有数据驱动的模型调度层，抄这个是倒退。唯一可取的：`finish_reason=="length" ⇒ retryable=False`（避免把截断当瞬时故障重试）。
2. **`ai_chat.py` 4,726 行 + system prompt 当字符串拼**：155 行散文串接，混着产品文案与业务规则，靠 `[:6000]` 硬截 JSON。不可单测、不可版本化。**你的 38 个独立 `.md` prompt 是对的，别退化。**
3. **伪 tool calling**：53 个 `ToolDefinition` 只是 prompt 里的文本（注释自认 "not an execution engine"），模型读 prompt 自己编 JSON，后端正则兜底。真工具化请直接用 provider 原生 function calling。

---

## 6. 工程化体系：这是最该整套借鉴的部分

### 6.1 CI 门禁清单（8 个 workflow）

| 门禁 | 手段 | 判据 |
|---|---|---|
| 语法 | `py_compile` + `compileall` | — |
| 规范 | ruff，但只选 `E9,F63,F7,F82` | **只拦语法/未定义名级错误，不查风格** |
| 结构护栏 | `scripts/backend_quality_check.py` | 自定义 |
| 依赖锁 | `check_requirements_lock.py` | 每个 direct 依赖必须在 lock 且唯一 `==` |
| 测试 | 手写分片（索引 %2），`-m "not integration and not stress"` | PG18 + Redis8 service |
| 迁移演练 | 两个 PG DSN 真库跑迁移 + 事务计账测试 | — |
| Release gate | 单文件单用例：限价部分成交→撤单→市价补单→跨进程重启重放幂等 | 独立于主套件 |
| API 兼容 | `export_openapi.py` 重新生成 + `diff` + `oasdiff breaking --fail-on ERR` | 拦"改了路由忘导出" |
| 供应链 | `pip-audit -r requirements.lock`、`bandit -lll -ii`、gitleaks 全历史、CodeQL | — |
| 许可 | REUSE、DCO 逐 commit 校验 `Signed-off-by` | — |
| 版本/编码 | `check_version.py`、`check_mojibake.py` | 见下 |
| 发布 | 多架构 + SBOM + provenance + **cosign keyless 签名**，release note 给 `cosign verify` 命令 | — |

**三个自定义检查的判据**（对你直接有用）：
- `check_docs.py`：`docs/` 根只允许两个 README；所有相对链接必须存在；code fence 必须配平；必须 UTF-8；`docs/screenshots` 下每个文件必须被引用（反查孤儿图）
- `check_version.py`：`VERSION` 必须 semver + 逐项比对清单，不一致则打印修复命令
- `check_mojibake.py`：只扫 `git ls-files` 的 tracked 文件，命中 16 个已知乱码串（GBK/CP936、CP1252、U+FFFD）+ 私用区 `U+E000-F8FF` —— **这条与你的中文项目同源痛点，几乎可原样搬**

### 6.2 三个"名不副实"的门禁（别被徽章骗）

1. `codecov.yml` 里 project 与 patch 都是 `informational: true` —— **覆盖率不阻断合并**
2. README 挂 SonarCloud 徽章、仓库有 `sonar-project.properties`，但**workflows 里没有任何 sonar 步骤** —— 门禁不可审计
3. `translate-readmes.mjs`（11 语言 README 同步，用 Edge 免费翻译端点 + token 保护/还原，丢 token 直接 throw）**没接进任何 workflow** —— "自动同步"实为手动工具

### 6.3 文档纪律（最值得学的气质）

- 8 份 V5.1→V5.7 架构演进文档，**每份开头明写 `Historical phase record`**，并指向当前有效路径
- 大量自我限定的句子：
  - `STRATEGY_MAX_ACTIVE is not a capacity guarantee`（V5.2:21）
  - `The evaluator pool ... does not by itself prove 100,000-strategy capacity`（V5.2:44）
  - `Kafka was a shadow path in V5.4`（V5.4:3），并明说初版是 **fail-open 影子路径**
  - 分区键被定义为"**正确性边界而非扩容键**"
- `POST_TRADE_ANALYSIS_V2.md` 通篇在划线："手续费加回**不是**零成本回测"、"当前配置杠杆**不等于**历史实际敞口"、"缺失数据是证据不足状态，**永远不是通过**"
- 副作用：`docs/` 里禁止性措辞（must not / never / 不能 / 不得）出现 **299 次**

这套"**每个门禁都写清为什么与边界声明**"的写法，正是你 CLAUDE.md 里治 over-promise 病想要的东西。

### 6.4 AGENTS.md / CLAUDE.md —— 全仓库最该抄的一个文件

`CLAUDE.md` 全文只有 3 行：`@AGENTS.md`。规则只有一处，工具入口只做薄引用。

`AGENTS.md` 46 行，五节：

1. **Repository map** —— 先划边界（"前端源码不在本仓"），避免代理在仓库里找不存在的东西
2. **Read by task** —— 不是"读所有文档"，而是路由表：改 Gateway→agent 文档；改策略/回测→策略指南；改运行时→架构索引；改 HTTP→API 约定
3. **Architecture and contract invariants** —— 全是"when 变 → what 必须一起变"的因果句：
   - 不许把持久交易循环挪进 HTTP 后端；不许让 evaluator 直接下单
   - PG 是唯一 durable source of truth；Kafka 传事件；缓存 Redis 可丢、jobs Redis 另有职责；**不许静默互相替代**
   - 改分布式/交易流程时，tenant isolation / idempotency / lease / fencing / audit 必须保持完好
4. **Safety** —— 永不提交真密钥；**"不得削弱实盘保护或绕过授权与人工复核，除非用户明确要求且限定范围"**；不做升级期数据重写清理本地残留；源码注释一律英文
5. **Verification** —— 教的是**跑单个测试文件**而非全量；收尾一句：`Match verification depth to risk`（交易/迁移/租户/分布式所有权必须有针对性回归）

**而且它被 CI 守着**：`tests/test_ai_agent_contract_alignment.py:37-42` 解析 `AGENTS.md` / `CLAUDE.md` 里所有 `[text](path)` 与 `@path` 引用，断言目标文件存在。**给 AI 的说明书本身是被校验的**。

---

## 7. 对 BottleneckHunter 的借鉴分级

先给对照事实（我实测你的仓库）：

| 能力 | BottleneckHunter | QuantDinger |
|---|---|---|
| 代码规模 | 435 py / 116,996 行 | 965 py / 189,674 行 |
| 测试 | 214 文件 / 2,316 用例 | 379 文件 / 2,989 用例 |
| **CI** | **无 `.github/workflows`** | 8 个 workflow + release gate |
| PIT 门禁 | ✅ `pit_gate.py` 296 行，显式拒未来数据 | ⚠️ 时序硬、数据访问软（可读未来 bar） |
| 证据引用 | ✅ `evidence.py` 102 行，无据投票不计入 | ✅ `llm_contract.py` + 剔空即丢 claim |
| 概率校准 | ✅ `signal_calibration.py`（PAV 等序回归 + ECE + Brier） | ❌ 仅阈值网格搜索 |
| 评委独立性 | ✅ `judge_independence.py`（N_eff = N²/Σ相似度） | ❌ 无 |
| 消融/增量证明 | ✅ `incremental_ablation.py`（跨种子稳定性 + `no_increment`） | ❌ 无 |
| PBO / DSR / walk-forward | ✅ `evaluation.py` 全有 | ✅ 有，含 embargo 与盲测区 |
| **因子研究** | ❌ 无（`information_coefficient`/`quantile_returns`/`benjamini` 零命中） | ✅ 63 因子 + BH-FDR + 六态生命周期 |
| **参数稳健性搜索** | ❌ 无 optuna/网格 | ✅ 随机/网格/TPE + PBO + DSR + 区块 bootstrap + 成本压力 |
| 回测引擎 | `event_backtest.py` 205 行（PIT 正确） | `strategy_v2/runtime.py` 2,948 行 + `_reconcile` 自证 |
| 实盘执行 | 仅模拟（`trade_executor.py` 549 行） | 6 家加密所 + IBKR/Alpaca，2,600+ 行 |
| **可观测性** | ❌ 无 request_id / 无 Prometheus | ✅ request-id 贯穿、7 指标、worker 心跳 critical 告警 |
| **Agent 网关 / MCP** | ❌ 无 | ✅ scope 分档 + 幂等 + 名义额预留 |
| 模拟成本口径 | `slippage.py`（成交量冲击）+ 佣金 | 0.05%+0.05% 写死，无资金校验 |

**关键判断：你的决策层（证据、PIT、校准、独立性、消融）已经强于 QD；你落后的是工程底座与"持续交易"的管道。**

### 7.1 第一优先：CI + 让 AI 说明书可被校验（成本最低，收益最高）

你现在**没有任何 CI**。这是最大的单点缺口。

最小可行门禁（照 QD 的做法，但别学它的松）：

1. `py_compile` + `compileall`（语法）
2. ruff（**保持你的完整规则集，别学 QD 只选 `E9/F*`**）
3. `pytest -q -m "not integration"`（2,316 个用例已有）
4. `check_mojibake.py`（中文项目刚需，可近乎原样搬）
5. `check_docs.py` 的链接存在性检查（你的 `docs/` 已有大量研究文档）
6. **`test_agents_md_references.py`** —— 断言 CLAUDE.md 里引用的每个文件/路径都存在

第 6 条尤其值得做：你的 `CLAUDE.md` 是"事实清单 + 历史教训索引"，随项目演进极易失效，而它恰恰是每次会话最信任的输入。

配套改写 `CLAUDE.md`：把"事实罗列"改成 QD 那种三段式——**按任务路由文档 / 不可违反的不变量 / 按风险分级验证**。你现在缺的是"改交易、改迁移、改多租户要有针对性回归"这类**分级**约束。

### 7.2 第二优先：可观测性三件套

- **进程角色进日志**：每个进程/线程自报 identity，多进程排查不用猜
- **request-id 贯穿**：入站校验（正则限长，否则重生成）+ 回写响应头 + ContextVar
- **worker 心跳缺失即 critical 告警**：这是把"容器活着但交易线程死了"抬成一等告警。你的 `system_watchdog` 是同一问题的另一解法，二者可以合并

### 7.3 第三优先：借鉴它的"自证"思路（你已有雏形）

你刚提交的 `feat(decision): 决策运行台账（每轮决策一行 + T+5 结果回填）` 正是 QD `_reconcile` 的同构做法。可以再进一步：

- 每轮决策台账附 `expected`（按当时快照推算的成交价/费用/仓位）
- T+5 回填时**逐项对账**，差异超阈值即标记 `passed=False` 并进报警
- 把口径漂移从"感觉不对"变成"断言失败"

### 7.4 第四优先：补因子研究（补的是"知识"不是"工程"）

你的决策链是"叙事驱动"，缺一层横截面统计验证。QD 这部分**方法论是对的**，可以直接移植：

- 63 个因子不必全抄，先要 `momentum / reversal / volatility / amihud / skewness + 3 个基本面`
- **BH-FDR 多重检验控制**：这是你目前完全缺的（`benjamini` 零命中）。你现在做 `incremental_ablation` 时，多点检验没有校正，容易把噪声当增量
- **六态生命周期**（`alive/reversed/candidate/decaying/dead/insufficient`）比二元"有效/无效"更诚实
- 跨因子 IC 相关矩阵用于去冗余

注意 QD 的实测教训：**IC 用全样本 = 没用**；必须按时间切分且留盲测区（这点 QD 做对了，且有 embargo）。

### 7.5 第五优先：Agent 网关（如果要做外部 Agent 接入）

若要对外开放 API 给 Cursor/Claude Code 之类：抄它的 **scope 分档 + 幂等表 + 名义额预留 + 双 kill switch**。表结构可直接搬：`{idempotency, audit, jobs, notional_reservations}`。这是"给外部 agent 一把全权限 key"的反面教材级正确做法。

---

## 8. 明确不要借鉴的

| 项 | 原因 |
|---|---|
| `llm.py` provider 矩阵 | 硬编码换家 vs 你的数据驱动调度；无连接池；**零 token 计数** |
| Kafka / 多进程角色全套 | 27 个服务是给 2 万用户/10 万策略设计的，你的规模上这套是纯负担。**只抄 `QD_PROCESS_ROLE` 理念，不抄部署** |
| 单文件 4,726 行 + prompt 字符串拼接 | 你 38 个独立 `.md` prompt 是正确形态 |
| 伪 tool calling | 直接上原生 function calling |
| 网格/DCA/马丁引擎 | 2,228 行只为加密所服务，A 股无意义 |
| 付费策略市场 | 与你的定位无关，且引入"读未来数据刷榜"的激励问题 |
| 它的风控设计 | 名义预算不是全局强制、账户级风控是死代码、无单日亏损限制 —— **把它当反面清单用** |

---

## 9. 关于 A 股：明确的能力边界

如果考虑接入 A 股，先看这张表：

| 环节 | 支持 | 证据 |
|---|---|---|
| K线/行情 | ✅ 四层 fallback：TwelveData → 腾讯(前复权) → yfinance → AkShare | `data_sources/cn_stock.py` |
| 基本面（当前值） | ✅ 但无 API Key 时**仅 AkShare**（注释自承"overseas fragile"） | `cn_hk_fundamentals.py:1-10` |
| 基本面（历史） | ❌ `mode=='history'` 时显式拒绝 CNStock | `fundamental_sync.py:56,88` |
| 因子研究 | ⚠️ 面板可构造，但历史基本面缺失 → 价值/质量类因子不可用 | 同上 |
| 回测 | ⚠️ 符号能被识别（`CNStock:`），但**无 T+1、无涨跌停、无停牌、无印花税** | `strategy_v2/` 零命中 |
| AI 研报 | ❌ `SUPPORTED_MARKETS` 不含 CNStock | `professional_report/providers/catalog.py:14` |
| 实盘 | ❌ `deployment.py:451` 只允许 `{Crypto, USStock, HKStock}` | 无 A 股券商适配（无 futu/moomoo/webull） |
| 网格 | ❌ 仅 crypto spot/swap | `grid/` 无 CNStock 引用 |

**结论：QuantDinger 对你没有"拿来即用"的价值，但有"骨架可抄"的价值。**

---

## 10. 其他值得记录的观察

**关于可靠性的一个反面**：`ai_decision_filter._persist` 吞掉所有异常（:699-700）。风控审计写失败却静默 —— 这与你治的"假成功掩盖 bug"是同一类病。

**关于规模的观察**：单次提交最大 12,992 行新增；9-10 月各 130+ 次提交、单日最高 11,238 行。结合 10 个 2,000+ 行巨型文件与 `#339` 这类"expand factor research and quick trade depth"的宽口径 PR —— 功能面扩张快于重构，`ARCHITECTURE.md` 自己列了 23 处 hotspot 并以"legacy 文件，别再加东西"标注。

**关于配置卫生**：`.env.example` 419 个键，其中 **40 个后端零引用**（多为 compose/运维变量，属正常）；但有 184 个键既不在 `getenv(...)` 也不在 `environ.get(...)` 字面量里（走 `config_loader` 动态读取），靠 `settings.py` 的 schema 表维护 —— 改配置项时容易漏。

**关于开源合规**：Apache-2.0 + REUSE + OpenChain ISO 5230/18974 自我认证 + `MAINTAINERS.md` 公开敏感权限名单 + `.gitleaksignore` 用 `commit:path:rule:line` **精确指纹**而非通配路径。这套是商业开源的正确姿势，但对自用项目过重。

---

## 11. 最终建议

**投入产出排序**：

1. **本周可做**：建 CI 六项门禁 + `test_agents_md_references.py` + 改写 `CLAUDE.md` 为"路由/不变量/风险分级"三段式。**零架构改动，收益长期。**
2. **两周可做**：可观测性三件套（request-id / 进程角色日志 / 心跳告警）。
3. **按需**：决策台账的 `expected vs actual` 逐项对账（在已交付的 T+5 回填上加断言）。
4. **有余力**：BH-FDR 多重检验 + 六态因子生命周期。这是你 `incremental_ablation` 的真实缺口。
5. **不做**：Kafka、多进程角色全套、LLM provider 矩阵、策略市场。

**一句话**：把 QuantDinger 当成一本**工程规范手册**和一个**反模式清单**来读，比当成一个可以 fork 的量化系统有价值得多。它的 README 描述的能力边界，与代码实测之间存在明显落差——这一点本身，就是对"如何不 over-promise"最好的反面教材。
