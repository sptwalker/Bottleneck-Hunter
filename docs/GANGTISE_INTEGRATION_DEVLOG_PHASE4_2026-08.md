# Gangtise 四期接线 开发日志（补两缺口：VIP 顾问推理期主动取数 + 逐标的研报/估值分位入清单）

> 完成日期：2026-08-31 · 承接：`GANGTISE_INTEGRATION_DEVLOG_PHASE3_2026-08.md`（三期行情路径）
> 验收：`python -m pytest -q` 全绿（**1503 passed, 4 skipped**）；研报（美股 NVDA + A股 600519 各取回近月真实研报、精简 3 篇入协商）、估值分位（A股 600519 真数据、美股正确无）三条链路以**真实返回数据**佐证（活体实测，非代码桩）。
> 铁律遵守：ak/sk 机密**未入任何代码/文档/日志/提交**（`.claude/` 全目录 gitignore，`.authorization` 仅运行时读取，验证脚本用完即删）；受控全局 key 仅走 `resolve_gangtise_credentials` 的 admin 双开关；**证伪即诚实留缺省**；最小 diff、无新建管线。

三期补齐了行情 fetcher 路径后，本期收口两个**能力已就绪却未被 AI 消费**的缺口——都属"算法未接"而非"数据不可得"，与 VIP 价值评估结论一致（详见 `VIP_ADVISOR_VALUE_ASSESSMENT_2026-08.md`）。

---

## 两个缺口

- **缺口①：VIP 顾问评审无法推理期主动补数据。** `vip/advisory.py::generate_account_advisory` 生成逐仓建议时，只能读 L1 宏观快照 + 预先归集的证据，草案模型**发不出 `[[DATA_REQ]]`** → 缺研报/估值/财务时只能凭既有上下文硬答。而分析流程/决策链早已通过 `ai_tools.negotiate` 支持推理期主动拉数。
- **缺口②：Gangtise 逐标的能力未进 DATA_REQ 清单。** `_CAP_LABELS` 未收录 `CAP_RESEARCH`/`CAP_VALUATION`，即便 hub 侧 `GangtiseProvider` 已能供数，`build_manifest` 也不会把它们列给模型（"清单即承诺，未收录不暴露"）→ AI 无从主动请求 Gangtise 的研报/估值分位。

---

## 改动清单（最小 diff）

### 1　`data_provider/ai_tools.py` — 逐标的研报/估值分位入清单（缺口②）

- hub import 块补 `CAP_RESEARCH`、`CAP_VALUATION`。
- `_CAP_LABELS` 增两条：
  - `CAP_RESEARCH → {label:"券商研报", returns:"近月中/外资研报标题+摘要要点+券商/分析师/日期/深度标签（取最近3篇，Gangtise）"}`（**不写"评级/目标价"**——见 §5 诚实兑现修正）
  - `CAP_VALUATION → {label:"估值分位", returns:"PE/PB/PEG 近3年窗内分位（仅A股，Gangtise）"}`
- **刻意不收录 `CAP_MACRO_EDB`/`CAP_KB`**：EDB 非"按 ticker 取一条"（ticker 槽被忽略）、KB 是关键词检索，均不符合 DATA_REQ 的**逐标的**协议（`_validate` 强制要求 ticker）→ 列了也兑现不了，违"清单即承诺"。注释同步说明。
- 门控天然正确：`build_manifest` 取 `available_capabilities ∩ _CAP_LABELS`，市场差异由 `GangtiseProvider.supports()` 决定 → **研报两市可见、估值分位仅 A股可见**（美股 VALUATION 返 120001，供应商已不 supports）。

### 2　`vip/advisory.py` — 草案模型接入 negotiate 协商环（缺口①）

将原直呼 `llm.ainvoke(prompt)` 的草案生成替换为 `ai_tools.negotiate` 包裹：

```python
holdings_tickers = [h.get("ticker") for h in dossier.get("holdings", []) if h.get("ticker")]
adv_market = getattr(wl_store, "_market", "") or ""
try:
    from bottleneck_hunter.data_provider import ai_tools
    draft_text, _fetch_log, _ = await ai_tools.negotiate(
        _ask, prompt, market=adv_market, user_id=user_id, allowed_tickers=holdings_tickers)
except Exception:  # noqa: BLE001  协商环/取数异常绝不阻断建议生成
    draft_text = await _ask(prompt)
draft = _validate_draft(draft_text)
```

- **可查范围硬约束**：`allowed_tickers` 只放本账户**持仓标的** → 顾问推理期能补研报/估值/财务，但绝拉不到账户外标的（越权即被 negotiate 内部过滤）。
- **诚实降级**：协商环或取数链异常 → 回退无补数据单趟 `_ask`，建议照常产出（与本文件其它缺省降级同风格，无 module-level logger 即静默降级）。至多 2 轮 8 次取数（negotiate 内建上限）。

### 3　`data_provider/providers.py` — 修研报保留窗（暴露的既有潜伏 bug）

缺口②接线后活体实测发现 `hub.fetch(CAP_RESEARCH, ...)` **恒空**。逐层探针根因定位：

