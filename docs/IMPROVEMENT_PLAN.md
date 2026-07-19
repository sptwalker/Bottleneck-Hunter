# BottleneckHunter 系统改进完善计划

> **基于**：[系统全面评审报告](SYSTEM_AUDIT_REPORT.md)（2026-06-27）  
> **原则**：先修数据和技术缺陷，再完善规则和闭环问题  
> **编号**：Phase 17 系列（承接 Phase 16 多用户系统）
>
> **✅ 完成状态（2026-07-19 更新）**：Phase 17A-17F 全部 26 项任务已于提交 `18ac7c8`（2026-06-27）交付。本文档保留为实施细节与验收依据的存档；此后（6-27 至今）的持续开发（决策闭环自进化、AI 模型智能调度、L1 宏观咨询、持仓风格硬约束、多用户/多市场隔离加固等）见 [PLAN.md](../PLAN.md) 的 Phase 18+。

---

## 改进路线总览

```
Phase 17A  数据基础修复        ━━  1~2 周  ━━  止血：补全缺失数据维度、修复数据一致性
     ↓
Phase 17B  技术缺陷修复        ━━  1~2 周  ━━  加固：约束硬验证、线程安全、时区处理
     ↓
Phase 17C  风控量化体系        ━━  2~3 周  ━━  造血：回测框架、风险度量、仓位算法
     ↓
Phase 17D  闭环反馈贯通        ━━  2~3 周  ━━  闭合：执行→反馈→学习链路完整运行
     ↓
Phase 17E  规则引擎优化        ━━  2~3 周  ━━  精炼：权重优化、可投性过滤、评分规则化
     ↓
Phase 17F  体验与可视化        ━━  2~3 周  ━━  打磨：决策追溯、风险面板、催化剂日历
```

---

## Phase 17A：数据基础修复（1~2 周）

> **目标**：补全系统最薄弱的数据维度，修复数据一致性问题。无数据支撑的决策引擎等于空转。

### 17A.1 market_snapshots 加 market 列

**问题**：market_snapshots 表无 market 列，A 股和美股 ticker 可能冲突（如 `000001` 平安银行 vs 美股）。当前通过 JOIN watchlist 获取 market 字段，但跨用户查询和聚合统计不便。

**改动**：
| 文件 | 改动 |
|------|------|
| `watchlist/store.py` | `_init_tables()` 的 market_snapshots DDL 加 `market TEXT DEFAULT 'us_stock'` 列 |
| `watchlist/store.py` | `save_snapshots()` 写入 market 字段 |
| `watchlist/store.py` | 幂等迁移：`ALTER TABLE market_snapshots ADD COLUMN market TEXT DEFAULT 'us_stock'` |
| `watchlist/store.py` | `get_stale_tickers()` 可直接按 market 过滤，无需 JOIN |
| `watchlist/price_pipeline.py` | `fetch_price_batch()` 传入 market 参数到 snapshot 对象 |

**工作量**：0.5 天  
**验证**：`SELECT DISTINCT market FROM market_snapshots` 返回 `us_stock` 和 `a_stock`

---

### 17A.2 宏观数据接入 L1 决策

**问题**：`_collect_market_context()` 的 `macro` 字段返回空字典。L1 宏观策略在没有 VIX、美债收益率、联储利率的情况下判断市场阶段，相当于"蒙眼开车"。

**改动**：
| 文件 | 改动 |
|------|------|
| 新建 `watchlist/macro_data.py` | 宏观数据采集模块，3 个数据源 |
| `watchlist/decision_engine.py` | `_collect_market_context()` 调用 macro_data 填充 macro 字段 |
| `watchlist/store.py` | 新建 `macro_snapshots` 表缓存宏观数据（避免重复请求） |
| `watchlist/scheduler.py` | 新增 `job_macro_update()` 每日更新宏观数据 |

**数据源**：

