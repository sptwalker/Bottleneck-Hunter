# 供应链瓶颈分析流程全面审查与提升方案

> 审查日期：2026-09-27
> 范围：`bottleneck_hunter/chain/` 三步法（产业链拆解 → 供应商检索 → 交叉验证）及其生产执行路径
> 视角：产业链专家 / 金融分析师
> 方法：逐模块通读代码 + 逐一核对调用点，**不采信任何既有审计转述**

---

## 0. 结论总览

三步法的**骨架是对的**：从终端产品逐层拆到原材料、沿瓶颈环节找被忽视的供应商、用独立证据拷问投资逻辑。这套方法论没有问题。

问题出在**四处"看起来做了、实际没生效"**：

1. **真实数据被下游覆盖**（A-6）：投了很多工程去拿真实 CR3/HHI 并做一致性校准，但校准跑在 z-score 标准化**之前**，结果在 ≥3 个环节时整批被重写 —— 而且**报告里那句"[校准: 6→4]"还留着**，读者看到的文本与实际分数不再一致。
2. **字段语义跨市场错配**（A-1/A-2）：美股的资产负债率位存的是 debt-to-equity、每股现金流位存的是现金流总额。这两个字段同时喂给评分和事实核查。
3. **事实核查有方向反了的规则**（A-3/A-4）：低负债、低 PE 这类**健康**声明被判为"与数据矛盾"，一家健康公司反而更容易被降级；另有 `cashflow_per_share` 一条恒真式、`market_share` 一条循环自证。
4. **两套相反的数据缺失原则并存**（A-7/B-2）：`investability_filter` 缺失即跳过（对），`AlphaScorer` 缺失即给中性 5.0（错），`_blend` 的回退链则写了但从不触发。

**共 23 项发现**，其中 P0（会直接产出错误结论）4 项，P1（口径失真）13 项，P2（覆盖缺口与维护债）6 项。

一句话判断：**当前流程的短板不在"分析深度"，在"分析链路上游的真实数据没有一路活到产出"**。所有 P0/P1 的修法都很小 —— 最多是几行到几十行，没有一项需要重构。

---

## 1. 前置：先确认哪些代码是活的

评估"这个缺陷有多严重"之前，必须先确定这条路径是否真的在生产上跑。审查中我逐个核对了调用点，结论如下（这一节是后面所有严重性判断的依据）：

| 路径 | 状态 | 证据 |
|---|---|---|
| `/api/phase1` `/api/phase2` `/api/phase4` → `web/streaming/phases.py` | **生产主路径** | `index.html:2563` 唯一 module script 是 `app.js`，它 import 了 `phases.js` |
| `_common.py` 的 `_run_*_with_progress` | **活的，且接线正确** | `_common.py:122` 传 `chain_graph`、`:133` 传 `financial_map` |
| `/api/screen` → `legacy.py:stream_screening` | HTTP 端点活着，**前端不可达** | `index.html` 无 `startScreening` 调用；`app.js` 未 import `pipeline.js`/`panel.js`/`dashboard.js`/`history.js` |
| `pipeline.js → panel.js → dashboard.js`、`history.js` | **前端孤岛** | 该簇只自引用；`history.js`/`account.js` 全仓零 importer |
| `cross_validation.py`（`CrossValidator`） | 主流程**死**，反向分析**活** | `graph.py:209` 构造后传入 `validator` 参数 —— 该参数在 `build_screening_graph` 正文**零引用**；但 `reverse_api.py:151`、`legacy.py:336/443` 在用 |
| `graph.py:run_screening` → CLI `screen` | **未接线** | 见 D-1 |

**一条被推翻的怀疑**：审查初期怀疑"FactCheck 结果算完只打了日志、没到前端"。**核实后不成立** —— `phases.py:838` 的 `step_done(step="fact_check_review", result=recommendations)` 把 credibility/recommendation 发给了前端，`:850` 的 `store.update_cross_validations` 落了库，`:815` 的 `passed_top` 让 REJECT 在截断前就被滤掉。**这条链路是通的，且是干净的。**

---

## 2. 【A 类】数据正确性缺陷

> 这一类的共同特征：不是"分析得不够深"，而是**分析用的数字本身是错的**。

### A-1【P0】美股 `debt_ratio_pct` 存的是 debt-to-equity，不是资产负债率

`financial_data.py:454`：

```python
snap.debt_ratio_pct = _safe_float(info.get("debtToEquity"))
```

注意 `_safe_float` 的第二参数 `scale` 默认 `1.0`（`:104`），而 A 股路径（`financial_data.py:321-323`）取的是同花顺「资产负债率」，是真 D/A：

```python
debt_cols = [c for c in cols if "资产负债率" in c]
if debt_cols:
    snap.debt_ratio_pct = _safe_float(row0[debt_cols[0]])
```

**这不是"略有偏差"，是两个不同的财务比率。**

| | 口径 | 典型量级 |
|---|---|---|
| A 股入参 | 资产负债率 = 总负债/总资产 | 30–70（百分数） |
| 美股入参 | `debtToEquity` = 总负债/股东权益 | Yahoo 返回百分数形式，典型 30–200 |

**下游两处消费，都按"D/A 越大越差"解读：**

1. `supplier_eval.py:88-93`（`_data_financial_health`，该分量权重 0.20）
   ```python
   if debt < 25:    s = 9.0
   elif debt < 40:  s = 7.5
   elif debt < 55:  s = 6.0
   elif debt < 70:  s = 4.5
   else:            s = 3.0
   ```
   一家真实 D/A=40%（健康）的美股公司，若 D/E=0.67 → Yahoo 返回 `67` → 落进 `<70 → 4.5` 档，**比正确档位（7.5）低 3.0**。D/E 越高偏差越大：D/E=1.5（负债率 60%，尚可）会得到 `>70 → 3.0`，即**最低档**。

   传导链：3.0 的偏差 → `data_fh` 降低约 3.0×0.20/2.0 ≈ 0.3（该分量内 0.20 权重的四项归一后）→ `_blend` 取 0.7 权重 → `financial_health` 降约 0.2 → 但 `fh_w = 2.0`，故 `base_overall` 降约 0.2×2.0/5.0 ≈ 0.08 → `overall = base×0.8 + moat×0.2` 降约 0.06 分。

   **单看数值影响不大（约 0.06-0.1 分），但方向是系统性的：所有美股公司的财务健康分一律被压低，且 D/E 越高压得越狠 —— 恰好惩罚了杠杆正常的重资产公司。**

2. `fact_check.py:33` 的规则 `(负债率?\s*(低|健康|可控), "debt_ratio_pct", "negative", "mismatch")`：`_judge_direction`（`:331-338`）对 `debt_ratio_pct` 是 `<50 → "positive"`（注释「低负债=好」，这个判据本身是对的）。美股入参普遍 ≥50，**一条成立的低杠杆声明在美股上会落进 `neutral` 或 `negative`，被判不成立。**

**净效果：美股公司的财务健康分被系统性低估，且低杠杆声明被系统性误判。A 股不受影响 —— 这正是 A/美股打分量级不一致的老病根（参见 memory `project_analyst_count_fix`）。**

> **验证建议（实施前必做）**：Yahoo 的 `debtToEquity` 是否始终为百分数，需要取几家已知财报数据的真实美股比对（如 AAPL / XOM 的最新 10-Q 资产负债表）。若某些标的自 `yfinance` 返回的是小数（0.67 而非 67），则 A-1 的修法需要**同时处理两种量级**（`if v < 10: v *= 100`），而不是单纯换算。

### A-2【P0】美股 `cashflow_per_share` 存的是经营现金流总额

`financial_data.py:455`：

```python
snap.cashflow_per_share = _safe_float(info.get("operatingCashflow"))
```

`operatingCashflow` 是**总额**（Yahoo 单位为单位美元，量级 10⁹–10¹¹），字段名却是 per-share。

**下游两处：**

