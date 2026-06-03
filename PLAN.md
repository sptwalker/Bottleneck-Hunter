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

**下一步**：接入真实 LLM API 进行端到端测试，验证完整流程

---

## 五、风险与注意事项

1. **LLM 幻觉风险**：产业链拆解结果可能包含不准确信息，需要与预设数据交叉核对
2. **数据源限制**：A 股供应商数据依赖 AKShare 接口稳定性，需做好降级处理
3. **API 成本**：多模型交叉验证会显著增加 API 调用成本，建议设置模型数量上限
4. **回测验证**：开发完成后，应选取历史案例（如光模块产业链）验证系统输出质量
5. **合规风险**：输出报告需明确标注"仅供参考，不构成投资建议"