| 指标 | 美股数据源 | A 股数据源 | 更新频率 |
|------|-----------|-----------|---------|
| VIX 恐慌指数 | yfinance `^VIX` | — | 日频 |
| 10Y 美债收益率 | yfinance `^TNX` | — | 日频 |
| 美元指数 DXY | yfinance `DX-Y.NYB` | — | 日频 |
| 沪深300 | — | akshare `index_zh_a_hist(000300)` | 日频 |
| 人民币汇率 | yfinance `CNY=X` | akshare | 日频 |
| 北向资金 | — | akshare `stock_hsgt_north_net_flow_in` | 日频 |

**macro_snapshots 表 Schema**：
```sql
CREATE TABLE IF NOT EXISTS macro_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    indicator TEXT NOT NULL,
    value REAL,
    market TEXT DEFAULT 'global',
    fetched_at TEXT,
    UNIQUE(date, indicator)
);
```

**工作量**：2 天  
**验证**：L1 宏观策略输出中包含具体的 VIX 数值和市场阶段判断依据

---

### 17A.3 Form 4 内幕交易真实解析

**问题**：insider_trades 表中 49 行数据全是占位符（shares=0, price=None, name=Unknown）。SEC pipeline 只存了文件元数据，未解析 Form 4 XML 获取真实交易详情。

**改动**：
| 文件 | 改动 |
|------|------|
| `watchlist/sec_pipeline.py` | `_parse_form4_xml()` — 解析 SEC Form 4 XML 获取交易人、交易类型、数量、价格 |
| `watchlist/sec_pipeline.py` | `_parse_insider_trades_from_filings()` 调用 XML 解析替代占位逻辑 |
| `tests/test_sec_pipeline.py` | 新增 Form 4 XML 解析测试 |

**Form 4 XML 关键字段**：
```xml
<reportingOwner>
  <reportingOwnerId><rptOwnerName>John Smith</rptOwnerName></reportingOwnerId>
  <reportingOwnerRelationship><officerTitle>CEO</officerTitle></reportingOwnerRelationship>
</reportingOwner>
<nonDerivativeTable>
  <nonDerivativeTransaction>
    <transactionAmounts>
      <transactionShares><value>10000</value></transactionShares>
      <transactionPricePerShare><value>150.00</value></transactionPricePerShare>
    </transactionAmounts>
    <transactionCoding><transactionCode>P</transactionCode></transactionCoding>
  </nonDerivativeTransaction>
</nonDerivativeTable>
```

**工作量**：2 天  
**验证**：`SELECT insider_name, shares, price FROM insider_trades WHERE shares > 0` 返回真实数据

---

### 17A.4 A 股数据增强

**问题**：A 股数据严重弱于美股。缺少基本面（PE/PB/ROE）、缺少机构持仓、缺少分析师评级。

**改动**：
| 文件 | 改动 |
|------|------|
| `watchlist/price_pipeline.py` | `_fetch_astock_daily()` 额外获取 PE/PB/总市值（akshare `stock_a_indicator_lg`） |
| 新建 `watchlist/fundamental_pipeline.py` | A 股基本面数据管道：ROE、营收增速、毛利率、机构持仓 |
| `watchlist/store.py` | 新建 `fundamental_snapshots` 表 |
| `watchlist/scheduler.py` | 新增 `job_fundamental_update()` 周度更新 |

**akshare 数据源**：

| 指标 | akshare 函数 | 频率 |
|------|-------------|------|
| PE/PB/总市值 | `stock_a_indicator_lg(symbol)` | 日频 |
| 十大股东 | `stock_gdfx_free_holding_detail_em(symbol)` | 季度 |
| 机构持仓 | `stock_institute_hold_detail_em(symbol)` | 季度 |
| 营收/净利润 | `stock_financial_abstract_ths(symbol)` | 季度 |

**工作量**：3 天  
**验证**：A 股标的详情页显示 PE/PB、十大股东、营收趋势

---

### 17A.5 机构持仓与分析师评级（美股）

**问题**：机构持仓和分析师评级是基本面分析最重要的两个维度，当前完全缺失。