1. `supplier_eval.py:227` 把它原样渲染进 prompt（`_format_financial_block`）：
   ```python
   ("每股经营现金流", snap.cashflow_per_share, ""),
   ```
   → 提示词里会出现「每股经营现金流: 48700000000」。LLM 面对这个数字只有两种反应：忽略，或据此认为现金流极好。**两种都不可控。**

2. `fact_check.py:32` 的规则 severity 是 **`fatal`**：
   ```python
   (r"现金流\s*(充裕|健康|良好)", "cashflow_per_share", "positive", "fatal"),
   ```
   而 `_judge_direction`（`:324-329`）对 `cashflow_per_share` **只看符号**：
   ```python
   if value is not None and value > 0:
       return "positive"
   else:
       return "negative"
   ```
   存入的是 `operatingCashflow` **总额**，一家正常经营的公司几乎必然为正 → 判定恒为 `positive` → 与规则声明的 `expected_dir="positive"` **恒相等** → **恒判 supported**。

   **这条 `fatal` 级别的规则实际上是个恒真式**：它从不产生 `fatal_contradiction`，只在 `supported_count` 上白送一分（credibility `+0.2`，上限 1.0）。**一条不会失败的检查，比没有这条检查更糟 —— 它让报告看起来经过了更严格的核查。**

### A-3【P0】FactCheck 四条规则的期望方向写反，健康公司反而被降级

`fact_check.py:27-47` 的 `_CLAIM_RULES` 里，`expected_dir` 与 `_judge_direction` 的返回值**系统性相反**：

| 规则 | 声明 | 表中 `expected_dir` | `_judge_direction` 实际返回 | 判定 |
|---|---|---|---|---|
| `:33` | 负债率低/健康/可控 | `"negative"` | `"positive"`（<50，注释明写"低负债=好"） | **恒 mismatch** |
| `:36` | 估值/PE 低/便宜/被低估 | `"negative"` | `"positive"`（<20，注释"低PE=便宜"） | **恒 mismatch** |
| `:37` | 高估/贵/泡沫 | `"positive"` | `"negative"`（>40，注释"高PE=贵"） | **恒 mismatch** |
| `:46` | 做空/沽空 压力/风险 | `"positive"` | `"neutral"`（≤10）/ `"positive"`（>10） | 低做空时 mismatch |

`_judge_direction` 的注释（`# 低负债=好`、`# 低PE=便宜`）说明**判据函数本身是对的**，是规则表抄错了符号。

**后果是反向激励**：一家负债率 30%、PE 18 的公司，如果它在 strengths 里诚实地说"负债率低"和"估值便宜"，会被记 **2 条 mismatch**；再加上 `:37` 那条（若它同时提到"不贵"）累计 **3 条 → 触发 REVIEW**。

credibility = `10 - fatal×3 - mismatch×0.5 + min(1.0, supported×0.2)`，3 条 mismatch → `-1.5` → credibility 8.5 → `penalty_factor = 1 - 0.3×(1-0.85) = 0.955`，`overall_score` 打 95.5 折。**折扣不大，但 REJECT/REVIEW 的标签会直接呈现在 Phase 4 表格的"事实核查"列上，误导用户。**

### A-4【P0】`market_share` 规则是循环自证

`fact_check.py` 的 `_get_data_value`：

```python
if field == "market_share":
    # 用 market_position 评分作为代理
    return scorecard.market_position, "market_position"
```

而规则是 `:40`：

```python
(r"(龙头|龙一|第一|领先|市占率高)", "market_share", "positive", "mismatch"),
```

**所谓"核实"，是拿 LLM 打的 `market_position` 分（0-10）去核实 LLM 自己写的"龙头"声明。** 没有引入任何外部事实。

`_judge_direction` 的判据是 `market_position >= 7.5 → positive`。所以这条规则的真实语义是：「LLM 写了『龙头』，且 LLM 给自己的 market_position ≥ 7.5」→ supported。**这不是事实核查，是自洽性检查。**

同一份 `_CLAIM_RULES` 里，`cr3_estimate`（`:41`）倒确实引用了 `bottleneck_report.cr3_estimate`（可能来自 akshare 真实数据）—— 说明**作者知道该怎么接真数据，只是 `market_share` 这条偷懒了**。

### A-5【P1】CR3 与 HHI 两条规则对同一事实复合计分，且不看 `company_count`

`_check_hhi_consistency`（`bottleneck.py:650-710`）里，**两条规则指向同一个维度**：

```python
if hhi is not None:
    if hhi > 2500:   scarcity:  if score < 6: score = max(6.0, score + 2)
    elif hhi < 1500: scarcity:  if score > 6: score = min(6.0, score - 2)
if cr3 is not None:
    if cr3 > 80:     scarcity:  if score < 7: score = max(7.0, score + 2)
    elif cr3 < 30:   scarcity:  if score > 4: score = min(4.0, score - 2)
```

两个 `if` 是**顺序执行、各自修正 `scarcity.score`** 的，于是同一个底层事实被计了两次：

| 起始 scarcity | 低集中（hhi<1500 且 cr3<30） | 高集中（hhi>2500 且 cr3>80） |
|---|---|---|
| 9.0 | → `min(6.0, 7.0)` = 6.0 → `min(4.0, 4.0)` = **4.0**（−5.0） | — |
| 2.0 | — | → `max(6.0, 4.0)` = 6.0 → `max(7.0, 8.0)` = **8.0**（+6.0） |

**一次"该板块不集中"的观察，最多可让 scarcity 掉 5 分（9→4）。** 而 HHI 与 CR3 在数学上高度相关（同为份额集中度的度量），本就不该独立计分。`pricing_power` 只吃 HHI 那条、不吃 CR3，两条路径的口径也不一致。

**另一处：`_check_hhi_consistency` 的签名里没有 `company_count`。**

```python
def _check_hhi_consistency(self, scores, cr3, hhi, node_name) -> list[str]:
```

而 `company_count` 就在 `real_conc` 里（`industry_concentration.py:144` 已带出）。**HHI=2500 在 5 家成分股的板块和 200 家成分股的板块上，含义完全不同** —— 东财概念板块的成分股数从个位数到上百都有，同一个绝对阈值套在所有板块上，窄板块（如「高纯石英砂」这类概念板块本身就只有十几家）会因为"板块窄"而不是"行业集中"触发高集中判定。

（注：`hhi` 本身用**全样本**份额计算，`industry_concentration.py:56` 的 `shares` 覆盖全部成分股，`:140` 的 `top_companies[:5]` 仅用于展示，不参与 HHI。所以 `hhi > 2500` 这条分支**是可达的**。）

### A-6【P1】真实数据的 HHI/CR3 校准被 z-score 标准化整批重写

这是本次审查**最重要的一条**。

`bottleneck.py` 里两个函数的执行顺序：

```python
# _analyze_node 内部（每节点，先执行）
adjustments = self._check_hhi_consistency(scores, cr3, hhi, node_name)   # :627  改 scores[i].score
overall = self._weighted_score(scores)                                    # :629
return BottleneckReport(..., scores=scores, ...)                          # :631

# analyze 内部（整批，后执行）
normalize_scores(reports)                                                 # :294  ← 覆盖上面的一切
for rpt in reports:
    rpt.overall_score = round(self._weighted_score(rpt.scores), 2)        # :295-296 用被覆盖后的分重算
reports.sort(key=lambda r: r.overall_score, reverse=True)                 # :298
```

`normalize_scores`（`:114-190`）对每个维度：

```python
values = [v for _, v in dim_scores]
mu = mean(values); sigma = stdev(values)
...
for idx, raw in dim_scores:
    z = (raw - mu) / sigma
    normalized = max(0.0, min(10.0, round(5.0 + z * 2.0, 1)))
    for s in reports[idx].scores:
        if s.dimension == dim.value:
            s.score = normalized      # ← 校准结果被丢弃
            break
```

`_check_hhi_consistency` 是把 scarcity 从 4 拔到 6 之类的**绝对修正**；`normalize_scores` 随后按**批次内的相对位置**重写为 `5 + 2z`，并且 `:295-296` 用重写后的分数**重算了 `overall_score`**。**真实数据做的绝对锚定，被相对标准化抹平，且总分也跟着被改。**

