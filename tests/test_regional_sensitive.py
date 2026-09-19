"""测试地域敏感词拦截功能"""
from __future__ import annotations

import pytest

from app.services.quality import evaluate, _check_sensitive_regions


def test_check_sensitive_regions_taiwan_keyword():
    """测试台湾关键词拦截"""
    title = "金倒永：我们比台湾更强"
    body = "<p>亚运会代表队迎来金倒永，WBC对台狂轰猛打的记忆。</p>"

    matched, reason = _check_sensitive_regions(title, body)

    assert matched == "台湾"
    assert "敏感地域信息" in reason
    assert "台湾" in reason


def test_check_sensitive_regions_hongkong_keyword():
    """测试香港关键词拦截"""
    title = "球员谈中国香港队"
    body = "<p>除了台湾，还有中国香港等多支队伍参赛。</p>"

    matched, reason = _check_sensitive_regions(title, body)

    # 应该匹配到第一个出现的敏感词
    assert matched in ("台湾", "中国香港", "香港")
    assert "敏感地域信息" in reason


def test_check_sensitive_regions_traditional_chinese():
    """测试繁体字拦截"""
    title = "國際賽事報導"
    body = "<p>本届赛事臺灣队表现出色。</p>"

    matched, reason = _check_sensitive_regions(title, body)

    assert matched == "臺灣"
    assert "敏感地域信息" in reason


def test_check_sensitive_regions_chinese_taipei():
    """测试中华台北拦截"""
    title = "中华台北队战胜对手"
    body = "<p>中华台北代表队在比赛中表现优异。</p>"

    matched, reason = _check_sensitive_regions(title, body)

    assert matched == "中华台北"
    assert "敏感地域信息" in reason


def test_check_sensitive_regions_city_names():
    """测试台湾城市名称拦截"""
    title = "台北球员加盟球队"
    body = "<p>来自台北的年轻球员签约成功。</p>"

    matched, reason = _check_sensitive_regions(title, body)

    assert matched == "台北"
    assert "敏感地域信息" in reason


def test_check_sensitive_regions_no_match():
    """测试正常文章不被拦截"""
    title = "皇马击败巴萨获得胜利"
    body = "<p>皇家马德里在主场3:1战胜巴塞罗那，取得关键三分。</p>"

    matched, reason = _check_sensitive_regions(title, body)

    assert matched is None
    assert reason is None


def test_check_sensitive_regions_macau():
    """测试澳门关键词拦截"""
    title = "球队澳门行"
    body = "<p>球队将前往澳门进行国家队。</p>"

    matched, reason = _check_sensitive_regions(title, body)

    assert matched == "澳门"
    assert "敏感地域信息" in reason


def test_evaluate_blocks_taiwan_content():
    """测试evaluate函数拦截台湾内容"""
    title = "金倒永：我们比台湾更强"
    body = "<p>亚运会代表队迎来金倒永，WBC对台狂轰猛打的记忆。代表队于14日集结，15日在首尔高尺天空巨蛋开始训练。</p>"

    result = evaluate(title=title, body=body, channels=[11, 12])

    assert result["pass"] is False
    assert result["needs_review"] is True
    assert result["regional_sensitive"] is True
    assert result["matched_keyword"] == "台湾"
    assert "敏感地域信息" in result["reason"]
    assert "台湾" in result["reason"]
    assert "regional_sensitive" in result["issues"]


def test_evaluate_blocks_hongkong_content():
    """测试evaluate函数拦截香港内容"""
    title = "港队出战国际赛"
    body = "<p>中国香港队将参加本次国际足球赛事。</p>"

    result = evaluate(title=title, body=body, channels=[11, 12])

    assert result["pass"] is False
    assert result["needs_review"] is True
    assert result["regional_sensitive"] is True
    assert result["matched_keyword"] in ("港队", "中国香港", "香港")


def test_evaluate_allows_normal_content():
    """测试evaluate函数放行正常内容"""
    title = "皇马3:1击败巴萨"
    body = "<p>皇家马德里在伯纳乌球场3:1战胜巴塞罗那，C罗梅开二度，本泽马锁定胜局。</p>"

    result = evaluate(title=title, body=body, channels=[11, 12])

    # 正常文章不应该因为地域敏感词被拦截
    assert "regional_sensitive" not in result or result.get("regional_sensitive") is not True


def test_evaluate_checks_body_content_deeply():
    """测试检查正文前2000字"""
    title = "国际足球赛事综述"
    long_body = "<p>" + "正常内容。" * 100 + "台湾球员表现出色。" + "</p>"

    result = evaluate(title=title, body=long_body, channels=[11, 12])

    # 即使在正文中间，也应该被检测到
    assert result["pass"] is False
    assert result["regional_sensitive"] is True
    assert result["matched_keyword"] == "台湾"


def test_evaluate_regional_check_before_llm():
    """测试地域敏感词检查优先于LLM调用（节省成本）"""
    title = "金倒永：我们比台湾更强"
    body = "<p>WBC对台狂轰猛打的记忆。</p>"

    # 不传入llm参数
    result = evaluate(title=title, body=body, channels=[11, 12])

    # 应该在LLM检查之前就被拦截
    assert result["pass"] is False
    assert result["regional_sensitive"] is True
    assert result["matched_keyword"] == "台湾"
    # semantic_check不应该被调用
    assert "semantic_check" not in result or result.get("semantic_check") == {}