**改动**：
| 文件 | 改动 |
|------|------|
| 新建 `watchlist/institutional_pipeline.py` | 机构持仓 + 分析师评级采集 |
| `watchlist/store.py` | 新建 `institutional_holdings` 表、`analyst_ratings` 表 |
| `watchlist/scheduler.py` | 新增周度更新任务 |
| `watchlist/decision_engine.py` | L3 战术计划中引用机构持仓变化和评级变化 |

**数据源**：

| 指标 | 数据源 | 备注 |
|------|--------|------|
| 机构持仓比例 | yfinance `Ticker.institutional_holders` | 前 10 大机构 |
| 分析师评级 | yfinance `Ticker.recommendations` | 最近 6 月覆盖机构数 |
| 目标价共识 | yfinance `Ticker.analyst_price_targets` | mean/median/high/low |

**工作量**：2 天  
**验证**：观察池标的显示机构持仓变化趋势、分析师评级分布

---

## Phase 17B：技术缺陷修复（1~2 周）

> **目标**：修复系统中可能导致数据损坏或决策错误的技术问题。

### 17B.1 L4 执行计划约束硬验证

**问题**：5 个硬性约束（单股 ≤20%、单板块 ≤40%、现金 ≥15%、单笔 ≤$1K、日交易 ≤30%）写在 prompt 中让 LLM 遵守，无代码层验证。LLM 可能生成违规计划直接写入数据库。

**改动**：
| 文件 | 改动 |
|------|------|
| 新建 `watchlist/constraint_validator.py` | 约束校验引擎 |
| `watchlist/decision_engine.py` | `run_execution_plans()` 生成后调用校验，违规计划标记 `rejected` |
| `watchlist/trade_executor.py` | `execute_trade()` 前再次校验 |
| `tests/` | 新增约束校验测试 |

**约束校验规则**：
```python
CONSTRAINTS = {
    "max_single_position_pct": 20.0,    # 单股不超过总权益 20%
    "max_sector_pct": 40.0,             # 单板块不超过 40%
    "min_cash_pct": 15.0,               # 现金不低于 15%
    "max_single_trade_amount": 1000.0,  # 单笔不超过 $1K（可配置）
    "max_daily_turnover_pct": 30.0,     # 日交易额不超过总权益 30%
}

def validate_execution_plan(plan, account, positions) -> ValidationResult:
    """校验执行计划是否满足所有硬性约束。
    返回：通过/拒绝 + 违反的具体约束列表。
    """
```

**工作量**：2 天  
**验证**：构造违反单股 20% 限制的执行计划，校验引擎自动拒绝并记录原因

---

### 17B.2 美股夏令时处理

**问题**：定时任务的 UTC 偏移硬编码。11 月~3 月（冬令时），美股开/收盘时间偏移 1 小时，导致盘前价格更新和盘后扫描时间错位。

**改动**：
| 文件 | 改动 |
|------|------|
| `watchlist/scheduler.py` | 引入 `zoneinfo.ZoneInfo("America/New_York")`，动态计算 UTC 偏移 |
| `watchlist/scheduler.py` | 定时任务使用 `timezone` 参数替代硬编码 UTC 时间 |
| `tests/test_scheduler_jobs.py` | 新增夏令时/冬令时切换测试 |

**工作量**：1 天  
**验证**：Mock 冬令时日期，确认任务触发时间正确偏移

---

### 17B.3 SQLite 连接安全加固

**问题**：`check_same_thread=False` 在高并发场景可能导致数据损坏。虽然当前每次调用新建连接缓解了风险，但缺少显式保障。

**改动**：
| 文件 | 改动 |
|------|------|
| `watchlist/store.py` | 所有写操作显式使用 `BEGIN IMMEDIATE` 事务 |
| `watchlist/store.py` | 关键写入路径加 `threading.Lock()` 保护 |
| `watchlist/store.py` | 连接创建后设置 `PRAGMA busy_timeout=5000`（5 秒等待而非立即失败） |
| `auth/store.py` | 同样加 `PRAGMA busy_timeout` |

**工作量**：1 天  
**验证**：多线程并发写入测试，无 `database is locked` 错误