残留的只有两样东西：

1. `reasoning` 里被追加的 `[HHI校准: HHI=1820<1500, 6→4]` 前缀 —— 但它旁边那个 `score` 已经被换了，**文本与数值不再一致**，报告读者会看到"校准到 4 分"而实际分数是 6.3。这是**比"校准被抹掉"更糟的状态**：日志和报告会主动误导。
2. `hhi_adjustments: list[str]` 字段 —— 前端 `charts.js` 只渲染 `concentration_detail`，不读 `hhi_adjustments`，所以**没有任何消费方**。

**适用条件（重要）**：`normalize_scores` 对样本数 < 3 的维度**直接跳过**（`:145` `if len(dim_scores) < 3: continue`）。所以：

- **只拆出 1-2 个瓶颈环节时，校准是生效的**；
- **≥3 个环节时（常态），校准被完全抹掉。**

即：这套机制的效果**随分析规模翻转**，同一份代码在小链路上对、在大链路上错。这比"一直错"更难发现。

`cr3_estimate` / `hhi_estimate` 两个**字段本身不受影响**（只有 `scores` 被标准化），所以报告上展示的 CR3/HHI 数字是真的 —— **只是它们对评分的校准作用没了**。

> 这条与 memory 里 `project_analyst_count_fix`（A股/美股量级不一致）、`yf_gate` 浮点临界雷属于同一类：**工程做对了，接线接错了。**

### A-7【P1】可投性筛选用了 `supplier.gross_margin`，而三条供应商源一条都不填这个字段

`investability_filter.py:85-89`：

```python
gross_margin = None
if financial and financial.gross_margin_pct is not None:
    gross_margin = financial.gross_margin_pct
elif supplier.gross_margin is not None:
    gross_margin = supplier.gross_margin

if gross_margin is not None:
    scores["gross_margin"] = gross_margin
    if gross_margin < self.min_gross_margin:      # 默认 20.0
        reasons.append(f"定价权弱 (毛利率 {gross_margin:.1f}% < 阈值 ...)")
else:
    scores["gross_margin"] = "N/A"                 # ← 缺失即跳过，不误杀
```

**全仓 grep 结论：`supplier.gross_margin` 与 `supplier.revenue_growth` 在 `bottleneck_hunter/` 下只有读取、没有任何写入点。**

四条 `SupplierInfo` 构造路径（`supplier_search.py:371` akshare / `:582` gangtise / `:790` A股LLM / `:864` 美股LLM）**都没有传 `gross_margin` / `revenue_growth`**。唯一的写入者是反向分析路径（`web/streaming/reverse.py:379-381`，从 `FinancialSnapshot` 回填三个字段）—— **三步法主线不经过它。**

**所以 `supplier.gross_margin` 在三步法里恒为 `None`，可投性规则 2 的第二个分支是死代码**，实际行为完全取决于 `financial`（即 `FinancialSnapshot`）是否拿到。

**这一点本身不是 bug** —— 缺失即跳过正是这个模块的设计原则（「不因缺少数据而误杀」）。**问题是它让这条规则的效力比看上去弱得多：**

- 规则 2 只剩 `FinancialSnapshot` 一条数据源；抓不到就整条跳过；
- 而 `_blend`（`:209`）里的 `data_fh` / `data_val` 回退链（`:51-66` / `:101-126`）**也指向同样为 None 的 `supplier.revenue_growth` / `supplier.gross_margin`** —— 这两处的回退分支同样是死的。

**净效果**：`_blend` 的回退逻辑写了但从不触发（这实际上是**好事** —— 它意味着 `data_*` 只可能来自真实 `FinancialSnapshot`，A-7 原文担心的"LLM 自报数字冒充数据"**不成立**）；真正的风险在反面：**数据抓取失败时，没有任何回退层，全部退化成纯 LLM 评分，且不留痕**。

`AlphaScorer` 是同一问题的另一面（见 B-2）：它把"没抓到数据"直接变成中性分 5.0，而不是像 `investability_filter` 那样显式跳过并标 `N/A`。**同一个仓库里，两种相反的处理原则并存，且没有说明哪个是有意的。**

---

## 3. 【B 类】口径与语义不一致

### B-1【P1】供应商层的"批次校准"是提示词建议，不是算法约束

两层的标准化确有不对称：

| 层 | 是否做 z-score | 机制 |
|---|---|---|
| 瓶颈 | **有** | `normalize_scores` 硬重写 `scores[i].score = 5 + 2z`（`bottleneck.py:114-190`） |
| 供应商 | **无硬约束，只有提示词** | `_compute_batch_context`（`supplier_eval.py:486-513`）把批次的市值/PE/毛利率**范围**写进 prompt |

`supplier_eval.py:340-353`：

```python
batch_block = ""
if batch_context and len(batch_context) > 1:
    blines = ["\n## 同批次候选概况（供参考，用于拉开差异）"]
    ...
    blines.append("- 请根据该公司在同批次中的相对位置拉开评分差异")
```

**这是"建议"而非"约束"** —— 与瓶颈层"算出 z 值直接赋值"是两种强度的干预。LLM 是否照做无保证，且**无任何事后校验**（没有"若批次方差过小则重新标准化"的兜底）。

而 `supplier_eval.md:50-54` 的强制分布要求（「9个维度中，至少 2 个维度的分数 ≤ 4 分或 ≥ 8 分」）是**同一个问题的第二个补丁**。两个补丁叠加的结果是：

- prompt 要求「拉开差异」（batch_block）
- prompt 又要求「每家都必须有极端分」（强制分布）
- **但没有任何机制校准"中心位置"**

**后果**：若 LLM 整体给分偏严（一家批次的公司全落在 5-6 分），`quality` 系统性偏低，下游无校正。而 `alpha` 里的 `bottleneck_score` 是**标准化过的**（尺度稳定在 5±2）——**两条输入一路经过 z-score、一路不经过，却在 `FinalScorer` 里直接相乘**：

```python
raw = (quality ** 0.55) * (alpha ** 0.45)
```

**这是本次审查里口径最不干净的一处**：乘法的两个因子尺度不同源，一个批次相对、一个绝对，`0.55/0.45` 的权重在两种情形下含义不同。

**另一个具体矛盾**：强制分布要求（「必须至少有 2 个维度是极端分」）与同文件 `:44` 的独立判断原则冲突。一家各方面都中规中矩的公司，被 prompt 逼着必须编出 ≤4 或 ≥8 的分。而这些被逼出来的极端分**恰好落在 4 个 moat 维度上**（因为 5 个核心维度有数据锚，LLM 不敢乱写），moat 又以 `overall = base_overall*0.8 + moat_overall*0.2` 进入总分。

### B-2【P1】`AlphaScorer._tier_score` 用 5.0 表示"无数据"，与"恰好平庸"不可分

`supplier_eval.py:678-685`：

```python
@classmethod
def _tier_score(cls, value: float | None, tiers: list[tuple]) -> float:
    if value is None:
        return 5.0
    ...
```

`compute` 里有**四处**这样的默认（`:751`、`:756`、`:763`、`:768`）：

```python
s_analyst = 5.0
if snap and snap.analyst_report_count is not None:
    s_analyst = cls._tier_score(...)       # 有数据才覆盖
```

于是**数据全缺的公司**：`raw = 5×0.20 + 5×0.267 + 5×0.333 + 5×0.20 = 5.0` → `market_attention = 5.0` → `information_gap = 5.0` → `base_alpha = sqrt(瓶颈分 × 0.5) × 2` → **一个完全中性的 alpha**。

**这直接击穿了这个模块的立意。** AlphaScorer 的全部目的是识别"**被市场忽视**"的公司（`alpha = 瓶颈重要性 × (1 − 关注度/10)`）。而"我们没拿到它的数据"被翻译成了"它被适度关注" —— 一个**我们不知道是否被忽视**的公司，拿到的分数与**确实被忽视**的公司（关注度 5）完全一致。

