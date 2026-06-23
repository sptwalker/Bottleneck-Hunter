# BottleneckHunter 交易决策系统设计文档

> **版本**：v4.0 — LLM 驱动的层级决策 + 多模型投委会 + 用户交互
> **日期**：2026-06-24

---

## 目录

1. [系统总览](#一系统总览)
2. [信息输入系统](#二信息输入系统)
3. [四层决策体系](#三四层决策体系)
4. [多 LLM 投委会机制](#四多-llm-投委会机制)
5. [闭环机制](#五闭环机制)
6. [用户交互接口](#六用户交互接口)
7. [数据库设计](#七数据库设计)
8. [API 设计](#八api-设计)
9. [前端设计](#九前端设计)
10. [实施路线图](#十实施路线图)

---

## 一、系统总览

### 1.1 设计理念

传统规则引擎难以覆盖复杂市场的各种情况，且缺乏长期视角。本系统采用 **LLM 驱动决策、规则辅助感知** 的架构：

- **规则系统**：只负责收集、结构化数据，输出信号供 LLM 参考
- **LLM 系统**：负责所有判断和决策，具备理解复杂矛盾信号的能力
- **投委会**：多个 LLM 从不同角度交叉验证，降低单一模型偏差
- **用户**：保留最终决定权，可质询、修改、拒绝任何建议

### 1.2 核心架构

```
┌─────────────────────────────────────────────────────────────────┐
│                      信息输入系统                                │
│  市场宏观 + 行业板块 + 个股数据 + K线图 + 新闻 + 期权 + 资金流   │
└─────────────────────────┬───────────────────────────────────────┘
                          ▼
┌─────────────────────────────────────────────────────────────────┐
│  Layer 1: 宏观环境策略      （每周生成，每日增量检查）            │
│  Layer 2: 中长期组合策略    （每周更新，日常偏离检查）            │
│  Layer 3: 短期战术计划      （每日更新）                         │
│  Layer 4: 账户执行策略      （每日生成）                         │
└─────────────────────────┬───────────────────────────────────────┘
                          ▼
┌─────────────────────────────────────────────────────────────────┐
│                   多 LLM 投委会评审                              │
│  风险控制官 + 成长投资人 + 价值投资人 + 反向投资人               │
│  独立评审 → 争议讨论 → 共识修改                                 │
└─────────────────────────┬───────────────────────────────────────┘
                          ▼
┌─────────────────────────────────────────────────────────────────┐
│                   用户确认 + 交互接口                            │
│  查看决策详情 → 质询 LLM → 修改参数 → 批准/拒绝                 │
└─────────────────────────┬───────────────────────────────────────┘
                          ▼
┌─────────────────────────────────────────────────────────────────┐
│                      闭环反馈系统                                │
│  交易反馈 → 催化剂追踪 → 自动复盘 → 经验卡片 → 观察池淘汰       │
└─────────────────────────────────────────────────────────────────┘
```

### 1.3 关键设计原则

| 原则 | 说明 |
|------|------|
| LLM 做判断，规则做感知 | 规则系统只输出结构化信号，不做买卖决策 |
| 策略稳定性优先 | 长期策略不因短期波动频繁切换 |
| 增量更新 | 宏观/中长期策略默认有效，只在重大变化时重建 |
| 多模型交叉验证 | 投委会机制降低单一 LLM 的幻觉和偏差 |
| 用户最终决定权 | 所有交易建议必须经用户确认 |
| 信息闭环 | 交易结果反馈回决策系统，不断学习优化 |

---

## 二、信息输入系统

### 2.1 信息分类与数据源

LLM 需要足够丰富的信息才能做出高质量决策。系统分三个层级收集信息：

#### A. 市场宏观信息（MarketContextCollector）

| 数据类别 | 数据项 | 数据源 | 更新频率 |
|----------|--------|--------|----------|
| 大盘指数 | SPX/NASDAQ/DOW 价格、涨跌幅、成交量、均线、RSI、MACD | yfinance | 每日 |
| 波动率 | VIX 指数、水平判断（low/normal/elevated/high） | yfinance | 每日 |
| 行业板块 | 11 个板块涨跌幅、相对强弱、板块内 Top Movers | yfinance | 每日 |
| 市场情绪 | 恐惧贪婪指数、Put/Call Ratio、涨跌家数比 | CNN/CBOE | 每日 |
| 宏观经济 | GDP、失业率、CPI、联储利率、FOMC 日期 | FRED/公开数据 | 事件驱动 |
| 资金流向 | ETF 资金流（QQQ/SPY/XLK等）、板块净流入流出 | yfinance | 每日 |
| 地缘风险 | 地缘政治事件、监管动态、市场结构异常 | 新闻聚合 | 事件驱动 |
| 重要新闻 | 市场重大新闻标题、情绪、相关性评分 | RSS/新闻 API | 每日 |

#### B. 个股深度信息（StockAnalysisCollector）

| 数据类别 | 数据项 | 数据源 |
|----------|--------|--------|
| 价格技术 | K线、支撑/阻力位、技术形态、均线、RSI、MACD、布林带、量价关系 | yfinance |
| 基本面 | PE/PB/PS/PEG/EV-EBITDA、营收增速、毛利率、ROE、ROIC、负债率、现金流 | yfinance |
| 分析师 | 覆盖机构数、评级分布（买入/持有/卖出）、目标价（均值/高/低）、近期评级变化 | yfinance |
| 期权市场 | Put/Call Ratio、隐含波动率、IV Rank、异常交易活动、最大痛苦点 | yfinance/options |
| 机构持仓 | 机构持仓比例、近期增减持、前十大机构 | SEC 13F |
| 内部人交易 | 高管买卖方向、金额、频率 | SEC Form 4 |
| 同行对比 | 估值/成长/盈利/动量在同行中的排名、市场份额趋势 | 计算 |
| 社交情绪 | Reddit/Twitter/StockTwits 情绪、讨论热度 | 可选 |

#### C. 瓶颈定位信息（来自已有系统）

| 数据类别 | 数据项 | 数据源 |
|----------|--------|--------|
| 瓶颈评分 | 稀缺性/不可替代性/供需缺口/定价权/技术壁垒（5 维 0-10 分） | Phase 1 分析结果 |
| 产业链层级 | L1/L2/L3/L4（上游更稀缺 → 更高权重） | Phase 1 |
| 市场地位 | 垄断/寡头/竞争 | Phase 2 供应商评估 |
| 客户验证 | 大客户订单、认证状态 | Phase 2 |
| 交叉验证 | 多模型验证结果、置信度 | Phase 4 |

### 2.2 K线图生成（ChartGenerator）

为支持 Vision LLM 分析技术形态，系统生成多时间尺度的 K 线图：

| 图表类型 | 内容 | 用途 |
|----------|------|------|
| 大盘 K 线（3 个月） | SPX/NASDAQ 日 K + 均线 + 标注关键事件 | Layer 1 宏观判断 |
| 行业对比图（1 个月） | 11 个板块相对表现热力图 | Layer 1 板块轮动 |
| 个股 K 线（3 时间尺度） | 5 日 / 1 月 / 3 月 K 线 + 成交量 + 技术指标 | Layer 3 战术判断 |

图表以 PNG 生成，base64 编码后传入 Vision LLM。

### 2.3 信号感知层（SignalPerception）

规则系统将原始数据转化为 LLM 易于理解的结构化信号：

```python
class SignalPerception:
    """规则引擎：将数据转化为结构化信号，NOT 决策"""
    
    def perceive(self, entry_id: str) -> dict:
        """为单只股票生成完整信号快照"""
        return {
            'ticker': str,
            'timestamp': str,
            'score': {
                'current': float, 'peak': float,
                'trend': str,  # rising | declining | stable
                'change_from_peak': float,
            },
            'strategy': {
                'signal': str,  # bullish | neutral | bearish
                'confidence': int,
                'core_logic': str,
            },
            'catalysts': [{
                'type': str, 'date': str, 'days_until': int,
                'urgency': str,  # imminent(<7d) | near(<30d) | far(>30d)
                'expected_impact': str, 'confidence': float,
                'outcome': str,  # pending | realized | failed
            }],
            'price': {
                'current': float, 'change_pct': float,
                'support': float, 'resistance': float,
                'volume_trend': str, 'technical_pattern': str,
            },
            'bottleneck': {
                'layer': str, 'scarcity_score': int,
                'market_position': str,
            },
            'intelligence': {
                'news_sentiment': str, 'insider_direction': str,
                'options_signal': str, 'smart_money': str,
            },
            'position': {
                'lifecycle_stage': str,  # watching | holding | exiting | archived
                'quantity': int, 'cost_basis': float,
                'unrealized_pnl_pct': float, 'days_held': int,
                'position_weight': float,
            },
            'history': {
                'buy_reason': str,
                'rejected_signals_30d': int,
                'last_signal': dict,
            },
            'competitors': [{'ticker': str, 'score': float, 'trend': str}],
        }
```

---

## 三、四层决策体系

### 3.1 架构设计

四层决策体系采用**递进推理**模式，每层基于上层输出，视角不同、更新频率不同、稳定性要求不同：

```
┌─────────────────────────────────────────────────────────────┐
│  Layer 1: 宏观环境策略（Market Regime Strategy）            │
│  输入：大盘+板块+宏观+新闻+K线图                              │
│  输出：市场阶段、风险偏好、板块轮动方向                        │
│  更新：每周全面生成，每日轻量级检查（增量更新）                │
│  稳定性：★★★★★（默认有效，重大变化才重建）                  │
└─────────────────────────────────────────────────────────────┘
                               ↓
┌─────────────────────────────────────────────────────────────┐
│  Layer 2: 中长期组合策略（Strategic Portfolio Plan）        │
│  输入：宏观策略 + 观察池信号 + 账户状态                       │
│  输出：目标仓位、行业配置、核心持仓、备选股票池                │
│  更新：每周更新，每日偏离度检查（微调为主）                    │
│  稳定性：★★★★（偏离<10%不调整）                            │
└─────────────────────────────────────────────────────────────┘
                               ↓
┌─────────────────────────────────────────────────────────────┐
│  Layer 3: 短期战术计划（Tactical Execution Plan）           │
│  输入：中长期策略 + 最新市场/个股信息 + K线图                 │
│  输出：本周买卖时机、价格区间、止损止盈、催化剂追踪            │
│  更新：每日更新                                              │
│  稳定性：★★（灵活应变市场变化）                             │
└─────────────────────────────────────────────────────────────┘
                               ↓
┌─────────────────────────────────────────────────────────────┐
│  Layer 4: 账户执行策略（Portfolio Execution Decision）      │
│  输入：战术计划 + 账户状态 + 资金管理规则                     │
│  输出：可执行操作序列（股数、价格、优先级、资金安排）          │
│  更新：每日生成                                              │
│  稳定性：★（每日全新生成）                                  │
└─────────────────────────────────────────────────────────────┘
```

### 3.2 Layer 1：宏观环境策略

#### **职责**
判断当前市场处于什么阶段（牛市/熊市/震荡/转折），应采取什么风险偏好，哪些行业值得配置。

#### **更新模式：观察式增量更新**

- **基准策略**：每周一生成完整的宏观判断（或重大事件触发）
- **日常检查**：每日运行轻量级检查，判断基准策略是否仍然有效
- **触发重建**：只有当市场环境发生重大变化时（VIX 突破 30、美联储紧急降息、地缘危机）才全面重建

#### **Prompt 要点**

```
输入：
- 大盘指数数据（SPX/NASDAQ/VIX）+ 近期走势
- 行业板块表现（11 个板块涨跌、资金流向）
- 市场情绪指标（恐惧贪婪、Put/Call Ratio）
- 宏观经济数据（GDP/CPI/失业率/联储政策）
- K 线图（SPX 3 个月、行业对比图）
- 重要市场新闻

任务：
1. 判断市场阶段（牛市/熊市/震荡/转折）+ 置信度
2. 评估风险偏好（进攻/平衡/防守）+ 推荐现金比例
3. 识别板块轮动方向（哪些板块走强/走弱）
4. 给出仓位建议（重点行业、规避行业）

输出：JSON 格式，包含完整推理过程（800-1200 字）
```

#### **增量检查 Prompt**

```
输入：当前有效策略（X 天前生成）+ 今日市场数据

任务：快速判断当前策略是否仍然有效？

输出：
- strategy_status: valid | needs_minor_tweak | needs_major_revision
- minor_tweaks: 轻微调整建议（如板块权重微调）
- major_revision_triggers: 如需重建，列出触发因素

原则：
- 默认策略有效，除非有明确证据
- 轻微偏差不触发调整
- 只有重大变化才全面重建
```

#### **数据持久化**

```sql
CREATE TABLE macro_strategies (
    id TEXT PRIMARY KEY,
    version INTEGER,
    effective_from TEXT,
    effective_until TEXT,  -- NULL = 当前有效
    market_regime TEXT,  -- JSON
    risk_appetite TEXT,
    sector_rotation TEXT,
    positioning_advice TEXT,
    full_reasoning TEXT,
    superseded_by TEXT  -- 被哪个版本替代
);

CREATE TABLE macro_daily_checks (
    id TEXT PRIMARY KEY,
    date TEXT,
    current_strategy_id TEXT,
    adjustment_needed BOOLEAN,
    adjustment_type TEXT,
    daily_commentary TEXT
);
```

### 3.3 Layer 2：中长期组合策略

#### **职责**
制定未来 1-3 个月的投资组合蓝图：目标仓位、行业配置、核心持仓 vs 波段持仓。

#### **更新模式：稳定为主，定期微调**

- **基准策略**：每周更新（基于 Layer 1 宏观策略）
- **日常维护**：每日检查持仓是否偏离目标，轻微偏离（<10%）不调整
- **触发调整**：宏观策略变化 OR 持仓偏离 >10% OR 新重大催化剂出现

#### **Prompt 要点**

```
输入：
- Layer 1 宏观策略（市场阶段、风险偏好、板块轮动）
- 观察池个股信号（完整的 SignalPerception 输出）
- 当前账户状态（持仓、现金、盈亏）
- 历史复盘教训（成功案例、失败教训、系统性偏差）

任务：
1. 制定目标仓位配置（现金/股票/对冲比例）
2. 行业配置（各行业目标权重，基于板块轮动）
3. 个股选择（核心持仓 vs 波段持仓）
4. 风险管理（单股上限、行业集中度上限）

输出：
- strategic_positioning: 目标配置、风险预算
- sector_allocation: 各行业目标权重
- stock_selection: 核心持仓列表 + 战术持仓列表
- rebalancing_triggers: 什么情况下需要调仓

核心原则：
- 核心持仓持有期 3-6 个月，基于瓶颈逻辑
- 战术持仓持有期 1-2 个月，基于催化剂
- 现金比例 20-30%，根据市场环境调整
```

#### **偏离度检查**

```
输入：目标策略 + 当前实际持仓

任务：检查实际持仓是否偏离目标

容忍度：
- 现金比例：±5%
- 板块权重：±8%
- 个股权重：±5%

输出：
- 偏离分析（各维度偏离度）
- rebalance_needed: true/false
- rebalance_actions: 具体调仓操作（如需要）
```

#### **数据持久化**

```sql
CREATE TABLE strategic_plans (
    id TEXT PRIMARY KEY,
    version INTEGER,
    macro_strategy_id TEXT,
    effective_from TEXT,
    effective_until TEXT,
    overall_stance TEXT,
    target_allocation TEXT,  -- JSON
    sector_allocation TEXT,
    stock_selection TEXT,
    full_reasoning TEXT
);

CREATE TABLE portfolio_deviation_checks (
    id TEXT PRIMARY KEY,
    date TEXT,
    strategic_plan_id TEXT,
    deviation_pct REAL,
    rebalance_needed BOOLEAN,
    rebalance_actions TEXT
);
```

### 3.4 Layer 3：短期战术计划

#### **职责**
将中长期策略转化为具体的买卖时机：什么价格买？什么时候卖？催化剂如何追踪？

#### **更新模式：每日更新**

基于 Layer 2 目标 + 最新市场信息，每日生成本周（未来 5-10 天）的战术计划。

#### **Prompt 要点**

```
输入：
- Layer 2 中长期策略（目标持仓列表）
- 最新市场信息（大盘走势、行业表现）
- 个股深度分析（技术面、基本面、期权、机构持仓）
- 个股 K 线图（5 日/1 月/3 月）
- 催化剂时间表（按紧迫度排序）

任务：
对中长期策略中的每只股票，制定本周执行计划：

1. 买入时机：立即 vs 等待回调？回调目标价位？
2. 卖出时机：是否到止盈/止损点？技术形态是否见顶？
3. 加减仓判断：持仓股票评分上升是否加仓？
4. 催化剂驱动：即将到来的催化剂如何把握时机？

输出：
- tactical_plans: 每只股票的战术计划
  - action: buy/sell/add/reduce/hold
  - timing: immediate/wait_for_pullback/after_catalyst
  - entry_plan: 理想价格、可接受区间、技术确认信号
  - risk_management: 止损、止盈、仓位上限
  - catalyst_watch: 关键日期、预期影响
```

#### **催化剂时效管理**

```python
class CatalystMonitor:
    """催化剂监控器"""
    
    def detect_upcoming_catalysts(self, entry_id: str) -> list:
        """从多个数据源检测催化剂"""
        sources = [
            '财报日期（earnings）',
            '策略记录中的时间线',
            '新闻中提取的事件日期（LLM）',
            'SEC 文件中的关键日期',
        ]
        
        return [{
            'id': str,
            'type': str,  # earnings/product_launch/...
            'date': str,
            'days_until': int,
            'urgency': str,  # imminent(<7d) | near(<30d) | far(>30d)
            'confidence': float,
            'expected_impact': str,
        }]
    
    def mark_catalyst_outcome(self, catalyst_id: str):
        """催化剂日期+2天后，自动判断结果"""
        # LLM 分析：realized | failed | neutral
        # 触发后续动作：兑现→考虑加仓，落空→立即止损
```

### 3.5 Layer 4：账户执行策略

#### **职责**
将战术计划转化为可执行的操作序列，考虑现金流、优先级、风险约束。

#### **更新模式：每日生成**

Layer 3 输出的是**理想化的买卖计划**，Layer 4 要解决：
- 账户现金是否充足？
- 多个买入信号如何排优先级？
- 是否需要先卖出腾挪资金？
- 单笔交易规模是否合理？

#### **Prompt 要点**

```
输入：
- Layer 3 战术计划（多只股票的买卖计划）
- 账户状态（总资产、现金、持仓）
- 历史交易反馈（拒绝模式、偏好学习）

任务：
制定今日可执行的操作序列

关键约束：
1. 现金约束：可用现金 $X，不足则先卖后买
2. 仓位约束：单股<20%，单板块<40%，现金>15%
3. 交易成本：单笔最小 $1000
4. 风险控制：单日交易规模<总资产 30%

优先级规则：
卖出优先级（先卖后买）：
  1. 止损单（最高）
  2. 止盈单
  3. 不在策略中的持仓
  4. 超重股票

买入优先级：
  1. 核心持仓建仓
  2. 催化剂<7天的战术持仓
  3. 加仓信号
  4. 新战术持仓

输出：
- operations: 操作序列（sequence, phase, action, execution, rationale）
- execution_summary: 资金安排、风险检查
- contingency_plans: 备选方案（如价格不合适）
```

#### **数据持久化**

```sql
CREATE TABLE daily_decisions (
    id TEXT PRIMARY KEY,
    date TEXT,
    macro_check TEXT,  -- JSON
    deviation_check TEXT,
    tactical_plans TEXT,
    execution_plan TEXT,
    committee_review TEXT,  -- 投委会意见
    final_execution_plan TEXT,  -- 应用修改后
    status TEXT,  -- awaiting_confirmation | confirmed | rejected
    full_context TEXT  -- 完整上下文供对话使用
);
```

---

## 四、多 LLM 投委会机制

### 4.1 设计理念

单一 LLM 可能存在：
- **幻觉**：生成看似合理但错误的判断
- **偏差**：过度乐观或过度保守
- **盲点**：忽视某些风险维度

**投委会机制**通过多个 LLM 从不同角度独立评审、交叉验证，降低系统性错误。

### 4.2 投委会成员配置

| 成员 | 角色 | LLM 模型 | 关注焦点 | 视角 |
|------|------|----------|----------|------|
| 风险控制官 | Risk Officer | DeepSeek | 仓位管理、止损纪律、黑天鹅风险 | 保守 |
| 成长投资人 | Growth Investor | Qwen-Max | 催化剂可靠性、成长逻辑可持续性 | 进攻 |
| 价值投资人 | Value Investor | GLM-4-Plus | 估值合理性、安全边际、护城河 | 平衡 |
| 反向投资人 | Contrarian | Kimi | 市场情绪、从众风险、逆向机会 | 质疑 |

### 4.3 评审流程

```
Layer 4 生成执行计划
         ↓
┌────────────────────────────────────────┐
│   第一轮：独立评审（并行）              │
│   4 位委员各自评审执行计划              │
│   输出：vote + concerns + suggestions   │
└────────────────┬───────────────────────┘
                 ↓
         意见是否冲突？
                 ↓
         是 ─→ 第二轮：圆桌讨论
                 主持人综合意见
                 达成共识建议
                 ↓
┌────────────────────────────────────────┐
│   汇总共识                             │
│   - 投票统计（通过率 ≥60% 则批准）     │
│   - 共识修改（≥50%委员建议 → 强制修改）│
│   - 少数派意见记录                     │
└────────────────┬───────────────────────┘
                 ↓
         最终决策 + 投委会意见
```

### 4.4 评审 Prompt 模板

```
# 投资委员会评审

你是投委会成员：**{member_name}**（{role}）

## 你的职责
{focus}

## 你的视角
{perspective}

## Portfolio Manager 的执行计划
{execution_plan}

---

## 评审任务

从你的专业角度独立评审。

### 评审要点

**风险控制官关注**：
- 止损设置是否合理？
- 单笔/单日交易规模是否过大？
- 仓位是否过重？现金是否充足？

**成长投资人关注**：
- 催化剂是否真实可靠？
- 成长逻辑是否可持续？

**价值投资人关注**：
- 估值是否合理？
- 安全边际是否足够？

**反向投资人关注**：
- 市场是否过于乐观/悲观？
- 决策是否从众？

### 输出格式

{
  "vote": "approve | approve_with_modification | reject",
  "confidence": 8,
  "key_concerns": [...],
  "suggestions": [
    {
      "ticker": "TSLA",
      "field": "timing",
      "original": "execute_within_1day",
      "suggested": "delay_until_after_earnings",
      "reason": "...",
      "priority": "high"
    }
  ],
  "strengths": [...],
  "overall_assessment": "..."
}
```

### 4.5 圆桌讨论机制

当投委会成员意见分歧较大时（如 2 人赞成 TSLA 加仓、2 人反对），触发圆桌讨论：

```
输入：
- 争议点（ticker + issue）
- 不同观点（各成员的意见 + 理由）
- 执行计划原方案

任务：
综合各方观点，给出平衡建议

输出：
{
  "consensus_reached": true,
  "final_recommendation": "delay_until_after_earnings",
  "reasoning": "综合各方意见，建议折中方案：等财报后...",
  "minority_view": "成长投资人坚持...",
  "risk_level": "medium"
}
```

### 4.6 共识汇总规则

| 规则 | 说明 |
|------|------|
| 多数通过 | 通过率 ≥60%（4 人中 ≥2.4 人）→ 批准 |
| 共识修改 | 超过 50% 委员建议同一修改 → 强制应用 |
| 少数派意见 | 即使不通过，也记录备查 |
| 讨论覆盖 | 圆桌讨论结果优先于独立评审 |

### 4.7 数据持久化

```sql
CREATE TABLE committee_reviews (
    id TEXT PRIMARY KEY,
    decision_id TEXT,
    date TEXT,
    approved BOOLEAN,
    approval_rate REAL,
    votes TEXT,  -- JSON: {member: vote}
    modifications TEXT,  -- JSON: 共识修改列表
    discussions TEXT,  -- JSON: 圆桌讨论记录
    summary TEXT,
    created_at TEXT
);
```

---

## 五、闭环机制

决策系统必须从历史交易中学习，形成完整的信息闭环。

### 5.1 交易反馈闭环

#### **用户确认交易后**

```python
async def on_trade_confirmed(trade_id: str):
    """用户确认交易后的状态更新"""
    
    # 1. 更新股票生命周期状态
    if action == 'buy':
        update_stock_state(
            lifecycle_stage='holding',
            buy_price=..., buy_score=..., buy_reason=...
        )
        
        # 激活催化剂监控
        catalysts = detect_upcoming_catalysts(entry_id)
        update_stock_state(active_catalysts=catalysts)
    
    elif action == 'sell':
        update_stock_state(
            lifecycle_stage='exited',
            sell_price=..., holding_period_days=...
        )
        
        # 触发自动复盘
        trigger_trade_review(trade_id)
    
    # 2. 标记信号为已执行
    update_signal_history(signal_id, action_taken='confirmed')
```

#### **用户拒绝交易后**

```python
async def on_trade_rejected(trade_id: str, reason: str):
    """用户拒绝交易后的冷却机制"""
    
    # 1. 记录拒绝
    update_signal_history(signal_id, action_taken='rejected')
    
    # 2. 累计拒绝计数
    rejection_count = increment_rejection_count(ticker, action_type)
    
    # 3. 设置冷却期
    if rejection_count >= 2:
        set_signal_cooldown(ticker, action_type, cooldown_days=7)
    
    if rejection_count >= 3:
        set_signal_cooldown(ticker, action_type, cooldown_days=90)
    
    # 4. 学习用户偏好（隐式）
    # 连续拒绝某类操作 → 降低该类建议权重
```

### 5.2 催化剂时效管理

#### **催化剂生命周期**

```
检测 → 监控 → 兑现判定 → 结果反馈 → 后续动作
```

#### **催化剂结果判定**

```python
async def mark_catalyst_outcome(catalyst_id: str):
    """催化剂日期+2天后，自动判断结果"""
    
    catalyst = get_catalyst(catalyst_id)
    
    if days_since(catalyst['date']) > 2:
        # LLM 分析判断
        prompt = f"""
        催化剂事件：{catalyst['type']} @ {catalyst['date']}
        预期影响：{catalyst['expected_impact']}
        
        请根据事后数据判断：
        1. 查看催化剂日期后3天的新闻
        2. 查看股价变化
        3. 判断：realized | failed | neutral
        
        输出：{"outcome": "realized", "evidence": "..."}
        """
        
        result = await llm.ainvoke(prompt)
        
        # 更新催化剂结果
        update_catalyst_outcome(catalyst_id, result['outcome'])
        
        # 触发后续动作
        if result['outcome'] == 'failed':
            create_signal(entry_id, type='sell', reason='catalyst_failed')
        elif result['outcome'] == 'realized':
            create_signal(entry_id, type='add', reason='catalyst_realized')
```

### 5.3 自动复盘机制

#### **触发时机**
股票卖出后，自动生成复盘报告。

#### **复盘 Prompt**

```
# 交易复盘

## 交易基本信息
- 股票：{ticker}
- 买入：{buy_date} @ ${buy_price}
- 卖出：{sell_date} @ ${sell_price}
- 持仓天数：{holding_days}
- 盈亏：{pnl_pct}%

## 买入决策回顾
- 买入理由：{buy_reason}
- 买入时评分：{buy_score}
- 买入时催化剂：{catalysts_at_buy}

## 持仓期间重大事件
{events_during_holding}

## 卖出决策回顾
- 卖出理由：{sell_reason}
- 催化剂结果：{catalyst_outcomes}

---

## 复盘任务

分析得失，提炼可复用的经验或教训。

输出：
{
  "verdict": "success | partial_success | failure",
  "key_factors": {
    "what_went_right": [...],
    "what_went_wrong": [...],
    "luck_vs_skill": "70%技能 + 30%运气"
  },
  "lessons_learned": [
    {
      "category": "catalyst_timing",
      "lesson": "催化剂前3天买入更安全",
      "applicability": "所有催化剂驱动型买入",
      "confidence": 0.8
    }
  ],
  "strategy_adjustments": [
    {
      "aspect": "entry_timing",
      "change": "催化剂买入提前量：7天 → 3-5天"
    }
  ]
}
```

#### **经验卡片生成**

置信度 ≥0.7 的教训自动生成经验卡片，供 Layer 2 策略更新时参考。

### 5.4 观察池淘汰机制

#### **淘汰条件**

| 条件 | 说明 | 动作 |
|------|------|------|
| 评分连续 7 天下降 | 逐级降级 | focus → normal → potential → 淘汰 |
| 评分 <40 | 直接淘汰 | 持仓中先强制卖出 |
| 催化剂全部落空 | 立即淘汰 | 无宽限期 |
| 90 天无起色 | 长期停滞 | 无催化剂且评分<峰值 80% |

#### **持仓保护**

有持仓的股票淘汰前给予 3 天宽限期：
1. 生成强制卖出信号
2. 等待用户确认
3. 卖出完成后再淘汰

#### **替代推荐**

```python
def suggest_replacement(eliminated_tier: str) -> list:
    """从分析历史推荐替代股票"""
    
    # 查找最近 30 天 Phase 4 分析中评分>70 但未入选的
    # 优先选择有即将到来催化剂的
    # 返回 Top 3 候选
```

### 5.5 数据模型

```sql
-- 股票生命周期状态
CREATE TABLE stock_states (
    entry_id TEXT PRIMARY KEY,
    lifecycle_stage TEXT,  -- watching | signaled | holding | exiting | archived
    last_trade_action TEXT,
    last_trade_at TEXT,
    buy_price REAL,
    buy_score REAL,
    active_catalysts TEXT,  -- JSON
    catalyst_outcome TEXT,  -- JSON: {catalyst_id: outcome}
    consecutive_decline_days INTEGER,
    peak_score REAL,
    elimination_grace_period_until TEXT
);

-- 信号历史
CREATE TABLE signal_history (
    id TEXT PRIMARY KEY,
    entry_id TEXT,
    signal_type TEXT,
    trigger_reason TEXT,
    action_taken TEXT,  -- confirmed | rejected | ignored | expired
    trade_id TEXT,
    created_at TEXT,
    expires_at TEXT
);

-- 交易复盘
CREATE TABLE trade_reviews (
    id TEXT PRIMARY KEY,
    trade_id TEXT,
    verdict TEXT,
    key_factors TEXT,
    lessons_learned TEXT,
    strategy_adjustments TEXT,
    created_at TEXT
);

-- 经验卡片
CREATE TABLE experience_cards (
    id TEXT PRIMARY KEY,
    category TEXT,
    lesson TEXT,
    applicability TEXT,
    confidence REAL,
    source_trade_id TEXT,
    created_at TEXT
);
```

---

## 六、用户交互接口

### 6.1 设计理念

用户不应只是被动接受 AI 决策，而应能够：
- **质询**：为什么要卖出 INTC？逻辑是什么？
- **修改**：AMD 建仓能不能改成 $8000？
- **约束**：我不想买金融股
- **学习**：怎么判断催化剂是否可靠？

**策略顾问对话**提供一个实时 WebSocket 接口，用户可以与决策 LLM 直接沟通。

### 6.2 对话接口架构

```
前端 WebSocket 客户端 → WebSocket 连接 → 策略对话服务器（FastAPI）
    → 主力 LLM（流式响应） → 解析修改指令 → 更新决策 + 学习偏好
```

### 6.3 偏好学习机制

从用户对话中自动学习偏好：风险偏好、持仓时间偏好、交易规模偏好、行业偏好、操作风格。

### 6.4 数据模型

```sql
CREATE TABLE user_llm_conversations (
    id TEXT PRIMARY KEY,
    decision_id TEXT,
    user_message TEXT,
    llm_response TEXT,
    modifications_made TEXT,
    created_at TEXT
);

CREATE TABLE user_preference_history (
    id TEXT PRIMARY KEY,
    preference_type TEXT,
    value TEXT,
    learned_from TEXT,
    confidence REAL,
    created_at TEXT
);
```

---

## 七、完整决策流程

### 每日决策工作流

```python
async def daily_decision_workflow():
    # Layer 1: 宏观策略检查（增量更新）
    macro_check = await MacroStrategyManager().daily_check(...)
    
    # Layer 2: 中长期策略偏离检查
    deviation_check = await StrategyDeviationChecker().check_deviation(...)
    
    # Layer 3: 短期战术规划
    tactical_plans = await TacticalExecutor().plan_tactics(...)
    
    # Layer 4: 账户执行策略
    execution_plan = await PortfolioExecutionPlanner().plan_execution(...)
    
    # 投委会评审
    committee_review = await InvestmentCommittee().review(
        execution_plan, context, user_preferences
    )
    
    # 应用共识修改
    final_plan = apply_committee_modifications(execution_plan, committee_review)
    
    # 保存决策，等待用户确认
    decision_id = store.save_daily_decision({...})
    
    return decision_id
```

---

## 八、实施路线图

### Phase 8B.1：信息输入与 Layer 1-2（2 周）
- MarketContextCollector + ChartGenerator
- MacroStrategyManager（增量更新）
- StrategicPositioner（偏离检查）

### Phase 8B.2：Layer 3-4 + 投委会（2 周）
- TacticalExecutor + CatalystMonitor
- PortfolioExecutionPlanner
- InvestmentCommittee（4 成员）

### Phase 8B.3：闭环机制（1.5 周）
- 交易反馈（confirmed/rejected）
- 自动复盘 + 经验卡片
- 观察池淘汰策略

### Phase 8B.4：用户交互接口（1 周）
- WebSocket 对话接口
- 偏好学习机制
- 前端决策页面

### Phase 8B.5：集成测试与优化（1 周）
- 端到端测试
- 性能优化
- 文档完善

---

## 总结

本系统通过 **LLM 驱动的四层决策 + 多模型投委会 + 完整闭环反馈**，构建了一个能够学习、进化的智能交易决策系统。

**关键创新点**：
1. **增量更新机制**：避免频繁策略切换
2. **催化剂时效管理**：从检测到兑现判定的完整生命周期
3. **拒绝模式学习**：避免重复被拒绝的建议
4. **经验卡片系统**：可复用的投资教训
5. **实时对话接口**：用户与 AI 直接沟通
6. **多模型交叉验证**：降低单一 LLM 偏差
7. **用户最终决定权**：所有建议必须经用户确认

