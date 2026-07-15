# 测试指南

## 快子集（日常/提交前跑，约 2 分钟）
排除 `slow` 标记（真实网络/外部依赖的慢测试）：
```bash
pytest -m "not slow"
```
**改了签名/形参、动了 store/pipeline/API 后，至少跑这个再提交。**

## 全量（含慢测试，约 28 分钟）
```bash
pytest
```
慢的几乎全在 `test_decision_8b5.py` 的 3 个 E2E 决策测试——它们**真实拉宏观数据**
（FRED/指数），各约 8 分钟，占全量 ~93% 时间。已标 `@pytest.mark.slow`。

## 只跑慢测试
```bash
pytest -m slow
```

## 基线（2026-07-15 确认）
- 全量：**1019 passed, 0 failed**（27:42）。
- 快子集：**1016 passed**（3 slow deselected），约 2 分钟。

## 约定
- 依赖真实网络/外部服务、单条 >5s 的测试，加 `@pytest.mark.slow`。
- 其余保持纯离线/mock，确保快子集稳定可秒级复跑。
- marker 在 `pyproject.toml [tool.pytest.ini_options].markers` 注册。
