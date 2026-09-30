# 供应链分析整改 开发日志（2026-09）

配套方案：[SUPPLY_CHAIN_REVIEW_2026-09-27.md](SUPPLY_CHAIN_REVIEW_2026-09-27.md)。该审查提出 23 项发现（4 P0 / 13 P1 / 6 P2），本日志按 `§7 执行顺序` 分四批落地，每批完成后**全量 pytest + 代码审核 + 记录本文档**，再进入下一批。

**贯穿原则：审查报告不是事实，是线索。** 每一条在动手前都先回代码里复核逻辑，被实测推翻的建议**不实施**并留证（见「审查被推翻项」）。根因是历史遗留的**"假闭环"**：流程看起来跑完了，但每一步的输出没有真的被下一步消费——所以本轮的判定标准不是"代码改了"，而是"数据真的流过去了"。

## 分期交付与门禁

| 批次 | 内容 | 涉及文件 | 全量 pytest | 状态 |
|---|---|---|---|---|
| Batch 1 | P0-1 ~ P0-4（口径污染 + 假规则） | `financial_data.py` / `models.py` / `fact_check.py` | 2408 passed, 6 skipped | ✅ 已落地 |
| Batch 2 | P1-1 / P1-2 / P1-3（评分尺度口径） | `bottleneck.py` / `supplier_eval.py` / `models.py` / `api.py` / `roundtable.py` / `reverse.py` | 2412 passed, 6 skipped | ✅ 已落地 |
| Batch 3 | P1-4 ~ P1-8（分散修补） | `supplier_eval.py` / `supplier_search.py` / `graph.py` / `models.py` / `reverse.py` | 2428 passed, 6 skipped | ✅ 已落地 |
| Batch 4 | P1-9 / P1-10 / P1-11 / P1-12 + P2-1 ~ P2-9（报告层与覆盖项） | `report.py` / `cli.py` / `graph.py` / `supplier_eval.py` / `models.py` | 2431 passed, 6 skipped | 🔶 部分落地（见下） |

> **Batch 4 的实际收口与审查预期不同，须先读这一句。** 审查 §7 自己写明：「建议先做前三批。P2 的覆盖补充（尤其 P2-1 节点 schema、P2-2 供需缺口锚点）涉及 prompt 与模型的联动改动，**且会改变现有分析的输出分布**，建议在 P0/P1 稳定后再单独立项。」本批据此**只落地不改变输出分布的部分**，其余逐条留下复核结论与不实施理由。P1-11 / P1-12 与 Batch 2 改动同文件、同期落地，但**此前三批的正文与验证记录均未提及**（`git show HEAD:` 对两者的实现均返回 0，属工作区新增），故本批**补记**并纳入本批门禁的覆盖范围。

---

## Batch 1 — P0：口径污染与假规则

四条都是**静默污染**：没有任何报错，没有任何日志，错的值一路流到评分、排序、事实核查，最后以"看起来合理的数字"呈现给人。这是最难发现、也最值得先修的一类。

### P0-1 美股 `debt_ratio_pct` 是 D/E 不是 D/A

`financial_data.py:_fetch_us_financial` 直接把 Yahoo 的 `info["debtToEquity"]` 存进 `debt_ratio_pct`。Yahoo 这个字段的名字是诚实的——它确实返回 **D/E**，且是**百分数形式**（AAPL 实测 78.445 = D/E 0.784，KO 115.519 = D/E 1.155）。而下游一律按**资产负债率 D/A** 解读：字段名是 `debt_ratio_pct`、模型 description 写「资产负债率(%)」、A 股路径取的是同花顺「资产负债率」列。**两条路径口径不同，却在同一个字段名底下比较和打分。**

后果是美股杠杆被系统性高估：D/E 1.155 的 KO 会被读成"资产负债率 115.5%"。

修法（新增 `_de_to_da_pct` 助手）：

```
D/A = (D/E) / (1 + D/E)      令 p 为百分数形式 → D/A% = 100p / (100 + p)
```

D/E = −100% 时公式发散，返回 `None`（对应资不抵债的极端值，本就该丢）。

配套在 `models.py:FinancialSnapshot` 加 `debt_ratio_pct` **值域守卫 0-100**：越界即丢弃并告警。守卫的意义不在于防这次，而在于**堵死下一次**——任何上游误塞 D/E 都会被拦在模型边界，而不是流进评分。

**真实数据验证**（审查 §7 对 A-1/A-2 的要求）：

| 标的 | Yahoo D/E | 换算 D/A | 合理性 |
|---|---|---|---|
| AAPL | 78.445 | 43.96% | ✅ 与已知资产负债表相符 |
| XOM | 15.921 | 13.73% | ✅ |
| JNJ | 57.709 | 36.59% | ✅ |
| KO | 115.519 | 53.60% | ✅ 与已知资产负债表相符 |
| NVDA | 16.971 | 14.51% | ✅ |

### P0-2 美股 `cashflow_per_share` 存的是现金流**总额**

`info["operatingCashflow"]` 是美元总额（AAPL 约 1.08e11），却被直接塞进语义为**每股**的 `cashflow_per_share`。下游的 FactCheck 规则 `现金流充裕 → cashflow_per_share > 0` 恰好永远成立——**一条永远为真的检查，等于没有检查**。

改为 `operatingCashflow / sharesOutstanding`，股本取不到就**置 None**（`unverifiable`，不罚分），绝不存总额。同样在 `models.py` 加量级守卫：`abs(v) > 1e6` 即丢弃并告警。

AAPL 验证：`10.0536` /股，量级正确。

### P0-3 四条事实核查规则的方向写反了

`_CLAIM_RULES` 的 `expected_dir` 与 `_judge_direction` 的返回语义**系统性相反**：

| 规则 | 原值 | 改为 | 理由 |
|---|---|---|---|
| `负债率?低\|健康\|可控` | `negative` | `positive` | `_judge_direction` 对 `debt_ratio_pct < 50` 返回 `positive`（低负债=好） |
| `估值\|PE 低\|便宜\|被低估\|合理` | `negative` | `positive` | 对 `consensus_pe < 20` 返回 `positive`（低 PE=便宜） |
| `高估\|贵\|泡沫` | `positive` | `negative` | 对 `> 40` 返回 `negative`（高 PE=贵） |
| `做空\|沽空 压力\|风险` | 保持 `positive` | — | **审查建议改 `neutral`，但 `_judge_direction` 就是返回 `positive`，改了反而错位。不动。** |

方向写反的后果是**反向激励**：一家诚实的公司说"我们负债率低、估值便宜"，会被记 3 条 mismatch → 触发 `REVIEW`；而一家吹牛的公司说"我们财务稳健"反而可能通过。这就是所谓"说真话受罚"。

补 `demo()` **Case 4 回归哨兵**：健康公司（负债率 30 / PE 18 / 正现金流）说真话，断言 `PASS` 且**零 mismatch**。原 demo 只覆盖了"硬矛盾→REJECT"和"无数据→不误杀"，恰好漏掉了这个方向——这正是它长期没被发现的原因。

### P0-4 `market_share` 规则拿 LLM 自己打的分自证

`_get_data_value` 的 `market_share` 分支原本 `return scorecard.market_position, "market_position"`——**用 LLM 打的"市场地位 8 分"去核实 LLM 自己写的"行业龙头"**。这是自洽性检查，不是事实核查；它只能测出 LLM 前后矛盾，永远测不出 LLM 说错话。

改为只引用**真实** CR3：`cr3_source == "akshare"`（东财板块成分股实算）才作数，`llm_estimate` 一律视为无数据 → `unverifiable`。**原则：宁可少一条规则，不要一条假规则。** 同时删掉 `_judge_direction` 里随之失效的 `market_position` 分支。

### 计划外但同源的一处修复：同字段重复计分

改 P0-4 时引入了一个新碰撞：`market_share` 现在解析到 `cr3_estimate`，与 `cr3_estimate` 规则落在**同一个真实字段**上。回查发现这是**既有模式**——`financial_health` 与 `gross_margin_trend` 两条规则共用毛利趋势，同一次观察被计两次分。

在 `check_scorecard` 加去重集合：同一 `actual_field` 只裁决一次，后续命中记 `verdict="duplicate_skipped"`（不参与计分，但保留在 findings 里可追溯）。这一并修掉了既有缺陷，不只是补我引入的。

> ⚠️ **这个键当时写窄了，审核时实测抓出并已改正——见文末「去重键写窄了」。** 只按字段去重会把 `consensus_pe` 上方向相反的两条独立主张吞掉一条。正确键是 `(actual_field, expected_dir)`。

`verdict` 的新取值 `duplicate_skipped` 已 grep 全仓确认**无外部消费方**（`chain/fact_check.py` 之外无读取；`vip/fact_check.py` 是另一套独立词表「✓认证/⚠纠正/？未核」，互不相干）。

## 审查被推翻项（不实施，留证）

**A-1 建议的 `if v < 10: v *= 100` 兜底守卫 —— 拒绝实施。**

审查报告在 §2 A-1 的「验证建议（实施前必做）」里提出：若发现某些 ticker 的 `debtToEquity` 返回小数形式（如 0.78），应加 `if v < 10: v *= 100` 换算。

实取 15 个美股 ticker 探针，**全部是百分数形式**，其中 **ASML = 9.092** —— 这是一个**真实且合理**的低杠杆（D/E 0.0909，对应 D/A ≈ 8.3%）。该守卫会把 ASML 抬成 D/E 9.09 → D/A 90.1%，把一家近乎无负债的公司变成高危高杠杆。

15/15 样本皆为百分数形式，这个启发式是**净负面**：它防的是一个从未观测到的情形，代价是把真实的低杠杆公司打错。**不加。** 真正需要的是 P0-1 已做的值域守卫 + `None` 语义化，而不是猜测性换算。

**P1-5 建议的"在上游调用点传 keywords" —— 无法实施，且删重复实现不是免费交换。**

审查要求在 `_common.py:122` 与 `graph.py:63` 两处传入 `keywords`。grep 全仓：**没有任何上游持有关键词来源**（`web/static/js/` grep `keywords` 返回为空，`model_tester.py` 的命中无关），这两个调用点本来就是内部自行派生。**没有词可传。** 真正可证的缺陷在 `search()` 内部两个源派生不一致（见 Batch 3 正文），已在根因处修掉。

该条的第二半（"删掉 `industry_concentration._extract_keywords`，改为 import 另一个"）**未实施**：两份实现**不相等**——`industry_concentration` 版只按 `[/、及和与]` 切分、无 12 字上限；`SupplierSearcher` 版按更宽的字符类切分、带 `2..12` 长度约束，且有自己的回归文件。合并会**静默改变其中一方的切词行为**。是否统一留待 Batch 4 的 P2-7 连同去重一起评估，本轮不顺手合并。

**P1-7 被判为"死接线，应删除" —— 实测两条链路都是活的。**

`cli.py:18` 导入、`cli.py:295` 调用 `run_screening`，CLI 是真实入口；`fetch_batch` 在 Web 侧（`phases.py:508`）也在用。缺陷不是"死代码"，而是**两条链路各自为政**：Web 自己取财务、CLI 从不取数，导致同一评分模型在 CLI 下系统少一层数据锚。**接线，不删除。**

**P1-9 被判为"报告层第 5 章是死的" —— 只对了一半，且修法方向要改。**

审查说 `result.cross_validations` 恒为 `[]`，故第 5 章与「共识」列是死的。复核**生产者**（这是判定分支死活的唯一办法）得到三条事实：

1. `legacy.py:337` 会真实填充它：`CrossValidator(validation_models=vm).validate_all(scorecards)`，且 `legacy.py:36 _stream_screening` 是一个**活着的 HTTP 端点**（`api.py:98`，前端 `panel.js` 的 `cv-toggle` 会传 `enable_cross_validation`）。
2. 审查在附录里说 `legacy.py`「已确认前端不可达」——**这一半是对的**：`index.html:2563` 只加载 `app.js`，而 `app.js:7` 导入的是 `phases.js`；`pipeline.js`/`panel.js`/`dashboard.js`/`history.js` 构成一个只互相引用的闭环（`pipeline.js` 只被 `panel.js` 引用，`panel.js` 只被该闭环引用），全仓无 `import()` 动态入口。`legacy.py` 因此是**无 UI 入口的 HTTP 端点**。
3. 真正断掉的是 **CLI 那一段**：`cli.py:261-284` 问用户「启用多模型交叉验证？」、收下模型列表、打印「交叉验证: N 个模型」，传进 `run_screening` 后——`graph.py:218` 构造了 `validator`、`graph.py:220` 传给了 `build_screening_graph`，而该函数体内(`:136` 形参 / `:155-172` 正文)**从未引用 `validator`**。用户看到的那行配置是空头支票，`cross_validations` 永远是 `[]`。

所以审查「删掉第 5 章」的建议**不实施**：第 5 章的渲染代码是好的，问题在上游生产者。`report.py` 由 `cli.py:312`（活）与 `legacy.py:371`（活端点）调用，`tests/test_report.py:156-201` 三处直接覆盖它——删章节等于删一个被测试与活端点共同使用的渲染器，并连带毁掉 `legacy.py` 的产物。**接线，不删除**（与 P1-7 同型判断）。