更严重的是 `score_all`（`:873`）：

```python
bn_score = bottleneck_map.get(sc.bottleneck_node.split(",")[0].strip(), 5.0)
```

**瓶颈分缺失也给 5.0。** 而 `bottleneck_node.split(",")[0]` 这个"取第一个环节"的操作本身也是隐患（见 C-5）。

对照：项目在别的模块（`_YF_DEGRADED`/`_YF_ATTEMPTED`、`yf_degraded` payload）是**明确区分"取到 0 分"和"没取到"**的，并且把它显式入档给用户看。**同一套原则没有贯彻到 AlphaScorer。**

### B-3【P1】催化剂加分完全不看置信度

`supplier_eval.py:726-732`：

```python
@classmethod
def _compute_catalyst_bonus(cls, scorecard: SupplierScorecard) -> float:
    cat = scorecard.catalyst
    if not cat or not cat.events:
        return 0.0
    return round(cat.urgency_score / 10 * 2.0, 2)
```

`CatalystEvent.confidence` 是模型里的正式字段（`catalyst.py:142` 从 LLM 输出解析并 clamp），`catalyst.py` 的 prompt 也明确要求评估置信度 —— **但它不参与任何计算。**

**后果**：一个"传闻新产线"（confidence=3）和一个"已公告投产"（confidence=9）的催化剂，只要 `urgency_score` 相同，加同样的分。而 `urgency_score` 是 LLM 对整个事件列表的整体打分（`catalyst.py:148`），同样没有按 confidence 加权。

`catalyst_bonus` 直接**加**在 alpha 上（`:801`），上限 2.0 —— 在 alpha 普遍落在 3-6 区间的现实下，**这是单项最大的加分项，却是唯一不看证据强度的加分项。**

### B-4【P2】催化剂 prompt 内嵌 2025Q3 示例，且时间字段无结构、无校验

`catalyst.py:105-110`：

```python
"events": [
    {{
      "event_type": "capacity",
      "description": "新产线投产，产能翻倍",
      "expected_date": "2025Q3",
```

示例日期距审查日（2026-09）已过期**整整一年**。LLM 对示例值有强锚定（项目在 `bottleneck.py:541-554` 里专门写了"注意：下面的数值仅为格式参考"来对抗这种锚定 —— **催化剂 prompt 没做这个防护**）。

同时 `expected_date` 与 `investment_window` 都是自由文本，没有格式约束也没有过期校验。`CatalystTimeline` 模型里也没有字段级 validator。**一个说"2025Q3 投产"的催化剂，在模型中与"2027Q1 投产"结构化程度完全相同，下游无法按时间过滤。**

### B-5【P2】报告层第 5 章与"共识"列是死的，而 FactCheck 结果零呈现

`report.py:120-131`、`:148-154`、`:220-222` 全部以 `if result.cross_validations:` 为条件。而 `graph.py:236` 恒传 `[]`：

```python
cross_validations=[],  # 旧字段保留兼容,已废弃
```

**所以第 5 章「多模型交叉验证」和 6 章的 cv_map 共识列永远不渲染** —— 但页脚 `:173` 照旧宣称：

```
*方法论：Serenity「三步法」— 产业链拆解 → 供应商检索 → 多模型交叉验证*
```

**问题不只是"死代码"**，而是：真正生效的 FactCheck 结果（`credibility`、`fact_check_recommendation`、`quality_adjusted`）**在报告里一个字都没有**。报告的读者看不到"这家公司的分数被事实核查打了折"。

同样缺失的还有 `cr3_source`（`"akshare"` 真实 / `"llm_estimate"` 估算）—— 报告把真实数据和 LLM 估算呈现为同一个数字，读者无从分辨。（前端 `charts.js:777` 是区分的，`if (report.cr3_source === 'akshare')`，**只有报告层没跟上**。）

### B-6【P1】"客户验证"与"产能状况"是纯 LLM 无锚维度，且数据越缺权重越高

`supplier_eval.py:430-437`：

```python
cv = data.get("customer_validation", 0)          # 纯 LLM，无 blend
cs = data.get("capacity_status", 0)              # 纯 LLM，无 blend
mp_w = 1.5 if data_mp is not None else 1.0
fh_w = 2.0 if data_fh is not None else 1.0
val_w = 2.0 if data_val is not None else 1.0
weighted_sum = mp * mp_w + cv * 1.0 + cs * 1.0 + fh * fh_w + val * val_w
total_weight = mp_w + 2.0 + fh_w + val_w
```

5 个维度中，`market_position`(0.5 混合)/`financial_health`(0.7)/`valuation`(0.7) 有数据锚，**`customer_validation` 与 `capacity_status` 完全没有**。

而这两者的合计权重**随数据缺失上升**：

| 情形 | total_weight | cv+cs 占比 |
|---|---|---|
| 数据齐全（mp=1.5, fh=2.0, val=2.0） | 7.5 | **26.7%** |
| 数据全缺（mp=1.0, fh=1.0, val=1.0） | 5.0 | **40.0%** |

**「越是拿不到数据的公司，评分越依赖 LLM 凭空判断的客户验证与产能状况」。** 加上 `overall = base×0.8 + moat×0.2` 里 moat 的 `capacity_lead_time`（0.2×¼ = 5%），**产能口径合计约 13–18% 的分完全无外部数据支撑**。

而"产能状况"恰恰是产业链专家视角下**最应该有数据、也最该有数据**的维度 —— 项目已经在 `financial_data.py:624-627` 抓了 `CAP_MAINBIZ`（主营构成，含分产品营收/毛利率），但**产能利用率、在建工程、扩产公告**一个都没有。

---

## 4. 【C 类】覆盖缺口（产业链专家视角）

### C-1【P2】产业链节点模型缺"约束类"字段

`decomposer.py` 的 per-node schema（在 user prompt 里内联）为：

```
name / description / function / key_parameters / upstream_deps /
dependency / alternatives / notes / representative_companies[{name, code}]
```

其中：
- `alternatives` 是**裸 int**（替代方案数量）
- `dependency` 是**裸 float**
- `notes` 是自由文本

**缺失的关键字段**（按"这个环节是否会被卡脖子"的判据重要性排序）：

| 缺失字段 | 为什么关键 |
|---|---|
| 单一/多源供应结构 | "只有一家能做"是最强的瓶颈信号，现在只能靠 LLM 在 notes 里带一句 |
| 产能 / 扩产周期 | 决定供需缺口持续多久 |
| 认证周期 | 半导体/医药/航空的核心壁垒，决定新进入者需要多久 |
| 地理集中度 | 台海/日本/荷兰的单一地域集中是最大的尾部风险 |
| 出口管制 / 政策风险 | 直接决定环节能否被投资，现在完全无法表达 |
| 库存水平 | 判断"缺货"还是"补库存"的关键 |

注意 `capacity_lead_time` **确实存在** —— 但在 `MoatScore`（`models.py`）里，是**供应商层**的 LLM 0-10 打分，不是**环节层**的事实字段。**"这个环节本身产能扩张要多久"这个产业链事实，被降格成了"这家公司在这点上打几分"的主观评价。**

### C-2【P1】权重最高的维度（供需缺口 0.25）是唯一无任何数据锚的

`bottleneck.py:33-39`：

```python
DEFAULT_WEIGHTS = {
    SCARCITY: 0.20, IRREPLACEABILITY: 0.20,
    SUPPLY_DEMAND_GAP: 0.25,        # ← 权重最高
    PRICING_POWER: 0.15, TECH_BARRIER: 0.20,
}
```

对照数据锚覆盖：

| 维度 | 数据锚 | 状态 |
|---|---|---|
| `scarcity` | CR3/HHI（akshare 板块） | 有，但被 A-6 抹掉 |
| `irreplaceability` | 无 | LLM |
| **`supply_demand_gap`** | **无** | **LLM（权重最高）** |
| `pricing_power` | HHI 间接（且被 A-6 抹掉） | 弱 |
| `tech_barrier` | 无 | LLM |