---

### 17B.4 SSE 自动重连

**问题**：SSE 连接中断后需手动刷新页面，长时间挂机的用户会错过数据更新。

**改动**：
| 文件 | 改动 |
|------|------|
| `web/static/js/app.js` | EventSource `onerror` 回调中实现指数退避重连（1s → 2s → 4s → 8s → 16s，上限 30s） |
| `web/static/js/app.js` | 重连成功后自动刷新最新数据 |
| `web/static/js/app.js` | 连接状态指示器（绿点/黄点/红点） |

**工作量**：0.5 天  
**验证**：断开服务器后前端自动重连，重连后数据刷新

---

### 17B.5 composite_score 实际生效

**问题**：7 个观察池标的的 composite_score 全部为 0.0，评分机制从未实际写入该字段。

**改动**：
| 文件 | 改动 |
|------|------|
| `watchlist/store.py` | `update_watchlist_score()` — 新方法，从最新的策略/委员会评审计算综合分 |
| `watchlist/scheduler.py` | 日决策完成后调用 `update_watchlist_score()` 更新各标的分数 |
| `watchlist/decision_engine.py` | `run_daily_decision()` 末尾触发分数更新 |

**评分公式**：
```python
composite_score = (
    committee_avg_score * 0.4 +        # 投委会平均评分
    strategy_confidence * 0.3 +         # 策略信心评分
    catalyst_score * 0.15 +             # 催化剂活跃度
    data_freshness_score * 0.15         # 数据新鲜度
)
```

**工作量**：1 天  
**验证**：运行日决策后，`SELECT ticker, composite_score FROM watchlist` 返回非零分数

---

## Phase 17C：风控量化体系（2~3 周）

> **目标**：建立量化风险度量和回测能力。没有回测的量化系统无法回答"这个系统赚钱吗"。

### 17C.1 回测框架

**问题**：系统最致命的缺失。无法回放历史信号、无法计算策略胜率、无法与基准对比。

**改动**：
| 文件 | 改动 |
|------|------|
| 新建 `watchlist/backtest.py` | 回测引擎核心 |
| 新建 `watchlist/performance.py` | 绩效指标计算模块 |
| `watchlist/store.py` | 新建 `backtest_runs` / `backtest_snapshots` 表 |
| `web/decision_api.py` | 新增回测 API 端点 |
| `web/static/js/phases.js` | 决策中心新增"绩效回测"面板 |

**回测引擎设计**：
```python
class BacktestEngine:
    """基于历史模拟交易数据的回测引擎。"""
    
    def run(self, start_date, end_date) -> BacktestResult:
        """回放 start_date 到 end_date 之间的所有模拟交易，
        计算净值曲线、风险指标、归因分析。"""
    
    def compute_metrics(self, equity_curve) -> PerformanceMetrics:
        """计算关键绩效指标。"""
```

**绩效指标**：

| 指标 | 公式 | 说明 |
|------|------|------|
| 年化收益率 | `(终值/初值)^(365/天数) - 1` | 基础收益指标 |
| Sharpe Ratio | `(Rp - Rf) / σp` | 风险调整收益，Rf=4%（当前无风险利率） |
| Sortino Ratio | `(Rp - Rf) / σ_downside` | 仅惩罚下行波动 |
| 最大回撤 | `max(peak - trough) / peak` | 最大亏损幅度 |
| 胜率 | `盈利交易数 / 总交易数` | 方向正确率 |
| 盈亏比 | `平均盈利 / 平均亏损` | 每笔交易的风险回报 |
| Calmar Ratio | `年化收益 / 最大回撤` | 收益与最大风险之比 |
| 基准对比 | `策略收益 - SPY/沪深300 收益` | 超额收益 |

**工作量**：5 天  
**验证**：回测面板展示净值曲线、与 SPY 对比、关键指标卡片

---

### 17C.2 组合风控模块

**问题**：风险控制完全停留在"定义"层面。缺少 VaR、Beta、关联性矩阵等量化风险度量。