**但 CLI 的模型→评估器接线不实施**，三条理由逐条可查：
- `run_screening` **只在 CLI 一处被调用**（全仓 grep），接线后不存在第二个受益方；
- 该路径**从未在生产验证过**（生产是 Web/phases 四阶段线，`report.py` 在那条线上无调用点），接一个没跑过的 4 模型并发评估进 CLI，属于给未知链路加负载；
- `CrossValidator.validate_all` 的成本是 `n_suppliers × n_models` 次 LLM 调用（`validation_models` 问答默认给的是**三个**模型），而 CLI 默认 `max_suppliers=20`。

修法改为：删掉那份空头承诺（`cli.py` 的 CV 问答与提示行），让 CLI 走 Web 已经在用的事实核查闸（`fact_check_step`），并**在页脚如实标注**。这同时兑现了 P2-7 的意图（清死参数）而不删活代码。

**P2-7 的其余三项同样按"生产者判定"逐个复核**：

| 审查主张 | 复核结果 |
|---|---|
| 删 `graph.py` 的 `CrossValidator` import / `validator` 参数 / `validation_models` 参数 | **实施**——`validator` 在 `build_screening_graph` 体内零引用，`run_screening` 的唯一调用方是 CLI，删参数不影响 `legacy.py`（它自己 `new CrossValidator`） |
| 删 `report.py` 死章节（并入 P1-9） | **不实施**——见上，第 5 章的上游生产者是活的 |
| 删 `industry_concentration._extract_keywords` 重复实现（并入 P1-5） | **不实施**——Batch 3 已记为"两份实现不等价"，合并会静默改变切词行为 |
| 修 `financial_data.py:624` 的过时注释 | **不实施**——审查给的行号 `:624` 落在 `fetch_financial_snapshot` 的 docstring 上，无任何"过时"表述；docstring 里"免费直连做基线，再用 DataHub 多源覆盖"与紧跟的 A 股/美股分支实现一致。**行号指向的文本不存在审查描述的问题** |

## 遗留待办（未擅自扩大范围）

**同源的 D/E 混用还在 FMP 路径上。** 审查未提，复核时发现：

- `data_provider/providers.py:178` 把 FMP 的 `debtToEquityRatio` `× 100` 后存进 `debt_ratio_pct`
- `watchlist/price_pipeline.py:289` 又把它重标为 `debt_to_equity_pct`（**名字改对了，值是 D/E，但同一条链路两处语义打架**）
- 消费方 `macro_consultation.py:486` 把整包 JSON 塞进 LLM prompt

对照 `price_pipeline.py:375` 的 baostock 路径 `1 - 权益/资产` 是**正确**的 D/A——**同一个代码库里两种理解并存**。倾向一并统一为 D/A，但涉及字段名变更（下游 label），**留待用户确认**再动。

## Batch 1 验证记录

```
python -m bottleneck_hunter.chain.fact_check      → demo 4/4 通过（含新增 Case 4 哨兵）
pytest tests/test_factcheck_integration.py tests/test_models.py tests/test_us_deep_financials.py -q
                                                  → 25 passed
pytest -q（全量）                                  → 2408 passed, 6 skipped（约 378s）
ruff check（三个改动文件）                          → 7 errors，与 HEAD 基线**逐条一致**，零新增
```

**skip 数由 5 变 6 的排查**：非回归。`tests/test_daily_turnover_gate.py:223` 在北京时间早于 07:30 时 `pytest.skip`（该用例的构造前提不成立）。运行时刻为北京时间 01:35，跳过条件成立。用例总数 2414 不变。

---

## Batch 2 — P1：评分尺度口径

三条的共同点是**尺度不可比**：分数看起来都在 0-10 里，但不同来路的分数不是同一把尺子。

### P1-1 真实集中度数据被 z-score 抹平，且校准跑在覆盖之前

执行顺序错误：`_analyze_with_verification` 里先算 `adjustments`、再算 `overall`，**然后才**用真实 `real_conc` 覆盖 `merged_cr3/merged_hhi`。也就是说辛苦从东财板块成分股取到的真实集中度，在一致性校准里**根本没参与** —— 校准用的是各子模型 LLM 估算值的中位数。

更严重的是 `normalize_scores` 紧随其后做 z-score，在有 ≥3 个环节时会把 `scarcity` / `pricing_power` 整批重写。这两个维度恰恰是 HHI/CR3 校准的作用对象，而真实 CR3 的**绝对锚定**正是 z-score 做不到的事。结果是报告里那句「[校准: 6→4]」与实际分数**文本与数值背离**——比不校准更误导。

修法两处：

1. `normalize_scores(reports, skip_dims)` 新增整维跳过；`_anchored_dims()` 在批次内存在 `cr3_source == "akshare"` 时返回 `{"scarcity", "pricing_power"}`。**整维跳过而非按节点跳过**，否则同一次分析内的尺度不一致。
2. `_check_hhi_consistency` 的调用移到真实值覆盖**之后**。

### P1-2 `_tier_score(None) → 5.0` 把"没数据"和"恰好平庸"混为一谈

这是本轮最核心的一条，**击穿了 AlphaScorer 的立意**。`alpha = 瓶颈重要性 × (1 − 关注度/10)` 只在关注度是**真实测得**时才成立。旧代码里：

- `_tier_score(None, tiers)` 返回 `5.0`；
- 调用点更直接：`s_analyst = 5.0` / `s_vol = 5.0` / `s_price = 5.0` / `s_inst = 5.0` 先赋中性值再条件覆盖；
- 于是五个维度全缺时 `raw = 5×0.20 + 5×0.267 + 5×0.333 + 5×0.20 = 5.0`，与一家**确实被适度关注**的公司**完全同分**。

而与它同仓库的 `investability_filter` 在缺数据时是跳过（`N/A`）、`_YF_DEGRADED`/`_YF_ATTEMPTED` 也区分"取过没取到"和"没取"。**AlphaScorer 是唯一的例外。**

修法（按审查 §line 723 的方案，逐条落地）：

- `AlphaScore.market_attention` / `information_gap` / `alpha_score` / `dim_*` 全部改为 `float | None`，默认 `None`（不是 `0.0`，也不是 `5.0`）。
- `_tier_score` 的 `None` 返回 `None`；`DIM_WEIGHTS = {cap .15, analyst .20, vol .25, price .15, inst .25}`，缺失维度**从加权中剔除并重新归一化**。
- 全维缺失 → `market_attention = None`，`alpha = None`（**不是中性分**）。
- 瓶颈分查不到 → `score_all` 不再 `bottleneck_map.get(..., 5.0)`，改为 `None` 并把 alpha 标为数据不足（关注度有了也算不出 alpha）。
- `FinalScorer.compute` 里 `max(0.1, None)` 会 `TypeError`，改为显式取 0.1 地板 —— 与"alpha 缺失"的既有回退一致，保证 `final_score` 仍是保守值（不把"不知道"当成"很好"）。

**A 股结果与改前逐字节一致**：剔除 `inst`（0.25）后其余四维按比例摊回恰好是旧的硬编码 `0.20 / 0.267 / 0.333 / 0.20`。测试用 `DIM_WEIGHTS` 自洽复算而非写死金标，权重被改动时才会失败。

**下游 None 守卫**（同一个 `None` 语义要一路守到底，否则只是把静默错分换成崩溃）：

| 位置 | 原行为 | 修法 |
|---|---|---|
| `api.py:425` `sc.alpha.alpha_score >= 7` | `None >= 7` → **TypeError** | 补 `is not None` |
| `api.py:864` `(sc.get("alpha") or {}).get("alpha_score", 0) >= 7` | 历史 JSON 里键存在且为 `null` 时 `.get` 的默认值不生效 → TypeError | `((...) or 0) >= 7` |
| `api.py:1053` `alpha_val = ... if a else 0`，`:1077` `{alpha_val:.1f}` | `None:.1f` → 崩溃 | `None` + 渲染「数据不足」 |
| `roundtable.py:215` `{sc.alpha.alpha_score:.1f}` | 同上 | 同上 |
| `reverse.py:461` 落库 `alpha_score=` | 会把 `NULL` 写进 `REAL DEFAULT 0` 的排序缓存列 | 按列既有约定落 `0.0`（真值在 `result_json`） |
| `drawer.js:211` `${a.toFixed(1)}` | `undefined.toFixed` → 崩溃 | 渲染「数据不足」 |
| `dashboard.js` `scoreClass(null)` | `null >= 8` 为假 → 误判为 `score-low`（"很差"） | 补 `val == null` 早退 |
| `dashboard.js` 三处 `.toFixed(1)` | 同上崩溃 | 渲染「数据不足」 |
| `watchlist.js:1177` / `phase-views.js:212,938,964` | `\|\| 0` / `?? 0` | 已安全，**不改** |

用**已存在**的 CSS 变量（`var(--muted)` 内联）而非新增 `val-na` 类 —— 全仓 grep 确认 `val-na` 从未定义过。

### P1-3 `supplier.revenue_growth` / `gross_margin` 回退是死代码

`_data_financial_health` / `_data_valuation` / `_data_market_position` 里各有一句 `if x is None: x = supplier.revenue_growth`。审计报告认为这两个字段**全仓无写入点**、应连同字段一并删除。

**复核推翻了一半**：`supplier_eval.py:299-300` 把它们读进 prompt、`reverse.py:379` 确实在写入。字段**有用**，删了会破。真正死掉的只是那三处 `is None` 回退分支（构造时即 `None`，`evaluate` 又已在前面用 snapshot 填充了局部变量，故分支永不触发）。

只删分支，**保留字段**。顺带 `_data_financial_health(snap, supplier)` 的 `supplier` 参数删掉——它现在完全没人用，留着就是给下一个人再塞死代码的接口。

## Batch 2 验证记录

```
pytest -q（全量）    → 2412 passed, 6 skipped（约 372s）
ruff check --select F（5 个改动 py 文件） → All checks passed
用例数 2414 → 2418（删 1 加 5，净增 4）：devlog 数目自洽
```

新增回归哨兵（P1-2 的判定核心，此前**没有任何用例覆盖全维缺失**）：

- `test_all_dims_missing_marks_insufficient` —— 全维缺失断言 `market_attention is None` / `alpha_score is None`
- `test_partial_dims_still_scores` —— 只有一维有数据仍应出分，不得误判为数据不足
- `test_unknown_bottleneck_marks_insufficient` —— 关注度齐备但瓶颈分未知 → alpha 仍为 None
- `test_missing_dims_renormalized_not_zeroed` / `test_a_share_drops_inst_dim` —— 权重重归一化自洽

## Batch 3 — P1：分散修补

本批五条的分布很散（催化剂加权 / 检索词统一 / 多源合并 / CLI 接线 / 评估失败处理），但**三条撞在同一处根因上**：一个值被算出来、存下来，然后没有任何消费方——或者消费方拿到的是一份**看起来合理、实则无意义**的替代品。

### P1-4 催化剂加分只看"什么时候"，不看"会不会"

`_compute_catalyst_bonus` 原来只读 `urgency_score`：

```
urgency_score / 10 * 2.0
```

但 `CatalystTimeline.urgency_score` 是**时点**语义（多快兑现），而 `confidence`（发生概率）与 `impact_score`（量级）挂在**每个 `CatalystEvent`** 上。原式等于：一个「下月可能有」但概率三成的事件，与「下月几乎必然」的事件**加同样的分**。

审查给的两个方案里，`Σ(impact×conf)/Σconf` 被否掉——那是以 confidence 为权重对 **impact** 求期望，把本项的基从"时点"换成了"量级"，超出修复范围（要改的是"打折"，不是"重定义"）。取第二个方案：

```python
conf_factor = sum(e.confidence for e in cat.events) / len(cat.events) / 10.0
return round(cat.urgency_score / 10 * 2.0 * conf_factor, 2)
```

`confidence` 的默认值是 `5.0`（不是 `None`），所以旧数据的缺失置信度按中性打折——不会把历史催化剂一次性清零。

### P1-5 同一次检索里，两个源在搜不同的词

**审查的方案在此被实测推翻（见「审查被推翻项」）。** 它要求"在上游调用点把 keywords 传进来"，但**全仓没有任何上游持有关键词来源**（`web/static/js/` grep `keywords` 为空），`_common.py` / `graph.py` 两个调用点都是内部自行派生。真正可证的缺陷在 `search()` **内部**：

- `_akshare_source` 用 `self._extract_keywords(node_name)` 得到的**短词**
- `_gangtise_source` 用 `bottleneck.node_name` 的**整个原串**（`keywords[0] if keywords else bottleneck.node_name`）

同一次检索里两路在搜不同的字符串，命中结果不可比。修法是在 `search()` 顶部派生一次两源共用：

```python
kw_terms = list(keywords) if keywords else self._extract_keywords(bottleneck.node_name)
```

### P1-6 多源合并：落败源的字段被整个丢弃

四个来源按 ticker **先到先得**合并（LLM > chain > gangtise > akshare）。命中优先级是对的，但落败源不是"只丢身份"——它的**字段全丢**。最典型的是 akshare 从东财板块成分股取到的**真实 `market_cap`**：LLM 与 chain 都不填市值，却排在前面，于是这一路真实数据静默消失。

新增 `_merge_supplier(keep, extra)`：把落败源的空缺字段**回填**进主源，同时把双方来源记进 `SupplierInfo.sources`。四条近乎复制的合并循环一并收敛成一张优先级表。

回填**只覆盖客观可测字段**（`market_cap` / `pe_ratio` / `revenue_growth` / `gross_margin` / `institution_holding_pct` / `market_share` / `name_cn` / `sector`），刻意**不含 `description`**——那是带源口吻的整段文本，回填等于让另一个源的措辞覆写。