**权重最高的维度，恰恰是唯一既没有数据锚、也没有校准逻辑的维度。** 而"供需缺口"是本方法论的核心命题（产能瓶颈 = 供不应求）。

**可获得但未接入的数据**：`CAP_MAINBIZ`（已在库）、在建工程/固定资产周转（财报可得）、产能利用率（部分行业披露）、产品价格趋势（`stock_zh_a_hist` 可得）、存货周转天数（财报可得）。**这些不是"数据不可得"，是"算法未接"** —— 与 memory 里 `project_vip_value_assessment_2026-08` 记录的结论同型。

### C-3【P1】`search_bottlenecks` 从不传 `keywords`，且 gangtise / akshare 两源的匹配口径已经分裂

`supplier_search.py:1047-1060`：

```python
async def _task(bn: BottleneckReport, idx: int):
    async with sem:
        await self._emit(f"▸ 检索: {bn.node_name} ({idx}/{total})")
        suppliers = await self.search(bn, chain_graph=chain_graph)      # ← 无 keywords
```

`_common.py:122` 同样不传。**全仓零调用点传 `keywords`。**

**后果一**：`_extract_keywords`（`:1030-1045`，专门写来处理"论述长句无法匹配板块名"的函数）**只对 akshare 源生效**（`:560`）。

**后果二**：gangtise 源（`:571-572`）走另一条路：

```python
kw = (keywords[0] if keywords else bottleneck.node_name) or ""
```

`keywords` 恒为 None → **恒用完整 `bottleneck.node_name`**（例如"高端石英坩埚用高纯石英砂"）去匹配板块名。

而 akshare 那条路的注释明写着这个坑（`:555-558`）：

```python
# 不再喂 key_insights——那是论述长句，无法 substring 匹配板块名，
# 反而把整句灌进 term 令 akshare 100% 0 命中（Loki 归因）。
```

**同一个坑，akshare 侧修了，gangtise 侧没修。** `CAP_SCREEN` 是"板块内主营含词"粗筛，用长环节名必然低召回。

**后果三**：`_extract_keywords` 与 `industry_concentration._extract_keywords` 是**两份实现**（`industry_concentration.py` 里注明"避免跨模块耦合"）—— 也就是说这个坑将来还要修第三次。

### C-4【P1】多源合并是"先到先得"，不是"交叉印证"

`supplier_search.py:583-598`：

```python
# --- 按 ticker 去重合并（LLM 优先 > chain > gangtise > akshare）---
for supplier in llm_results:
    if supplier.ticker not in merged:
        merged[supplier.ticker] = supplier
        source_stats["llm"] += 1
for supplier in chain_results:
    if supplier.ticker not in merged:
        ...
```

**固定优先级，首次命中即定，后续源的同 ticker 条目直接丢弃。**

进度消息（`:615-618`）报告"LLM 3 家 + 产业链 2 家 + 选股 5 家 → 去重后 7 家" —— 这个"去重后"数字掩盖了一个事实：**被 3 个源同时命中的公司，和被 1 个源命中的公司，在下游完全等价。**

`SupplierInfo` 上没有 `sources: list[str]` 或 `source_count` 字段，`source` 是单值。所以下游无从加权。

**"多源交叉验证"是这个方法论的卖点之一，但在实现上它不是验证，是取并集后按源排序。**

### C-5【P2】产业链图谱候选只取"本节点 + 1 跳直接上游"

`supplier_search.py:957-962`：

```python
target_nodes = []
node = chain.get_node(bottleneck.node_name)
if node:
    target_nodes.append(node)
    target_nodes.extend(chain.get_upstream(bottleneck.node_name))
```

只含本节点与其直接上游。**不含**：同层竞争环节、2+ 跳上游、下游（对下游有议价权的环节，其下游才是客户）。

另外 `_extract_chain_candidates` 取的是 `representative_companies` —— **拆解阶段 LLM 自报的公司列表**。虽然过了两道校验（`decomposer._market_code_allowed` 的代码形态校验、`fetch_us_quotes` 的美股行情校验），但**"这个环节有哪些公司"这个事实本身仍是 LLM 提供、未经外部数据源核对的**。而同一环节在 `industry_concentration` 里明明已经有 akshare 板块成分股名单（真实上市公司全名单）—— **两条数据源都拿到了同一环节的公司名单，却没有交叉。**

### C-6【P1】CR3/HHI 只覆盖 A 股，美股无锚；且 A 股用市值份额代理市占率

`industry_concentration.py:8` 明写「仅 A 股可用；美股无等价免费数据源」。于是：

- **A 股**：`cr3_source = "akshare"`（真实板块成分股市值计算）
- **美股**：`cr3_source = "llm_estimate"`（LLM 估算，**无任何锚**）

**一、美股这条腿是不对称的（prompt 本身没问题）。** `bottleneck.md:64-66` 的条件语气是**正确**的 —— 「**若**上文提供了真实数据 → 必须直接采用；**仅当没有** → 才由你估算，且必须标注『（估算，未经数据核实）』」。`real_conc_block`（`bottleneck.py:510-519`）也只在 `self.market == "a_stock"` 时才拼进 prompt。所以**不存在"prompt 自相矛盾"**。

真正的问题在**下游没有区分**：`cr3_source` 字段被算出来了（`bottleneck.py:445/462/614/620`）、也落了库，但 **`scarcity` / `pricing_power` 的 0-10 分不区分两种来源** —— 一个凭 LLM 估的 `hhi=3000` 和一个由东财成分股算出的 `hhi=3000`，在 `_check_hhi_consistency` 里受到的校准完全一样，在最终 `overall_score` 里权重也一样。**不确定性没有被传导到分数上。**（`cr3_source` 目前只有前端 `charts.js:777` 一个消费者，报告层与评分层都不看。）

**二、市值份额 ≠ 营收市占率。** `industry_concentration.py:56`：

```python
shares = [m / total * 100 for m in mcaps]  # 各家市占率%（市值份额代理）
```

高毛利、小营收公司的市值份额会远超其真实市占率，误差方向**不确定**：一个环节若由几家高估值小营收公司组成，`cr3` 被**高估** → 触发 `cr3 > 80` → scarcity **被拔高**；反之若是几家低估值大营收公司，`cr3` 被**低估** → 触发 `cr3 < 30` → scarcity **被压低**。所以不能说"系统性偏高"，只能说"用市值代理营收，误差方向随估值水平翻转，且没有任何提示告诉下游这不是真市占率"。

**三、`company_count` 与 `cr5` 已经算出来但没用于分档。** `concentration_detail`（`bottleneck.py:622-627`）里存了 `board_name` / `company_count` / `cr5` / `top_companies`，其中 `company_count` 正是 A-5 缺的那个分档依据 —— 数据在手上，规则没用。

---

## 5. 【D 类】死路径与维护债

### D-1【P1】CLI `run_screening` 未接线：不传 `chain_graph`、不传 `financial_map`

`graph.py` 三步步进函数：

```python
async def supplier_search_step(state: dict, searcher: SupplierSearcher) -> dict:
    supplier_map = await searcher.search_bottlenecks(bottlenecks)       # :63  无 chain_graph
...
async def supplier_eval_step(state: dict, evaluator: SupplierEvaluator) -> dict:
    scorecards = await evaluator.evaluate_all(supplier_map, bottlenecks) # :86  无 financial_map
```

对照生产路径（`_common.py:122/133`）**两个都传了**。

**后果：CLI `bottleneck-hunter screen` 跑的是完全降级版的流程** —— 无产业链图谱候选（少一路供应商源）、无真实财务数据（所有 `_blend` 的 `data_*` 全 None，评分退化为纯 LLM）。而 CLI 仍会生成一份看起来同样正式的报告。

**同一份代码，两条路径产出质量差一个量级，且用户无法从产物上分辨。**