**改动**：
| 文件 | 改动 |
|------|------|
| 新建 `watchlist/risk_metrics.py` | 组合风险度量计算 |
| `watchlist/store.py` | 新建 `risk_snapshots` 表记录每日风险指标 |
| `watchlist/scheduler.py` | 每日决策后计算并存储风险快照 |
| `web/decision_api.py` | 风险指标 API 端点 |

**风险度量**：
```python
class PortfolioRiskMetrics:
    """组合级风险度量。"""
    
    portfolio_beta: float           # 组合 Beta（相对大盘）
    var_95: float                   # 95% VaR（日）
    cvar_95: float                  # 95% CVaR（条件在险价值）
    concentration_index: float      # HHI 集中度指数
    max_sector_weight: float        # 最大板块权重
    correlation_matrix: dict        # 持仓相关性矩阵
    
    @staticmethod
    def compute(positions, price_history, benchmark) -> "PortfolioRiskMetrics":
        """从持仓和历史价格计算所有风险指标。"""
```

**计算方法**：
- **VaR**: 历史模拟法（250 日滚动窗口，95% 分位数）
- **Beta**: `Cov(Rp, Rm) / Var(Rm)`，基准为 SPY 或沪深 300
- **HHI**: `Σ(wi²)`，>0.25 表示过度集中
- **相关性**: Pearson 相关系数矩阵，标记 ρ>0.7 的高相关持仓对

**工作量**：5 天  
**验证**：决策中心显示 VaR/Beta/集中度指标，高风险时显示警告

---

### 17C.3 仓位管理算法

**问题**：当前仓位分配无科学算法，缺少凯利公式、风险平价、波动率缩放。

**改动**：
| 文件 | 改动 |
|------|------|
| 新建 `watchlist/position_sizing.py` | 仓位算法模块 |
| `watchlist/decision_engine.py` | L4 执行方案引用仓位算法建议 |

**三种仓位算法**：

```python
class PositionSizer:
    """仓位管理算法集合。"""
    
    def kelly_fraction(self, win_rate, avg_win, avg_loss) -> float:
        """凯利公式：f* = (p * b - q) / b
        其中 p=胜率, q=败率, b=盈亏比。
        实际使用半凯利（f*/2）降低波动。"""
    
    def volatility_scaled(self, target_vol, stock_vol, account_equity) -> float:
        """波动率缩放：position_size = target_vol / stock_vol * equity
        高波动率股票分配更少资金。"""
    
    def risk_parity(self, positions, cov_matrix) -> dict:
        """风险平价：使每个持仓对组合风险的贡献相等。"""
```

**工作量**：3 天  
**验证**：L4 执行方案中的建议仓位基于算法计算而非 LLM 直觉

---

## Phase 17D：闭环反馈贯通（2~3 周）

> **目标**：让"执行→反馈→学习"链路真正运行起来。当前后半段完全断裂。

### 17D.1 模拟交易流程验证与修复

**问题**：sim_trades 表 0 行数据表明模拟交易流程从未成功运行。trade_executor 代码存在但实际触发链路可能断裂。

**改动**：
| 文件 | 改动 |
|------|------|
| `watchlist/trade_executor.py` | 排查 `execute_trade()` 的实际调用路径，确保前端确认后能成功写入 sim_trades |
| `web/decision_api.py` | 排查 `/api/decision/execute` 端点的参数传递和错误处理 |
| `web/static/js/phases.js` | 排查"确认执行"按钮的 fetch 调用和错误提示 |
| `tests/` | 新增端到端交易执行测试 |

**验证链路**：
```
前端"确认执行"按钮 → POST /api/decision/execute → trade_executor.execute_trade()
→ store.create_sim_trade() → store.create_sim_position() / update_sim_position()
→ store.update_sim_account() → (if sell) _auto_review_sell()
```

**工作量**：2 天  
**验证**：通过前端确认一笔买入，sim_trades 表出现记录，sim_positions 表显示持仓

---

### 17D.2 自动复盘链路验证

