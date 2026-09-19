#!/usr/bin/env python3
"""验证地域敏感词筛选功能的代码集成"""

import sys
import inspect

print("=" * 60)
print("验证地域敏感词筛选功能的代码集成")
print("=" * 60)

# 检查 1: repository.py 中 list_articles 函数是否有 filter_type 参数
print("\n【检查 1】repository.py - list_articles 函数")
try:
    from app.repository import list_articles
    sig = inspect.signature(list_articles)
    params = list(sig.parameters.keys())

    if 'filter_type' in params:
        print("✅ list_articles 函数已包含 filter_type 参数")
        print(f"   参数列表: {', '.join(params)}")
    else:
        print("❌ list_articles 函数缺少 filter_type 参数")
        print(f"   当前参数: {', '.join(params)}")
        sys.exit(1)
except Exception as e:
    print(f"❌ 导入失败: {e}")
    sys.exit(1)

# 检查 2: 查看 list_articles 的源码中是否有筛选逻辑
print("\n【检查 2】repository.py - 筛选逻辑")
try:
    import app.repository
    import inspect
    source = inspect.getsource(app.repository.list_articles)

    if 'filter_type' in source and 'regional_sensitive' in source:
        print("✅ list_articles 函数包含地域敏感筛选逻辑")
        # 提取关键行
        for line in source.split('\n'):
            if 'filter_type' in line or 'regional_sensitive' in line:
                print(f"   {line.strip()}")
    else:
        print("❌ list_articles 函数缺少筛选逻辑")
        sys.exit(1)
except Exception as e:
    print(f"❌ 检查失败: {e}")
    sys.exit(1)

# 检查 3: web.py 中 articles 路由是否传递 filter_type
print("\n【检查 3】web.py - articles 路由")
try:
    with open('/Users/demo/Desktop/automatic/automatic post/app/web.py', 'r', encoding='utf-8') as f:
        content = f.read()

    # 查找 articles 函数
    if 'filter_type = request.args.get("filter_type")' in content:
        print("✅ articles 路由已获取 filter_type 参数")
    else:
        print("❌ articles 路由未获取 filter_type 参数")
        sys.exit(1)

    if 'filter_type=filter_type' in content:
        print("✅ articles 路由已传递 filter_type 到 list_articles")
    else:
        print("❌ articles 路由未传递 filter_type")
        sys.exit(1)

    if '"filter_type": filter_type' in content:
        print("✅ articles 路由已将 filter_type 传递到模板")
    else:
        print("❌ articles 路由未将 filter_type 传递到模板")
        sys.exit(1)

except Exception as e:
    print(f"❌ 检查失败: {e}")
    sys.exit(1)

# 检查 4: templates/articles.html 是否有筛选下拉菜单
print("\n【检查 4】templates/articles.html - 筛选下拉菜单")
try:
    with open('/Users/demo/Desktop/automatic/automatic post/templates/articles.html', 'r', encoding='utf-8') as f:
        content = f.read()

    if '拦截类型' in content:
        print("✅ 模板已添加'拦截类型'下拉菜单")
    else:
        print("❌ 模板缺少'拦截类型'下拉菜单")
        sys.exit(1)

    if 'regional_sensitive' in content and 'filter_type' in content:
        print("✅ 模板包含地域敏感选项")
    else:
        print("❌ 模板缺少地域敏感选项")
        sys.exit(1)

    if 'filters.filter_type' in content:
        print("✅ 模板已绑定 filter_type 到表单")
    else:
        print("❌ 模板未绑定 filter_type")
        sys.exit(1)

except Exception as e:
    print(f"❌ 检查失败: {e}")
    sys.exit(1)

# 检查 5: templates/articles.html 质检列是否显示地域敏感标记
print("\n【检查 5】templates/articles.html - 质检列显示")
try:
    with open('/Users/demo/Desktop/automatic/automatic post/templates/articles.html', 'r', encoding='utf-8') as f:
        content = f.read()

    if 'is_regional_sensitive' in content:
        print("✅ 质检列已添加地域敏感判断逻辑")
    else:
        print("❌ 质检列缺少地域敏感判断逻辑")
        sys.exit(1)

    if 'matched_keyword' in content:
        print("✅ 质检列已显示匹配的关键词")
    else:
        print("❌ 质检列未显示匹配的关键词")
        sys.exit(1)

except Exception as e:
    print(f"❌ 检查失败: {e}")
    sys.exit(1)

# 检查 6: templates/article_detail.html 是否显示地域敏感警告
print("\n【检查 6】templates/article_detail.html - 详情页显示")
try:
    with open('/Users/demo/Desktop/automatic/automatic post/templates/article_detail.html', 'r', encoding='utf-8') as f:
        content = f.read()

    if 'is_regional_sensitive' in content:
        print("✅ 详情页已添加地域敏感判断逻辑")
    else:
        print("❌ 详情页缺少地域敏感判断逻辑")
        sys.exit(1)

    if '地域敏感词拦截' in content:
        print("✅ 详情页已显示地域敏感警告框")
    else:
        print("❌ 详情页未显示地域敏感警告框")
        sys.exit(1)

except Exception as e:
    print(f"❌ 检查失败: {e}")
    sys.exit(1)

print("\n" + "=" * 60)
print("✅ 所有检查通过！地域敏感词筛选功能已完整集成")
print("=" * 60)
print("\n功能说明：")
print("1. 在'全部文章'页面的筛选栏，新增'拦截类型'下拉菜单")
print("2. 选择'🚨 地域敏感（台港澳）'可筛选出所有被地域敏感词拦截的文章")
print("3. 文章列表的'质检'列会显示'地域敏感'标记和匹配的关键词")
print("4. 文章详情页的质检结果区域会突出显示地域敏感警告框")
print("\n使用方式：")
print("- 访问 /articles 页面")
print("- 在筛选栏选择'拦截类型 → 🚨 地域敏感（台港澳）'")
print("- 点击'筛选'按钮")
print("- 查看筛选结果，判断拦截是否正确")