`sources` 的一个诚实说明：它目前只被进度消息（「其中 N 家为多源交叉命中」）与本批哨兵消费，**尚未进报告或前端**。这与 P1-4 的 `confidence` 病同源，故此处明写而非假装已闭环。

### P1-7 CLI 路径少一路来源、少一层数据锚

审查说这两条是"死接线，删掉"。**实测两条链路都是活的**：

- `cli.py:18` 导入 `run_screening`、`cli.py:295` 调用它——CLI 是真实入口；
- 问题不是死代码，是**两条链路各自为政**：Web 侧在 `streaming/phases.py:508` 自己 `fetch_batch` 取财务再喂评估层；CLI 走的 `graph.py` 则**从不取数**。

后果是同一个评分模型在 CLI 下系统性地少一层数据锚（`_data_*` 系列全空 → 退回纯 LLM 口径），且链内候选（`_extract_chain_candidates`）因 `chain_graph` 恒未传而恒为空。两处补上：

- `supplier_search_step` 传 `chain_graph=state.get("chain")`
- `supplier_eval_step` 自己 `fetch_batch`（**不依赖调用方预先备好**）

**安全性复核**：CLI 路径从没 `start()` 过 `fetch_budget`，而 `fetch_budget.expired()` 在 `_started_at <= 0` 时返回 `False`——即**未启用预算时不受限**。故在 CLI 里取数是安全的，不会因缺预算而全部落空。

一处自查后回退的过度设计：我最初给 `supplier_eval_step` 加了 `financial_map: dict | None = None` 形参，好"让已取好数的调用方复用"。grep 后发现**没有任何调用方会传它**——正是我在 P1-3 里刚删掉的那种"写了但从不触发"的死代码。撤回形参，改成无条件自取。

### P1-8 评估失败长成一张"全 0 分卡"

两条缺陷叠在一起，才让它完全隐形：

1. `evaluate` **没有重试**（同仓的 `BottleneckAnalyzer._analyze_node` 有 `MAX_RETRIES = 2`），一次限流/超时就放弃；
2. 放弃时 `except` 返回一张**真实的 0 分 scorecard**——它会照常排序（永远垫底）、照常进报告、照常有理由有强弱项，与"一家真的很差的公司"**在报告里完全无法区分**。

而且 `evaluate_batch` 的 `isinstance(r, Exception)` 过滤**永远看不到它**——异常在 `evaluate` 内部就被吞了，`gather` 拿到的是一张合法卡片。

修法：

- 加 `MAX_RETRIES = 2` 重试循环（与 `BottleneckAnalyzer` 对齐），带进度提示与 2s 退避；
- 重试用尽 → 返回 `None`（**不再是 0 分卡**），并用 `llm_clients.fallback.classify_reason` 记下可读原因；
- `evaluate_batch` 把失败票收进 `self.failed_suppliers`，**不进结果列表**；`evaluate_all` 显式汇总上报（「评估失败 N 家（未计入结果）」）。

**并发下的一个坑**：失败原因最初按 `BottleneckAnalyzer._last_fail_reason` 的形状写成单个实例字段，但 `evaluate_batch` 是并发的——谁最后失败谁覆盖，读到的会**串台**。改为按 ticker 的 `dict`，并配一个公开的 `fail_reason(ticker)` 访问器，让 `reverse.py` 不必去摸私有属性。

配套改 `web/streaming/reverse.py`：反查是**单票路径**，没有"跳过这家继续"的余地，且报告已经无法成立——如实 SSE 报错，而不是发一份全 0 的评分。

## Batch 3 验证记录

```
pytest tests/ -q -k "supplier or eval or chain or graph or screen or reverse or alpha or catalyst"
                     → 187 passed, 2231 deselected
pytest -q（全量）    → 2428 passed, 6 skipped
ruff check --select F（5 个改动 py 文件 + 新哨兵） → All checks passed
```

新增 `tests/test_batch3_sentinels.py`（16 例）。**五条修改此前在 tests/ 下零覆盖**——grep `catalyst_bonus` / `evaluate_batch` / `_merge_supplier` 在新增前均无命中。每例都对应一个"改回去就会静默出错"的行为：

- `test_low_confidence_discounts_bonus` —— 同样紧迫但概率三成的卡，加分必须是满置信度的 **0.3 倍**（旧实现下两者**完全相同**，这条正是 P1-4 的判定核心）
- `test_backfills_missing_market_cap` —— akshare 的真实市值回填进首源 LLM 条目（P1-6 审查点名的场景）
- `test_description_not_backfilled` —— 文本字段**不**参与回填，防止"顺手扩大范围"
- `test_returns_none_not_zero_card_when_exhausted` + `test_evaluate_batch_excludes_failures` —— 失败返回 `None`、不收进结果、但留痕
- `test_retries_then_succeeds` —— 断言 `ainvoke.await_count == 2`，确认真的重试了而不是直接放弃

---

## Batch 4 — 报告层对齐 / 覆盖项

本批是四批里唯一**没有全部照做**的。原因不是工作量，而是审查自己对这一批的判断就是"先别做"：

> 「**建议先做前三批。** P2 的覆盖补充（尤其 P2-1 节点 schema、P2-2 供需缺口锚点）涉及 prompt 与模型的联动改动，且会改变现有分析的输出分布，建议在 P0/P1 稳定后再单独立项。」 —— 审查 §7

P2 里绝大多数条目会**改变生产输出分布**（改 prompt → 同一批公司的分数重算），这与前三批"修错值、不加新行为"的性质不同。逐条按"生产者判定"复核后，只落地了不改变输出分布的部分。

### 已落地

#### P1-9 报告层对齐事实核查（含 CLI 空头承诺的清理）

三条分开做的：

**(a) 报告渲染事实核查闸的结论。** `fact_check_recommendation` / `data_coverage` / `llm_only_dims` 此前**只写进 scorecard、全仓零渲染**——而 `fact_check_recommendation` 正是"入围与否"的判据。第 6 章表格补 `核查` 与 `数据覆盖` 两列，配 `_fc_badge` / `_coverage_str` 两个小助手（`None` 渲染成 `-` 而非留空，如实标注"该票没跑核查"）。

**(b) CR3 标来源。** 第 2 章补 `CR3` 列，`cr3_source == "akshare"`（东财板块成分股实算的真值）不带标注，`llm_estimate`（自估）追加「（估）」/` (est.)`。前端 `charts.js` 早就有这个徽章，报告层从来没有——两者不确定性差一个量级，不标就是同权同效。

**(c) 页脚方法论如实改写：** 中文页脚由旧文案改为「产业链拆解 → 供应商检索 → **数据核查（事实核查闸）**」。英文页脚**本来就没有方法论那一行**（审查的「英文页脚同理」无对应目标），故只改中文。

> 补一处澄清（审核时核对）：审查的「同理」只对**页脚**成立。英文报告的 Top Picks 表**确有 `Data Coverage` 列**（`report.py:253/265`，与中文共用 `_coverage_str`），所以覆盖度在中英两侧都渲染了。副作用是英文表里会带出中文标签「LLM 独判」——纯外观问题，不影响数值，记为已知项。

**(d) 第 5 章「多模型交叉验证」刻意保留。** 审查判定它是死的、应删。复核**生产者**后推翻：`legacy.py:337` 会真实填充 `result.cross_validations`（`CrossValidator(validation_models=vm).validate_all(...)`），而 `legacy.py` 是一个活着的 HTTP 端点（`api.py:98`）。渲染器是好的，删章节等于删一个被活端点与 `tests/test_report.py` 三处共同使用的渲染器。

**(e) CLI 的交叉验证空头承诺——删。** 这条是**真的死**，且用户可见：`cli.py` 问「启用多模型交叉验证？」、收下模型列表、打印「交叉验证: N 个模型」，传给 `run_screening` 后 `graph.py` 构造了 `validator` 并作为第 5 个参数传入 `build_screening_graph`——**而该函数体内从未引用它**。删除的是一条只指向自己的闭环：删掉问答块、删掉那行提示、删掉 `CrossValidator` import、`validator` 形参、`validation_models` 形参与调用点实参、`initial_state` 里的 `"cross_validations": []`、以及 CLI 那段永远走不到的 CV 展示表。

不动 `ScreeningResult.cross_validations` **字段本身**（`legacy.py` 仍然填它），在字段旁留注释说明"CLI 不再产出，字段仍活"。

#### 计划外但同源：`moat_overall` 的幻影零分（同 Batch 2 P1-2 一类）

复核 P1-10 时顺手发现的，审查没提：

```python
moat_scores = [data.get(f, 0) for f in moat_fields]          # ← 缺的补 0
moat_overall = sum(moat_scores) / len(moat_scores) if any(s > 0 for s in moat_scores) else 0
```

LLM 少答一个护城河维度，就等于给那一项打了 0 分。实测：LLM 只返回 `patent_moat=8, switching_cost=7` 时，`moat_overall = 3.8`（`=(8+7+0+0)/4`），真实均值是 `7.5` —— 一次部分响应把护城河从"强"砸成"弱"，并经 `overall = base*0.8 + moat*0.2` 把总分从 `6.94` 拖到 `6.19`。

修法：只对**真答了的**维度取均值（键缺失/非数值才剔除；**显式答 0 算答了**，照常进均值，与 `MoatScore` 逐维字段经 pydantic 的 `"8"`→`8.0` 口径保持一致）。四维齐全时结果与旧口径逐字节相同，正常路径不受影响。

`tests/test_batch4_sentinels.py` 三例：部分响应取真均值 / 四维齐全不变 / 一维未答维持 0 回退。

#### P1-11 补齐无锚维度的语义 —— 补记（与 Batch 2 同文件落地，先前漏记）

审查原文（`:732`）说得很准：`customer_validation` / `capacity_status` 是唯二**无数据锚**的维度，而「它们的合计权重**随数据缺失从 26.7% 升到 40%**」——即「缺数据」被算成了「更依赖 LLM」，卡片看起来一样自信。

采纳审查**推荐的 (a) 最小改法**（未采纳 (b) 的彻底改法，理由与审查一致：(b) 会与 `investability_filter` 的「不因缺数据误杀」原则冲突）：

- `ScoringResult` 加两个字段（`models.py:362-365`）：`data_coverage`（有真实锚的维度权重占比）与 `llm_only_dims`（纯 LLM 给分、无锚的维度名列表）。
- 计算在 `supplier_eval.py:463-473`：`anchored_weight = total_weight - 2.0`（扣掉恒为 LLM 的 cv + cs），三个可选锚 `market_position` / `financial_health` / `valuation` 谁缺谁进 `llm_only_dims`。
- 透出在 `supplier_eval.py:516-517`，渲染在 `report.py:29-34` 的 `_coverage_str`（中英第 6 章共用；`data_coverage is None` 时渲染 `-`）。

此处的「全仓零消费方」判断有一个易踩的坑：`data_coverage` 在**本批补 P1-9(a) 之前**已写入模型但确无渲染方，与 `fact_check_recommendation` 同病——所以 P1-9(a) 补的两列同时兑现了 P1-11 的「透出到 scorecard 与前端」要求。

#### P1-12 `cr3_source` 影响校准幅度 —— 补记（与 Batch 2 同文件落地，先前漏记）

审查原文（`:733`）：「LLM 自估的 HHI 与真实成分股算出的 HHI 不确定性差一个量级，现在却同权同效。」

审查建议二选一（减半 / 只提示不改分）。**采纳减半**，理由是「只在 reasoning 提示」会让校准幅度仍与来源无关，等于没修：

- `_check_hhi_consistency` 加 `cr3_source: str = "llm_estimate"` 形参（`bottleneck.py:690`），两处调用点（`:481` merge 路径、`:662` 单报告路径）都传真实来源。
- `step = 2.0 if cr3_source == "akshare" else 1.0`（`bottleneck.py:702`），配 `tag = "(估算)"` 注入每条 `reasoning`（`:703`），使读者不会把估算值当事实（`:755` 日志同样带 `source=`）。

**遗留**：这条修的是「幅度随来源缩放」，但**没有**修掉同函数里 `scarcity` 被 HHI 与 CR3 两次推分的问题——那是 P2-5，已单独立项。

### 未落地：逐条复核结论

#### P1-10 批次标准化 —— 前提属实，但审查开的药方是错的

**前提复核“属实”**：`normalize_scores` 确实把瓶颈层每个维度重写成 `5 + 2z`（`bottleneck.py:199`），`overall_score` 是它的加权和（`:327`）；而供应商层的 `overall_score` 是**绝对分**。`FinalScorer` 又把两者相乘 `quality**0.55 * alpha**0.45`——**尺度确实不可比**，审查这条没说错。

**但"对 5 个维度做批次内 z-score"这个修法不能做**，两条硬理由：

1. `evaluate_batch(suppliers, bottleneck, financial_map)` 的批次是**按瓶颈节点切**的（`supplier_eval.py:560`），一个节点通常只有 **2-5 家**公司。n≈3 的 z-score 在统计上没有意义——`normalize_scores` 自己都设了 `len(reports) < 3` 就跳过的门槛，而且它处理的是"5-10 个环节"，不是"3 家公司"。
2. z-score 恰恰是审查在 P1-1 / P1-10 里反复指为**「口径最不干净」**的那个构造。用它去修一个"尺度不可比"的毛病，等于用一个已知有缺陷的尺子去校另一把尺子。