附带：`bottleneck_step` 传的 `top_n=state.get("top_n", 5)`，而 `analyze(self, graph, top_n=5, ...)` 的 `top_n` 参数**在函数体内从未使用**（`analyze` 对**全部**候选节点打分，见 `:249`）。`graph.py:236` 的 `canonical_picks(scorecards, top_n=5)` 也是硬编码，`run_screening` 的 `top_n` 参数没流到这里。

### D-2【P2】`validator` 是死参数

`graph.py:127` 签名有、`:209` 构造了、`:216` 传入 `build_screening_graph`，但 `build_screening_graph` 正文**零引用**（已 grep 验证）。

`CrossValidator` 在主流程是死的（`fact_check` 节点替代了它，Node 名和 `graph.py:79` 的注释都写明了 `"""Step 5: FactCheck gate (替代原 cross_validation)."""`）。它在 `reverse_api.py:151` 和 `legacy.py:336/443` 仍是活的。

**维护债**：`graph.py:16` 的 import、`:127` 的参数、`:209` 的构造可以删；`run_screening` 的 `validation_models` 参数也是死的（构造出的 validator 没人用）。

### D-3【P1】供应商评估层无重试，异常即留 0 分且进榜

`supplier_eval.py:409-418` 是**单次调用，无重试循环**（grep 确认 `evaluate` 体内无 `for attempt` / `MAX_RETRIES`）：

```python
try:
    response = await self.llm.ainvoke([...])
    ...
except Exception:
    logger.exception(f"Failed to evaluate supplier: {supplier.name}")
    return SupplierScorecard(
        ..., market_position=0, customer_validation=0, capacity_status=0,
        financial_health=0, valuation=0, overall_score=0,
        strengths=[], weaknesses=["评估失败"],
    )
```

**关键：这个 `except` 在 `evaluate` 内部把异常吞掉并返回一张正常对象**，所以 `evaluate_batch` 看不到任何异常：

```python
results = await asyncio.gather(*tasks, return_exceptions=True)
for r in results:
    if isinstance(r, Exception):        # ← 永远不成立，异常已在 evaluate 内被吞
        logger.warning(f"Supplier evaluation failed: {r}")
        continue
    scorecards.append(r)                # ← 0 分卡照常进列表
```

`return_exceptions=True` 与这个 `isinstance` 检查在这里是**纯装饰** —— 异常从来不会传到这里。

**对比同项目的其他层**：
- `BottleneckAnalyzer._analyze_node` 有 `MAX_RETRIES = 2` 的重试循环（`bottleneck.py:563-599`），且**失败返回 `None`**（`:646`），`analyze` 里 `if r is not None: reports.append(r)` 把它排除在外
- `BottleneckAnalyzer.retry_failed_nodes` 提供批次级补跑
- `SupplierSearcher._llm_recommend` 也有重试循环（`supplier_search.py:699-733`）

**唯独评估层没有重试，且失败产物会进榜。** 一次瞬时网络抖动，这家公司就带 `overall_score=0` 进榜（`quality = max(0.1, 0) = 0.1`），且**没有任何机制发现或补跑**。它还会让 `stats.after_eval` 的观感失真（用户看到"N 家候选"但其中若干是 0 分）。

**这一层与瓶颈层的处理原则相反**，而瓶颈层的做法（失败返回 None → 排除）才是对的。

### D-4【P2】`CAP_MAINBIZ` 的注释指向已废弃的消费者

`financial_data.py:624-627`：

```python
from bottleneck_hunter.data_provider.hub import CAP_MAINBIZ, get_hub
mb = await get_hub().fetch(CAP_MAINBIZ, tk, market, user_id)
if mb:
    base.main_business = mb
```

同一段逻辑在 `supplier_eval.py:243` 的注释是：

```python
# 主营构成（仅 A股 Gangtise）：供交叉验证判断供应商营收是否真来自瓶颈环节
```

**核实结论：这个抓取是活的** —— `_format_financial_block`（`supplier_eval.py:244-256`）把它渲染进供应商评估 prompt。所以不是死代码。

**但注释指向的消费者（`cross_validation.py`）已经废弃。** 一条会误导后续维护者的注释：读者会以为 `main_business` 服务于交叉验证，实际上它服务于供应商评估 prompt —— 而**"供应商营收是否真来自瓶颈环节"这个判断，在 FactCheck 里并没有对应的规则**（`_CLAIM_RULES` 12 条规则中没有一条涉及主营构成）。

**即：数据抓了、进了 prompt、但没有任何确定性核查消费它。** 这是 C-2「无锚维度」的一个具体实例。

---

## 6. 提升方案

> 排序原则：**先修"会产出错误结论"的，再修"口径失真"的，最后补覆盖**。
> 所有 P0/P1 项的修法都不大 —— 没有一项需要重构架构。

**发现 → 措施 对照**（23 项发现全覆盖，可逐条核对）：

| 发现 | 措施 | | 发现 | 措施 |
|---|---|---|---|---|
| A-1 | P0-1 | | B-5 | P1-9 |
| A-2 | P0-2 | | B-6 | P1-11 |
| A-3 | P0-3 | | C-1 | P2-1 |
| A-4 | P0-4 | | C-2 | P2-2 / P2-4 |
| A-5 | P2-5 / P2-9 | | C-3 | P1-5 |
| A-6 | P1-1 | | C-4 | P1-6 |
| A-7 | P1-3 | | C-5 | P2-3 / P2-8 |
| B-1 | P1-10 | | C-6 | P1-12 / P2-9 |
| B-2 | P1-2 | | D-1 | P1-7 |
| B-3 | P1-4 | | D-2 | P2-7 |
| B-4 | P2-6 | | D-3 | P1-8 |
| | | | D-4 | P2-4 / P2-7 |

措施数（25）多于发现数（23），因为 A-5/C-5/C-6 各拆成两条独立措施；反向地，C-2 的两条修法都落在 P2 段（见下）。

### P0 — 正确性（会直接产出错误结论）

| # | 措施 | 落点 | 规模 |
|---|---|---|---|
| **P0-1** | 美股 `debt_ratio_pct` 换算为真资产负债率：`D/A = D/E ÷ (1 + D/E)`（Yahoo 的 `debtToEquity` 是百分数，先 /100）。同时给 `FinancialSnapshot` 加字段级校验，A股/美股两条路径都断言值域 0-100 | `financial_data.py:454` | ~5 行 |
| **P0-2** | 美股 `cashflow_per_share` 除以股本：`operatingCashflow / (marketCap / price)` 或用 `info["sharesOutstanding"]`。取不到就**置 None**，不要存总额 | `financial_data.py:455` | ~8 行 |
| **P0-3** | 修正 `_CLAIM_RULES` 四条规则的 `expected_dir` 符号，与 `_judge_direction` 对齐（负债率→`positive`、低PE→`positive`、高估→`negative`、做空→`neutral`）。**并在 `fact_check.py` 的 `demo()` 里补一条"健康公司不应触发 REVIEW"的断言** —— 现有 demo 只测了"硬矛盾→REJECT"和"无数据→不误杀"，恰好漏了这个方向 | `fact_check.py:33,36,37,46` | 4 行 + 1 断言 |
| **P0-4** | `market_share` 规则改为引用真实数据：用 `bottleneck_report.cr3_estimate` + `cr3_source`，或在拿不到真实 CR3 时**丢弃该规则**（而不是拿 LLM 评分自证）。**原则：宁可少一条规则，不要一条假规则** | `fact_check.py` `_get_data_value` | ~6 行 |

### P1 — 口径（分数算出来了，但含义不对）

