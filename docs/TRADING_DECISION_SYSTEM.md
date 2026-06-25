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
9. [前端展示与交互设计](#九前端展示与交互设计)
10. [完整决策流程](#十完整决策流程)
11. [现有代码适配方案](#十一现有代码适配方案)
12. [LLM 调用成本估算](#十二llm-调用成本估算)
13. [数据迁移方案](#十三数据迁移方案)
14. [实施路线图](#十四实施路线图)

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

## 七、数据库设计

### 7.1 新增表（Phase 8B 专用，14 张）

```sql
-- Layer 1: 宏观策略
CREATE TABLE macro_strategies (
    id              TEXT PRIMARY KEY,
    version         INTEGER NOT NULL DEFAULT 1,
    regime          TEXT NOT NULL DEFAULT 'neutral',  -- bull/neutral/bear
    confidence      INTEGER DEFAULT 5,
    market_summary  TEXT DEFAULT '',
    key_signals     TEXT DEFAULT '[]',       -- JSON: [{name, value, interpretation}]
    strategy_text   TEXT DEFAULT '',
    valid_until     TEXT,                    -- 有效期
    created_at      TEXT NOT NULL,
    updated_at      TEXT
);

-- Layer 2: 组合策略
CREATE TABLE strategic_plans (
    id              TEXT PRIMARY KEY,
    macro_id        TEXT REFERENCES macro_strategies(id),
    version         INTEGER NOT NULL DEFAULT 1,
    allocation      TEXT DEFAULT '{}',       -- JSON: {ticker: {target_weight, reason}}
    sector_targets  TEXT DEFAULT '{}',       -- JSON: {sector: target_pct}
    risk_limits     TEXT DEFAULT '{}',       -- JSON: {max_position, max_sector, max_beta}
    deviation_rules TEXT DEFAULT '{}',       -- JSON: 偏离触发规则
    strategy_text   TEXT DEFAULT '',
    created_at      TEXT NOT NULL,
    updated_at      TEXT
);

-- Layer 3: 战术计划
CREATE TABLE tactical_plans (
    id              TEXT PRIMARY KEY,
    strategic_id    TEXT REFERENCES strategic_plans(id),
    entry_id        TEXT REFERENCES watchlist(id),
    ticker          TEXT NOT NULL,
    action          TEXT NOT NULL,           -- buy/sell/add/reduce/hold
    amount          REAL,
    target_price    REAL,
    stop_loss       REAL,
    catalyst_id     TEXT,                    -- 触发催化剂
    reasoning       TEXT DEFAULT '',
    confidence      INTEGER DEFAULT 5,
    created_at      TEXT NOT NULL,
    expires_at      TEXT                     -- 战术有效期
);

-- Layer 4: 执行策略
CREATE TABLE execution_plans (
    id              TEXT PRIMARY KEY,
    tactical_id     TEXT REFERENCES tactical_plans(id),
    entry_id        TEXT REFERENCES watchlist(id),
    ticker          TEXT NOT NULL,
    action          TEXT NOT NULL,
    shares          INTEGER,
    estimated_amount REAL,
    execution_method TEXT DEFAULT 'market',  -- market/limit/split
    split_plan      TEXT DEFAULT '{}',       -- JSON: 分批执行计划
    position_impact TEXT DEFAULT '{}',       -- JSON: 执行后仓位变化
    committee_status TEXT DEFAULT 'pending', -- pending/approved/rejected
    user_status     TEXT DEFAULT 'pending',  -- pending/confirmed/rejected
    user_reject_reason TEXT DEFAULT '',
    created_at      TEXT NOT NULL,
    confirmed_at    TEXT,
    executed_at     TEXT
);

-- 投委会评审（每个成员一条记录）
CREATE TABLE committee_reviews (
    id              TEXT PRIMARY KEY,
    execution_id    TEXT REFERENCES execution_plans(id),
    member_role     TEXT NOT NULL,           -- risk_officer/growth/value/contrarian
    verdict         TEXT NOT NULL,           -- approve/reserve/reject
    confidence      INTEGER DEFAULT 5,
    key_point       TEXT DEFAULT '',
    full_analysis   TEXT DEFAULT '',
    scores          TEXT DEFAULT '{}',       -- JSON: 多维评分
    created_at      TEXT NOT NULL
);

-- 投委会共识（每个执行计划一条汇总）
CREATE TABLE committee_consensus (
    id              TEXT PRIMARY KEY,
    execution_id    TEXT REFERENCES execution_plans(id),
    approve_count   INTEGER DEFAULT 0,
    reject_count    INTEGER DEFAULT 0,
    reserve_count   INTEGER DEFAULT 0,
    consensus_pct   REAL DEFAULT 0.0,
    final_verdict   TEXT DEFAULT 'pending',
    key_disagreement TEXT DEFAULT '',
    modifications   TEXT DEFAULT '[]',       -- JSON: 建议修改
    created_at      TEXT NOT NULL
);

-- 催化剂追踪
CREATE TABLE catalyst_tracking (
    id              TEXT PRIMARY KEY,
    entry_id        TEXT REFERENCES watchlist(id),
    ticker          TEXT NOT NULL,
    catalyst_type   TEXT NOT NULL,           -- earnings/product/regulation/macro/news
    description     TEXT NOT NULL,
    expected_date   TEXT,
    status          TEXT DEFAULT 'pending',  -- pending/monitoring/realized_positive/realized_negative/expired
    confidence      REAL DEFAULT 0.5,
    impact_estimate TEXT DEFAULT 'medium',   -- low/medium/high
    outcome_note    TEXT DEFAULT '',
    created_at      TEXT NOT NULL,
    updated_at      TEXT,
    resolved_at     TEXT
);

-- 交易反馈（确认/拒绝记录）
CREATE TABLE trade_feedback (
    id              TEXT PRIMARY KEY,
    execution_id    TEXT REFERENCES execution_plans(id),
    feedback_type   TEXT NOT NULL,           -- confirmed/rejected
    user_reason     TEXT DEFAULT '',
    cooldown_until  TEXT,                    -- 拒绝后冷却期
    created_at      TEXT NOT NULL
);

-- 自动复盘
CREATE TABLE auto_reviews (
    id              TEXT PRIMARY KEY,
    trade_id        TEXT,                    -- sim_trades.id
    ticker          TEXT NOT NULL,
    entry_price     REAL,
    exit_price      REAL,
    holding_days    INTEGER,
    pnl             REAL,
    pnl_pct         REAL,
    what_right      TEXT DEFAULT '',
    what_wrong      TEXT DEFAULT '',
    lesson          TEXT DEFAULT '',
    experience_card_ids TEXT DEFAULT '[]',   -- JSON: 生成的经验卡片 IDs
    created_at      TEXT NOT NULL
);

-- 模拟账户
CREATE TABLE sim_account (
    id              TEXT PRIMARY KEY DEFAULT 'default',
    initial_capital REAL DEFAULT 100000,
    current_cash    REAL DEFAULT 100000,
    total_value     REAL DEFAULT 100000,
    total_pnl       REAL DEFAULT 0,
    total_pnl_pct   REAL DEFAULT 0,
    max_position_pct REAL DEFAULT 0.2,
    created_at      TEXT NOT NULL,
    updated_at      TEXT
);

-- 模拟持仓
CREATE TABLE sim_positions (
    id              TEXT PRIMARY KEY,
    ticker          TEXT NOT NULL UNIQUE,
    entry_id        TEXT REFERENCES watchlist(id),
    shares          INTEGER DEFAULT 0,
    avg_cost        REAL DEFAULT 0,
    current_price   REAL DEFAULT 0,
    unrealized_pnl  REAL DEFAULT 0,
    weight_pct      REAL DEFAULT 0,
    first_buy_at    TEXT,
    updated_at      TEXT
);

-- 模拟交易记录
CREATE TABLE sim_trades (
    id              TEXT PRIMARY KEY,
    execution_id    TEXT REFERENCES execution_plans(id),
    ticker          TEXT NOT NULL,
    action          TEXT NOT NULL,           -- buy/sell
    shares          INTEGER NOT NULL,
    price           REAL NOT NULL,
    total_value     REAL NOT NULL,
    reasoning       TEXT DEFAULT '',
    status          TEXT DEFAULT 'executed',
    executed_at     TEXT NOT NULL
);

-- 用户偏好（从对话中学习）
CREATE TABLE user_preferences (
    id              TEXT PRIMARY KEY,
    preference_type TEXT NOT NULL,           -- risk/holding_period/position_size/sector/style
    value           TEXT NOT NULL,
    learned_from    TEXT DEFAULT '',
    confidence      REAL DEFAULT 0.5,
    created_at      TEXT NOT NULL,
    updated_at      TEXT
);
```

### 7.2 表关系概览

```
macro_strategies ──1:N──► strategic_plans ──1:N──► tactical_plans ──1:1──► execution_plans
                                                                              │
                                                         committee_reviews ◄──┤──► committee_consensus
                                                         trade_feedback    ◄──┤
                                                         sim_trades        ◄──┘──► auto_reviews
                                                                                      │
                                                                                      ▼
                                                                              experience_cards (已有)

watchlist ──1:N──► catalyst_tracking
          ──1:N──► tactical_plans
          ──1:1──► sim_positions

sim_account ──1:N──► sim_positions
```

---

## 八、API 设计

### 8.1 决策中心 API（`/api/decision/`）

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/decision/refresh` | SSE 流：刷新决策（query: `scope=l1\|l3l4\|full`） |
| GET | `/api/decision/latest` | 获取最新决策概览（L1-L4 + 投委会） |
| GET | `/api/decision/macro` | L1 宏观策略最新版 |
| GET | `/api/decision/macro/history` | L1 策略历史列表 |
| GET | `/api/decision/strategic` | L2 组合策略最新版 |
| GET | `/api/decision/tactical` | L3 今日战术计划列表 |
| GET | `/api/decision/execution` | L4 待确认执行计划列表 |
| POST | `/api/decision/execution/{id}/confirm` | 确认执行（body: `{amount?}`） |
| POST | `/api/decision/execution/{id}/reject` | 拒绝执行（body: `{reason?}`） |
| GET | `/api/decision/committee/{execution_id}` | 投委会评审详情 |
| GET | `/api/decision/timeline` | 策略更新时间线 |

### 8.2 交易中心 API（`/api/sim/`）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/sim/account` | 模拟账户概览 |
| POST | `/api/sim/account/reset` | 重置账户（body: `{initial_capital}`） |
| GET | `/api/sim/positions` | 当前持仓列表 |
| GET | `/api/sim/trades` | 交易记录（query: `ticker, action, status, from, to`） |
| GET | `/api/sim/performance` | 绩效统计（胜率/盈亏比/夏普等） |
| GET | `/api/sim/equity-curve` | 净值曲线数据 |

### 8.3 复盘系统 API（`/api/review/`）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/review/trades` | 交易复盘列表 |
| GET | `/api/review/trades/{id}` | 单笔复盘详情 |
| GET | `/api/review/cards` | 经验卡片列表（query: `scope, category`） |
| PUT | `/api/review/cards/{id}` | 编辑经验卡片 |
| DELETE | `/api/review/cards/{id}` | 删除经验卡片 |

### 8.4 催化剂追踪 API

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/watchlist/{id}/catalysts` | 催化剂列表 |
| POST | `/api/watchlist/{id}/catalysts` | 手动添加催化剂 |
| PATCH | `/api/watchlist/{id}/catalysts/{cid}` | 更新催化剂状态 |

### 8.5 策略对话 API

| 类型 | 路径 | 说明 |
|------|------|------|
| WebSocket | `/ws/strategy-chat` | 策略对话（JSON 消息帧） |
| GET | `/api/chat/history` | 对话历史（query: `limit, before`） |
| GET | `/api/chat/preferences` | 已学习的用户偏好列表 |

---

## 九、前端展示与交互设计

> **设计原则**：策略中心覆盖整体观察池，以抽屉面板方式从观察池打开，而非独立页面。个股抽屉负责个股策略分析。最终操作决策在策略中心面板中完成。

### 9.1 导航与入口结构

导航保持 3 个入口，**不新增独立页面**：

```
导航栏:  [ 产业链分析 ] [ 观察池 ] [ 交易中心 ] [ ⚙ ]
```

| 视图 | data-view | 说明 |
|------|-----------|------|
| 产业链分析 | `screen` | 现有 Phase 1-4 Wizard，不变 |
| 观察池 | `watchlist` | 现有 + **策略中心抽屉入口** + 个股抽屉策略升级 |
| 交易中心 | `trading` | **启用**：模拟账户 + 持仓 + 交易记录 + 复盘 |

CSS 前缀约定：策略中心抽屉 `.dc-`（decision center）、交易中心 `.tc-`（trading center）

**入口体系**：

```
观察池页面
    │
    ├── 工具栏: [策略中心📊] [刷新数据] [刷新信息] [刷新策略] [+ 添加]
    │               │
    │               └──► 策略中心抽屉（全宽，组合级决策面板）
    │
    ├── 表格/卡片 → 点击行 → 个股详情抽屉（现有 1000px，增强策略 Tab）
    │
    └── 底部: 管道状态 + LLM 预算
```

| 入口 | 触发方式 | 内容层级 |
|------|---------|---------|
| 策略中心抽屉 | 工具栏"策略中心📊"按钮 | 组合级：L1 市场 + L2 组合 + 今日决策 + 投委会 + 对话 |
| 个股详情抽屉 | 点击表格行 | 个股级：现有 7 Tab + 策略 Tab 升级（四层策略+催化剂+投委会） |
| 交易中心页面 | 导航栏"交易中心" | 模拟账户 + 持仓 + 记录 + 复盘 |

### 9.2 策略中心抽屉（组合级决策面板）

#### 9.2.0 抽屉规格

- **宽度**：100%（全宽覆盖观察池，区别于个股抽屉的 1000px）
- **方向**：从右侧滑入
- **z-index**：高于个股抽屉（策略中心抽屉可覆盖个股抽屉）
- **ID**：`#dc-strategy-drawer`

#### 9.2.1 抽屉布局

```
┌─────────────────────────────────────────────────────────────┐
│ 策略中心                [刷新决策▼] [对话💬] [✕]             │
├───────────────────┬─────────────────────────────────────────┤
│                   │                                         │
│   市场概况 (L1)    │    组合策略 (L2)                         │
│   ┌─────────┐    │    ┌───────────────────────────────┐    │
│   │大盘状态   │    │    │ 仓位分布饼图  │  行业配置条形图 │    │
│   │市场信号   │    │    └───────────────────────────────┘    │
│   │信心指数   │    │    当前配置建议 / 偏离度警告              │
│   │上次更新   │    │    策略要点列表                          │
│   └─────────┘    │                                         │
├───────────────────┴─────────────────────────────────────────┤
│ 今日决策 (L3+L4 汇总)                            [全部展开]   │
│ ┌──────────────┐ ┌──────────────┐ ┌──────────────┐         │
│ │🟢 NVDA 买入   │ │🟡 AMD 持有    │ │🔴 INTC 减仓   │         │
│ │ 信心 8/10    │ │ 信心 6/10    │ │ 信心 7/10    │         │
│ │ 投委会 3/4   │ │ 投委会 4/4   │ │ 投委会 2/4   │         │
│ │ [确认][拒绝]  │ │              │ │ [确认][拒绝]  │         │
│ │ [个股详情→]   │ │ [个股详情→]   │ │ [个股详情→]   │         │
│ └──────────────┘ └──────────────┘ └──────────────┘         │
├─────────────────────────────────────────────────────────────┤
│ 投委会评审                                                   │
│ ┌──────────┬──────────┬──────────┬──────────┐              │
│ │ 🛡 风控官 │ 📈 成长型 │ 💎 价值型 │ 🔄 逆向  │              │
│ │ 谨慎通过  │ 强烈推荐  │ 有保留    │ 反对     │              │
│ └──────────┴──────────┴──────────┴──────────┘              │
│ 共识度: ██████████░░░ 68%  建议: 买入(有分歧)                │
├─────────────────────────────────────────────────────────────┤
│ 策略时间线                                                   │
│ ──●── L1 ──●── L2 ──●── L3 ──●── L4 ──                     │
└─────────────────────────────────────────────────────────────┘
```

#### 9.2.2 市场概况区（Layer 1）

左侧固定宽度面板（280px），卡片式信息展示：

```html
<div class="dc-market-panel">
  <div class="dc-panel-header">
    <h3>市场概况</h3>
    <span class="dc-regime-badge dc-regime-{bull|neutral|bear}">看多</span>
  </div>
  <div class="dc-market-indicators">
    <!-- 指数卡片组：标普/纳指/道指，迷你折线 + 涨跌% -->
    <div class="dc-index-card">
      <span class="dc-index-name">标普 500</span>
      <span class="dc-index-value dc-change-up">5,520.3 (+0.8%)</span>
      <canvas class="dc-spark-chart" width="80" height="24"></canvas>
    </div>
  </div>
  <div class="dc-macro-signals">
    <!-- 关键信号列表：VIX / 美债收益率 / 美元指数 -->
    <div class="dc-signal-row">
      <span class="dc-signal-label">VIX 恐慌指数</span>
      <span class="dc-signal-value dc-signal-{low|mid|high}">14.2 低</span>
    </div>
  </div>
  <div class="dc-macro-summary">
    <p class="dc-summary-text">市场处于温和牛市区间，大型科技股领涨...</p>
  </div>
  <div class="dc-macro-meta">
    <span>v3 · 2026-06-20 生成</span>
    <span>下次全面更新：周一</span>
  </div>
</div>
```

市场信号可视化：
- 牛市 `.dc-regime-bull` → 绿色渐变背景
- 中性 `.dc-regime-neutral` → 灰色
- 熊市 `.dc-regime-bear` → 红色渐变背景
- 信心条：10 格水平条形图，填充颜色随分值变化

#### 9.2.3 组合策略区（Layer 2）

市场面板右侧，自适应宽度。核心图表（ECharts）：

1. **仓位分布饼图**：现金 / 各持仓股占比，中心显示总资产
2. **行业配置条形图**：当前配置 vs 建议配置对比（grouped bar）
3. **风险暴露仪表盘**：beta / 集中度 / 行业偏离度

偏离度警告：
```html
<div class="dc-deviation-alert dc-deviation-{ok|warning|danger}">
  <span class="dc-deviation-icon">⚠</span>
  <span>科技股集中度 72% 超过建议上限 60%，建议分散配置</span>
</div>
```

策略要点：
```html
<div class="dc-strategy-points">
  <div class="dc-point dc-point-add">加仓建议：消费防御板块</div>
  <div class="dc-point dc-point-reduce">减仓建议：半导体（获利了结）</div>
  <div class="dc-point dc-point-hold">维持：AI 基础设施核心仓</div>
</div>
```

#### 9.2.4 今日决策区（Layer 3 + Layer 4 汇总）

全宽区域，横向排列决策卡片，可滚动。**这是最终操作决策的确认/拒绝区域**。

**决策卡片** `.dc-action-card`：

```
┌──────────────────────────┐
│ 🟢 NVDA                  │  ← 信号色圆点 + Ticker
│ NVIDIA Corporation       │  ← 公司名
│ ─────────────────────── │
│ 建议操作: 买入            │  ← 操作类型
│ 建议金额: $5,000         │  ← 金额/股数
│ 目标仓位: 15%            │  ← 仓位目标
│ ─────────────────────── │
│ 信心: ████████░░ 8/10    │  ← 信心条
│ 投委会: 3/4 通过          │  ← 投委会共识
│ 催化剂: H100 新订单       │  ← 触发催化剂
│ ─────────────────────── │
│ [✓ 确认] [✕ 拒绝] [? 问AI]│ ← 操作按钮
│ [个股详情→]               │  ← 跳转个股抽屉
└──────────────────────────┘
```

卡片状态色：

| 操作 | CSS 类 | 左边框色 |
|------|--------|---------|
| 买入/加仓 | `.dc-action-buy` | 绿色 |
| 卖出/减仓 | `.dc-action-sell` | 红色 |
| 持有 | `.dc-action-hold` | 灰色 |
| 待确认 | 脉冲动画边框 | — |
| 已确认 | 半透明 + 勾号 | — |
| 已拒绝 | 删除线 + 半透明 | — |

展开详情面板（点击卡片展开更多信息）：

```
├──────────────────────────────────────────────────┤
│ 📋 战术分析 (L3)                                  │
│ • 近期催化剂：6/28 新品发布会                       │
│ • 技术形态：突破关键阻力位 $920                     │
│ • 量价配合：成交量放大 2.3x                        │
│                                                  │
│ 💰 执行细节 (L4)                                  │
│ • 建议分批：60% 即时 + 40% 回调至 $880             │
│ • 止损位：$850 (-7.6%)                            │
│ • 仓位控制：不超总资产 15%                         │
│                                                  │
│ 🛡 投委会意见摘要                                  │
│ 风控官：风险可控，但注意集中度 → 通过               │
│ 成长型：强催化剂+技术突破 → 强烈推荐               │
│ 价值型：当前PE偏高，建议小仓 → 有保留通过           │
│ 逆向型：市场共识过强，注意回调 → 反对               │
├──────────────────────────────────────────────────┤
```

#### 9.2.5 投委会评审区

全宽横条，4 成员并排。每个成员卡片：

```html
<div class="dc-member-card">
  <div class="dc-member-avatar dc-member-risk">🛡</div>
  <div class="dc-member-name">风控官</div>
  <div class="dc-member-verdict dc-verdict-{approve|reserve|reject}">谨慎通过</div>
  <div class="dc-member-confidence">信心 5/10</div>
  <div class="dc-member-key-point">关注集中度风险</div>
</div>
```

共识度可视化：水平进度条（同意绿/保留黄/反对红 分段），百分比数字，文字结论（`共识买入` / `有分歧` / `否决`）。

#### 9.2.6 策略时间线

底部窄条，水平时间线。节点状态色：完成→绿色实心，进行中→蓝色脉冲，待处理→灰色空心，异常→红色。

#### 9.2.7 关键交互

1. **"策略中心📊"按钮** → 打开策略中心抽屉 + 加载最新决策数据
2. **"刷新决策"下拉** → SSE 进度流（仅 L1 检查 / 仅 L3-L4 更新 / 全量刷新）
3. **决策卡片 [确认/拒绝]** → 卡片内展开确认框 / 拒绝理由输入 → 写入 `trade_feedback`
4. **决策卡片 [个股详情→]** → 关闭策略中心抽屉 → 打开该股票的个股详情抽屉（策略 Tab 激活）
5. **"对话💬"按钮** → 打开策略对话浮窗

### 9.3 个股详情抽屉增强（个股级策略）

现有个股抽屉（1000px 宽，7 个 Tab）保持不变，仅升级策略 Tab。

#### 9.3.1 策略 Tab 升级

现有 8 板块 → 升级为**顶部固定概要条 + 4 个子 Tab**：

```
┌─────────────────────────────────────────────────────┐
│ 策略概要（顶部固定条）                                │
│ 信号: 🟢看多  信心: 8/10  投委会: 3/4  上次: 2h前     │
├─────────────────────────────────────────────────────┤
│ [策略分析] [催化剂] [投委会] [历史]    ← 子 Tab        │
├─────────────────────────────────────────────────────┤
│                                                     │
│ 「策略分析」子 Tab:                                   │
│   📊 情报摘要（最新数据要点）                         │
│   ⚖ 多空分析（✅ 多头 / ❌ 空头）                     │
│   🎯 核心逻辑（2-3 句投资主线）                       │
│   📋 四层策略定位:                                    │
│     L1: 宏观环境对该股的影响                          │
│     L2: 在组合中的定位和目标仓位                       │
│     L3: 近期战术计划（买卖时机/价格区间）              │
│     L4: 具体操作建议（分批策略/止损/仓位）             │
│   🛡 风险控制（止损位/仓位/对冲）                      │
│   ⏱ 目标与时间                                       │
│                                                     │
│ 「催化剂」子 Tab:                                    │
│   催化剂时间线可视化（过去→现在→未来）                 │
│   各催化剂状态 / 置信度 / 影响                         │
│                                                     │
│ 「投委会」子 Tab:                                    │
│   4 成员独立评分和观点                                 │
│   共识度指示器                                        │
│   [问 AI 关于该股] 按钮                               │
│                                                     │
│ 「历史」子 Tab:                                      │
│   策略版本时间线（现有，不变）                         │
│                                                     │
└─────────────────────────────────────────────────────┘
```

#### 9.3.2 操作建议区（策略 Tab 底部固定）

如果该股票有待确认操作，底部固定显示操作建议条：

```
┌─────────────────────────────────────────────────┐
│ 📌 待确认操作: 建议买入 $5,000 (30股 × $166.7)  │
│ 催化剂: H100 新订单  信心: 8/10                  │
│ [✓ 确认执行] [✕ 拒绝] [? 问AI]                  │
│ [← 在策略中心查看完整决策]                        │
└─────────────────────────────────────────────────┘
```

#### 9.3.3 催化剂时间线组件

```
──[ 过去 ]──────────────[ 现在 ]──────────────[ 未来 ]──
   ●                       ▼                    ●
 6/10                    6/24                 7/15
 Q1财报超预期            当前                  新品发布会
 ✅ 已兑现(正面)          │                    ⏳ 待验证
 影响: +5%               │                   预期影响: 高
                         │
                    ● 6/20 大客户订单传闻
                    🔍 监控中  置信度: 60%
```

催化剂状态色：`⏳ 待验证`→黄色、`🔍 监控中`→蓝色、`✅ 已兑现(正面)`→绿色、`❌ 已兑现(负面)`→红色、`⏰ 已过期`→灰色

#### 9.3.4 抽屉切换交互

- **策略中心 → 个股**：点击决策卡片"个股详情→" → 关闭策略中心抽屉 → 打开个股抽屉（策略 Tab 激活）
- **个股 → 策略中心**：个股抽屉内点击"← 在策略中心查看完整决策" → 关闭个股抽屉 → 重新打开策略中心抽屉
- 策略中心抽屉 z-index 高于个股抽屉，两者不会同时显示

### 9.4 观察池表格增强

#### 9.4.1 策略列升级

表格增加策略信息密度：

```
| 层级 | Ticker | 名称 | 价格 | 涨跌% | RSI | 评分 | 策略信号 | 操作建议 | 行业 | 操作 |
                                                      │           │
                                              看多 8/10  买入$5k ⏳
                                              ✓✓✓✗       ← 投委会微图标
```

- **策略信号列**：信号徽章(看多/中性/看空) + 信心分 + 投委会共识度小图标（✓✓✓✗ 形式）
- **操作建议列**（新增）：简短操作文字 + 状态图标（⏳待确认/✅已确认/—无操作）
- 有待确认操作的行高亮（左边框脉冲动画）
- Tooltip 悬停显示操作摘要

#### 9.4.2 工具栏调整

```
[策略中心📊]  [刷新数据] [刷新信息] [刷新策略]  [+ 添加]
     │
     └── 新增按钮，打开策略中心抽屉
```

"策略中心📊"按钮样式区别于其他按钮（`.btn-accent` 或带图标），醒目提示用户这是组合级入口。

### 9.5 策略对话浮窗（全局组件）

#### 触发方式

1. 策略中心抽屉"对话💬"按钮 → 打开空白对话
2. 决策卡片 / 个股抽屉"问AI"按钮 → 打开并预填上下文
3. 键盘快捷键 `Ctrl+/` → 切换对话窗

#### 浮窗布局

```
                                    ┌──────────────────────┐
                                    │ 策略对话     [_][✕]   │
                                    │ ─────────────────── │
                                    │ 👤 为什么建议买入NVDA？│
                                    │                      │
                                    │ 🤖 基于以下分析...    │
                                    │ 1. H100 新订单确认    │
                                    │ 2. 技术突破 $920     │
                                    │ 3. 投委会 3/4 通过   │
                                    │                      │
                                    │ 👤 能把金额改成$3000  │
                                    │    吗？               │
                                    │                      │
                                    │ 🤖 已将NVDA买入金额   │
                                    │ 调整为$3,000。        │
                                    │ ⚡ 已修改决策参数      │
                                    │ ─────────────────── │
                                    │ [输入消息...]   [发送] │
                                    └──────────────────────┘
```

设计规范：
- 位置：右下角固定定位，400px 宽 × 500px 高
- 可拖拽、可折叠（最小化为圆形图标）
- 消息气泡：用户右对齐蓝色，AI 左对齐灰色
- 修改指令反馈：⚡ 图标特殊卡片
- 流式输出：WebSocket 逐 token 推送，打字机效果
- 历史记录：本地存储最近 50 条

#### 特殊交互

- **修改确认**：AI 识别修改指令时，先展示预览 → 用户确认 → 执行
- **快捷指令**：输入 `/` 显示命令列表（`/修改 NVDA 金额 3000` / `/拒绝 INTC` / `/偏好 不买金融股`）
- **上下文感知**：从"问AI"进入时，自动携带该股票完整决策上下文

### 9.6 交易中心页面（`view-trading`）

#### 总布局

```
┌─────────────────────────────────────────────────────────────┐
│ 模拟交易中心                    [重置账户] [LLM 预算: $1.2/$3]│
├──────────┬────────────────────┬──────────────────────────────┤
│ 账户概览  │ 收益率曲线(ECharts) │                              │
│          │                    │                              │
│ 总资产    │  ╱╲   ╱╲          │  统计卡片                     │
│ $102,350 │ ╱  ╲╱╱  ╲╱╲       │  胜率 62%                    │
│          │╱            ╲      │  盈亏比 2.3                  │
│ 持仓市值  │                    │  最大回撤 -5.2%              │
│ $72,350  ├────────────────────┤  夏普比率 1.8                │
│          │ 仓位分布饼图         │  持仓周期 14.5天             │
│ 现金      │                    │                              │
│ $30,000  │                    │                              │
│          │                    │                              │
│ 总盈亏    │                    │                              │
│ +$2,350  │                    │                              │
│ (+2.35%) │                    │                              │
├──────────┴────────────────────┴──────────────────────────────┤
│ [持仓一览] [交易记录] [复盘面板] [经验卡片]                     │
├─────────────────────────────────────────────────────────────┤
│ Tab Content Area                                            │
└─────────────────────────────────────────────────────────────┘
```

#### 9.6.1 账户概览（顶部固定区）

- **左侧面板**（200px）：总资产（大号数字+日涨跌%）、持仓市值、现金、总盈亏
- **中间图表区**：收益率曲线（ECharts line chart）对比基准 SPY + 仓位分布饼图
- **右侧统计卡片**：胜率 / 盈亏比 / 最大回撤 / 夏普比率 / 平均持仓周期
- 时间范围切换：1W / 1M / 3M / 全部

#### 9.6.2 持仓一览 Tab

| Ticker | 名称 | 数量 | 均价 | 现价 | 盈亏 | 盈亏% | 仓位% | 持仓天数 | 策略 | 操作 |
|--------|------|------|------|------|------|-------|-------|---------|------|------|

行点击展开成本详情 + 买入理由 + 当前策略建议。盈亏颜色：绿/红。支持列排序。

#### 9.6.3 交易记录 Tab

| 日期 | 操作 | Ticker | 数量 | 价格 | 金额 | 来源 | 状态 | 决策理由 |
|------|------|--------|------|------|------|------|------|---------|

操作色：买入绿/卖出红。状态：✅已执行/❌已拒绝/⏳待确认。决策理由悬停展开。支持筛选（操作类型/状态/日期/Ticker）。

#### 9.6.4 复盘面板 Tab

子 Tab：`[交易复盘]` `[经验卡片]` `[调优建议]` `[绩效报告]`

**交易复盘**：
```
┌──────────────────────────────────────────────────┐
│ INTC 卖出复盘                         盈亏 +8.5% │
│ 2026-05-10 买入 $35 → 2026-06-15 卖出 $38        │
│ 持仓 36天                                         │
├──────────────────────────────────────────────────┤
│ ✅ 做对了什么                                     │
│ • 识别了Q2财报超预期的催化剂                        │
│ • 在技术阻力位附近止盈                              │
│ ❌ 做错了什么                                     │
│ • 建仓时机偏晚，错过了 $32 低点                     │
│ • 仓位偏小（5%），应该更果断                        │
│ 💡 经验教训                                       │
│ • 催化剂确认后应在 2 天内建仓                       │
│ • INTC 类反转标的可以给更大仓位                     │
└──────────────────────────────────────────────────┘
```

**经验卡片**（三列网格，按 scope 筛选：全局/行业/个股）：
```
┌────────────────────────────┐
│ 🌐 全局经验                 │ ← scope badge
│ ─────────────────────────  │
│ 催化剂驱动的反转标的         │ ← 标题
│ 应在催化剂确认后 1-2 天内    │
│ 建仓，仓位可放大至 10-15%   │ ← 内容
│ ─────────────────────────  │
│ 证据: INTC 6月, AMD 4月     │ ← 支撑案例
│ 置信度: ████████░░ 80%     │ ← 置信度条
│ 已应用 3 次                 │ ← 使用计数
└────────────────────────────┘
```

### 9.7 关键交互流程

#### 9.7.1 日常使用路径

```
进入观察池
    ↓
查看表格 → 策略信号列 + 操作建议列 一目了然
    ↓
点击 [策略中心📊] → 打开策略中心抽屉
    ↓
查看 L1 市场概况 + L2 组合配置
    ↓
查看"今日决策"卡片 → 确认/拒绝操作
    ↓
想了解某只股详情 → 点击"个股详情→" → 切换到个股抽屉
    ↓
个股抽屉策略 Tab → 查看四层策略定位 + 催化剂 + 投委会
    ↓
点击"← 在策略中心查看完整决策" → 回到策略中心继续处理其他决策
```

#### 9.7.2 层级关系

```
观察池页面（表格/卡片）
    │
    ├── 策略中心抽屉（组合级）
    │   ├── L1 市场概况
    │   ├── L2 组合策略
    │   ├── 今日决策卡片（L3+L4 汇总，含确认/拒绝）  ← 最终操作决策
    │   ├── 投委会评审
    │   ├── 策略时间线
    │   └── 对话入口
    │
    └── 个股详情抽屉（个股级）
        ├── 基本信息/价格/新闻/资金/情报 Tab（不变）
        ├── 策略 Tab（升级）
        │   ├── 策略分析子 Tab（四层策略定位）
        │   ├── 催化剂子 Tab（时间线）
        │   ├── 投委会子 Tab（4 成员评分）
        │   └── 历史子 Tab（版本时间线）
        ├── UZI 分析 Tab（不变）
        └── 底部操作建议条
```

#### 9.7.3 每日决策生成流程

```
用户在策略中心抽屉点击"刷新决策"
    ↓
下拉菜单：[仅 L1 检查] [仅 L3-L4 更新] [全量刷新]
    ↓
SSE 流式进度（复用现有 wl-refresh-bar 模式）
    ↓
┌──────────────────────────────────────┐
│ ⏳ Layer 1 宏观检查中...              │  ← 步骤指示器
│ ████████████░░░░░░░░ 60%             │  ← 进度条
│ 正在分析美联储议息会议影响...           │  ← 实时文字
└──────────────────────────────────────┘
    ↓
各 Layer 逐步完成，界面逐区域更新
    ↓
投委会评审自动启动（如有待决策操作）
    ↓
完成 → 通知 "今日有 N 条待确认操作"
```

刷新选项：仅 L1 检查（~30s）/ 仅 L3-L4（~2min）/ 全量刷新（~5min）

#### 9.7.4 交易确认流程

```
策略中心抽屉 → 决策卡片 [确认] 按钮
    ↓
卡片内展开确认框：
  - 数量 + 金额（可编辑）
  - [确认执行] [取消]
    ↓
执行 → 卡片变"已确认"（绿色勾号+半透明）→ 写入 sim_trades
拒绝 → 输入拒绝理由（可选）→ 卡片变"已拒绝"→ 写入 trade_feedback
    ↓
拒绝信息 LLM 学习避免类似建议
```

#### 9.7.5 策略对话流程

```
用户打开对话浮窗 / 点击"问AI"
    ↓
WebSocket 连接（ws://localhost:8001/ws/strategy-chat）
    ↓
用户发送 → 前端显示气泡 + "AI 思考中..."
    ↓
服务端：解析意图 → 构建上下文 → LLM 流式生成 → 解析修改指令
    ↓
前端：流式展示回复 → 修改预览卡片 → 用户确认 → 界面刷新
    ↓
偏好自动学习（后台静默）
```

### 9.8 组件规范

#### CSS 设计令牌（新增）

```css
--dc-regime-bull:    oklch(0.75 0.15 145);   /* 牛市绿 */
--dc-regime-neutral: oklch(0.65 0.05 250);   /* 中性蓝灰 */
--dc-regime-bear:    oklch(0.65 0.15 25);    /* 熊市红 */
--dc-action-buy:     oklch(0.75 0.15 145);   /* 买入 */
--dc-action-sell:    oklch(0.65 0.15 25);    /* 卖出 */
--dc-action-hold:    oklch(0.55 0.02 250);   /* 持有 */
--dc-committee-approve: oklch(0.75 0.12 145);
--dc-committee-reject:  oklch(0.65 0.12 25);
--dc-committee-reserve: oklch(0.75 0.12 85);
```

#### 响应式策略

| 断点 | 布局调整 |
|------|---------|
| ≥1400px | 市场面板+策略区并排，决策卡片 3 列 |
| 1024-1399px | 市场面板+策略区并排窄化，决策卡片 2 列 |
| <1024px | 市场面板折叠为顶部横条，策略区全宽，决策卡片 1 列 |

#### 动画规范

| 场景 | 动画 |
|------|------|
| 策略中心抽屉打开 | slideInRight, 300ms ease-out |
| 决策卡片加载 | fadeInUp, 依次延迟 100ms |
| 确认/拒绝 | 卡片 scale(0.95) → 状态变色 → 200ms |
| 投委会成员出场 | 从左到右依次 slideIn, 间隔 200ms |
| 共识度进度条 | 数值动画 0→目标, 800ms ease-out |
| 对话气泡 | fadeIn + slideUp, 150ms |
| 策略时间线节点 | 脉冲动画（进行中状态） |
| 抽屉切换 | 当前抽屉 slideOutRight → 新抽屉 slideInRight, 200ms |

### 9.9 文件清单

| 文件 | 操作 | 说明 |
|------|------|------|
| `web/static/index.html` | 修改 | 添加策略中心抽屉 HTML + 工具栏"策略中心📊"按钮 + 启用交易中心 + 对话浮窗 |
| `web/static/js/decision.js` | 新建 | 策略中心抽屉逻辑（组合级决策渲染 + 交互 + 抽屉切换） |
| `web/static/js/trading.js` | 新建 | 交易中心页面逻辑 |
| `web/static/js/strategy-chat.js` | 新建 | WebSocket 对话组件 |
| `web/static/js/watchlist.js` | 修改 | 策略 Tab 子 Tab 升级 + 催化剂时间线 + 表格操作建议列 |
| `web/static/css/style.css` | 修改 | `.dc-` / `.tc-` 前缀样式 |
| `web/decision_api.py` | 新建 | 决策中心 API Router |
| `web/trading_api.py` | 新建 | 交易中心 API Router |
| `web/chat_ws.py` | 新建 | WebSocket 对话处理 |
| `watchlist/store.py` | 修改 | 新增 14 张表 + CRUD 方法 |
| `watchlist/decision_engine.py` | 新建 | L1-L4 决策引擎 |
| `watchlist/committee.py` | 新建 | 投委会机制 |
| `watchlist/trade_executor.py` | 新建 | 模拟交易执行 |

---

### 9.10 Prompt 模板清单

所有 prompt 模板位于 `chain/prompts/` 目录，使用 `{placeholder}` 占位符，运行时填充实际数据。

| 文件 | 用途 | 输出格式 |
|------|------|---------|
| `decision_macro.md` | L1 宏观策略生成（每周） | JSON: regime, risk_appetite, sector_rotation |
| `decision_macro_check.md` | L1 日常检查（每日） | JSON: strategy_status, notable_changes |
| `decision_strategic.md` | L2 组合策略生成（每周） | JSON: allocation, stock_selection, risk_limits |
| `decision_deviation_check.md` | L2 偏离度检查（每日） | JSON: rebalance_needed, deviations |
| `decision_tactical.md` | L3 战术计划（每日） | JSON: tactical_plans[], priority_ranking |
| `decision_execution.md` | L4 执行策略（每日） | JSON: execution_plans[], execution_summary |
| `committee_risk.md` | 投委会：风险控制官 | JSON: vote, risk_score, stress_test |
| `committee_growth.md` | 投委会：成长投资人 | JSON: vote, growth_score, catalyst_assessment |
| `committee_value.md` | 投委会：价值投资人 | JSON: vote, value_score, valuation_assessment |
| `committee_contrarian.md` | 投委会：逆向投资人 | JSON: vote, contrarian_score, crowding_analysis |
| `committee_discussion.md` | 投委会：圆桌讨论（分歧时） | JSON: consensus, final_recommendation |
| `committee_consensus.md` | 投委会：共识汇总 | JSON: final_verdict, consensus_modifications |

---

## 十、完整决策流程

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

## 十一、现有代码适配方案

### 11.1 核心设计决策

**保留 `strategy_engine.py` 不变**，新建独立模块处理四层决策。两者的关系：

```
strategy_engine.py（现有，不改）          decision_engine.py（新建）
┌─────────────────────┐                  ┌────────────────────────┐
│ 个股级情报聚合        │                  │ 组合级四层决策           │
│ • refresh_intelligence│ ──输出──→       │ • L1: 宏观策略           │
│ • refresh_strategy    │ (个股信号)       │ • L2: 组合配置           │
│                      │                  │ • L3: 战术计划           │
│ 输出:                │                  │ • L4: 执行方案           │
│ • stock_intelligence │                  │                        │
│ • strategy_records   │                  │ 输入:                   │
│   (8板块策略)         │                  │ • 所有个股策略信号        │
└─────────────────────┘                  │ • 账户状态              │
                                         │ • 历史教训              │
                                         │ • 用户偏好              │
                                         └──────────┬─────────────┘
                                                    ↓
                                         ┌────────────────────────┐
                                         │ committee.py（新建）     │
                                         │ 投委会独立评审 + 共识     │
                                         └──────────┬─────────────┘
                                                    ↓
                                         ┌────────────────────────┐
                                         │ trade_executor.py（新建）│
                                         │ 模拟交易执行 + 复盘      │
                                         └────────────────────────┘
```

### 11.2 理由

| 方案 | 优势 | 劣势 |
|------|------|------|
| **A: 保留不改 + 新建** ✅ | 风险低、不影响现有功能、关注点分离清晰 | 两套系统并存，有重复 |
| B: 改造现有 | 代码更紧凑 | 大量重构风险、影响已有前端功能 |
| C: 完全重写 | 架构最干净 | 工作量最大、回归测试成本高 |

选择方案 A：`strategy_engine.py` 产出的个股信号（signal/confidence/核心逻辑）作为四层决策系统的**输入数据源**，不需要修改其接口。

### 11.3 模块职责划分

| 模块 | 文件 | 职责 | 状态 |
|------|------|------|------|
| 情报聚合 | `watchlist/strategy_engine.py` | 个股数据聚合 + LLM 生成 8 板块策略 | **保留不改** |
| 宏观引擎 | `watchlist/decision_engine.py` | L1 宏观策略 + L1 日常检查 | **新建** |
| 组合引擎 | `watchlist/decision_engine.py` | L2 组合策略 + L2 偏离检查 | **新建** |
| 战术引擎 | `watchlist/decision_engine.py` | L3 战术计划 + L4 执行方案 | **新建** |
| 投委会 | `watchlist/committee.py` | 4 成员独立评审 + 圆桌讨论 + 共识汇总 | **新建** |
| 交易执行 | `watchlist/trade_executor.py` | 模拟账户 + 持仓 + 交易 + 复盘 | **新建** |
| 催化剂 | `watchlist/catalyst_monitor.py` | 催化剂检测 + 生命周期追踪 + 兑现判定 | **新建** |
| 数据存储 | `watchlist/store.py` | 新增 14 张表 + CRUD 方法 | **扩展** |
| 预算 | `watchlist/budget.py` | 扩展 LLM 使用场景的标识 | **微改** |
| 调度器 | `watchlist/scheduler.py` | 新增决策流程的定时任务 | **扩展** |

### 11.4 数据流衔接

```
每日决策流程（scheduler 触发或用户手动）：

1. [现有] scheduler → price/news/sec/options pipelines → store
2. [现有] strategy_engine.refresh_intelligence_all() → stock_intelligence 表
3. [现有] strategy_engine.refresh_strategy_all() → strategy_records 表
4. [新增] decision_engine.run_macro_check()
   └── 读取 store 中的最新 macro_strategy
   └── 填充 decision_macro_check.md prompt → LLM
   └── 写入 macro_strategies / macro_daily_checks
5. [新增] decision_engine.run_deviation_check()
   └── 读取 strategic_plans + sim_positions
   └── 填充 decision_deviation_check.md prompt → LLM
   └── 如偏离超限 → 触发 L2 重建
6. [新增] decision_engine.run_tactical_plans()
   └── 读取 strategy_records（所有个股信号）+ macro + strategic
   └── 填充 decision_tactical.md prompt → LLM
   └── 写入 tactical_plans
7. [新增] decision_engine.run_execution_plans()
   └── 读取 tactical_plans + sim_account + trade_feedback
   └── 填充 decision_execution.md prompt → LLM
   └── 写入 execution_plans
8. [新增] committee.review_execution()
   └── 读取 execution_plans
   └── 4 成员并行评审（4 个 LLM 调用）
   └── 如有分歧 → 圆桌讨论（1 个 LLM 调用）
   └── 共识汇总 → 修改 execution_plans
   └── 写入 committee_reviews + committee_consensus
9. [等待] 用户在前端确认/拒绝
10. [新增] trade_executor.execute_confirmed()
    └── 更新 sim_positions + sim_trades
```

### 11.5 个股策略 Tab 数据来源映射

个股抽屉策略 Tab 的新结构如何从现有 + 新增数据中获取内容：

| 策略 Tab 子 Tab | 数据来源 | 现有/新增 |
|----------------|---------|----------|
| 策略分析 — 情报摘要 | `stock_intelligence.brief_text` | 现有 |
| 策略分析 — 多空分析 | `strategy_records.bull_bear_analysis` | 现有 |
| 策略分析 — 核心逻辑 | `strategy_records.core_logic` | 现有 |
| 策略分析 — L1 宏观影响 | `macro_strategies.strategy_text`（提取该股相关部分）| 新增 |
| 策略分析 — L2 组合定位 | `strategic_plans.stock_selection`（该股的角色和目标权重）| 新增 |
| 策略分析 — L3 战术计划 | `tactical_plans`（该股的 entry） | 新增 |
| 策略分析 — L4 操作建议 | `execution_plans`（该股的 entry） | 新增 |
| 策略分析 — 风险控制 | `strategy_records.risk_control` + `execution_plans.split_plan` | 混合 |
| 催化剂 | `catalyst_tracking` | 新增 |
| 投委会 | `committee_reviews` + `committee_consensus` | 新增 |
| 历史 | `strategy_records`（版本列表，不变） | 现有 |

### 11.6 LLM 调用策略

| 场景 | 模型选择 | 原因 |
|------|---------|------|
| L1 宏观策略生成 | deepseek-chat | 低成本、长上下文 |
| L1 日常检查 | deepseek-chat | 轻量级判断 |
| L2 组合策略 | deepseek-chat | 需要综合多股信息 |
| L2 偏离检查 | deepseek-chat | 简单对比计算 |
| L3 战术计划 | deepseek-chat | 需要技术分析能力 |
| L4 执行策略 | deepseek-chat | 约束推理 |
| 投委会 — 风控官 | deepseek-chat | 保守视角 |
| 投委会 — 成长型 | qwen-plus | 不同模型增加多样性 |
| 投委会 — 价值型 | glm-4-flash | 不同模型增加多样性 |
| 投委会 — 逆向型 | kimi (moonshot) | 不同模型增加多样性 |
| 投委会 — 圆桌/共识 | deepseek-chat | 综合能力 |

> 投委会 4 成员使用不同 LLM 是核心设计，确保观点多样性。如某 LLM 不可用，降级为 deepseek-chat 但增加 temperature 差异化。

---

## 十二、LLM 调用成本估算

### 12.1 单次完整决策流程

以观察池 10 只股票、deepseek-chat 为主为例（$0.14/1M input + $0.28/1M output）：

| 步骤 | 调用次数 | Input tokens | Output tokens | 估算成本 |
|------|---------|-------------|---------------|---------|
| L1 日常检查 | 1 | ~2,000 | ~500 | $0.0004 |
| L2 偏离检查 | 1 | ~3,000 | ~800 | $0.0006 |
| L3 战术计划 | 1（含所有股票） | ~8,000 | ~3,000 | $0.0020 |
| L4 执行策略 | 1 | ~5,000 | ~2,000 | $0.0013 |
| 投委会 4 成员 | 4 | ~4,000×4 | ~1,500×4 | $0.0039 |
| 投委会圆桌（50%触发） | 0.5 | ~6,000 | ~1,500 | $0.0006 |
| 投委会共识 | 1 | ~8,000 | ~1,500 | $0.0015 |
| **日常检查合计** | **~9** | **~46,000** | **~14,800** | **~$0.01** |

| 步骤 | 调用次数 | Input tokens | Output tokens | 估算成本 |
|------|---------|-------------|---------------|---------|
| L1 全面生成（每周） | 1 | ~5,000 | ~2,000 | $0.0013 |
| L2 全面生成（每周） | 1 | ~8,000 | ~3,000 | $0.0020 |
| **周度全面刷新额外** | **2** | **~13,000** | **~5,000** | **~$0.003** |

### 12.2 月度成本估算

| 场景 | 计算 | 月度成本 |
|------|------|---------|
| 日常检查（22 个交易日） | 22 × $0.01 | ~$0.22 |
| 周度全面刷新（4 次） | 4 × $0.003 | ~$0.012 |
| 情报聚合（现有，10 股） | 22 × 10 × $0.0005 | ~$0.11 |
| 策略生成（现有，10 股） | 22 × 10 × $0.001 | ~$0.22 |
| **月度总计** | | **~$0.56** |

> 以 deepseek-chat 计算。如投委会使用 qwen/glm/kimi 等模型，成本可能增加 2-5 倍，但总体仍在 $3/月以内。现有 budget 系统（默认 $3/天、$30/月）完全覆盖。

### 12.3 成本控制策略

| 策略 | 说明 |
|------|------|
| 增量更新优先 | L1 每日只做检查（~500 output tokens），非全面生成（~2000） |
| 偏离容忍 | L2 偏离 <10% 不触发重建 |
| 投委会按需触发 | 只有 L4 产出操作建议时才启动投委会 |
| 预算降级 | 超预算时跳过投委会或减少成员数 |
| 缓存策略 | L1 策略有效期内不重复生成 |

---

## 十三、数据迁移方案

### 13.1 迁移策略

**两套表并存，不迁移历史数据**。

| 现有表 | 新增表 | 关系 |
|--------|--------|------|
| `stock_intelligence` | — | **保留不变**，继续由 strategy_engine 写入 |
| `strategy_records` | — | **保留不变**，继续由 strategy_engine 写入 |
| — | `macro_strategies` | 新增，L1 层独立数据 |
| — | `strategic_plans` | 新增，L2 层引用 macro_strategies |
| — | `tactical_plans` | 新增，L3 层引用 strategic_plans + watchlist(entry_id) |
| — | `execution_plans` | 新增，L4 层引用 tactical_plans |
| — | `committee_reviews` | 新增，引用 execution_plans |
| — | `committee_consensus` | 新增，引用 execution_plans |
| — | `catalyst_tracking` | 新增，引用 watchlist(entry_id) |
| — | `trade_feedback` | 新增，引用 execution_plans |
| — | `auto_reviews` | 新增，引用 sim_trades |
| — | `sim_account` | 新增，独立（默认一条记录） |
| — | `sim_positions` | 新增，引用 watchlist(entry_id) |
| — | `sim_trades` | 新增，引用 execution_plans |
| — | `user_preferences` | 新增，独立 |

### 13.2 实施要点

1. **store.py 扩展**：在 `_init_db()` 中添加 14 张新表的 CREATE TABLE IF NOT EXISTS，不影响现有表
2. **外键关系**：新表之间通过 TEXT 类型 ID 关联（与现有表风格一致，不用 SQLite 外键约束）
3. **读取兼容**：个股策略 Tab 的数据同时从 `strategy_records`（现有 8 板块）和新表（L1-L4 定位）读取，前端合并展示
4. **无破坏性变更**：不修改任何现有表的 schema，新表全部通过 `CREATE TABLE IF NOT EXISTS` 创建
5. **首次运行**：新表首次创建后为空，前端显示"尚未生成决策"占位符，用户点击"刷新决策"触发首次生成

### 13.3 store.py 新增方法清单

```python
# L1 宏观策略
create_macro_strategy() → str       # 创建新版本
get_latest_macro_strategy() → dict  # 最新有效策略
get_macro_history() → list          # 历史版本

# L2 组合策略
create_strategic_plan() → str
get_latest_strategic_plan() → dict
get_strategic_history() → list

# L3 战术计划
create_tactical_plan() → str
get_tactical_plans_by_date() → list  # 当日所有战术计划
get_tactical_plan_for_ticker() → dict

# L4 执行计划
create_execution_plan() → str
get_pending_executions() → list      # 待确认列表
confirm_execution() → bool
reject_execution() → bool

# 投委会
create_committee_review() → str
get_reviews_for_execution() → list
create_committee_consensus() → str

# 催化剂
create_catalyst() → str
get_catalysts_for_entry() → list
update_catalyst_status() → bool

# 模拟交易
get_sim_account() → dict
update_sim_account() → bool
create_sim_position() → str
update_sim_position() → bool
create_sim_trade() → str
get_sim_trades() → list
get_sim_positions() → list

# 反馈 & 复盘
create_trade_feedback() → str
get_rejection_patterns() → list      # 用于 LLM 学习
create_auto_review() → str

# 用户偏好
save_preference() → str
get_preferences() → list
```

---

## 十四、实施路线图

### Phase 8B.1：Layer 1-2 + 数据层（2 周）

| 任务 | 依赖 | 说明 |
|------|------|------|
| store.py 扩展 14 张表 | 无 | CREATE TABLE + 基础 CRUD |
| decision_engine.py — L1 宏观策略 | store, prompts | `run_macro_strategy()` + `run_macro_check()` |
| decision_engine.py — L2 组合策略 | L1, store | `run_strategic_plan()` + `run_deviation_check()` |
| catalyst_monitor.py | store | 催化剂检测 + 生命周期管理 |
| decision_api.py — L1/L2 端点 | decision_engine | GET/POST macro, strategic |

### Phase 8B.2：Layer 3-4 + 投委会（2 周）

| 任务 | 依赖 | 说明 |
|------|------|------|
| decision_engine.py — L3 战术计划 | L2, strategy_records | `run_tactical_plans()` |
| decision_engine.py — L4 执行方案 | L3, sim_account | `run_execution_plans()` |
| committee.py — 4 成员评审 | L4, 4 个 LLM | 并行调用 + 共识汇总 |
| decision_api.py — L3/L4/投委会端点 | committee | 确认/拒绝/时间线 |

### Phase 8B.3：闭环机制 + 模拟交易（1.5 周）

| 任务 | 依赖 | 说明 |
|------|------|------|
| trade_executor.py — 模拟执行 | store, sim_* 表 | 确认→写入 sim_trades + 更新 sim_positions |
| trade_executor.py — 自动复盘 | sim_trades | 交易关闭后 LLM 复盘 + 经验卡片 |
| trade_feedback 学习 | trade_feedback 表 | 拒绝模式提取 → L4 prompt 注入 |
| trading_api.py | trade_executor | 账户/持仓/交易/复盘 API |

### Phase 8B.4：前端实现（2 周）

| 任务 | 依赖 | 说明 |
|------|------|------|
| index.html — 策略中心抽屉 HTML | — | `#dc-strategy-drawer` + 工具栏按钮 |
| decision.js — 策略中心逻辑 | decision_api | 加载/渲染/确认/拒绝/抽屉切换 |
| watchlist.js — 策略 Tab 升级 | decision_api | 子 Tab + 催化剂时间线 + 操作建议列 |
| trading.js — 交易中心页面 | trading_api | 账户/持仓/交易记录/复盘 |
| style.css — `.dc-` / `.tc-` 样式 | — | 设计令牌 + 动画 + 响应式 |
| strategy-chat.js + chat_ws.py | WebSocket | 对话浮窗（可推迟到 8B.5） |

### Phase 8B.5：集成测试与优化（1 周）

| 任务 | 依赖 | 说明 |
|------|------|------|
| 端到端测试 | 全部模块 | 完整日常决策流程 → 确认 → 交易 → 复盘 |
| 性能优化 | — | LLM 并行调用、前端懒加载 |
| 调度器集成 | scheduler | 决策流程的定时触发 |
| 预算系统对接 | budget | 确保降级模式下决策流程可用 |

### 依赖关系图

```
Phase 8B.1 ────→ Phase 8B.2 ────→ Phase 8B.3
     │                │                │
     └────────────────┴────────────────┘
                      ↓
                Phase 8B.4 ────→ Phase 8B.5
```

> Phase 8B.1-3 是后端核心，可以在无前端情况下通过 API 测试。Phase 8B.4 前端可以在 8B.2 完成后并行启动（用 mock 数据）。

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

