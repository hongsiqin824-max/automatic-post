"""测试推广内容识别和删除 - 验证 #17196 类似文章能否通过质检"""

from app.services.quality import semantic_check, LLMService
import os

# 模拟文章 #17196 的内容
TEST_TITLE = "环球体育：阿森纳客战桑德兰，延贝尔莫斯克拉伤缺"

TEST_BODY = """
<p>阿森纳在意大利1比0战胜那不勒斯完成欧冠开门红后，本周六将客场对阵桑德兰，比赛北京时间16点在光明球场打响。ge 将实时跟进本场比赛（点击这里）。</p>

<p>阿尔特塔的球队本赛季开局并不只靠欧战取胜。阿森纳已经赢下全部5场正式比赛，其中3场英超、前文提到的欧冠，以及在英格兰超级杯中3比0轻取曼城。</p>

<p>阿森纳在英超的第三场胜利来自上周日的伦敦德比，对手是切尔西。切尔西早早破门，但哈弗茨和厄德高帮助阿森纳以2比1完成逆转。</p>

<p>比赛时间：2026年9月12日 北京时间16点<br/>
比赛地点：英格兰桑德兰，光明球场<br/>
直播平台：Disney+（流媒体）和ge实时跟进（点击这里）。</p>

<p>桑德兰：鲁夫斯；穆杰莱、巴拉德、丹索和雷尼尔多；扎卡和萨迪基；安古洛、勒费和福法纳；布罗比。主教练：罗热-勒布里斯。</p>
"""


def test_promotion_detection():
    """测试 AI 能否正确识别并提供删除推广内容的修复计划"""

    # 从环境变量获取 API 配置
    api_key = os.environ.get("LLM_API_KEY", "")
    base_url = os.environ.get("LLM_BASE_URL", "")
    model = os.environ.get("LLM_MODEL", "gpt-4o")

    if not api_key:
        print("⚠️  未配置 LLM_API_KEY，跳过测试")
        return

    llm = LLMService(
        api_key=api_key,
        base_url=base_url,
        model=model,
        timeout=45,
    )

    print(f"🔍 使用模型：{model}")
    print(f"📝 测试文章：{TEST_TITLE}")
    print("-" * 80)

    # 调用语义质检
    result = semantic_check(TEST_TITLE, TEST_BODY, llm)

    print("\n✅ AI 质检结果：")
    print(f"  - 标题完整：{result.get('title_complete')}")
    print(f"  - 正文完整：{result.get('body_complete')}")
    print(f"  - 含广告/脏内容：{result.get('has_ad_or_dirty')}")
    print(f"  - 可修复：{result.get('repairable')}")
    print(f"  - 需要人工审核：{result.get('needs_review')}")
    print(f"  - 原因：{result.get('reason')}")

    repair_plans = result.get("repair_plans", [])
    print(f"\n📋 修复计划数量：{len(repair_plans)}")

    if repair_plans:
        for i, plan in enumerate(repair_plans, 1):
            print(f"\n  计划 {i}:")
            print(f"    - 目标 ID：{plan.get('block_id') or plan.get('segment_id') or plan.get('link_id')}")
            print(f"    - 操作：{plan.get('action')}")
            print(f"    - 问题类型：{plan.get('issue_type')}")
            print(f"    - 置信度：{plan.get('confidence')}")
            print(f"    - 原因：{plan.get('reason')}")
            print(f"    - 证据：{plan.get('evidence', '')[:80]}...")

    # 验证结果
    print("\n" + "=" * 80)

    success = True

    # 检查 1：应该识别出广告内容
    if result.get('has_ad_or_dirty') is not True:
        print("❌ 失败：AI 未识别出推广内容")
        success = False
    else:
        print("✅ 通过：AI 正确识别出推广内容")

    # 检查 2：应该提供修复计划
    if not repair_plans:
        print("❌ 失败：AI 未提供修复计划")
        success = False
    else:
        print("✅ 通过：AI 提供了修复计划")

    # 检查 3：应该标记为可修复
    if result.get('repairable') is not True:
        print("❌ 失败：AI 判断无法修复")
        success = False
    else:
        print("✅ 通过：AI 判断可以修复")

    # 检查 4：修复计划应该包含 "ge 将买时跟进本场比赛（点击这里）"
    if repair_plans:
        evidence_list = [p.get('evidence', '') for p in repair_plans]
        has_target_evidence = any('ge' in e and '跟进' in e for e in evidence_list)
        if not has_target_evidence:
            print("❌ 失败：修复计划未定位到目标推广内容")
            success = False
        else:
            print("✅ 通过：修复计划正确定位到推广内容")

    print("\n" + "=" * 80)
    if success:
        print("🎉 测试全部通过！AI 现在能正确识别并删除推广内容了")
    else:
        print("⚠️  部分测试未通过，需要进一步调整提示词")

    return success


if __name__ == "__main__":
    test_promotion_detection()
