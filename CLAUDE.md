# BottleneckHunter - CLAUDE.md

## 项目概述

BottleneckHunter 是一个 AI 驱动的产业链瓶颈选股系统，已从一次性 CLI 选股工具演进为**持续跟踪 + 多层决策的 Web 平台**。

**两大形态：**
- **分析流程（CLI + Wizard）** — Serenity"三步法"：产业链拆解 → 供应商检索 → 多模型交叉验证
- **决策中心（Web）** — 观察池持续跟踪，L1 宏观 / L2 组合 / L3 战术 / L4 执行四层决策 + 投委会评审 + 模拟交易闭环 + 自动复盘

三步法核心：
1. **产业链拆解** — 从终端产品逐层拆解到原材料，3+ 层深度
2. **供应商检索** — 沿瓶颈环节找到被忽视的优质供应商
3. **交叉验证** — 多个 LLM 从反面角度拷问投资逻辑

## 技术栈

- Python 3.10+
- LangGraph / LangChain — 工作流编排
- Pydantic v2 — 数据模型（产业链模型在 `chain/models.py`）
- FastAPI + APScheduler — Web 服务与内置定时调度
- SQLite — 决策中心持久化（表结构在 `watchlist/store_schema.py`）
- Rich / Questionary / Typer — CLI
- yfinance / akshare / tushare 等 — 多市场数据（`data_provider/`）
- JWT (HttpOnly cookie) — 多用户认证与 Key 隔离

## 项目结构

```
bottleneck_hunter/
├── cli.py              # Typer CLI 入口（screen / hot / serve）
├── chain/              # 分析流程：产业链拆解 → 瓶颈 → 供应商 → 交叉验证
│   ├── models.py       # 产业链 Pydantic 数据模型
│   ├── decomposer.py   # LLM 产业链拆解引擎
│   ├── bottleneck.py   # 瓶颈评分算法
│   ├── graph.py        # LangGraph 筛选工作流
│   ├── report.py       # Markdown 报告生成
│   ├── prompts/        # LLM 提示词模板 (.md，37 个)
│   └── data/           # 预设产业链 JSON
├── watchlist/          # 决策中心核心：观察池 + L1-L4 决策引擎 + 投委会 + 模拟交易
│   ├── decision_engine.py  # L1-L4 四层决策 + 投委会串联
│   ├── persona.py          # 用户持仓风格（硬约束注入各层）
│   ├── store_schema.py     # 全部 DB 表结构（53 张）
│   ├── store_*.py          # 分模块 Store（watchlist/decision/research…）
│   ├── scheduler.py        # APScheduler 定时任务（双市场）
│   └── …                   # 催化剂/风控/复盘/偏好学习/模型调度等
├── web/                # FastAPI 应用与各 API 路由
│   ├── app.py              # 应用装配 + 中间件 + 静态资源
│   ├── decision_api.py     # 决策中心 API（含持仓风格 /style）
│   ├── trading_api.py      # 模拟交易 API
│   ├── auth_api.py / admin_api.py / ai_config_api.py …
│   └── static/             # 前端（index.html + js/ + css/，本地 vendor 库）
├── auth/               # JWT 认证、用户/邀请码/系统配置、Key 加密隔离
├── data_provider/      # 多市场数据源适配（yfinance/akshare/tushare/FMP 等）
├── dataflows/          # 数据流接口
└── llm_clients/
    └── factory.py      # 多 provider LLM 客户端工厂 + 智能调度
```

## 开发规范

- 中文注释和提示词（面向中文用户）
- 代码风格：ruff, line-length=120
- 异步优先：所有 LLM 调用使用 `async/await`
- Pydantic 模型做数据验证，不使用裸 dict
- LLM 返回 JSON 时，做好 markdown code fence 剥离和容错解析
- **时区**：UTC 存储 / 北京展示（`fmtBJ`）/ 调度 Asia-Shanghai，勿引入美东或非北京时区
- **多用户隔离**：Store 用 `.for_user(sub).for_market(market)`；Key 严格按用户解析（缺 Key 即 `MissingUserKeyError`），绝无全局 Key
- **前端库**：国内 CDN 不可达，前端库一律本地 `web/static/vendor/`（用 npmmirror 下载）
- **更新历史**：commit message 含 `📢` 行首独立白话行才会进首页 `UPDATE_HISTORY.json`

## 环境配置

复制 `.env.example` 为 `.env`，填入 API key。支持 provider: openai, anthropic, deepseek, google, qwen, glm, ollama, openrouter 等（Web 端可在顶栏 AI 配置中心按用户配置）

## 运行

```bash
pip install -e .

# CLI 分析流程
bottleneck-hunter screen      # 完整选股向导
bottleneck-hunter hot         # 全市场热点扫描

# Web 决策中心（必须用 serve 启动；python -m web.app 不会起服务）
bottleneck-hunter serve                 # 默认 127.0.0.1:8000
bottleneck-hunter serve --port 8010     # 换端口
```

## 开发计划

详见 `PLAN.md`（阶段路线与里程碑）与 `docs/IMPROVEMENT_PLAN.md`（Phase 17+ 改进）。核心决策闭环（L1-L4 + 投委会 + 复盘 + 自进化）已贯通，重心转向实盘数据质量、回测校准与体验打磨。