**问题**：auto_reviews 表 0 行。卖出后的自动复盘（`_auto_review_sell`）可能因预算不足、LLM 错误等原因静默失败。

**改动**：
| 文件 | 改动 |
|------|------|
| `watchlist/trade_executor.py` | `_auto_review_sell()` 加异常捕获和日志记录 |
| `watchlist/trade_reviewer.py` | `run_trade_review()` 增加容错：LLM 失败时生成基于规则的简易复盘 |
| `watchlist/scheduler.py` | `job_auto_review()` 加详细日志，记录跳过原因 |

**工作量**：1.5 天  
**验证**：模拟一笔完整的买→卖流程，auto_reviews 表出现复盘记录

---

### 17D.3 经验卡片生成与应用

**问题**：experience_cards 表 0 行。经验卡片的生成依赖自动复盘，而复盘链路断裂导致卡片为空。L4 执行方案中的 `{experience_cards}` 替换始终为空。

**改动**：
| 文件 | 改动 |
|------|------|
| `watchlist/trade_reviewer.py` | 确保复盘成功后必定生成经验卡片 |
| `watchlist/decision_engine.py` | `run_execution_plans()` 中 `get_relevant_cards()` 的调用逻辑验证 |
| `watchlist/store.py` | `increment_card_applied()` 记录每张卡片被引用的次数 |

**经验卡片分级**：
```
global  — 全局经验（如"追高买入亏损概率高"）
sector  — 板块经验（如"半导体板块在利率上升期表现差"）
ticker  — 个股经验（如"NVDA 财报前一周容易回调"）
```

**工作量**：1.5 天  
**验证**：L4 执行方案的 prompt 中包含相关经验卡片内容

---

### 17D.4 催化剂结果判定

**问题**：催化剂事件日期过后，系统不判断该事件是否真的发生了（realized/failed/neutral），导致过期催化剂只是简单标记为 expired 而非评估影响。

**改动**：
| 文件 | 改动 |
|------|------|
| `watchlist/catalyst_monitor.py` | 新增 `judge_catalyst_outcome()` — LLM 判断催化剂实际结果 |
| `watchlist/store.py` | catalyst_tracking 表新增 `outcome` / `outcome_impact` / `judged_at` 字段 |
| `watchlist/scheduler.py` | 催化剂扫描任务中，对过期催化剂调用结果判定 |

**判定逻辑**：
```python
async def judge_catalyst_outcome(ticker, catalyst, market_data) -> CatalystOutcome:
    """催化剂结果判定：
    - 收集事件前后 5 个交易日的价格和新闻
    - LLM 判断：realized（已触发）/ failed（未触发）/ partial（部分触发）
    - 评估影响度：-5 到 +5（对股价的实际影响）
    """
```

**工作量**：2 天  
**验证**：过期催化剂显示判定结果（"已触发 +3"或"未触发 0"）

---

### 17D.5 用户偏好学习

**问题**：user_preferences 表 0 行。用户的拒绝/接受模式从未被记录和学习。

**改动**：
| 文件 | 改动 |
|------|------|
| `watchlist/store.py` | 记录用户对执行方案的确认/拒绝 + 拒绝理由 |
| 新建 `watchlist/preference_learner.py` | 从用户反馈中归纳偏好模式 |
| `watchlist/decision_engine.py` | L4 prompt 中注入用户偏好摘要 |
| `watchlist/scheduler.py` | 周度任务：分析本周反馈，更新偏好摘要 |

**偏好维度**：
```python
PREFERENCE_DIMENSIONS = {
    "risk_tolerance": "low/medium/high",       # 风险偏好
    "position_size_preference": "small/medium/large",  # 仓位偏好
    "sector_preference": ["tech", "healthcare"],  # 偏好板块
    "avoided_sectors": ["energy"],              # 回避板块
    "holding_period": "short/medium/long",      # 持仓周期
}
```

**工作量**：2 天  
**验证**：连续拒绝 3 次高风险建议后，后续 L4 方案倾向保守

---

## Phase 17E：规则引擎优化（2~3 周）

