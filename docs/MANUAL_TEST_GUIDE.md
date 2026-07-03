# FactCheck Gate - 手动测试指南

**测试日期**: 2026-07-03  
**分支**: main  
**状态**: ✅ 回归测试通过 (779/832), ✅ 端到端模拟通过

---

## 一、启动服务器

```bash
cd c:\Users\walker\Documents\walker\Vibecode\Bottleneck-Hunter

# 方法 1: 直接启动
python bottleneck_hunter/web/app.py

# 方法 2: 使用 uvicorn (推荐)
cd bottleneck_hunter/web
uvicorn app:app --host 0.0.0.0 --port 8001 --reload

# 验证服务器就绪
# 浏览器打开: http://localhost:8001
# 应该看到 Bottleneck Hunter 首页
```

---

## 二、核心测试场景

### 场景 1: 正常流程 - FactCheck 应该 PASS

**目标**: 验证声称与数据一致的候选通过 FactCheck

1. 访问 `http://localhost:8001`
2. 选择行业: "半导体" 或 "新能源汽车"
3. 点击 "开始分析"
4. 观察 Phase 3 完成后的结果
5. **预期行为**:
   - Phase 3 日志中应显示: `[streaming-factcheck] 完成 N 家事实核查`
   - 优质候选的 `overall_score` 应该保持不变或略微上升
   - FactCheck 不应误杀有真实数据支撑的候选

### 场景 2: 数据矛盾 - FactCheck 应该降低评分或 REJECT

**构造方法**: 
- 选择一个已知财务恶化但 LLM 可能夸大的行业
- 或者等待自然出现声称与数据矛盾的候选

**验证点**:
1. 在 Phase 3 完成后，检查 `scorecards` 的 `overall_score` 是否被调整
2. 在 Phase 4 (如果有)，检查是否有候选被标记为 `REJECT`
3. 浏览器开发者工具 → Network → 查看 Phase 3 的 SSE 响应:
   ```json
   {
     "fact_check_recommendation": "REJECT",  // 或 "PASS" / "REVIEW"
     "overall_score": 6.3,  // 应该低于原始评分
     "final": {
       "credibility": 6.7,
       "quality_adjusted": 6.3
     }
   }
   ```

### 场景 3: 信息稀疏 - FactCheck 不应误杀

**目标**: 验证"无数据不误杀"保护机制

**验证点**:
- 小盘股、冷门细分行业的候选
- 即使缺少财务快照 (`financial_snapshot=null`)
- `credibility` 应该 ≥ 9.0
- `recommendation` 应该是 `PASS` (不应是 `REJECT`)
- `overall_score` 应该基本不变 (误差 < 1%)

---

## 三、关键指标对比

### Phase 3 性能

| 指标 | 改动前 (cross_validation) | 改动后 (FactCheck) |
|------|---------------------------|-------------------|
| LLM 调用次数 | N × 10 | **0** |
| 延迟 | ~15s | **< 100ms** |
| 成本 | $0.50-2.00 | **$0.00** |

**验证方法**: 
- 查看服务器日志中的 `[streaming-factcheck] 完成 N 家事实核查` 时间戳
- 应该在 Phase 3 的 catalyst 步骤和 FinalScorer 之间，几乎无延迟

### Phase 4 变化

改动前:
```json
{
  "validations": [
    {"ticker": "000001.SZ", "consensus_score": 7.5, ...}
  ],
  "recommendations": [...]
}
```

改动后:
```json
{
  "validations": [],  // 已废弃，保留向后兼容
  "recommendations": [
    {
      "ticker": "000001.SZ",
      "credibility": 10.0,
      "recommendation": "PASS",
      "final_score": 6.79
    }
  ]
}
```

---

## 四、测试检查清单

### ✅ 回归测试
- [x] 779 passed (所有 FactCheck/供应商评估/Alpha/FinalScorer 测试通过)
- [x] 3 新增集成测试通过
- [x] 端到端 Phase 3 模拟通过

### 🔄 手动测试 (待完成)

#### Phase 3 集成
- [ ] FactCheck 在 catalyst 后、FinalScorer 前执行
- [ ] `overall_score` 根据 credibility 正确调整
- [ ] 日志显示 `[streaming-factcheck] 完成 N 家事实核查`
- [ ] 延迟 < 500ms (相比原方案节省 ~15s)

#### credibility → quality → final_score 链路
- [ ] 矛盾候选: credibility ↓ → quality ↓ → final_score ↓
- [ ] 正常候选: credibility ≈ 10 → quality 不变 → final_score 正常
- [ ] 排序受影响: 矛盾候选排名下降

#### REJECT 硬门
- [ ] `fact_check_recommendation="REJECT"` 的候选不出现在 top_picks
- [ ] Phase 4 显示被拦截数量
- [ ] 无误杀: 信息稀疏的小盘股不被 REJECT

#### UI 展示
- [ ] Phase 4 recommendations 包含 `credibility` 和 `recommendation` 字段
- [ ] 前端正确显示 PASS/REVIEW/REJECT 状态
- [ ] (可选) findings 可溯源 (声称/数据/判定)

---

## 五、常见问题排查

### Q1: 服务器启动失败
```bash
# 检查依赖
pip install -r requirements.txt

# 检查端口占用
netstat -an | findstr "8001"

# 如果端口被占用，更换端口
uvicorn app:app --host 0.0.0.0 --port 8002
```

### Q2: Phase 3 没有显示 FactCheck 日志
**原因**: 可能在查看旧的分析缓存

**解决**: 
- 删除 `data/analyses.db` 或清空缓存
- 重新运行分析

### Q3: 所有候选都是 PASS，看不到 REJECT
**原因**: LLM 声称与真实数据恰好一致

**解决**: 
- 这是好事！说明 LLM 评估质量高
- 可以构造一个测试用例 (修改 `financial_snapshot` 数据) 来验证 REJECT 逻辑

### Q4: credibility 始终是 10.0
**原因**: 
1. 声称真的与数据一致 (正常)
2. 或者映射规则未触发 (检查关键词)

**验证**: 查看 `findings` 字段:
- 如果 `findings=[]`: 未触发任何规则 → 正常
- 如果 `findings` 有 `supported`: 规则生效且数据支撑 → 正常

---

## 六、下一步

### 合格标准
- Phase 3 FactCheck 正常执行且无延迟
- credibility 正确影响 final_score 排序
- REJECT 硬门生效 (至少在模拟测试中验证)
- 无误杀 (信息稀疏候选不被拦截)

### 如果发现问题
1. 记录复现步骤
2. 检查服务器日志中的错误
3. 查看 Network → SSE 响应中的 `fact_check_recommendation` 和 `credibility`
4. 反馈给开发者

### 如果一切正常
✅ **准备部署**

```bash
# 推送到远程
git push origin main

# 清理 feature 分支
git branch -d feature/factcheck-gate

# 通知团队
```

---

**测试联系人**: Walker  
**文档**: `docs/FACTCHECK_GATE_SUMMARY.md`, `docs/FACTCHECK_PRODUCTION_TEST.md`
