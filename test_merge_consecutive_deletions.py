"""测试合并连续删除操作功能"""

import sys
sys.path.insert(0, '/Users/demo/Desktop/automatic/automatic post')

from app.services.promotion_repair import apply_repair_plan


def test_merge_consecutive_promotional_lines():
    """测试：合并三行连续推广内容的删除操作"""

    print("=" * 80)
    print("测试场景：三行连续推广内容")
    print("=" * 80)

    body = """<p>这是正文第一段内容。</p>
<p>更多精彩内容请关注我们
点击查看完整赛程
立即订阅不错过任何比赛</p>
<p>这是正文第二段内容。</p>"""

    # AI 生成的修复计划：删除三行连续推广内容
    # 每行都会吞掉自己的分隔符，导致操作区间相接
    plan = [
        {
            "action": "remove_text_line",
            "segment_id": "b2.s1",
            "evidence": "更多精彩内容请关注我们",
            "issue_type": "promotion",
            "reason": "推广引流内容",
            "confidence": 0.98,
        },
        {
            "action": "remove_text_line",
            "segment_id": "b2.s2",
            "evidence": "点击查看完整赛程",
            "issue_type": "promotion",
            "reason": "推广引流内容",
            "confidence": 0.98,
        },
        {
            "action": "remove_text_line",
            "segment_id": "b2.s3",
            "evidence": "立即订阅不错过任何比赛",
            "issue_type": "promotion",
            "reason": "推广引流内容",
            "confidence": 0.98,
        },
    ]

    print("\n原始正文：")
    print(body)
    print(f"\n修复计划：删除 {len(plan)} 行推广内容")

    cleaned, matches, error = apply_repair_plan(body, plan)

    if error:
        print(f"\n❌ 修复失败：{error}")
        return False
    else:
        print(f"\n✅ 修复成功！")
        print(f"\n修复后正文：")
        print(cleaned)

        # 验证推广内容已被删除
        assert "更多精彩内容请关注我们" not in cleaned
        assert "点击查看完整赛程" not in cleaned
        assert "立即订阅不错过任何比赛" not in cleaned

        # 验证正常内容保留
        assert "这是正文第一段内容" in cleaned
        assert "这是正文第二段内容" in cleaned

        print("\n✅ 验证通过：推广内容已删除，正常内容保留")
        return True


def test_merge_consecutive_blocks():
    """测试：合并三个连续块的删除操作"""

    print("\n" + "=" * 80)
    print("测试场景：三个连续推广块")
    print("=" * 80)

    body = """<p>这是正文第一段。</p>
<p>关注我们获取更多资讯</p>
<p>点击订阅精彩内容</p>
<p>立即加入我们的社区</p>
<p>这是正文第二段。</p>"""

    plan = [
        {
            "action": "remove_block",
            "block_id": "b2",
            "evidence": "关注我们获取更多资讯",
            "issue_type": "promotion",
            "reason": "推广内容",
            "confidence": 0.98,
        },
        {
            "action": "remove_block",
            "block_id": "b3",
            "evidence": "点击订阅精彩内容",
            "issue_type": "promotion",
            "reason": "推广内容",
            "confidence": 0.98,
        },
        {
            "action": "remove_block",
            "block_id": "b4",
            "evidence": "立即加入我们的社区",
            "issue_type": "promotion",
            "reason": "推广内容",
            "confidence": 0.98,
        },
    ]

    print("\n原始正文：")
    print(body)
    print(f"\n修复计划：删除 {len(plan)} 个推广块")

    cleaned, matches, error = apply_repair_plan(body, plan)

    if error:
        print(f"\n❌ 修复失败：{error}")
        return False
    else:
        print(f"\n✅ 修复成功！")
        print(f"\n修复后正文：")
        print(cleaned)

        # 验证推广内容已被删除
        assert "关注我们获取更多资讯" not in cleaned
        assert "点击订阅精彩内容" not in cleaned
        assert "立即加入我们的社区" not in cleaned

        # 验证正常内容保留
        assert "这是正文第一段" in cleaned
        assert "这是正文第二段" in cleaned

        print("\n✅ 验证通过：所有推广块已删除，正常内容保留")
        return True


def test_non_consecutive_deletions_not_merged():
    """测试：不相接的删除操作不应该被合并"""

    print("\n" + "=" * 80)
    print("测试场景：不连续的删除操作（不应合并）")
    print("=" * 80)

    body = """<p>这是正文第一段。</p>
<p>这是推广内容A</p>
<p>这是正常内容。</p>
<p>这是推广内容B</p>
<p>这是正文第二段。</p>"""

    plan = [
        {
            "action": "remove_block",
            "block_id": "b2",
            "evidence": "这是推广内容A",
            "issue_type": "promotion",
            "reason": "推广内容",
            "confidence": 0.98,
        },
        {
            "action": "remove_block",
            "block_id": "b4",
            "evidence": "这是推广内容B",
            "issue_type": "promotion",
            "reason": "推广内容",
            "confidence": 0.98,
        },
    ]

    print("\n原始正文：")
    print(body)
    print(f"\n修复计划：删除 2 个不连续的推广块")

    cleaned, matches, error = apply_repair_plan(body, plan)

    if error:
        print(f"\n❌ 修复失败：{error}")
        return False
    else:
        print(f"\n✅ 修复成功！")
        print(f"\n修复后正文：")
        print(cleaned)

        # 验证推广内容已被删除
        assert "这是推广内容A" not in cleaned
        assert "这是推广内容B" not in cleaned

        # 验证正常内容保留
        assert "这是正文第一段" in cleaned
        assert "这是正常内容" in cleaned
        assert "这是正文第二段" in cleaned

        print("\n✅ 验证通过：两个推广块分别删除，正常内容保留")
        return True


if __name__ == "__main__":
    print("\n🧪 测试合并连续删除操作功能\n")

    results = []

    try:
        results.append(("三行连续推广", test_merge_consecutive_promotional_lines()))
    except Exception as e:
        print(f"\n❌ 测试异常：{e}")
        import traceback
        traceback.print_exc()
        results.append(("三行连续推广", False))

    try:
        results.append(("三个连续块", test_merge_consecutive_blocks()))
    except Exception as e:
        print(f"\n❌ 测试异常：{e}")
        import traceback
        traceback.print_exc()
        results.append(("三个连续块", False))

    try:
        results.append(("不连续删除", test_non_consecutive_deletions_not_merged()))
    except Exception as e:
        print(f"\n❌ 测试异常：{e}")
        import traceback
        traceback.print_exc()
        results.append(("不连续删除", False))

    print("\n" + "=" * 80)
    print("测试结果汇总")
    print("=" * 80)
    for name, passed in results:
        status = "✅ 通过" if passed else "❌ 失败"
        print(f"{status}: {name}")

    all_passed = all(passed for _, passed in results)
    if all_passed:
        print("\n🎉 所有测试通过！方案 A 实施成功！")
    else:
        print("\n⚠️ 部分测试失败，需要调试")