正确解法是**中心校准**（让 LLM 作为裁判的严厉度在批次间可比），那是设计改动，不是 25 行补丁。**不实施，单独立项**——与审查 §7 自己的建议一致。

**同时复核了审查的第二半**（「若采纳，`supplier_eval.md` 的强制分布要求必须同时删除」）：该强制分布要求**是活的**——`supplier_eval.py:284` 把它加载为 system prompt（`_load_prompt("supplier_eval")`），不是死文本。审查指出的冲突是真的（它要求「9 个维度中至少 2 个 ≤4 或 ≥8」，与同文件 `:44` 的「每个维度必须独立评估」互相拉扯，会把 LLM 推向在权重仅 0.2 的 4 个护城河维度上制造极端值），但影响有界，且改它会**改变现有输出分布**，一并留待 P1-10 立项。

#### P2-1 / P2-2 / P2-6 —— 会改变输出分布，按 §7 单独立项

- **P2-1**（节点 schema 补约束类字段）：`IndustryNode` 现有 11 个字段（`models.py:29-43`），确实无 `supply_structure` / `capacity_lead_time_months` / `export_control_risk` 之类。加字段要同步改 `decomposer` 解析 + `prompts/decompose.md`，**拆解结果因此变化**。
- **P2-2**（给权重最高的 `supply_demand_gap` 接数据锚）：复核确认 `_ANCHORED_DIMS = ("scarcity", "pricing_power")`（`bottleneck.py:119`），**`supply_demand_gap` 确实无锚**。这是**权重最高**的维度却全凭 LLM 估算。审查说得对，但它要新增 `_compute_supply_demand_anchor` 并接入存货周转 / 在建工程 / 分产品营收增速 / 价格趋势四路数据——是**新增数据链路**，不是修错值。
- **P2-6**（催化剂日期结构化）：`expected_date` 现为自由字符串（`models.py:314`），消费方 `_days_until_date` 只做 `fromisoformat(str(v)[:10])`，**`"2025Q3"` 这种格式解析不了、静默返回 `None`**（= 视为无催化剂）——审查指出的"未结构化"问题真实存在。

  **但审查给的文件位置错了**：它说示例在 `prompts/catalyst.md`；grep 该文件**零年份串**，`2025Q3` 只出现在 `catalyst.py:104` 的**内联 user prompt 的 JSON 示例**里（`catalyst.md` 是 system prompt，不含该示例）。改法（结构化 + 相对表述 + 防锚定声明）会改变催化剂时间分布，一并立项。

#### P2-3 / P2-8 交叉核对 —— 部分前提与事实不符

审查说"图谱候选仍是 LLM 自报"，要求与 `industry_concentration` 拿到的真实板块成分股名单交叉核对。复核：

- **A 股这一路已经有真实成分股来源**：`supplier_search.py:330-382` 的 `_akshare_board_source` 直接调 `stock_board_industry_cons_em` / `stock_board_concept_cons_em` 取板块成分股，与 LLM 自报的候选**在合并阶段相遇**（P1-6 的 `_merge_supplier` 已按 ticker 合并并留痕 `sources`）。所以"两个源都有公司列表"这件事**已经是现状**，缺的只是"标注来源分歧"这一层展示。
- 但**真实**的缺口在 `_extract_chain_candidates`（`:967-995`）：它只取本节点 + **直接上游**（`:977-981`），同层竞争与 2 跳上游确实没覆盖。这一条**属实**。
- 扩 `target_nodes` 改变候选池 → 改变供应商层输出 → 同属"改变输出分布"，立项。

#### P2-4 `market_business` 接 FactCheck —— 前提与事实不符，不实施

审查说「数据**已在库**」，读起来像"接一根线"。复核后三处不符：

| 审查主张 | 实际 |
|---|---|
| 「`market_business`（主营构成）」 | 实际字段名是 **`main_business`**（`models.py:213`），无 `market_business` 这一字段 |
| 「数据已在库」 | A 股走 Gangtise 拿到，但需要**用户级凭据 + A 股限定**（`financial_data.py:644-652`）。美股路径**没有**这个数据，而三步法的默认市场是「全部市场」 |
| 「接入 FactCheck 是关键」 | `main_business` **已被消费**：`supplier_eval.py:235-245` 把它渲染进评估 prompt，并写明用途「供交叉验证判断供应商营收是否真来自瓶颈环节」。它缺的不是"接一根线"，而是**没有对应的可判定规则** |

要真做，得先定义"营收占比多少算真来自瓶颈环节"的可判定规则（现在没有），再处理美股无数据的降级——**这是新增判据，不是接线**。不实施。

#### P2-5 HHI 双重扣分 —— 属实，但修法会改变分数

**实测证实**：`_check_hhi_consistency` 里 HHI>2500 同时推 `scarcity`（`bottleneck.py:715`）**和** `pricing_power`（`:720`），随后 CR3>80 又推一次 `scarcity`（`:740`）。**一次集中度观察 → `scarcity` 被推两次**（`scarcity=3` 起，akshare 步长 2.0 走 3→6→8，llm_estimate 步长 1.0 走 3→6→7），而 CR3 的两个分支**只碰 `scarcity`**（不对称）。这是真的双重计分。

但修它 = 改分数（合并判据或拆分维度归属），**改变输出分布**。且它与 **P2-9** 同处 `_check_hhi_consistency`（P1-12 已在上方补记、只动了幅度缩放，未动推分去重），三者应一起设计，故一并立项。

#### P2-9 `company_count` 未用于分档 —— 属实，同 P2-5 一并立项

复核证实：`compute_concentration` 已算出并缓存 `company_count`（`industry_concentration.py:60/144`），也一路流进 `concentration_detail`（`bottleneck.py:473/498/658`）与前端（`charts.js:778` 显示「A股N家」），**但 `_check_hhi_consistency` 的签名里没有它**——HHI 阈值 2500/1500 对 3 家公司的窄板块和 80 家公司的宽板块一视同仁，「板块窄」本身就会推高 HHI，属审查所说的"规则没用手上的数"。**属实**，但同属改分项，与 P2-5 一并立项。

另附一条审查未提的口径注记：`industry_concentration` 的 CR3/HHI 份额是**市值口径**（`_concentration_from_mcaps`），不是营收口径——P2-9 的"能拿到分产品营收时优先用营收份额"因此是**换口径**，不只是换算法。

#### P2-7 —— 三项已实施、两项不实施（见「审查被推翻项」）

`CrossValidator` import / `validator` 形参 / `validation_models` 形参已删（并入 P1-9(e)）。不实施的三项（`report.py` 第 5 章 / `industry_concentration._extract_keywords` / `financial_data.py:624` 注释）理由见上。

### Batch 4 验证记录

```
python -m pytest tests/test_batch4_sentinels.py tests/test_batch3_sentinels.py tests/test_alpha_scorer.py -q
                     → 35 passed
pytest -q（全量，含新增哨兵）        → 2431 passed, 6 skipped（约 375s）
ruff check --select F（改动 py 文件 + 新哨兵） → All checks passed
```

新增 `tests/test_batch4_sentinels.py`（3 例，覆盖 `moat_overall` 幻影零分）：

- `test_partial_response_averages_only_answered_dims` —— 只答 2 维 → `overall_moat == 7.5`（旧实现得 3.8，这条正是判定核心）
- `test_full_response_unchanged` —— 四维齐全时与旧口径逐字节一致，确认修复不误伤正常路径
- `test_no_moat_fields_keeps_zero_fallback` —— 一维未答维持 0 回退，既有语义不变

## 全工作区代码审核

四批做完后对整个未提交改动（16 个文件、652 增 / 277 删）做一次完整审核。分两路：一路是独立审核代理逐个改动文件找缺陷，一路是我自己核对**风险面最大的一处语义变更**——`AlphaScore` 的 8 个字段由 `float` 改成 `float | None`。

### `AlphaScore` 可选化：全仓消费方逐个核对

把 `market_attention` / `information_gap` / `alpha_score` / `dim_cap` / `dim_analyst` / `dim_volume` / `dim_price` / `dim_institution` 八个字段在全仓（`bottleneck_hunter/` 含前端 JS，排除 `.venv` / `build/lib` / `.agents` / worktrees）的每一处引用都过一遍。结论：**Python 侧七处全部已有 `None` 守卫，前端格式化器全部 `== null` 安全**，无需补丁。

**Python 侧（逐处确认）：**

| 位置 | 守卫形式 |
|---|---|
| `supplier_eval.py:872-878` | 写入侧：五维全缺 → `market_attention = information_gap = None`；`bottleneck_score is None` → `base_alpha = None` → `alpha = None` |
| `supplier_eval.py:917-922` / `:948-950` | reasoning 分支显式判 `None` |
| `supplier_eval.py:1007` | `raw_alpha = scorecard.alpha.alpha_score if scorecard.alpha else None`，再 `max(0.1, raw_alpha) if raw_alpha is not None else 0.1` |
| `roundtable.py:215-216` | `if sc.alpha.alpha_score is not None else "｜预期差: 数据不足"` |
| `web/api.py:425` | `sc.alpha and sc.alpha.alpha_score is not None and ... >= 7` |
| `web/api.py:864` | `((sc.get("alpha") or {}).get("alpha_score") or 0) >= 7` |
| `web/api.py:1053/1075-1081` | 摘要行显式分成「有值 → 格式化」「None → `Alpha=数据不足`」两支 |
| `web/streaming/reverse.py:468-470` | 落库列是 `REAL DEFAULT 0` 的排序缓存，注释说明真值在 `result_json`，故按列约定落 0.0 而非 NULL |

**前端：** `fmtScore`（`phase-views.js:667`）、`_fmtVal`（`dashboard.js:1661`）、`scoreClass`（`dashboard.js:15`）、`sectionBgClass`（`phase-views.js:692`）四个公共格式化器**第一行就是 `if (v == null) return '-'` / `return ''`**。其余大多是 `?? '-'`、`|| 0`、`!= null` 守卫。

**两处"中性值兜底"是既有行为，不是本次改动引出的**（已用 `git show HEAD:` 逐字核对，两段代码在 HEAD 中完全相同）：

- `phase-views.js:586` `const s = score != null ? score : 5;` —— 缺失维度在条形图上画成中性 5 分。**但在 HEAD 中行为更糟**：旧实现的 `_tier_score` 对缺数据返回 `5.0`，也就是 `dim_*` **从不缺失**，这个 `5` 分支永远走不到、且真值本身就是那个幻觉 5.0。本次改动让 `None` 真的能出现，这个分支才开始生效——而它渲染的正是"未知"。
- `drawer.js:228` `const val = alpha[d.key] ?? 0;` —— 只在整个 `alpha_score != null` 时才渲染（`drawer.js:225`），A 股 `has_inst=False` 展示成 0 是既有取舍。

这两处都是**展示层的语义含糊**（"未知"画成中性分），非崩溃、非数据污染；且都在 `values > 0` 时才可见。记为已知项，不在本工作区改（改的是渲染口径，属体验打磨，需要与前端一并设计）。

**一处真正会静默丢因子的点：** `phase-views.js:718-720` `if (r.alpha?.dim_cap >= 7) factors.push('小市值');`。JS 里 `null >= 7` 是 `false`（不抛错），所以不会崩，但**「小市值 / 低关注 / 量能放大」这三个因子标签在维度为 `None` 时会静默不出现**——修复前 `dim_cap` 恒有值（幻觉 5.0），所以这是修复**引出**的行为差异。结果是保守的（少一个标签，而不是多一个错的标签），且该行仅在 `alpha_score` 有值时才走到，故不违背"不把未知当中性"的立意。已知项，记录在案。

### 审核中另发现一处「假闭环」（既有缺陷，非本工作区，不擅自扩大范围）

`watchlist/strategy_engine.py:251-282` `_aggregate_source_scorecard` 把评分卡拍平成**顶层**键读取：

```python
return {"overall_score": sc.get("overall_score"), "quality_score": sc.get("quality_score"),
        "alpha_score": sc.get("alpha_score"), "final_score": sc.get("final_score"),
        "bottleneck_node": sc.get("bottleneck_node", "")}
```

但 `result_json["supplier_scorecards"]` 是 `SupplierScorecard.model_dump()`（`web/api.py:955`），
读的五个键里有三个在**顶层并不存在**——实测：

```
顶层字段里存在的 : ['overall_score', 'bottleneck_node']
顶层字段里缺失的 : ['quality_score', 'alpha_score', 'final_score']
嵌套存在的       : ['final', 'alpha']
```

真值在 `final.final_score` / `final.quality_score` / `final.alpha_score`（`alpha_score` 还有 `alpha.alpha_score` 这个第二来源）。
可见路径：`entry["source"] == "phase4"` 时走这段（`:93` 与其它聚合并列，异常被 `return_exceptions=True` 吞掉）。

**后果**：观察池里所有来自 Phase 4 的标的，喂给 LLM 的「供应链评分」里那三个分数**恒为 `null`**，
而 `overall_score` 与 `bottleneck_node` 是好的——所以简报读起来「有评分」，只是关键的三项一直空着。
与本次全工作区的主线（**每一步的产出没被下一步消费**）是同一类病，只是位置在观察池侧。

**本工作区不修**：`strategy_engine.py` 不在 16 个改动文件内（`git diff --stat` 无它），
属审查报告之外的既有缺陷；且修它会改变喂给 LLM 的简报内容（= 改变输出分布），
按本项目对 P2 类改动的既定处置单独立项。已在此留证，待批准后处理。

