"""检查数据库中 credibility 字段是否正确保存"""
import sqlite3
import json

conn = sqlite3.connect('data/analyses.db')
cursor = conn.cursor()

# 获取最新分析
cursor.execute('''
    SELECT id, sector, end_product, created_at, result_json
    FROM analyses
    ORDER BY created_at DESC
    LIMIT 1
''')
latest = cursor.fetchone()

print(f"最新分析: {latest[0]}")
print(f"行业: {latest[1]}/{latest[2]}")
print(f"时间: {latest[3]}")

if latest[4]:
    result = json.loads(latest[4])
    suppliers = result.get('supplier_scorecards', [])

    print(f"\n供应商总数: {len(suppliers)}")

    has_credibility = 0
    has_quality_adjusted = 0
    has_recommendation = 0

    print("\n检查前5个供应商:")
    for i, sc in enumerate(suppliers[:5], 1):
        symbol = sc.get('symbol', 'N/A')
        name = sc.get('name', 'N/A')
        recommendation = sc.get('fact_check_recommendation')

        final = sc.get('final', {})
        credibility = final.get('credibility')
        quality_adjusted = final.get('quality_adjusted')

        print(f"\n{i}. {symbol} {name}")
        print(f"   fact_check_recommendation: {recommendation}")
        print(f"   final.credibility: {credibility}")
        print(f"   final.quality_adjusted: {quality_adjusted}")

        if recommendation:
            has_recommendation += 1
        if credibility is not None:
            has_credibility += 1
        if quality_adjusted is not None:
            has_quality_adjusted += 1

    print(f"\n=== 统计 ===")
    print(f"有 fact_check_recommendation 的: {has_recommendation}/5")
    print(f"有 credibility 的: {has_credibility}/5")
    print(f"有 quality_adjusted 的: {has_quality_adjusted}/5")

    if has_credibility == 5 and has_quality_adjusted == 5:
        print("\n✅ 修复成功！所有字段都已保存")
    else:
        print("\n❌ 仍有问题，部分字段缺失")
        print("   需要重新运行分析来验证修复")

conn.close()
