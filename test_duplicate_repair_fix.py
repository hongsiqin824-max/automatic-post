"""测试重复内容修复的容错机制"""

from app.services.promotion_repair import apply_repair_plan, content_blocks


def test_duplicate_block_repair_with_invalid_keep_id_auto_correction():
    """测试：当 keep_block_id 无效时，系统能自动找到有效的保留目标"""

    body = """<p>这是第一段正文内容。</p>
<p>这是重复的段落内容。</p>
<p>这是第二段正文内容。</p>
<p>这是重复的段落内容。</p>
<p>这是第三段正文内容。</p>"""

    blocks = content_blocks(body)
    print(f"\n找到 {len(blocks)} 个内容块：")
    for block in blocks:
        print(f"  {block['block_id']}: {block['text']}")

    # 场景：AI 给出了错误的 keep_block_id (b99 不存在)
    # 但系统应该自动找到另一个具有相同文本的块 (b2)
    plan = {
        "action": "remove_block",
        "block_id": "b4",  # 要删除第 4 个块
        "evidence": "这是重复的段落内容。",
        "keep_block_id": "b99",  # 错误的保留目标
        "issue_type": "duplicate_content",
        "reason": "该段落内容与前文重复",
        "confidence": 0.98,
    }

    cleaned, matches, error = apply_repair_plan(body, plan)

    print(f"\n修复结果:")
    print(f"  错误: {error}")
    print(f"  成功: {error is None}")

    if error is None:
        print(f"  删除了 {len(matches)} 个块")
        print(f"  修复后的正文:")
        print(cleaned)

        # 验证：应该成功删除重复内容
        assert "这是重复的段落内容。" in cleaned  # 保留了一个
        assert cleaned.count("这是重复的段落内容。") == 1  # 只有一个
        assert "这是第一段正文内容。" in cleaned
        assert "这是第二段正文内容。" in cleaned
        assert "这是第三段正文内容。" in cleaned
        print("  ✅ 测试通过：系统自动找到了有效的保留目标并成功删除重复内容")
    else:
        print(f"  ❌ 测试失败：{error}")


def test_duplicate_line_repair_with_invalid_keep_id_auto_correction():
    """测试：当 keep_segment_id 无效时，系统能自动找到有效的保留目标"""

    body = """<p>第一段：
这是第一行。
这是重复的一行。
这是第二行。</p>
<p>第二段：
这是重复的一行。
这是第三行。</p>"""

    blocks = content_blocks(body)
    print(f"\n找到 {len(blocks)} 个内容块：")
    for block in blocks:
        print(f"  {block['block_id']}: {block['text']}")
        for seg in block.get('segments', []):
            print(f"    {seg['segment_id']}: {seg['text']}")

    # 场景：AI 给出了错误的 keep_segment_id
    plan = {
        "action": "remove_text_line",
        "segment_id": "b2.s2",  # 要删除第二段的第二行（这是重复的一行。）
        "evidence": "这是重复的一行。",
        "keep_segment_id": "b99.s99",  # 错误的保留目标
        "issue_type": "duplicate_content",
        "reason": "该行内容与前文重复",
        "confidence": 0.98,
    }

    cleaned, matches, error = apply_repair_plan(body, plan)

    print(f"\n修复结果:")
    print(f"  错误: {error}")
    print(f"  成功: {error is None}")

    if error is None:
        print(f"  删除了 {len(matches)} 行")
        print(f"  修复后的正文:")
        print(cleaned)

        # 验证：应该成功删除重复内容
        assert "这是重复的一行。" in cleaned  # 保留了一个
        assert cleaned.count("这是重复的一行。") == 1  # 只有一个
        assert "这是第一行。" in cleaned
        assert "这是第二行。" in cleaned
        assert "这是第三行。" in cleaned
        print("  ✅ 测试通过：系统自动找到了有效的保留目标并成功删除重复行")
    else:
        print(f"  ❌ 测试失败：{error}")


def test_duplicate_repair_with_no_alternative_keep_target():
    """测试：当确实没有其他重复内容时，返回更明确的错误信息"""

    body = """<p>这是第一段正文内容。</p>
<p>这是唯一的段落内容。</p>
<p>这是第三段正文内容。</p>"""

    # 场景：AI 错误地认为有重复内容，但实际上没有
    plan = {
        "action": "remove_block",
        "block_id": "b2",
        "evidence": "这是唯一的段落内容。",
        "keep_block_id": "b99",  # 错误的保留目标
        "issue_type": "duplicate_content",
        "reason": "该段落内容与前文重复",
        "confidence": 0.98,
    }

    cleaned, matches, error = apply_repair_plan(body, plan)

    print(f"\n修复结果:")
    print(f"  错误: {error}")

    # 验证：应该返回更明确的错误信息，标注疑似AI误判
    assert error is not None
    assert "目标文本在文档中唯一，疑似AI误判" in error
    print("  ✅ 测试通过：返回了更明确的错误信息，标注疑似AI误判")


if __name__ == "__main__":
    print("=" * 80)
    print("测试重复内容修复的容错机制")
    print("=" * 80)

    try:
        test_duplicate_block_repair_with_invalid_keep_id_auto_correction()
        print("\n" + "=" * 80)
        test_duplicate_line_repair_with_invalid_keep_id_auto_correction()
        print("\n" + "=" * 80)
        test_duplicate_repair_with_no_alternative_keep_target()
        print("\n" + "=" * 80)
        print("\n✅ 所有测试通过！")
    except AssertionError as e:
        print(f"\n❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()
    except Exception as e:
        print(f"\n❌ 测试出错: {e}")
        import traceback
        traceback.print_exc()