### 审核中自己改出来的一个缺陷（已修，留证）

`_merge_supplier` 是本次 P1-6 新写的。第一版把回填字段写成一张表：

```python
_MERGE_BACKFILL_FIELDS = ("market_cap", ..., "market_share", "name_cn", "sector")
for f in _MERGE_BACKFILL_FIELDS:
    if getattr(keep, f) is None and getattr(extra, f) is not None: ...
```

`name_cn` 是 `str = Field(default="")`、`sector` 是必填 `str`（`models.py:156/160`）——
**两者都不可能是 `None`**，所以那两条分支永不可达，是纯死代码；而紧邻的注释还写着「不碰 description / sector」，与字段表自相矛盾。

第一次修法（删掉这两个字段）也不对：它把「判据写错」当成了「不该回填」。
`sector` / `name_cn` 的真值是空串，确实**该**回填（akshare 板块成分股那条路 `:377` 会填真实行业），
错的是 `is None` 这个判据。所以最终改成两段，各自用对的判据：

```python
_MERGE_BACKFILL_FIELDS = ("market_cap", "pe_ratio", "revenue_growth",
                          "gross_margin", "institution_holding_pct", "market_share")
_MERGE_BACKFILL_TEXT_FIELDS = ("sector", "name_cn")
...
for f in _MERGE_BACKFILL_FIELDS:      # 数值型：缺席 == None
    if getattr(keep, f) is None and getattr(extra, f) is not None: setattr(...)
for f in _MERGE_BACKFILL_TEXT_FIELDS: # 文本型：空 == 假值
    if not getattr(keep, f) and getattr(extra, f): setattr(...)
```

`description` 依旧不回填：它是带源口吻的整段论述，覆写会让描述与主源不匹配（`test_description_not_backfilled` 钉住这一点）。
文本回填这一路另补了 `test_backfills_empty_sector_and_name_cn`——已实测它在第一版（`is None` 判据）下**必然失败**，不是空转断言。

### 本次新增/改动文件的测试覆盖自查

按「每处非平凡逻辑留一个会失败的检查」逐项核对，仍**零覆盖**的是：
`data_coverage` / `llm_only_dims` 两个字段的计算（P1-11）、`_tier_score` 的 `None` 分支、
`evaluate_batch` 失败路径的 `_fail_reasons` 内部状态（行为已被 `test_batch3_sentinels.py` 覆盖，内部状态没有）。
其中 `_tier_score(None)` 是本批改动的核心之一（P1-2），仅由 `test_alpha_scorer.py` 的间接路径触及——
记在此处，不额外补测（补一套只测实现的镜像测试，价值低于维护成本）。

### 全工作区代码审核的验证记录（收口门禁）

审核过程中我自己又改了两处（`supplier_search.py` 的回填判据 + 对应哨兵），
上一次 `2431 passed` 早于这两处改动，对本轮要提交的字节不成立，故重跑：

```
python -m pytest -q（全量）                      → 2432 passed, 5 skipped（372s，exit 0）
python -m pytest tests/test_batch3_sentinels.py tests/test_batch4_sentinels.py \
                 tests/test_alpha_scorer.py tests/test_models.py -q
                                                 → 55 passed
ruff check --select F（supplier_search.py + 两个哨兵文件） → All checks passed
```

审核末尾又改了 `fact_check.py` 的去重键（见下「去重键写窄了」），上面那轮同样作废，故**再跑一次终轮**——
这就是要提交的字节：

```
python -m pytest -q（全量，终轮）                → 2433 passed, 5 skipped（373s，exit 0）
python -m pytest tests/ -q -k "fact_check or factcheck or batch3 or batch4 or alpha_scorer"
                                                 → 48 passed
python -c "from bottleneck_hunter.chain.fact_check import demo; demo()"
                                                 → Case1~Case5 全部通过
ruff check --select F（fact_check.py）           → All checks passed
```

（2432 → 2433 是 `tests/test_batch3_sentinels.py` 里那条文本回填哨兵。
终轮之后又加了 `tests/test_fact_check_rules.py`（3 例），故**最终门禁**为：）

```
python -m pytest -q（全量，最终）                → 2436 passed, 5 skipped（373s，exit 0）
python -m pytest tests/test_fact_check_rules.py tests/test_batch3_sentinels.py -q → 20 passed
ruff check --select F（fact_check.py + 三个哨兵文件）  → All checks passed
```

> **skip 数从 6 变 5，不是回归。** 两轮总数都是 2437，是一个用例从"跳过"变成了"跑过"：
> `tests/test_daily_turnover_gate.py:223` 按**北京挂钟时间**决定是否跳过（`bj < 07:30` 时跳过）。
> 首次全量跑在 UTC 22:5x / 北京 06:5x，命中跳过；复跑时北京已 07:55，该用例的前提成立并**通过**。
> 与本次改动无关，记录在此以免后来者把时间敏感的 skip 误读成 flake。

### 审核中自己改出来的**第二处**缺陷：去重键写窄了（已修，留证）

Batch 1 为「同字段重复计分」加的去重集合，键取的是 `actual_field`。**这个键是错的。**

拿规则表实跑一遍，找出所有「两条规则落到同一个 `actual_field`」的碰撞，共三对：

| actual_field | 规则对 | 期望方向 |
|---|---|---|
| `gross_margin_trend` | `financial_health` / `gross_margin_trend` | positive / positive ✅ 同向 |
| `cr3_estimate` | `market_share` / `cr3_estimate` | positive / positive ✅ 同向 |
| `consensus_pe` | `估值低\|便宜\|被低估` / `高估\|贵\|泡沫` | **positive / negative ⚠️ 反向** |

第三对是真碰撞：`claims_text = " ".join(strengths + weaknesses)`（`fact_check.py:132`），
所以**同一张卡的两个文件**可以同时说「估值便宜」和「估值被高估」——
这是两条方向相反、结论完全不同的独立主张，而不是同一条事实被数了两遍。

只按字段去重时，索引靠后的「被高估」落进 `duplicate_skipped`，
**连同它的 mismatch 计数一起消失**。实跑对照（PE=18，两条同时命中）：

```
旧: 只按字段   → mismatch=0   明细=[('consensus_pe','supported'), ('consensus_pe','duplicate_skipped')]
新: 字段+方向  → mismatch=1   明细=[('consensus_pe','supported'), ('consensus_pe','MISMATCH')]
```

丢一条 mismatch 不只是少 0.5 可信度：`mismatch_count >= 3 → REVIEW` 的阈值因此更难触发，
属**放宽**方向。真正冗余的只有「同字段 **且** 同方向」——那才是同一次观察被计两次分。

修法：键改为 `(actual_field, expected_dir)`。

```python
judged: set[tuple[str, str]] = set()
...
if (actual_field, expected_dir) in judged:   # 同一条事实 + 同方向 → 才算重复
    ...verdict="duplicate_skipped"
    continue
judged.add((actual_field, expected_dir))
```

哨兵是 `demo()` 新增的 **Case 5**：一张卡 `strengths=["估值便宜"]` + `weaknesses=["估值被高估"]`，
断言恰好 **1** 条 mismatch 且**不存在** `duplicate_skipped`。
已按上表实测：旧的字段键下该断言必然失败（mismatch=0），非空转。

### 审核中自己改出来的**第三处**问题：`demo()` 根本不在测试套件里

写上面那两段时我顺手写了一句「Case 5 由 `tests/test_fact_check.py` 驱动，故计入总数」——
**这是臆断，实测为假。** `demo()` 挂在 `if __name__ == "__main__":` 下，
全仓 grep 无任何测试导入或调用它；`tests/test_fact_check.py` 是**另一套东西**
（提示词防火墙 + 来源校验，与本模块的 `chain.fact_check` 无关）。

后果比"没测到"更隐蔽：本轮我在 `demo()` 里新加的 Case 4 / Case 5 哨兵，
**一次都不会在 CI/本地全量里跑**——它们是只在手工执行 `python -m ...fact_check` 时才生效的装饰。
这正是本工作区反复出现的同一类病（产出没有被下游消费）在测试层的翻版。

修法：新建 `tests/test_fact_check_rules.py`（3 例）把案例接进套件。

```
python -m pytest tests/test_fact_check_rules.py -q   → 3 passed
```

- `test_demo_cases_all_pass` —— 直接驱动 `demo()`，五个案例的 assert 全部进套件
- `test_opposite_directions_on_same_field_both_counted` —— Case 5 的正证（1 条 mismatch、无 duplicate_skipped）
- `test_same_direction_on_same_field_still_deduped` —— **反向**：同字段同方向必须**仍然**去重，否则回到"一次观察计两分"

> 第三例是写这条测试时才补的：只断言"反向不去重"会让去重功能整体被删掉也不报警，
> 得同时钉住"同向必须去掉"。两条一起才框得住那个键。

## Batch 5 — 真实错误（P2-6 / P2-5+P2-9 / 假闭环收尾）

### 5A P2-6 催化剂日期：链侧 `expected_date` 是自由文本，下游只认 ISO

**症状属实，但审查说的传播路径是错的 —— 我自己核实了一遍。**

审查称 `models.py` 的 `expected_date` 会流到 `_days_until_date`，于是 `2025Q3` 被当成
「这家没有催化剂」。我按「生产者判定」追了写入侧：

- `watchlist` 侧 `create_catalyst` 只有三个调用方（`catalyst_monitor.py:99`、`:125`、`scheduler.py:929`）；
  其中 `catalyst_monitor` 的 LLM prompt 早已**强制** `YYYY-MM-DD 或 null`；
  另有一处从 `action_strategy` JSON 取 `item.get("date") or item.get("expected_date")`，
  而它的产出方从不写期间串。
- **生产库实证**：`catalyst_tracking` 表里非 ISO 值 **0 条**（384 个 NULL，57 个 distinct ISO）。
- 但 chain 侧 `analyses.db` 里有大量非 ISO：`2025Q3` x338、`2025Q4` x272、`2025H2` x76、
  `2025年下半年` x6 ……（实测 148 个 distinct / 1783 行）。

所以：**症状真实，但炸点不在观察池，在 chain 侧** —— 而 chain 侧的 `expected_date`
目前**只被当展示文本**渲染（`dashboard.js:791-810`、`phase-views.js:627`），暂无日期排序消费方。

**修法选择（最小且面向未来）**：在**两个写入收口各归一一次**，而不是去给「还不存在的消费方」
加过滤。

1. `chain/models.py`：`CatalystEvent._normalize_date`（`mode="before"` validator）——
   任何构造路径都归一，不依赖调用方自觉。
2. `watchlist/store_committee.py:create_catalyst`：**唯一的写入收口**。它的下游是
   **字符串比较**（SQL `expected_date <= ?`、`snap_date <= expected_date`、`_date_diff` 的
   strptime），一旦存进 `2025Q3` 那些判断全部静默不成立，而
   `WHERE expected_date IS NOT NULL` 仍会把它当有效日期捞出来 —— 比"没有日期"更坏。
   解析不了存 `NULL`，三个调用方自动受益。

**期间语义**：模糊期间（`2025Q3`）归一到**该期间最后一天**。方向是保守的 ——
到期日算得越晚，越不会把还没到期的催化剂误判成「已过期」。区间（`2025Q4-2026Q1`）
取**最晚**者 → `2026-03-31`，与 prompt 里「未来 6-18 个月」的时间窗一致。

**连带修掉的一处 prompt 病**：`catalyst.py` 的示例日期**写死**且是 `2025Q3` ——
既教 LLM 用下游解析不了的格式，又已过期一年。改为 `_sample_expected_date()`：
今天 + 2 个季度、取季末，与那条相对时间窗同构；并补上「仅为格式参考」的反锚定声明
（与 `bottleneck.py:576` 一致）。

**实测收口率**（拿生产库 148 个真实 distinct 值跑新代码）：

```
distinct=148  rows=1783
归一成功 1779 行（99.8%）  失败 4 行（0.2%）
归一后仍不可解析（严重 bug）  0 行
```

剩 4 行全是相对表述（`未来6-12个月` / `未来1-3个月` / `未来3-6个月`）——
解析它们需要**写入时刻**，此处拿不到，故返回 `""`（= 无日期）。
不拿今天去代入：那会把「6 个月后」写成今天附近的某天，是把「不知道」伪装成「知道」。

### 5A 过程中自己改出来的三个缺陷（已修，留证）

1. **区间被悄悄截断成起点**（我的测试先发现的，不是审查给的）。
   `2025Q3-Q4` 返回 `2025-09-30` 而非 `2025-12-31` —— 因为每条期间正则都要求 4 位年份，
   裸写的 `Q4` 被整段丢弃，区间塌缩成单点，**到期日凭空提前一个季度**。
   修法是加 `_expand_bare_periods` 把省略的年份补回来，**并且**让 `2025年Q3-Q4`
   这种中英混写也吃进同一个 token（否则同一个 bug 会换个写法复发）。
2. **整年兜底盖掉精确写法**。我加「`2025全年`→`2025-12-31`」兜底时最初无条件参与 `max`，
   于是 `2025Q3` 多出一个 12-31 候选并被选中，季度末语义被整年末吃掉。改为
   **仅当更精确的写法一个都没匹配上时**才启用。
3. **数量被读成日期**。整年兜底会把「订单金额2026万元」读成「2026 年兑现」——
   凭空造出一个从未存在过的到期日。加末位断言排除「4 位数 + 单位」。
   （`2025%` / `5000万台` / `2026万股` 均已覆盖为返回 `""`。）