| # | 措施 | 落点 | 规模 |
|---|---|---|---|
| **P1-1** | **决定 z-score 与 HHI 校准的先后。** 二选一：(a) 把 `_check_hhi_consistency` 移到 `normalize_scores` **之后**，让真实数据做最终裁决；(b) 保留现顺序，但删掉 `_check_hhi_consistency` 与 `hhi_adjustments`，并在 prompt 里不再声称"直接采用"。**推荐 (a)** —— 真实数据的绝对锚定正是 z-score 做不到的事，两者是互补而非竞争。若选 (a)，需在 `normalize_scores` 里**跳过有真实锚点的维度**（否则同一次分析内两类维度尺度不一致） | `bottleneck.py:294/627` | ~30 行，含分支 |
| **P1-2** | `_YF_DEGRADED` 那套"区分取到/没取到"的原则贯彻到 `AlphaScorer`：`_tier_score` 的 `None` 不再返回 5.0，而是返回 `None` 并**从加权中剔除该维度、重新归一化权重**；若全部维度缺失，`market_attention` 置 `None`，`alpha` 标记为"数据不足"而非给中性分。`score_all` 的 `bottleneck_map.get(..., 5.0)` 同样处理 | `supplier_eval.py:678-685,751-778,873` | ~40 行 |
| **P1-3** | `_blend` 的两条死回退（`supplier.revenue_growth` / `supplier.gross_margin` 全仓无写入点）**删除或补齐**，二选一：(a) 删掉回退分支，让 `data_*` 的语义明确为"仅真 `FinancialSnapshot`"；(b) 在检索阶段真的去填这两个字段。**推荐 (a)** —— 现在这种"写了但从不触发"的状态会误导后续维护者以为有回退层。同时给 `data_fh`/`data_val` 为 None 的情形加一个降级标记（对齐 `investability_filter` 的 `"N/A"` 做法） | `supplier_eval.py:51-66,123-126` | ~15 行 |
| **P1-4** | 催化剂加分按置信度加权：`urgency` 改为按 `confidence` 加权的均值（`Σ(impact×conf)/Σconf`），或至少 `catalyst_bonus × 平均confidence/10` | `supplier_eval.py:726-732` | ~10 行 |
| **P1-5** | `search_bottlenecks` 传 `keywords`：在 `_common.py:122` 与 `graph.py:63` 调用处，用**同一个** `_extract_keywords`（把 `industry_concentration` 那份重复实现删掉，改为 import） | `supplier_search.py:1054`, `graph.py:63`, `_common.py:122` | ~10 行，删一份重复实现 |
| **P1-6** | 多源命中留痕：`SupplierInfo` 加 `sources: list[str]`，合并时追加而非丢弃首个；下游 `_validate_*_candidates` / 评估 prompt 可据此加权或至少展示 | `models.py` + `supplier_search.py:583-598` | ~15 行 |
| **P1-7** | CLI 路径接线：`supplier_search_step` 传 `state["chain"]`、`supplier_eval_step` 传 `financial_map`（须先 `fetch_batch`）。**若判定 CLI 已废弃，则应删除而非保留** —— 现状是"看起来能用，实际降级" | `graph.py:63,86` | ~15 行，或删除 |
| **P1-8** | 评估层补重试 + 失败留痕：`evaluate` 加 `MAX_RETRIES=2` 循环（对齐 `_analyze_node`）；异常返回的 0 分卡**不进列表**，改为记入 `failed_tickers` 式的汇总并上报 | `supplier_eval.py:409-418`, `:516-540` | ~20 行 |
| **P1-9** | 报告层对齐事实核查：删掉死掉的第 5 章与 cv_map 共识列，改为渲染 `credibility` / `fact_check_recommendation` / `quality_adjusted`；CR3/HHI 旁标注 `cr3_source`（真实/估算）；页脚方法论文案改为「产业链拆解 → 供应商检索 → 数据核查」 | `report.py:120-131,148-154,173,220-222` | ~40 行，净减少 |
| **P1-10** | 供应商层加批次标准化（或明确放弃）：与瓶颈层对齐，对 5 个维度做批次内 z-score；**若采纳，`supplier_eval.md` 的"强制分布要求"必须同时删除**（两者叠加会互相抵消） | `supplier_eval.py:evaluate_batch` + prompt | ~25 行 |
| **P1-11** | **补齐无锚维度的语义**（B-6）：`customer_validation` / `capacity_status` 是唯二无数据锚的维度，而它们的合计权重**随数据缺失从 26.7% 升到 40%**。修法二选一：(a) 最小改法 —— `overall` 结果附带 `data_coverage`（有锚维度权重占比），并把 `llm_only` 标注透出到 scorecard 与前端；(b) 彻底改法 —— 数据全缺时把 cv/cs 也一并降权，让"缺数据"体现为"结论不可靠"而非"更依赖 LLM"。**推荐 (a)**：(b) 会与 `investability_filter` 的"不因缺数据误杀"原则冲突 | `supplier_eval.py:430-437` | ~15 行 |
| **P1-12** | **让 `cr3_source` 影响置信度**（C-6）：`llm_estimate` 来源时，`_check_hhi_consistency` 的校准幅度减半（或改为只在 reasoning 里提示、不改分）。**理由**：LLM 自估的 HHI 与真实成分股算出的 HHI 不确定性差一个量级，现在却同权同效 | `bottleneck.py:650-710` | ~12 行 |

### P2 — 覆盖（方法论能表达的东西太少）

| # | 措施 | 落点 |
|---|---|---|
| **P2-1** | 产业链节点 schema 补"约束类"字段：`supply_structure`（单一源/多源）、`capacity_lead_time_months`、`qualification_cycle_months`、`geo_concentration`、`export_control_risk`。拆解 prompt 与 `decomposer` 的解析同步更新，`IndustryNode` 模型加对应可选字段 | `decomposer.py` + `models.py` + `prompts/decompose.md` |
| **P2-2** | **给 `supply_demand_gap`（权重最高的维度）接数据锚。** 可用：存货周转天数、在建工程/固定资产、`CAP_MAINBIZ` 分产品营收增速、产品价格趋势（`stock_zh_a_hist`）。**优先接 A 股**（数据可得），美股沿用 LLM 估算但**在 prompt 中标注"无数据锚，请保守打分"** | `bottleneck.py` 新增 `_compute_supply_demand_anchor` |
| **P2-3** | 产业链公司名单与 akshare 板块成分股**交叉核对**：同一环节两个源都有公司列表，取交集/并集并标注来源分歧。把"LLM 自报的 representative_companies" 从"事实"降级为"候选之一" | `supplier_search.py:939` + `industry_concentration.py` |
| **P2-4** | `market_business`（主营构成）接入 FactCheck：**这是解决 C-2 / D-4 的关键** —— 用分产品营收占比核实"该供应商的营收是否真来自瓶颈环节"。这是产业链专家视角下最有价值的一条事实核查，数据已在库 | `fact_check.py` 新增规则 + `_get_data_value` |
| **P2-5** | `_check_hhi_consistency` 去重：HHI 与 CR3 指向同一维度 scarcity，合并为单一判据（或让 CR3 只修正 `pricing_power`、HHI 只修正 `scarcity`），避免一次观察扣 5 分。签名补 `company_count`，按成分股数分档给阈值（或把 HHI 阈值改为随 n 调整） | `bottleneck.py:650-710` | ~30 行 |
| **P2-6** | 催化剂时间结构化：`expected_date` 改为 `{year, quarter}` 或 ISO 日期 + `CatalystTimeline` 加 validator；prompt 里的 `2025Q3` 示例改为相对表述（「示例：从当前日期起 2 个季度内」）；补上 prompt 的"数值仅为格式参考"防锚定声明（对齐 `bottleneck.py:541`） | `catalyst.py:105-110` + `prompts/catalyst.md` |
| **P2-7** | 清理死路径：删 `graph.py` 的 `CrossValidator` import / `validator` 参数 / `validation_models` 参数；删 `report.py` 死章节（并入 P1-9）；删 `industry_concentration._extract_keywords` 重复实现（并入 P1-5）；修 `financial_data.py:624` 的过时注释 | 多处，净减少 |
| **P2-8** | **图谱候选扩到同层竞争 + 2 跳上游**（C-5）：`target_nodes` 现只含本节点与直接上游；且候选来源仍是 LLM 自报的 `representative_companies`。把它与 `industry_concentration` 已拿到的**真实板块成分股名单做交叉核对**，两者都有的公司升权、只在一边的标注来源分歧 | `supplier_search.py:957-962,939` | ~30 行 |
| **P2-9** | **`compute_concentration` 改用 `company_count` 分档 + 补营收口径**（A-5 / C-6）：HHI 阈值随成分股数调整（窄板块不因"板块窄"触发高集中）；能拿到分产品营收时优先用营收份额而非市值份额。**数据已在 `concentration_detail` 里，属于"规则没用手上的数"** | `bottleneck.py:650-710` + `industry_concentration.py:56` | ~30 行 |