> **目标**：优化评分规则、权重设计和可投性过滤，提升选股质量。

### 17E.1 可投性过滤层

**问题**：瓶颈度高的环节，其供应商不一定是好投资标的。缺少市场规模、客户数、毛利率的硬性门槛。

**改动**：
| 文件 | 改动 |
|------|------|
| `chain/supplier_eval.py` | 在 Alpha 评分前增加可投性预筛选 |
| 新建 `chain/investability_filter.py` | 可投性过滤引擎 |
| `chain/prompts/supplier_eval.md` | 增加"不投原因"的结构化输出 |

**过滤规则**：

| 维度 | 阈值 | 淘汰原因 |
|------|------|---------|
| 市场规模（TAM） | < ¥10 亿 / $1.5B | 天花板太低，成长空间不足 |
| 下游客户数 | < 5 家 | 客户集中风险，失去一个客户即暴跌 |
| 毛利率 | < 20% | 定价权弱，竞争激烈 |
| 日均成交量 | < ¥500 万 / $500K | 流动性不足，进出困难 |
| 上市时间 | < 1 年 | 信息不充分 |

**工作量**：2 天  
**验证**：供应商评估阶段自动淘汰不满足可投性条件的候选，并记录淘汰原因

---

### 17E.2 瓶颈权重分行业方案

**问题**：稀缺性和不可替代性各占 0.25，但两者高度相关（ρ≈0.8），导致稀缺维度过度放大。不同行业的瓶颈驱动因素不同。

**改动**：
| 文件 | 改动 |
|------|------|
| `chain/bottleneck.py` | 分行业权重方案替代单一默认权重 |
| `chain/bottleneck.py` | 降低稀缺性和不可替代性的相关性放大效应 |

**分行业权重**：

| 行业 | 稀缺性 | 不可替代性 | 供需缺口 | 定价权 | 技术壁垒 |
|------|--------|----------|---------|--------|---------|
| 半导体 | 0.15 | 0.20 | 0.20 | 0.15 | **0.30** |
| 医药 | 0.15 | **0.30** | 0.20 | 0.20 | 0.15 |
| 新能源 | 0.20 | 0.15 | **0.30** | 0.20 | 0.15 |
| 消费 | 0.10 | 0.15 | 0.20 | **0.35** | 0.20 |
| 默认 | 0.20 | 0.20 | 0.20 | 0.20 | 0.20 |

**工作量**：2 天  
**验证**：同一瓶颈节点在不同行业下评分排序发生合理变化

---

### 17E.3 LLM 评分规则化

**问题**：5 个维度的 0-10 分全部由 LLM 凭"世界知识"评判，不同模型评分差异可能达 2-3 分。

**改动**：
| 文件 | 改动 |
|------|------|
| `chain/prompts/bottleneck.md` | 为每个维度添加刻度锚定（anchoring） |
| `chain/bottleneck.py` | 评分后进行标准化（z-score normalization） |

**刻度锚定示例**（稀缺性）：
```
0-2: 全球供应商 >20 家，无产能瓶颈（如：普通钢材）
3-4: 供应商 10-20 家，存在区域性供应限制
5-6: 供应商 5-10 家，部分被少数企业垄断
7-8: 供应商 3-5 家，全球产能紧张（如：EUV 光刻机关键镜头）
9-10: 供应商 1-2 家，几乎完全垄断（如：ASML EUV 光刻机）
```

**工作量**：3 天  
**验证**：同一产业链使用不同 LLM provider，瓶颈评分差异 < 1.5 分

---

### 17E.4 产业链版本管理

**问题**：每次拆解同一产品，结果都可能不同。无法追踪变化、无法对比不同版本。

**改动**：
| 文件 | 改动 |
|------|------|
| `chain/decomposer.py` | 拆解结果自动存储版本（带时间戳和 LLM 模型标注） |
| `chain/models.py` | `ChainGraph` 增加 `version` / `created_at` / `model_used` 字段 |
| 新建 `chain/chain_store.py` | 产业链版本存储（SQLite） |
| `web/static/js/phases.js` | 拆解页面支持查看历史版本和差异对比 |