第 3 条是**凭空造日期**，比返回「无日期」坏得多 —— 与 5A 的整个立意同向。

### 5A 验证记录

```
python -m pytest tests/test_batch5_sentinels.py -q          → 53 passed
python -m pytest tests/test_catalyst.py tests/test_batch5_sentinels.py \
                 tests/test_batch3_sentinels.py -q          → 64 passed
python -m pytest -q                                          → 2493 passed, 5 skipped
ruff check --select F,E9 <4 touched files>                   → All checks passed
ruff check <4 touched files>                                 → 4 errors,
    全部是 E501 且**逐行核对为 HEAD 既有**（models.py:41/146、store_committee.py:182/322），
    非本次引入，不擅自扩大范围。
```

前端 `dashboard.js` / `phase-views.js` 只渲染文本、不做日期运算，
因此本轮改动对现有展示**无行为变化**；改的是「未来任何消费方都拿得到可解析日期」。

### 5B P2-9 集中度链路：审查给的前提是**错的**，真缺陷在别处

**审查原话**：真实集中度（akshare）取到了，但 `_check_hhi_consistency` 的校准结果被
`normalize_scores` 抹掉 —— 所以「校准形同虚设」。

按「生产者判定」先追**写入侧**，结论是：**这条链路上根本没有任何一次真实数据进来过**。
拿生产 `analyses.db` 全量跑（去重后 8437 个环节）：

```
去重节点数: 8437
(market, cr3_source): {('us_stock', None): 577, ('us_stock','llm_estimate'): 6381,
                       ('a_stock','llm_estimate'): 1479}
非空 concentration_detail: 0
带校准文本的维度数: 30  /  文本≠分数: 28
  背离样例: ('高纯多晶硅','pricing_power','[HHI校准: HHI=2800>2500, 4→6]', 3.9)
            ('抗辐射绝缘涂层材料','pricing_power','[HHI校准: HHI=1200<1500, 7→5]', 1.8)
```

**akshare 来源 0 条。** 也就是说「校准被 z-score 抹掉」这个描述虽然**恰好也成立**
（下条详述），但它描述的是一条**从未被走到过**的路径。

**排除了三种「取样/接线」解释**（都要动手验，不能靠推理）：

- **功能比样本新？** 否。该功能 2026-07-03 上线（`25e250d`），而所有 A 股分析落在
  07-06 … 09-10，**全部在其之后**。
- **市场没接上？** 否。`phases.py:251` 确实按 market 传参。
- **名字对不上？** 否。`光刻机`/`PCB`/`先进封装`/`存储芯片`/`光刻胶`/`MLCC`
  在东财**概念板块**列表里逐字存在。

三个真正的根因：

1. **无重试，且先试最不稳的那个源。** `stock_board_industry_name_em` 实测
   **连续 5–6 次 `RemoteDisconnected`**（`concept_name` 反而间歇可用，且**名字对得上的
   正是概念板块**）。原实现 industry 在前、concept 在后、**且完全不重试** ——
   一次抖动就静默判死一个板块。
2. **`None` 被永久缓存。** 失败也写进 `_CONCENTRATION_CACHE`，而 `clear_cache()` 在
   生产代码里**零调用方** —— 一次抖动 = 该板块在进程存续期内永久判死。
3. **校准确实被 z-score 抹掉（28/30 实证）。**

第 3 条的真根因不是「z-score 太晚」，而是**跑了两遍**：一遍在 `_analyze_node`
（标准化**之前**），一遍在 `_calibrate_concentration`（**之后**）。前一遍是纯负债 ——
它的分数**必然**被 `5+2z` 整维重写，只有那句「[HHI校准…]」留在 reasoning 里没被抹掉，
于是报告同时出现「校准到 5」和实际 1.8。**删掉前一遍**既是根因修复，也是净删除；
审查自己的 P1-1 建议的正是这条路（选项 a）。

### 5B 改了什么

| # | 改动 | 为什么 |
|---|------|--------|
| 1 | 删 `_analyze_node` / `_merge_sub_reports` 里的**标准化前校准**；`hhi_adjustments` 不再在那里构造 | 结果必被覆写，只留下自相矛盾的文本（28/30） |
| 2 | `_check_hhi_consistency` **重写**：所有规则先跑完，**最后每个维度写一个戳**，取最终分 | 结构性缺陷 —— HHI 与 CR3 会先后命中同一维度，各自写戳时先写的记的是中间值 |
| 3 | `retry_failed_nodes` 补上 `normalize_scores` + `_calibrate_concentration` | 它此前**从不**过标准化，产出与首批**两套不可比的尺度**混在同一次分析里 |
| 4 | `_extract_keywords` 两份实现**合一** | 见下 |
| 5 | `_match_boards` / `_fetch_cons` 抽出共用；**概念板块在前**；带重试；`regex=False` | 根因 1 与 2 |
| 6 | 板块列表进程级缓存；失败**不写** `_CONCENTRATION_CACHE` | 每次 term 都重拉整张板块表，而它恰是最不稳的接口 |
| 7 | `_try_akshare_search` 改走共用实现 | 顺带白拿重试/缓存/字面匹配 |
| 8 | 校准幅度随来源缩放：`akshare` 2 分、`llm_estimate` 1 分 + 文本标「(估算)」 | 自估的 HHI 与真实算出的差一个量级，此前同权同效 |
| 9 | 取不到真实数据时 prompt **明说**「无真实数据，保守估算」 | 此前留空 → LLM 照提示词里的示例 HHI=1800 编数，而下游把编出来的数**当锚点改分** |

### 关键词两份实现分叉（实测，非推断）

15 个真实环节名，**HEAD 上分叉 3 个**，方向全部是「落空」形态：

```
'高端光刻胶（ArF 浸没式）'  concentration: ['光刻胶（ArF 浸没式）']   supplier: ['光刻胶','ArF','浸没式']
'电子特种气体 高纯'         concentration: ['电子特种气体 高纯']       supplier: ['电子特种气体','高纯']
'光刻胶，显影液'           concentration: ['光刻胶，显影液']         supplier: ['光刻胶','显影液']
当前实现分叉数: 0
```

**注意方向与我最初的判断相反**：持有**弱化版**的是 `industry_concentration`
（它不切括号/空白、无长度上限），`supplier_search` 那份才是强的。
弱的那份把整条 `光刻胶（ArF 浸没式）` 拿去 substring 匹配板块名 → **必然 0 命中，且静默**。
两边匹配的是同一个东西（东财板块名），故统一到强的实现，放在无 LLM 依赖的
`industry_concentration` 里，`supplier_search` 反过来委托它。

**市值换算也分叉**（`_mcap_to_yi` 共用一个 vs `supplier_search` 里的内联版）：

```
'1,234,567,890'   共用=12.3457   内联=12.35   <<< 分叉（精度）
'0'               共用=None     内联=0.0     <<< 分叉（0 市值被当成有效值参与 CR3/HHI）
'-5'              共用=None     内联=-5.0    <<< 分叉
```

第 2 行是有后果的：`0.0` 会作为一家公司进 `_concentration_from_mcaps`，把 HHI 分母撑大、
**摊薄真实集中度**。统一到共用实现后这三行都收口。

### 5B 过程中自己改出来的缺陷（已修，留证）

1. **枚举 repr 漏进台账**：我的重写写 `f"{s.dimension} ..."`，产出
   `BottleneckDimension.PRICING_POWER 4→6`；旧代码写的是字面量。这条串会进日志，
   已改 `getattr(s.dimension, "value", s.dimension)`。
2. **`{:.0f}` 重犯本条要修的错**：探针实测 `光刻机 scarcity score=3.9 claims=4` ——
   写「→4」正是「文本说的和旁边分数不是一回事」。改 `:g`，并把哨兵从
   `round(s.score) == round(claimed)` **收紧为精确相等**。
3. **一条测试建立在错误前提上**：`test_counter_reports_only_real_changes` 断言 `== 0`。
   探针证明 `scarcity=4/pricing=4 + CR3=25/HHI=2800` 下 scarcity 是 `4→6→4`（净零、不记），
   但 `pricing_power 4→6` **是真改动**。正确断言是 `== 1`，已按「哪一维真的动了」重写。
4. **`test_us_stock_never_probes_the_source` 测错了层级**：直接调
   `_fetch_real_concentration` 测的是「该函数自身不做市场判断」，与美股无关 ——
   闸门在 `_analyze_node`。已改为驱动 `_analyze_node`，并加断言「美股不碰类级熔断计数」
   （那是进程共享的，误加会**误伤同进程的 A 股分析**）。

第 4 条值得单独记：**测试打到错误层级时，它给的是虚假安心** —— 绿是因为测的不是生产路径。

### 5B 验证记录

```
python -m pytest tests/test_batch5b_sentinels.py tests/test_supplier_keywords.py -q
                                    → 42 passed
python -m pytest -q                 → 2512 passed, 5 skipped in 380.69s
                                     （较 5A 的 2493 增 19 条哨兵）
ruff check --no-cache <5 改动文件>   → 9 findings，**逐条比对 HEAD 基线**
                                     （基线 15 条，消失的 6 条是我删掉的长 f-string E501）
                                     新增 0 条
```

`hhi_adjustments` 已确认**无消费方**（grep 全仓：只写不读），故本轮只把它从
「构造于错误时机」改为「构造于校准之后」，不动其下游。

### 5C 假闭环收尾：源评分卡喂给简报 prompt 的一直是三个 `null`

**生产实证（2026-09-28，`analyses.db`，375 张 scorecard）**

```
顶层含 quality_score/alpha_score/final_score 的: 0
在 final.* 下齐全的:                            375
```

消费方 `strategy_engine._aggregate_source_scorecard` 读的是**顶层**，
于是每次都返回 `{"quality_score": None, "alpha_score": None, "final_score": None}`。
`json.dumps` 把 `None` 写成 `null` 不报错，LLM 简报 prompt 里那行「供应链评分」
长期是三个 null，面板上也一直空着。**全程无异常，所以无人发现。**

这是本工作区反复出现的同一类病：**静默的 `None` 比抛异常更坏** ——
它长得像「这家公司没评分」，而不是「代码读错了地方」。

**改法（生产者侧一处）**

`final = sc.get("final") or {}` 起手，三个分依次从生产者实际写入的位置取；
`final` 缺失的老记录退到 `overall_score` / `alpha.alpha_score`，
`final_score` 确实没有就**如实为 None**，不回退到中性 5.0 把「不知道」
装成「中等」（与 P1-2 同一原则）。

只改这一处的原因是**核对过消费链**：该 dict 在 `:485` 被读进
`source_scorecard_summary`，`:543` 渲染进 prompt，两者都在这个生产者下游。
改生产者一处即覆盖三处。同类读取方（`web/api.py:862,879`、
`web/reverse_api.py:162,171`、`web/streaming/phases.py:832`、
`web/watchlist_api.py:440`）早已全部走 `final.*`，**这个函数是唯一的漏网者**。

**验证**

- 哨兵 5 条，`git stash` 实测**修复前 2 条必红**（非空断言那 2 条），修复后 5 条全绿。
- 决定性证据不是测试，是**真实生产记录**：拿 NVO 那张 scorecard 驱动修复后的函数，
  返回 `{'overall_score': 6.7, 'quality_score': 6.7, 'alpha_score': 6.7,
  'final_score': 6.7, 'bottleneck_node': '预填充注射笔'}`，与 `final.*` 逐字一致。
- 测试打到正确层级：驱动的是 `_aggregate_source_scorecard` 本身，即生产路径上那个函数。

```
python -m pytest tests/test_batch5c_sentinels.py tests/test_strategy_engine.py -q
                                    → 25 passed
python -m pytest -q                 → 2517 passed, 5 skipped in 419.69s
                                     （较 5B 的 2512 增 5 条哨兵）
ruff check --no-cache <strategy_engine.py>  → 5 findings
ruff check --no-cache <HEAD 基线>           → 5 findings，**同一集合，新增 0 条**
tests/test_batch5c_sentinels.py             → ruff 干净
```

## Batch 6 — 覆盖项（6A 节点约束 / 6B 供需锚 / 6C 候选来源 / 6D 强制分布 / 6E 主营规则）

| 项 | 结论 | 代码改动 |
|---|---|---|
| 6A | 审查前提错（`IndustryNode` 无 notes）；真问题是 13960 条 `link.notes` 零读取方 → 接入打分 prompt，覆盖 0 → 99.0% | `models.py` / `decomposer.py` / `bottleneck.py` |
| 6B | 审查的 akshare 财报锚生产不可达 → 用 6A 采到的扩产周期作锚，无锚时明说「保守打分」 | `bottleneck.py` |
| 6C | 同层扩池实测是互补件、+199 家噪声 → 不做；改为消费 `sources`（截断排序 + 评估 prompt 标注） | `supplier_search.py` / `supplier_eval.py` |
| 6D | 删强制分布；中心平移会抹真信号、撞绝对阈值 → 不做 | `prompts/supplier_eval.md` |
| 6E | 主营占比规则在 20 个生产样本上 ≥3 个误判 → 不上硬门禁 | 无 |

### 6A 链节点约束字段：审查的前提错了，真正的问题比它说的更大

**审查原话**：五项约束事实（供应结构 / 扩产周期 / 认证周期 / 地理集中度 / 出口管制）
「此前只能靠 LLM 在 `notes` 自由文本里带一句」，建议结构化。

