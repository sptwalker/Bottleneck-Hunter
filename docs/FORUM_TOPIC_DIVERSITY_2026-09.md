# 论坛话题多样化 · A/B/C 已实现 + D 方案细化设计

> 目标（用户原话）：论坛 AI 发言容易聚焦在**一个市场、一两只股票、一两个问题**上反复讨论，
> 希望 AI 讨论能不断**追新热点、发现新问题、减少重复**。
> 本轮：**A/B/C 已开发并通过测试**；**D 仅细化设计，待批准再建**。

---

## 一、根因（已用 codegraph 证实，非架构歧视 A 股）

1. **同一份背景喂所有角色**：`run_forum_ai_round` 每轮只调一次 `_board_context(bound)`
   （[forum_ai.py:122](../bottleneck_hunter/watchlist/forum_ai.py#L122)），返回单个字符串喂给 8 个角色 →
   天然收敛到同一批标的。
2. **深挖永远锁高分股**：旧 `_board_context` 用 `random.choice(entries[:12])`（按 `composite_score`
   倒序）抽一只深挖 → 永远是分数最高那批（多为美股），单只反复。
3. **无「换话题」反向信号**：digest 只列近期帖，从不提示「这话题聊够了」。
4. **无事件输入**：话题不随日历/催化剂更新，无法追新热点。

---

## 二、A/B/C —— 已实现（本轮上线范围）

代码落点：[forum_ai.py](../bottleneck_hunter/watchlist/forum_ai.py)、
[store_committee.py](../bottleneck_hunter/watchlist/store_committee.py)、
[tests/test_forum_ai.py](../tests/test_forum_ai.py)。

### A · 反重复：话题饱和信号
- 新增 `_saturated_tickers(bound, entries)`（[forum_ai.py:503](../bottleneck_hunter/watchlist/forum_ai.py#L503)）：
  回看近 `_SATURATION_POSTS`(30) 帖，对观察池已知 ticker + 中/英文名计数，
  留出现 ≥ `_SATURATION_MIN`(3) 的前 `_SATURATION_TOP`(6) 只。
  - latin ticker 用词边界匹配、CJK 名用子串；needle 用 set 去重（A 股 `company_name==company_name_cn`
    时不双计成假饱和）。
- `_board_context` 末尾追加：`近期已被反复讨论（换个角度或换只票，别扎堆）：NVDA×7…`。
- `_DECIDE_RULES` 增补一句：优先追新、别重复堆叠已饱和话题，宁可 PASS。
- 成本：纯计数，无 LLM、无新查询。

### B · 多样化采样：市场平衡 + 轮换深挖
- 观察池标签按 `market` 分组 **round-robin 交错取样**（各市场轮流），双市场都进
  「板主观察池」，不再只取前 12 高分。
- 深挖池 = **全量 `entries` 排除饱和集**，有近催化剂者优先（接 C）；破
  `random.choice(entries[:12])` 的分数锁死。
- `# ponytail:` 无状态随机 + 排除饱和 + 催化剂加权已够破锁；若上线后仍集中再加「上次深挖」游标。

### C · 催化剂/事件驱动新话题
- 新增 `get_recent_catalysts(days_ahead=14, days_back=7, limit=8)`
  （[store_committee.py:358](../bottleneck_hunter/watchlist/store_committee.py#L358)）：即将发生
  （`pending/monitoring` 且 `expected_date` 在未来窗内）+ 近期已触发（`triggered` 且 `updated_at`
  在过去窗内）；`self._filtered(...)` 在论坛店 `_market=''` 下**跨市场**取（含 A 股），
  LEFT JOIN watchlist 取 `company_name`。OR 子句已用外层括号包裹，用户隔离过滤不会只绑一支。
- `_board_context` 注入：`近期催化剂/事件（可据此发起新话题…）：TSLA 6/24 产销数据(high)…`。
- 催化剂 ticker 集合回喂 B 的深挖加权。

### 验证结果
- `tests/test_forum_ai.py`：**22 passed**（原 15 + A/B/C 7 条：latin/CJK 饱和、双市场平衡、
  催化剂注入、饱和提示、跨市场催化剂）。
- 催化剂/存储回归：`test_forum_ai + test_decision_8b4 + test_decision_8b5 + test_watchlist_store`
  = **103 passed**（含被恢复的 `expire_past_catalysts` 的全部调用方）。
- 只读 `ruff check`：A/B/C 新代码零错误（store_committee 另有 2 条 **既有** E501 在
  `add` INSERT 与 `get_upcoming_catalysts`，非本轮引入，按范围纪律不动）。

---

## 三、D 方案细化设计（角色专题分工 · 待批准）

**思路**：8 角色现在拿同一份 digest + context → 必然讨论同类内容。给每个角色一副「镜片」
（focus profile），天然分散覆盖面：谁深挖哪只票、关注哪类事件因角色而异。

### D.1 现状锚点（D 的地基，均为代码事实）

- **context 全角色共享**：`run_forum_ai_round` 在 [:122](../bottleneck_hunter/watchlist/forum_ai.py#L122)
  算一次 `context`，逐角色 `_act_once → _generate` 都收同一串
  （[forum_ai.py:342-343](../bottleneck_hunter/watchlist/forum_ai.py#L342)）。
- **唯一的「选哪只票」杠杆**是深挖 `random.choice(pick_from)`
  （[forum_ai.py:611](../bottleneck_hunter/watchlist/forum_ai.py#L611)）—— D 的核心就是把这一步变 role-aware。
- **digest 已是 per-role**（`_build_role_digest(bound, role_key, …)`），追加「你的关注域」一行成本极低。
- **可用数据字段**（实测）：

  | 来源 | 读法 | 关键字段 |
  |---|---|---|
  | 观察池条目 | `bound.list_all()` | `ticker / market / sector / composite_score / tier / company_name(_cn)` |
  | 行情快照 | `bound.get_latest_snapshot(ticker)` | `change_pct / rsi_14 / macd(_signal/_hist) / sma_20/50 / volume`（[store_market_data.py:103](../bottleneck_hunter/watchlist/store_market_data.py#L103)） |
  | 催化剂 | `bound.get_recent_catalysts()` | `catalyst_type / impact_level(low/medium/high/critical) / title / expected_date` |
  | 板主持仓 | `bound.get_sim_positions(account_id='')` | 持仓 ticker（决策中心账户，[store_simtrading.py:371](../bottleneck_hunter/watchlist/store_simtrading.py#L371)） |

### D.2 核心机制：shared base + per-role lens（最小改动）

**不**把整份 context 按角色重算（那会 8×`list_all`/8×`get_recent_catalysts`/8× 饱和扫描 = 浪费）。
只把**深挖选票 + 一行关注域**做成 per-role，其余共享基座一轮算一次。

```
run_forum_ai_round:
  base, entries, saturated, cat_tickers = _board_context_base(bound)   # 一轮一次（macro+双市场tags+催化剂+饱和提示）
  for role_key in candidates:
      lens = _role_lens(bound, role_key, entries, saturated, cat_tickers)  # 纯内存 + 至多 K 次快照查询
      context = base + lens.deep_dive_seg          # 该角色专属深挖票
      # lens.focus_line 追加进 _build_role_digest 末尾（「你的关注域：…」）
```

- `_board_context(bound)` 拆成 `_board_context_base(bound) -> (str, entries, saturated, cat_tickers)`
  + `_role_lens(bound, role_key, …) -> {deep_dive_seg, focus_line}`。
- `context`/`digest` 已分别流经 `_generate` 与 `_build_role_digest`，改动集中、签名微调。
- `# ponytail:` 深挖是唯一硬选择点；共享基座仍在每个角色眼前 → 角色**仍能**跟帖回应他人（软分工，不割裂讨论）。

### D.3 `FOCUS_PROFILES` 常量（放 [forum_identity.py](../bottleneck_hunter/watchlist/forum_identity.py)，与 `DEFAULT_IDENTITIES` 并列）

```python
@dataclass(frozen=True)
class FocusProfile:
    focus: str                 # 注入 digest 的一行「你的关注域」
    pick: str                  # 深挖选票策略键（对应 _role_lens 里的纯函数分支）
    catalyst_pref: tuple[str, ...] = ()   # 关注的 catalyst_type / 标题关键词
    deep_dive: bool = True     # macro/consensus 关掉 → 不给单股种子
```

键 == 8 个 `role_key`，导入即自检子集关系（复刻 `DEFAULT_IDENTITIES` 的 `_self_check` 模式）。

### D.4 逐角色镜片可行性（诚实标注数据缺口）

| 角色 | 镜片 focus | 选票策略 pick | 数据源 | 可行性 / 降级 |
|---|---|---|---|---|
| committee_value 老陈 | 估值/现金流/防御 | `composite_score` 中低位 + 防御 sector | entries | ✅ 现成字段 |
| committee_growth Vera | 渗透率/赛道 | `sector ∈ {半导体,新能源,AI,科技}` | entries.sector | ✅ 现成字段 |
| committee_risk 秦姐 | 风险/回撤/爆雷 | `impact_level='critical'` 催化剂标的 / `change_pct` 大跌 | catalysts + snapshot | ✅ 缺快照→退催化剂 |
| committee_contrarian 老康 | 情绪反面/超买超卖 | `change_pct` 或 `rsi_14` 极值（最热/最冷） | snapshot | ✅ 缺快照→退饱和集反选 |
| committee_consensus 明哥 | 共识/整合分歧 | **不深挖**（`deep_dive=False`），用共享基座 | — | ✅ 本就靠人设整合 |
| L1_macro David | 板块轮动/宏观 | **不给单股种子**，给 sector 聚合分布 | entries 聚合 | ✅（见 D.5 拍板 3） |
| vip_advisor Linda | 持仓相关 | 板主持仓 ticker | `get_sim_positions('')` | ⚠️ 空仓→退高分优质标的 |
| watchlist_uzi 阿泽 | 技术面/放量 | `rsi_14`/`macd` 信号 + `volume` 放量 | snapshot | ✅ 缺快照→退催化剂/成交额 |

**快照成本护栏**：contrarian/uzi 需按 ticker 查 `get_latest_snapshot`。`# ponytail:` 只对
按 `composite_score` 排序的**前 K≈15** 只候选打分，避免每轮对全池逐票查询（K 是调参旋钮）。

### D.5 三个「待拍板」问题 —— 细化后的建议

1. **过滤激进度（硬过滤 vs 软提示）** → **建议软**：只把「深挖种子」按角色硬选（这一步就足以打散收敛），
   共享基座 + 全景观察池仍在每个角色眼前，角色可自由跟帖。不做「角色只能看自己那批票」的硬隔离
   （会割裂讨论、丧失交锋）。
2. **是否投入做可靠涨跌幅信号支撑 contrarian/uzi** → **无需额外投入，风险已解除**：
   `market_snapshots` 表已存 `change_pct/rsi_14/macd*/volume`，由 scheduler 定时
   `save_snapshots`（[scheduler.py:325](../bottleneck_hunter/watchlist/scheduler.py#L325)）+
   price_pipeline 落库，读用现成 `get_latest_snapshot`；VIP 侧已有「缺价诚实降级」既定约定可照搬
   （[vip/stress.py](../bottleneck_hunter/vip/stress.py)）。A 股新鲜度依赖抓取成功率，靠
   `data_quality` 标记 + 缺价降级兜底，**绝不在发帖轮引入实时行情拉取**。
3. **macro 是否彻底不发个股** → **建议软**：`deep_dive=False` 不给 macro 单股种子、改给
   sector 聚合镜片；但不禁止它在跟帖里提及个股（宏观观点常需举例）。硬禁发个股帖会显得机械。

### D.6 数据缺口与约束（诚实）

- **无结构化情绪/方向列**：`catalyst_tracking` 只有 `catalyst_type` + `impact_level`，没有
  direction(pos/neg)。故 risk/contrarian 的「爆雷/利空」镜片靠 `impact_level='critical'` +
  `catalyst_type` + 标题关键词近似，**不能**指望一个不存在的情绪字段。
- **A 股快照覆盖**：akshare/tushare 失败率高 → contrarian/uzi 对 A 股标的可能缺快照，
  必须逐票 best-effort + `data_quality` 校验 + 降级，不得因缺价崩整轮。
- **vip_advisor 持仓源**：决策中心账户 `account_id=''`（见 [[project_dc_sim_account_decoupled_from_vip]]）；
  空仓时退化为「高 `composite_score` 优质标的」。

### D.7 落地与验证（待批准再建）

- **改动点**：`forum_identity.py` 加 `FocusProfile` + `FOCUS_PROFILES` + 自检；
  `forum_ai.py` 拆 `_board_context_base` / `_role_lens`，`run_forum_ai_round` 算一次 base、
  逐角色套 lens，`_build_role_digest` 末尾追加 focus 行。
- **测试**：每角色 lens 命中预期票种（value 选低分、growth 选科技 sector、contrarian 选
  change_pct 极值、uzi 选放量、macro 无单股种子、vip_advisor 选持仓票）；缺快照/空仓降级不崩；
  全程显式 tmp `db_path` + `for_user("alice")`，snapshot/持仓用 stub 种入。
- **排期**：A/B/C 先上线观察多样性效果，再决定是否建 D（D 复用 A 的饱和集 + C 的催化剂 feed）。

### D.8 明确不做（YAGNI）

- 不引入实时行情拉取（沿用 best-effort 快照/静态字段）。
- 不做 A×B×role 三维矩阵（记忆已是自述备忘，够用）。
- 不做持久「上次露出」游标，除非 A/B/C 上线后观察仍集中。
- 不动市场隔离/打分逻辑、不动前端。