**工作量**：3 天  
**验证**：同一产品拆解两次后，可查看两次结果的差异对比

---

## Phase 17F：体验与可视化（2~3 周）

> **目标**：提升前端交互体验，让分析师能快速理解和追溯决策逻辑。

### 17F.1 决策链路追溯

**问题**：看到最终排序无法理解"为什么 A > B"。

**改动**：
| 文件 | 改动 |
|------|------|
| `web/static/js/phases.js` | 观察池标的卡片增加"决策路径"展开面板 |
| `web/decision_api.py` | 新增 `/api/decision/trace/{ticker}` — 返回完整决策链路 |

**决策链路展示**：
```
L1 宏观：震荡市，降低风险偏好 → 建议减少进攻性持仓
  ↓
L2 组合：NVDA 目标权重 12%（当前 18%）→ 建议减仓
  ↓
L3 战术：短期支撑位 $120，催化剂 7/15 财报 → 持有等财报
  ↓
L4 执行：暂不操作，等待催化剂触发
  ↓
投委会：3:1 通过（风控官反对，认为持仓过重）
```

**工作量**：3 天

---

### 17F.2 风险仪表盘

**改动**：
| 文件 | 改动 |
|------|------|
| `web/static/js/phases.js` | 决策中心新增"风险面板"tab |
| `web/static/css/decision.css` | 风险面板样式 |

**面板内容**：
- 仓位饼图（按标的/板块）
- VaR/Beta/Sharpe 指标卡片
- 持仓相关性热力图
- 集中度预警（HHI > 0.25 标红）
- 历史回撤曲线

**工作量**：3 天

---

### 17F.3 催化剂日历视图

**改动**：
| 文件 | 改动 |
|------|------|
| `web/static/js/phases.js` | 催化剂 tab 增加日历视图 |
| `web/decision_api.py` | `/api/catalysts/calendar` — 按日期聚合催化剂 |

**展示**：月历格式，每天显示当日到期的催化剂事件，倒数 ≤3 天标红。

**工作量**：2 天

---

### 17F.4 A/B 对比功能

**问题**：修改权重/参数后需要重跑全流程才能看到影响，无快速对比。

**改动**：
| 文件 | 改动 |
|------|------|
| 新建 `watchlist/ab_compare.py` | 参数配置快照 + 对比分析 |
| `web/static/js/phases.js` | "对比分析"面板 |

**对比维度**：
- 不同瓶颈权重 → 排名变化
- 不同约束参数 → 持仓变化
- 不同 LLM provider → 评分差异

**工作量**：3 天

---

## 进度追踪

| Phase | 任务数 | 预计工作量 | 状态 |
|-------|--------|-----------|------|
| 17A 数据基础修复 | 5 项 | 9.5 天 | ⬜ 待开始 |
| 17B 技术缺陷修复 | 5 项 | 5.5 天 | ⬜ 待开始 |
| 17C 风控量化体系 | 3 项 | 13 天 | ⬜ 待开始 |
| 17D 闭环反馈贯通 | 5 项 | 9 天 | ⬜ 待开始 |
| 17E 规则引擎优化 | 4 项 | 10 天 | ⬜ 待开始 |
| 17F 体验与可视化 | 4 项 | 11 天 | ⬜ 待开始 |
| **合计** | **26 项** | **~58 天** | |

---

## 优先级与依赖关系

```
17A (数据基础) ──→ 17C (风控量化) ──→ 17F (可视化)
     │                  ↑
     ↓                  │
17B (技术缺陷) ──→ 17D (闭环反馈) ──→ 17E (规则优化)
```

- **17A → 17C**：宏观数据和基本面数据是风控指标计算的前提
- **17B → 17D**：约束硬验证是闭环反馈可靠运行的前提
- **17D → 17E**：闭环跑通后才能基于反馈优化规则
- **17C → 17F**：风控指标计算完成后才能展示风险面板

---

*本计划基于系统评审报告和三项专项代码审计结果综合制定。*