**逐条核实（2026-09-28，生产 `analyses.db`，32 条链）**

```
IndustryNode.model_fields 里有 notes 吗？     没有（ChainLink 才有）
8484 个节点里带任一约束字段的：               0
14064 条 link 里 notes 非空的：               13960
全仓读取 ChainLink.notes 的代码：             0 处
```

审查错在两层：`IndustryNode` **没有 `notes` 字段**，所以不是「有位置但不结构化」，
而是**根本没地方放**；而真正装着约束事实的是 `ChainLink.notes` ——
「高端纯化填料被GE(现Cytiva)、Waters等外资厂商垄断」「高端品种高度依赖进口」——
拆解 prompt 一直在要，存进了 DB，**然后没有任何人读**。又一个假闭环。

**改法（三处）**

1. `models.py` — `IndustryNode` 加五个字段，缺失一律 `None`（未采集），不用默认值冒充。
2. `decomposer.py` — prompt schema 加五项 + 「没把握就填 null」；四个归一函数：
   - 周期月数 `0 → None`（扩产周期不可能为 0，存 0 是**反向事实**）；
     容忍「约 18 个月」「2 年」（年 ×12，不换算就是 12 倍口径错）；
   - 枚举只做**精确别名匹配**，不做子串猜测（「供不应求」含「不应」，
     子串匹配会把一句话读成结论）；剥括号以收住 LLM 照抄的 `single(独家)`。
3. `bottleneck._build_context` — **消费侧**，本批关键。只加字段不接线，
   就是再造一个假闭环。把五个结构化字段**和** `ChainLink.notes/alternatives`
   都接进实际发给 LLM 的打分 prompt；什么都没有时整段不出现（空标题会被读成
   「已确认无约束」）。

**为什么接 `link.notes` 比加字段更要紧**

新字段要等用户**重新拆解**才会有值；而 `phases.py:156`（共享模板）与 `:180`
（14 天缓存）都是反序列化旧链后直接跑 `_analyze_node`。接上 `link.notes`，
**库里已有的链立即受益，不用重拆**。

**真实数据回放（生产 32 条链原样喂修复后的 `_build_context`）**

```
                         修复前（容器现网代码）  修复后
prompt 带约束事实的节点   0 / 8484               8398 / 8484 (99.0%)
  其中 拆解备注                                  8381
  其中 已知替代方案                              7526
含完全重复行的节点                               0（去重前 1282）
```

**回放揪出的第二个问题**：通用名节点（如「控制系统」）在多个子树里撞名，
单节点出边最多 24 条，初版逐条照搬 → 1282 个节点出现重复行，还有互相矛盾的
「替代方案 1 个 / 2 个 / 3 个」。改为 notes 去重、上限 5 条（覆盖 97% 节点），
alternatives 合为一行（多值时写区间并注明口径不一）。撞名本身要在拆解侧解决，
不在本批范围，已用 `ponytail:` 注释标出上限与升级路径。

**自己踩到、自己修掉的缺陷**

- `"18个月"` 被旧的 `_safe_int(default=-1)` 吞成 None —— 正是本批要根除的
  静默丢数；改为正则取数 + 年换算。
- `"认证周期" not in prompt` 假红：DIMENSION_DESC 本来就有这四个字。
  改标签为「新进入者认证周期」，断言改为**带冒号的标签行**。
- `alternatives` 被错绑在 `notes` 非空的条件下 —— 两个独立事实共用一个守卫，
  生产里有 104 条 link 会因此静默丢掉替代方案数。拆成两个独立判断。

**验证**

- 哨兵 41 条。非空性双向实证：(a) 回退到 HEAD / 回退到「只接字段不接 notes」，
  正向哨兵转红；(b) 注入「总是输出标题」「不按 upstream 过滤」的草率实现，
  反向哨兵转红。
- 预设链 JSON（ev/gpu/robot）与缺字段的旧记录都能正常反序列化（→ None）。

```
python -m pytest tests/test_batch6a_sentinels.py -q → 41 passed
python -m pytest -q                                  → 2557 passed, 5 skipped, 1 failed
                                                        （失败项 test_chain_us_candidates_validated 是
                                                         活网 yfinance 取 NVDA 报价，单跑 3/3 通过，与 6A 无关）
ruff（改动 3 文件）基线 22 → 16，消失的 6 条是被重写掉的 E501，新增 0 条
tests/test_batch6a_sentinels.py                      → ruff 干净
```

### 6B 供需缺口锚点：审查建议的数据路在生产走不通，改用 6A 已采到的事实

**审查原话（C-2 / P2-2）**：`supply_demand_gap` 权重最高（0.25）却是唯一无数据锚的维度；
建议 A 股接存货周转 / 在建工程 / 分产品营收增速 / 价格趋势，美股在 prompt 标注「无数据锚，请保守打分」。

**逐条核实（2026-09-28）**

- 「唯一无锚」**属实**：`_check_hhi_consistency` 只动 scarcity / pricing_power。
- 更糟的一点审查没说：集中度块在无数据时会**明说**「无真实数据，请保守估算」，
  而供需缺口连这句都没有 —— LLM 只能照着 prompt 里的示例 JSON 值编。
- 审查建议的 A 股数据路**在生产走不通**：容器内实测 `compute_concentration`（同一套
  akshare 板块链路）对 光刻胶 / 锂电池隔膜 / 多肽原料药 / HBM **4/4 返回 None，每次约 22 秒**；
  生产 8437 份瓶颈报告里 `cr3_source="akshare"` 为 **0**。
- 且生产 32 次分析中 **24 次是美股** —— A 股优先的锚点对大多数分析本就不生效。

**改法（`_analyze_node` 一处）**

system prompt 的 supply_demand_gap 刻度本来就按「扩产周期」分档（1-2 年 / 2-3 年 / >3 年），
缺的只是把事实递过去。6A 让拆解阶段采集了 `capacity_lead_time_months`，于是：

- 有扩产周期 → `## 供需缺口锚点`，写明「估计值，非披露数据」，要求对照刻度打分、不符须说明；
- 没有 → `## 供需缺口：无数据锚`，要求保守打分并注明「估算」（即审查对美股的建议，
  现在对所有无数据节点生效，不分市场）。
- 扩产周期从 6A 的「结构性事实」段移出，只出现在锚点段一次，不在同一 prompt 里说两遍。

**刻意没做**：新增 `_compute_supply_demand_anchor` 的财报四路数据。数据源在生产不可达，
接上只会多一次 22 秒超时后降级回同一句「无数据锚」。已用 `ponytail:` 注释标出：
真实产能利用率数据源接通后再加第二路锚。

**已知局限**：锚点是 LLM 拆解时的**估计**，不是披露数据。它的作用是让打分与拆解
自洽、让「无数据」被明说，而不是替代真实供需数据。老链（6A 之前拆解）没有这个字段，
全部走「无数据锚」分支 —— 这正是它们的真实状态。

**验证**

- 哨兵 2 条（`TestSupplyDemandAnchor`）。非空性：去掉 `{sdg_block}` 后 **2/2 转红**，恢复后转绿。
- `test_lead_time_becomes_the_anchor` 同时断言「30 个月」全 prompt 只出现一次，防回归成双写。

```
python -m pytest tests/test_batch6a_sentinels.py tests/test_bottleneck.py -q → 58 passed
ruff bottleneck.py → 5 findings，与 6A 后同一集合，新增 0
```

### 6C 候选池：审查要的「扩池」在生产数据上是负收益，真正的假闭环是 `sources` 没人读

**审查原话（C-5 / P2-8）**：`_extract_chain_candidates` 只取本节点 + 直接上游，应扩到同层竞争 +
2 跳上游；并与真实板块成分股交叉核对，两边都有的升权、只在一边的标注来源分歧。

**用生产 32 条链（8452 个非根节点）重放后逐条核实**

| 方案 | 实测 | 结论 |
|---|---|---|
| 同层节点并入 | 新增候选**中位数 +199 家**、最多 +629；抽样「注射笔用弹簧」的同层是针头/活塞/外壳/剂量控制 —— **互补件，不是竞品** | 不做。`layer` 是深度，不是「同一环节」；并入等于把整层公司灌进一个节点 |
| 2 跳上游并入 | 仅 21.2% 节点有增量；本节点+直接上游为 0 家、而 2 跳能补上的**只有 13 个节点** | 不做。收益面太窄，且上游的上游是供应商的供应商，与本环节瓶颈关系更弱 |
| 来源交叉 / 升权 | `_merge_supplier` 已按 ticker 写 `sources`（P1-6），但**全仓零读取方**；`search()` 末尾按合并顺序（LLM 优先）`[:max_results]` 截断 | **这才是真缺口**，做 |

另一个审查没提的事实：生产 `supplier_scorecards` 里被选中的瓶颈节点名有 **195/310 是逗号拼接的
多节点名**（`get_node` 查不到），这些节点图谱源本来就取不到候选 —— 扩 `target_nodes` 对它们零效果。
属于拆解/合并侧的命名问题，不在本批范围，记录备查。

**改法（两处消费 `sources`）**

1. `search()` 截断前稳定排序：有外部数据（`akshare` 板块成分股 / `gangtise` 指标选股）核对过的票排前，
   其次按命中源数。`chain` 也是拆解阶段 LLM 自报，**不算外部核对** —— `llm+chain` 双命中不得冒充交叉验证。
   同档内保持原 LLM 优先级。`EXTERNAL_SOURCES` 常量定义在 `supplier_search.py`。
2. 评估 prompt 基本面加一行「候选来源」：含外部核对 / 均为 LLM 自报（环节归属未经外部数据核对）。
   `sources` 为空（用户手动输入 ticker、反向分析等不经检索的路径）**不标**，免得把用户指定误标成 LLM 自报。

**已知局限**：生产现存 375 张评分卡的 `source` 只有 `llm`/`chain`，外部源在美股天然为空、A 股 akshare
板块检索在生产基本不可达（见 6B）。所以本改动在当前生产上的主要效果是**评估 prompt 如实告知「环节归属未经核对」**；
排序升权要等外部源真正命中时才生效 —— 这是正确的顺序：先让系统说真话，数据源接通后自动受益。

**验证**

- 哨兵 6 条（`tests/test_batch6c_sentinels.py`，gangtise 已 monkeypatch 不碰网络）。
- 非空性三向：去掉排序 → 截断哨兵 **2 红**；换成草率的「只按源数排」→ `llm+chain` 冒充外部的哨兵 **红**；
  去掉 prompt 标注 → prompt 哨兵 **2 红**。均恢复后转绿。
- ruff：`supplier_search.py` / `supplier_eval.py` 新增 0 条。

### 6D 中心校准：删掉强制分布；**不做**批次中心平移（生产数据证明它会抹掉真信号）

**审查原话（B-1 / P1-10）**：供应商层无中心校准，LLM 偏严时 quality 系统性偏低；又因强制分布要求
与「独立评分」冲突，逼出来的极端分落在 moat 维度上。

**删强制分布：做。** `supplier_eval.md` 的「9 个维度至少 2 个 ≤4 或 ≥8 / 全在 5-7 分说明不够深入」两条删除，
换成明确的反向指令「不要为了拉开差异编造极端分」。**同段 6 条数据触发的锚定规则保留**（PE>100 → valuation≤4 等）——
它们由真实数据触发，是锚，不是凑分。生产 375 张卡里有 8 张「核心 5 维 0 极端、极端分全靠 moat 凑满 2 个」，
与审查描述的机制吻合。

**中心校准：不做。生产实测三条理由**

1. **偏移里有真信号**：22 次分析的 quality 均值跨 5.62 ~ 7.77。把每批平移到同一中心，等于断言「每个产业链的
   供应商平均质量相同」—— A 股小市值链与美股 AI 算力链的差距会被抹平。区分「裁判偏严」与「这批确实弱」需要
   **每批评一组固定参照公司**（额外 LLM 成本 + 参照集维护），是设计项，不是补丁。
2. **核心维度已有绝对锚**：`financial_health` / `valuation` 以 0.7、`market_position` 以 0.5 的权重与真实数据
   `_blend`（`supplier_eval.py:457-459`），平移会把这部分绝对锚一起挪走。
3. **下游有绝对阈值**：`ShortlistConfig.min_overall_score`（`phases.py:599`）与「高质量」标签 `overall_score >= 7`
   （`api.py:427`）都按绝对分读；按批次平移会让同一家公司在不同分析里过/不过线。
   重放：把每批 quality 均值平移到 6.5 后，22 次分析中 **11 次排序变化、2 次 Top5 换人** —— 改动不小，却没有依据说新排序更对。

**已知局限（如实记录）**：`FinalScorer` 里 alpha 的 `bottleneck_score` 来自 z-score 过的瓶颈层、quality 是绝对分，
两个因子的尺度来源确实不同。本批没有解决它，只是拒绝用一个会引入新错误的办法去「解决」。

**验证**：哨兵 2 条（`tests/test_batch6d_sentinels.py`）—— 强制分布文字不在、6 条数据锚仍在、反向指令在。
非空性：还原 prompt → **2/2 红**，恢复后转绿。

### 6E `main_business` → FactCheck 可判定规则：用生产数据试了阈值，**结论是不上**

上一轮复核（P2-4）的结论是「缺的不是接线，是可判定规则」。本批按约定由我来定规则与阈值，
先在生产数据上试，再决定上不上。