- `gc.fetch_research` 抛 `GangtiseError: research HTTP 400`，报文 `code:110003 TIME_RANGE_EXCEEDED`。
- 原始端点 1 月窗探针全 HTTP 200 有真数据 → 端点/鉴权/参数无误。
- 窗口二分：`start=今日-30 → OK`、`今日-31 → 400`；与 endTime 无关 → **是相对 now 的保留限（约 30 天），非跨度限**。broker-report/foreign-report 的 `getList` 只服务近月发布。
- 而 `_fetch_research_sync` 请求 **180 天** → 必越限 → 从接线起研报就从未真正可用（潜伏 bug，被本期消费方暴露）。

**修法**：`_RESEARCH_LOOKBACK_DAYS = 28`（保留限内留 2 天余量，因 `_format_time_range_ms` 会把 end 外扩 1 天）。对 AI 顾问而言近月研报本就是决策最相关部分，非功能损失。

> 附注：`_fetch_research_sync` 的"无标签退回"分支（tagged 无结果时退回取全部）此前因 tagged 调用直接 raise 而永不触达；窗口修好后不再 raise，该退回逻辑恢复生效。

### 4　`data_provider/providers.py` — 研报回注精简（"能拉到"≠"能读懂"）

窗口修好、研报能真取回后，进一步活体实测发现第二层缺陷：**协商环把研报截没了**。

- `execute_requests` 回注前有 `[:_RESULT_CHARS]`（=1200）硬截断防上下文膨胀。
- 而单篇 `brief` 实测 1~2.6k 字（600519 五篇为 1292/2637/961/1703/1561），原 `reports[:5]` 全量 JSON ≈ **9785 字** → 截断后模型**只读到首篇标题 + 半句摘要**，其余 4 篇及所有券商/日期尽失。
- 结果：AI"能主动拉到研报"但"读不到内容"，无法真正完成解读——与用户诉求正相反。

**修法**：`_fetch_research_sync` 从"回全量 5 篇原始 dict"改为**每篇只留决策相关字段**（`title / brief[:180] / broker / analyst / date / tags`）、取**最近 3 篇**。实测两市 JSON 均确定性落入 1200：**A股 976 字、美股 1140 字，均 JSON 闭合**，模型可完整读到 3 篇研报的标题+结论摘要+出处。外资报优先 `title_zh/brief_zh` 中文译文。

> 为何不抬 `_RESULT_CHARS`：该常量是**全能力**回注上限，抬它会放大所有 cap 的上下文占用（跨切面副作用），且研报单篇原文长尾不可控。精简字段 + 限篇是**确定性**方案，与 ponytail 最小影响面一致。

### 5　`data_provider/ai_tools.py` — 清单标签诚实兑现（去"评级/目标价"）

缺口②初版标签写 `"…标题/评级/目标价/摘要…"`，但研报单篇结构化字段（`_parse_research_body`）**只有** `title/brief/publish_date/securities/industries/category/broker/analyst/llm_tags/page_number/foreign` —— **无评级、无目标价结构化字段**（评级/目标价仅散见于自由文本 `brief` 内，不保证有、不可程序化提取）。

标签承诺了拿不出的字段 → 违"清单即承诺"、正是备忘录记的"over-promise 病"。**修正**为 `"近月中/外资研报标题+摘要要点+券商/分析师/日期/深度标签（取最近3篇，Gangtise）"`——**只承诺确实回注的字段**。

---

## 验收证据（真实数据活体实测）

清单门控 + hub 真取（临时脚本，ak/sk 仅内存注入 `resolve_gangtise_credentials`，用完即删）：

```
US manifest research: True | valuation: False      # 研报两市可见、估值仅A股
A  manifest research: True | valuation: True
600519/a_stock: n=3 chars=976  fit1200=True json_closed=True   # 国海/国海/西南，各含标题+180字摘要
NVDA/us_stock:  n=3 chars=1140 fit1200=True json_closed=True   # 瑞银/瑞银/开普勒，中文译标题+摘要
valuation A 600519: DATA keys=[data_source, pe_ttm, pe_ttm_percentile, pb_mrq, pb_mrq_percentile, peg]
VERIFY OK
```

> **关键佐证**：两市研报 JSON 均 <1200 且闭合 → 协商环回注**不再截断**，模型能完整读到 3 篇研报全部字段。这才是"能拉到 **且** 能解读"的实证（仅"chars<9785"证明拉到，"fit1200+closed"才证明读得全）。

全量测试：**1503 passed, 4 skipped**（`ai_tools` / `advisory` 自检均过；`tests/test_ai_data_tools.py` 的 `caps=={CAP_QUOTE,CAP_EARNINGS}` 断言用假 hub，新增标签不受影响）。

---

## 为什么这么切范围（对齐 ponytail/YAGNI）

- **只接已就绪能力**：研报/估值分位在 hub 侧早已能供数，本期只做"暴露给 AI + 让顾问会用"，零新建供应商/管线。
- **EDB/KB 刻意不接 DATA_REQ**：协议是逐标的，二者非逐标的 → 接了就是无法兑现的死承诺。EDB 已在 L1 宏观管线消费、KB 走独立检索，各得其所。
- **研报窗从 180→28 是修 bug 非砍需求**：180 天从来就取不到，28 天是端点真实能力边界内的最大可用窗。
- **精简回注 3 篇是"让解读真发生"非砍数据**：全量 5 篇被 1200 截断后模型只读到半篇，等于没读；精简字段+限篇让 3 篇**完整**进模型，可读性从 0 变满。"能拉到"是链路通，"读得全"才是需求达成。
