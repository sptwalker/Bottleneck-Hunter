# FactCheck 验证步骤

## 问题原因

数据库中的分析记录是旧的 (2026-07-02)，在 FactCheck 合并之前。需要运行一次新的分析。

---

## 验证步骤

### 1. 运行新的分析

1. 打开浏览器: **http://localhost:8002**

2. 在首页选择一个行业，推荐：
   - "半导体" (容易出现财务数据)
   - "新能源汽车"
   - "AI芯片"

3. 点击 **"开始分析"**

4. **等待 Phase 1-3 完成** (预计 2-5 分钟)

### 2. 验证 FactCheck 执行

**方法 A: 浏览器开发者工具 (推荐)**

1. 打开浏览器开发者工具 (F12)
2. 切换到 **Console** 标签
3. 在 Phase 3 完成时，应该看到类似：
   ```
   [streaming-catalyst] 完成 | 总耗时=XX.Xs
   [streaming-factcheck] 完成 N 家事实核查  ← 关键！
   ```

4. 切换到 **Network** 标签
5. 找到 `stream/phase/3` 的 SSE 连接
6. 查看返回的事件流，找到 `step_done` 事件
7. 检查 `supplier_scorecards` 中是否有：
   ```json
   {
     "fact_check_recommendation": "PASS",  // 或 "REVIEW" / "REJECT"
     "final": {
       "credibility": 10.0,
       "quality_adjusted": 8.2
     }
   }
   ```

**方法 B: 服务器日志**

在服务器终端查看日志，应该看到：
```
[streaming-factcheck] 完成 N 家事实核查
```

**方法 C: 数据库验证 (分析完成后)**

运行以下命令检查：

```bash
cd c:\Users\walker\Documents\walker\Vibecode\Bottleneck-Hunter

python -c "
import sqlite3
import json
from datetime import datetime

conn = sqlite3.connect('data/analyses.db')
cursor = conn.cursor()

# 获取最新分析
cursor.execute('SELECT sector, created_at, result_json FROM analyses ORDER BY created_at DESC LIMIT 1')
row = cursor.fetchone()

if row:
    sector, created_at, result_json = row
    result = json.loads(result_json)
    
    print(f'最新分析: {sector}')
    print(f'创建时间: {created_at}')
    print()
    
    scorecards = result.get('supplier_scorecards', [])
    
    # 统计 FactCheck 结果
    has_factcheck = sum(1 for sc in scorecards if sc.get('fact_check_recommendation'))
    pass_count = sum(1 for sc in scorecards if sc.get('fact_check_recommendation') == 'PASS')
    review_count = sum(1 for sc in scorecards if sc.get('fact_check_recommendation') == 'REVIEW')
    reject_count = sum(1 for sc in scorecards if sc.get('fact_check_recommendation') == 'REJECT')
    
    print(f'总候选数: {len(scorecards)}')
    print(f'有 FactCheck 结果: {has_factcheck}')
    print(f'  PASS: {pass_count}')
    print(f'  REVIEW: {review_count}')
    print(f'  REJECT: {reject_count}')
    print()
    
    if has_factcheck > 0:
        print('✓ FactCheck 已成功执行！')
        print()
        print('示例 (前3个):')
        for i, sc in enumerate(scorecards[:3]):
            name = sc.get('supplier', {}).get('name', 'N/A')
            rec = sc.get('fact_check_recommendation', 'N/A')
            cred = sc.get('final', {}).get('credibility', 'N/A')
            print(f'  {i+1}. {name}: {rec} (credibility={cred})')
    else:
        print('× FactCheck 未执行')
        print('可能原因: 分析是在合并前运行的，请运行新的分析')

conn.close()
"
```

---

## 预期结果

### ✅ 成功标志

1. **服务器日志**: 显示 `[streaming-factcheck] 完成 N 家事实核查`
2. **执行时间**: < 1 秒 (几乎无感)
3. **数据库**: `fact_check_recommendation` 字段存在且有值
4. **credibility**: 大部分候选应该在 8.0-10.0 之间
5. **REJECT**: 如果有数据矛盾的候选，应该被标记为 REJECT

### ⚠️ 关于"交叉验证"页面

**Phase 4 的"交叉验证"功能已被重构**:

- 旧版: 多 LLM 再打分 (N×10 调用, ~15s, $0.50-2.00)
- 新版: FactCheck 在 Phase 3 执行 (0 调用, <100ms, $0.00)

**建议**:
- Phase 4 UI 需要更新以展示 FactCheck 结果而不是旧的 consensus_score
- 或者隐藏 Phase 4 页面，因为 FactCheck 已在 Phase 3 完成
- 这是一个 **前端 UI 适配**的问题，不是 FactCheck 的 bug

---

## 如果仍然没有 FactCheck 结果

检查 Phase 3 代码是否正确集成:

```bash
cd c:\Users\walker\Documents\walker\Vibecode\Bottleneck-Hunter

# 检查 phases.py 中是否有 FactCheck 调用
grep -n "fact_check" bottleneck_hunter/web/streaming/phases.py
```

应该看到类似:
```python
from bottleneck_hunter.chain.fact_check import apply_fact_check_to_scorecards
fact_check_reports = apply_fact_check_to_scorecards(scorecards, all_reports)
```

---

## 下一步

1. ✅ 运行一次新的分析
2. ✅ 验证 FactCheck 执行 (上述方法)
3. ✅ 确认性能/成本改进
4. 📝 (可选) 更新前端 UI 以展示 FactCheck 结果
5. 🚀 推送到远程仓库

---

**当前服务器**: http://localhost:8002 (正在运行)
