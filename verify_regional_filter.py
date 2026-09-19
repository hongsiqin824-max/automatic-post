#!/usr/bin/env python3
"""验证地域敏感词拦截功能"""

from app.services.quality import evaluate

# 测试用例：包含台湾关键词的真实文章
test_cases = [
    {
        "name": "包含'台湾'关键词",
        "title": "金倒永：我们比台湾更强",
        "body": """
            <p>亚运会代表队迎来金倒永，WBC对台狂轰猛打的记忆："如果能发挥到那时的水准......"我们球员比台湾球员更出色"</p>
            <p>本届亚运会棒球项目最大的竞争对手是台湾。伤愈后顺利入选代表队的金倒永，曾在世界棒球经典赛（WBC）对台湾吐火力全开。他期待自己这次也能打出同样的表现。</p>
            <p>代表队于14日集结，15日在首尔高尺天空巨蛋开始训练。当天最受关注的无疑是金倒永。他在9日光州对NC的比赛中膝盖被来球击中鼻子，随后被诊断为鼻骨骨折，但他顺利完成手术并加入了代表队。</p>
            <p>他戴着护鼻器参加训练，据称完全没有不适。金倒永表示了决心，尤其是最强劲的竞争对手台湾视为重点攻坚对象。</p>
            <p>15日训练结束后接受采访时，金倒永强调："球会同样扎心念头去面对所有球队。要想保持状态，每一场都不能放松。除了台湾，还有中国香港等多支队伍，我都会全力以赴。"</p>
        """
    },
    {
        "name": "包含'香港'关键词",
        "title": "国际足球赛事综述",
        "body": "<p>除了日本队之外，中国香港队也将参加本次比赛。港队近期表现不俗。</p>"
    },
    {
        "name": "包含'中华台北'",
        "title": "亚运会赛况",
        "body": "<p>中华台北代表队在本届亚运会上取得了不错的成绩。</p>"
    },
    {
        "name": "包含台湾城市'台北'",
        "title": "台北球员加盟欧洲球队",
        "body": "<p>来自台北的年轻球员成功签约德甲球队。</p>"
    },
    {
        "name": "正常文章（不应被拦截）",
        "title": "皇马3:1击败巴萨",
        "body": "<p>皇家马德里在伯纳乌球场3:1战胜巴塞罗那，C罗梅开二度，本泽马锁定胜局。比赛精彩纷呈，双方球员发挥出色。</p>"
    }
]

print("=" * 80)
print("地域敏感词拦截功能验证")
print("=" * 80)

for i, case in enumerate(test_cases, 1):
    print(f"\n【测试 {i}】{case['name']}")
    print(f"标题：{case['title']}")

    result = evaluate(
        title=case['title'],
        body=case['body'],
        channels=[11, 12]
    )

    if result.get("regional_sensitive"):
        print(f"✅ 已拦截")
        print(f"   匹配关键词：{result.get('matched_keyword')}")
        print(f"   拦截原因：{result.get('reason')}")
        print(f"   需人工审核：{result.get('needs_review')}")
    else:
        print(f"✓ 未拦截（正常通过）")
        if result.get("pass"):
            print(f"   质检结果：通过")
        else:
            print(f"   质检结果：因其他原因需审核")
            if result.get("reason"):
                print(f"   原因：{result.get('reason')}")

print("\n" + "=" * 80)
print("验证完成")
print("=" * 80)
