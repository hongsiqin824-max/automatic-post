#!/usr/bin/env python3
"""测试LLM API Key是否有效"""

import json
import requests
import time

# 主模型配置
PRIMARY_KEY = "sk-0sQ14kjzXTIQasDrnvupLXTme5Tdcq29y8gA3QVMMwRwXwSV"
PRIMARY_BASE = "https://ai.flashapi.top/v1"
PRIMARY_MODEL = "gpt-5.5"

# 降级模型配置
FALLBACK_KEY = "sk-pUX73JXuz9JZ0X2VL8vImsHGiwNk44d95SEt6IFQSJeUmnoD"
FALLBACK_BASE = "https://tokenhub.dongqiudi.com/v1"
FALLBACK_MODEL = "deepseek-v4-pro"

def test_api(name, base_url, api_key, model):
    """测试单个API"""
    print(f"\n{'='*60}")
    print(f"测试 {name}")
    print(f"Base URL: {base_url}")
    print(f"Model: {model}")
    print(f"Key: {api_key[:20]}...")

    url = f"{base_url}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": model,
        "messages": [
            {"role": "user", "content": "测试：请回复'OK'"}
        ],
        "max_tokens": 10,
        "temperature": 0
    }

    try:
        start = time.time()
        response = requests.post(url, headers=headers, json=payload, timeout=30)
        elapsed = int((time.time() - start) * 1000)

        print(f"状态码: {response.status_code}")
        print(f"耗时: {elapsed}ms")

        if response.status_code == 200:
            data = response.json()
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            print(f"✅ 成功！响应: {content}")
            return True
        else:
            print(f"❌ 失败！")
            print(f"响应: {response.text[:500]}")
            return False

    except Exception as e:
        print(f"❌ 异常: {e}")
        return False

if __name__ == "__main__":
    print("开始测试LLM API Keys...")

    # 测试主模型
    primary_ok = test_api("主模型 (flashapi.top)", PRIMARY_BASE, PRIMARY_KEY, PRIMARY_MODEL)

    # 测试降级模型
    fallback_ok = test_api("降级模型 (tokenhub.dongqiudi.com)", FALLBACK_BASE, FALLBACK_KEY, FALLBACK_MODEL)

    # 总结
    print(f"\n{'='*60}")
    print("测试结果总结:")
    print(f"  主模型 (gpt-5.5):        {'✅ 正常' if primary_ok else '❌ 失败'}")
    print(f"  降级模型 (deepseek-v4-pro): {'✅ 正常' if fallback_ok else '❌ 失败'}")

    if not primary_ok and not fallback_ok:
        print("\n⚠️  两个模型都无法使用！需要立即处理")
    elif not primary_ok:
        print("\n⚠️  主模型失效，但降级模型可用")
    elif not fallback_ok:
        print("\n⚠️  主模型正常，但降级模型失效")
    else:
        print("\n✅ 两个模型都正常")
