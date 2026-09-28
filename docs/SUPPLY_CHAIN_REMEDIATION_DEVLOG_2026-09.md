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