**覆盖面**：生产 375 张评分卡里 `main_business.segments` 非空的只有 **20 张（5.3%）**，全部是 A 股 Gangtise 路径；
美股为 0。

**试的规则**：主营分部名与瓶颈节点名做 2-gram 重叠（剔除「系统/设备/产品/业务」等通用字），
命中分部营收占比 < 阈值 → 「营收不来自瓶颈环节」。20 张卡的重放结果（节选）：

| 公司 | 瓶颈节点 | 匹配占比 | 实际 |
|---|---|---|---|
| 中微公司 | 蚀刻设备 | **0%** | 主营就是刻蚀设备，但分部只有一行「主营业务」—— **误判** |
| 宇瞳光学 | 光学镜头 | **2.3%** | 「安防类 50.55%」就是安防镜头 —— **误判** |
| 新益昌 | 精密运动控制系统 | 0% | 分部按下游（LED/半导体设备）切，不按部件切 —— 无法判定 |
| 亚翔集成 | 离子注入机 | 0% | 洁净室工程，确实不相关 —— 正确 |
| 扬杰科技 | 功率半导体 | 97.8% | 正确 |

**不上的理由**：分部口径是公司自定的（按产品 / 按下游 / 只有一行「主营业务」），与产业链节点名不在同一套词表里，
字符串匹配在 20 个样本里就有 ≥3 个明确误判。而 FactCheck 的输出是**硬门禁**（fatal → REJECT、mismatch 累计 → REVIEW），
一条会冤枉中微公司的规则比没有规则更坏 —— 正是本工作区反复在堵的「规则没数据支撑却产生确定性结论」。

**保持现状**：`main_business` 继续渲染进评估 prompt（`supplier_eval.py:235-245`），由 LLM 做语义判断 —— 这是它当前唯一可靠的用法。
**什么时候再做**：分部 → 产业链节点有了映射表（或 Gangtise 提供按产品细分的口径），再把占比阈值规则接进 `_CLAIM_RULES`。
（「不上」是我的判断，可推翻；推翻时上面的重放脚本可直接复用。）

本项无代码改动。

### Batch 6 收口门禁

```
python -m pytest -q  → 2568 passed, 5 skipped in 486.77s（6A~6D 全部改动后的最终状态）
```

---

## 批后复核（2026-09-28）：生产实跑 5cbf9130 验收 + A股财务趋势取错期修复

**实跑验收**（AI计算中心/机房电源，A股，4 阶段完成）：6A 约束字段 103 节点中 93–102 有值、202 条 link notes 全非空；
6B 供需缺口 reasoning 普遍引用「产能扩张周期约 N 个月（拆解阶段估计）」；6C `sources` 全部落库（llm 16/chain 2/llm+chain 2）；
6D 总分 4.2–8.3 无硬凑极端分。外部源本轮 0 命中：AKShare 板块列表服务器不可达；Gangtise 选股 curated 表只收中信一级
行业词，「光刻胶/激光器」等具体环节名全部 `sector_id_for → None` 静默降级（已知天花板，见 gangtise_sector_ids ponytail）。

**新发现（自 4495daf 起存在）**：`stock_financial_abstract_ths` 按报告期**升序**返回（生产实测 603986 第 0 行 2011-12-31，
末行 2026-06-30），而 `_fetch_astock_financial` 用 `df.head(8)` / `df.iloc[0]` 按「最新在前」取数 → 本轮 20 家趋势区间全落在
2004–2022，`trend_bonus` +0.4~+2.5 与喂给 LLM 的营收加速度全错；主数字被 gangtise overlay 覆盖才显得正常（overlay 失败时
主数字也会退回上市首期）。老测试只 mock 1 行，覆盖不到排序。

**修复**：取数前按报告期倒序（3 行）。哨兵 `test_ths_ascending_rows_take_latest_quarters`：升序 12 期输入 → 最新期
2026-06-30、趋势 8 期不含 201x；回退修复即红。全量 2569 passed, 5 skipped。

## DeepSeek 超时切换 vs 配置中心测试结论不一致（2026-09-29）

**现象**：deepseek_company/deepseek-v4-pro 频繁「超时→自动替换→超时频发禁用」，配置中心点测试却每次都通过。

**生产实证**：
- 真实长输出实测 99.2s（首 chunk 0.2s，生成本身慢），配置中心 "hi" 探活 2.7s；
- `model_call_stats` 均延迟 09-23 84s / 09-25 72s / 09-27 65s；09-28 7 调 7 超时，全部精确卡在 60s；
- 20:10:31 同秒 ~4 个并发超时 → 一波就打满 3 击阈值 → 持久 `disabled_timeout`。

**结论**：两个猜测都成立，外加第三个放大器。
1. 判定过严：`_CAND_TIMEOUT` 60s 是**总时长**上限，对推理/Pro 档长输出天然不够；
2. 测试过松：短问答只证「连得上」，证不了「长输出不超时」；
3. 并发放大：同一波扇出同时超时，每个都记一击 → 一次慢 = 三次挂。

**修复**（最小改动）：
- `fallback.cand_timeout(model)`：推理/Pro 档（reasoner / -pro / thinking / -r1 / o1-o4 / gpt-5 / kimi-k）上限 180s（`BH_LLM_SLOW_TIMEOUT`），其余仍 60s。SDK timeout（factory）/候选 wait_for/委员会外壳/配置测试与恢复**同一口径**。
- `provider_gate`：距上一击 <30s 的超时并入上一击（`_TIMEOUT_BURST`），分波超时照常累计 3 击禁用。
- 配置中心测试：回报本次耗时 + 超时上限 + 近 7 日真实调用均耗时；均耗时 >80% 上限时给出警示，让「测试通过」与「生产超时」可直接对照。

**刻意未做**：按遥测自适应超时——超时样本被截断在上限处，均值自带天花板，自适应会自我锁死；等有「未截断的长尾延迟」数据再考虑。

**哨兵**：`test_concurrent_timeouts_count_as_one_strike`、`test_slow_reasoning_model_gets_longer_timeout`、`test_fast_model_on_slow_budget_not_cut`（去掉修复即红）。

## 美股决策中心 9-25 起零成交复核（2026-09-29）

**现象**：美股从 9-25 起没有任何一笔成交，股票仓位 14.6%，远低于 51.1% 的下限。

**生产实证与根因**（按影响排序）：
1. **挂单死锁（主因）**：L4 会跳过所有「已有挂单」的票。旧挂单是 LLM 按「理想价」挂的，普遍低于现价 3~20%（TSM 420~435 vs ~450、NVDA 210 vs 225、MRVL 210 vs 262、MSFT 490 vs 516、SNPS 380 vs 426），市场走高后成交不了，却要占位 14 天（10-05~10-09 才到期）。缺口驱动每天点名 NVDA/TSM/MRVL/SNPS/MSFT，每天都被「跳过已有挂单」挡掉。
2. **行情「假新鲜」**：yfinance 被 429 限流，兜底源 akshare_us 在北京 05:30 还没出当日 bar，但照样返回「抓取成功」并把 fetched_at 刷新，旧的新鲜度闸看不出问题。9-28 的 47 票里只有 12 票有当日 bar。
3. **投委会独立性否决**：glm-5.3 / qwen3.8-max 在投委会长 prompt 下均耗时 57/66s，60s 上限下 7 日成功率只有 22%/28%，超时被禁后委员集中到 deepseek，9-28 的 CDNS 因此被「独立性不足」否决。其余否决（CDNS 9-26、AMZN 9-28 的 needs_discussion）是正常结论。
4. **挂单日志刷屏**：挂单轮询每小时调一次 `rest_execution`，每次都记一条「转挂单，到期 <新算的日期>」，而这个日期并不落库，日志里全是假到期日。

**修复**：
- `decision_engine._supersede_stale_resting`：本轮决策又点名某票，且其旧挂单在不利侧偏离现价超过 3%（`BH_RESTING_SUPERSEDE_GAP_PCT`，买单挂太低 / 卖单挂太高）→ 作废旧挂单，让位本轮新计划；贴近现价的挂单保留。
- `_ensure_price_freshness`：以同批票的最新 bar 日期为基准，落后的票单独补刷一次。补刷不进「过半失败即硬停」的判定（落后一天不值得停掉决策链），也不依赖交易日历（节假日全体一起停，就不会有落后者）。
- `fallback._SLOW_PATTERNS` 加入 `glm-5`、`-max`，走 180s 慢档上限。
- `store_decision.rest_execution`：只在首次转挂单时返回 True 并记日志。

**运维动作（非代码）**：部署后需要在 AI 配置中心对 glm / qwen 执行「测试并恢复」，解除 disabled_timeout。

**刻意未做**：挂单阈值按 ATR 缩放（见 ponytail 注释）。

**哨兵**：`test_stale_resting_yields_to_new_decision`、`test_rest_logs_only_first_time`、`test_lagging_bar_date_gets_refreshed_without_hard_stop`。全量 2577 passed, 5 skipped。

### 补充：新挂单价源头收紧（2026-09-29）

**漏洞**：「旧挂单让位」只解决了 14 天冻结。如果 LLM 新一轮仍把买单挂在现价下 10%，新单同样成交不了，下轮又被作废、再重挂，形成空转。

**修复**：`decision_engine._clamp_limit_price` 在 L4 计划落库前执行。挂单价在不利侧偏离现价超过同一阈值（`BH_RESTING_SUPERSEDE_GAP_PCT`，默认 3%）时，压回边界（买单 ≥ 现价 × 0.97，卖单 ≤ 现价 × 1.03），同步重算 estimated_amount，原价记在 `limit_clamped_from` 以便复盘。有利侧（买价 ≥ 现价 / 卖价 ≤ 现价）不动，执行器按真实市价立即成交。两处用同一阈值，保证「新挂单不会一挂上就满足作废条件」。

**已知边界**：收紧发生在 L4 预校验之后，买单金额最多比校验时高约 3%；执行器成交前会按真实市价重新做约束校验，不会越限成交。

**哨兵**：`test_new_limit_clamped_near_market`（TSM 420→436.5、AVGO 卖 380→362.56，NVDA 贴近 / META 有利侧 / hold 不动）。全量 2578 passed, 5 skipped。

## 观察池入池上市守卫（2026-09-30）

**问题**：RNECY（瑞萨 ADR，场外粉单）与 HAM（滨松光子东京代码，美股查无）于 2026-08-15 经 phase4「加入观察池」入池，行情永远拉不到。

**根因**：
- 供应链分析的上游守卫（692ba63，09-01：拆解形态过滤 + 美股主板报价校验 + 无报价剔除）晚于这两只入池。
- 更根本的是，唯一写入口 `POST /api/watchlist` → `store.add` 只有 `validate_ticker` 格式校验：RNECY/HAM 格式合法即放行。
- 前端把分析产出的 ticker 原样提交，没有任何上市核验。
- 三处入口（phase4 / reverse / reverse_cross）都走这一个 API，守卫放在这里一次覆盖全部。

**修复**（`web/watchlist_api.py::_verify_listing`，在 add API 入库前调用）：
- 先过 `validate_ticker` 格式校验，防止拼接多码注入行情 URL。
- 市场只收 us_stock / a_stock，其余（含 hk_stock）一律拒绝。
- A股：代码须为 6 位且前缀在 60/68/00/30/4/8/920；拒 900/200 B股、指数码等。
- 腾讯 `qt.gtimg.cn` 实时核验：
  - 查无（`v_pv_none_match`）即拒。
  - 美股 field[2] 交易所后缀须 ∈ {OQ, N, AM}；`.PS` 粉单即拒，并给出中文原因。
- fail-closed：行情源不可达时拒绝入池并提示稍后重试（宁拒勿脏）。
- 已在池的 ticker 跳过核验，仍走原「已存在」提示。
- 前端「失败」按钮悬停 title 显示后端拒因。

**实网验证**：
- 放行：NVDA / TSM / BRK-B / SPY / 600519 / 688981 / 430047。
- 拒绝：RNECY(PS) / HAM(查无) / HPHTY(PS) / 999999。

**附带**：`test_batch5_sentinels` 把 `2026-09-30` 硬编码为「未来日期」，今天到期失败，改为 2099-09-30。

**生产存量脏数据**（待用户确认处置）：
- RNECY、HAM（用户 903115）。
- 7 月反向分析遗留的小写 ticker：pltr/orcl/sndk/mu/tsla/spcx/lite（用户 007172）与 avav（d32a4a），均早于 normalize_ticker 入池。

## 模拟交易权益曲线时段切换无效（2026-09-30）

- 现象：30天/90天/半年/一年切换，曲线形状不变。
- 根因：`/api/trading/account/equity-history` 只按「有成交的日子」累加现金流，`history[-days:]` 截的是**成交日个数**而非自然日（生产美股 16 个成交日、A股 18 个，任意时段都 <30，全部返回同一组点）。而且它只算现金、不盯市，买入即表现为权益下跌，曲线本身也是错的。
- 修复：改为逐日盯市：
  - 权益 = 现金 + Σ 持仓 × 共享日线收盘价；
  - 现金按成交（含 0.1% 佣金）+ 出入金回放，期初现金 = initial_capital − 累计入金；
  - 轴取窗口内的交易日 + 今天。
- 验证：生产美股 30/90/180 天分别返回 21/62/64 点，末点 1,009,442 ≈ 账户权益 1,009,518；A股末点与账户权益一致。新增回归 `test_equity_history_range_and_mark_to_market`，全量 2589 passed。