---

## 7. 建议的执行顺序

**第一批（P0，四项）** —— 一次提交可完成。这四项都是"数字/符号写错"，改动合计约 25 行。做完后**立即用真实美股数据验证 A-1/A-2**（取一家已知资产负债率的美股公司，比对 Yahoo 与财报），因为这是唯一需要外部数据对账的项。

**第二批（P1-1 / P1-3 / P1-2）** —— 口径三项，改动集中在 `bottleneck.py` 与 `supplier_eval.py`。P1-1 是其中唯一有设计取舍的（z-score 与真锚谁最终裁决），需要先定方案再动手。

**第三批（P1-4 ~ P1-8）** —— 分散的修补，彼此独立，可并行。

**第四批（P1-9 / P1-10 / P2-*）** —— 报告层重写与覆盖补充。P1-10 需与 prompt 同步改，否则会互相抵消。

**建议先做前三批。** P2 的覆盖补充（尤其 P2-1 节点 schema、P2-2 供需缺口锚点）涉及 prompt 与模型的联动改动，且会改变现有分析的输出分布，**建议在 P0/P1 稳定后再单独立项**。

---

## 附录 A：本次审查的方法与自证

**方法**：逐模块通读源码 + 逐一 grep 核对调用点。**不采信任何既有审计转述**（用户明确要求）。所有行号在交付前用 `grep -n` / `sed -n` 复核过一遍。

**关键结论的自证方式**：

| 结论 | 自证手段 |
|---|---|
| A-1/A-2 字段语义 | 读 `financial_data.py:454-455` 的赋值语句，对照 A 股路径 `:321-326` 的 `资产负债率` / `每股经营现金流` 列名来源 |
| A-3 方向写反 | 并列 `_CLAIM_RULES`（`:27-47`）的 `expected_dir` 与 `_judge_direction`（`:311-394`）的 return 值，逐条比对 |
| A-4 循环自证 | 读 `_get_data_value:284-286` 的 `market_share` 分支，确认返回的是 `scorecard.market_position` |
| A-6 校准被覆盖 | 确认 `_check_hhi_consistency`（`bottleneck.py:627`，在 `_analyze_node` 内）与 `normalize_scores`（`:294`，在 `analyze` 内）操作的是**同一批 `BottleneckScore` 对象**，且后者在后、并紧接着 `:295-296` 重算 `overall_score`；再读 `normalize_scores:145` 的 `len(dim_scores) < 3` 跳过条件得出适用边界 |
| A-7 回退链是死的 | 全仓 `grep -rn "revenue_growth\|gross_margin="` 对照四条 `SupplierInfo` 构造点（`:371/:582/:790/:864`），确认无一传这两个字段；唯一写入者是 `reverse.py:379-381` |
| B-1 批次校准是建议 | 读 `_compute_batch_context:486-513` 与 prompt 段 `:340-353`，确认只把范围写进提示词、无事后校验 |
| B-2 5.0 混淆 | 手算数据全缺时的 `raw = 5×0.20 + 5×0.267 + 5×0.333 + 5×0.20 = 5.0`；并读 `:873` 的 `bottleneck_map.get(..., 5.0)` |
| B-6 权重随缺失上升 | 手算 `total_weight` 在数据齐全(7.5)与全缺(5.0)两种情形下的 cv+cs 占比 |
| C-3 keywords 从不传入 | 全仓 grep `search_bottlenecks` 的全部调用点（`_common.py:122`、`graph.py:64`、`:1071`），确认无一处传 `keywords` |
| D-1 CLI 未接线 | 读 `graph.py:64` / `:88`，对照 `_common.py:122` / `:133` |
| D-2 `validator` 死参数 | grep `validator` 在 `graph.py:122-165`（`build_screening_graph` 正文），零命中 |
| D-3 异常被内吞 | 读 `evaluate:468` 的 `except` 返回 0 分卡 + `evaluate_batch:533-537` 的 `isinstance(r, Exception)` 检查，确认后者**永不成立** |
| 生产路径是活的 | `index.html:2563` → `app.js` 的 import 列表 → 逐层确认 `phases.js` 可达；`charts.js:777` 确认 `cr3_source` 有前端消费 |

**本次审查自己推翻的四处结论** —— 全部记录在此，因为它们正好说明"读一半代码得出的结论可以是错的"：

1. **"FactCheck 结果算完只打了日志、没到前端"** —— **错**。审查初期读到 `phases.py:684` 就下了结论；实际 `:838` 发了 `step_done` 事件、`:850` 落了库、`:815` 的 `passed_top` 让 REJECT 参与截断。**这条链路是通的，且是干净的。**

2. **"`hhi > 2500` 分支结构性不可达"** —— **错**。我根据 `industry_concentration.py:140` 的 `top_companies[:5]` 推出 `hhi ≤ 2000` —— **但那个 `[:5]` 只用于展示**，HHI 用的是 `:56` 的**全样本** `shares`。误把展示逻辑当成了计算逻辑。修正后 A-5 从"不可达分支"改为"CR3/HHI 复合计分 + 不看 `company_count`"。

3. **"`_blend` 被 LLM 自报数字击穿"** —— **错**。链条本身成立（`_data_financial_health:51-66` 确实回退到 `supplier.revenue_growth`），但**这个回退从不触发**：全仓 grep 显示四条 `SupplierInfo` 构造路径没有一条填这两个字段。误把"写了的分支"当成了"会走的分支"。修正后 A-7 从"数据权威被冒充"改为"回退链是死代码 + 缺失时无降级痕迹"。

4. **"`bottleneck.md` 对美股下了自相矛盾的强指令"** —— **错**。我只看了 `bottleneck.md:66` 的「不得另行估算」这一行，就断定它对美股也生效。实际 `bottleneck.py:510-519` 的 `real_conc_block`（含那句 `⚠ 请【直接采用】…` ）只在 `self.market == "a_stock"` 时拼进 prompt，而 `bottleneck.md:64-66` 本身写的正是条件语气（「**若**提供了…**仅当没有**…」）—— **prompt 是对的**。误把"文件里的一句强指令"当成了"对所有市场生效的指令"。修正后 C-6 从"prompt 自相矛盾"改为"美股无锚 + `cr3_source` 不影响置信度"，并顺带纠正了市值代理误差方向的断言（不是"系统性偏高"，而是随估值水平翻转）。

**方法论教训**：四次误判有一个共同形状 —— **看到一段代码，就假定它在运行 / 看到一句话，就假定它对所有输入生效**。动态语言里"能编译"与"会被执行"是两件事；判定一个分支是否活着，必须查它的**写入端**；判定一句 prompt 是否生效，必须查它的**装配点**。三者的共同解药是同一个动作：**去查它上游谁提供了输入，而不是只看它自己写了什么。**

## 附录 B：本次审查**未**覆盖的部分

以下模块与本次审查的两个视角（产业链专家/金融分析师）关系较远，或属于决策中心范畴，**未纳入**：

- `roundtable.py` / `meeting.py` / `meeting_data.py`（投委会，属决策中心）
- `hot_sector.py`（全市场热点扫描，非三步法主线）
- `reverse.py` / `reverse_api.py`（反向分析，独立功能）
- `chain_store.py` / `evidence.py` / `json_utils.py` / `fetch_budget.py` / `quotes_cache.py`（基础设施）
- `legacy.py`（已确认前端不可达，但作为**活着的 HTTP 端点**仍有其自身的审计价值）
- 前端 `pipeline.js` / `panel.js` / `dashboard.js` / `history.js` 孤岛簇（确认不可达后未深入）
