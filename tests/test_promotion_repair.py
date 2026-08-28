from __future__ import annotations

from app.services.promotion_repair import (
    body_safety_stats,
    find_promotional_blocks,
    normalize_photo_credits,
    remove_promotional_blocks,
)


def test_removes_only_high_confidence_standalone_plain_text_block() -> None:
    body = (
        '<p class="lead">球队在下半场完成逆转。</p>'
        '<p data-source="feed">立即点击官网查看完整赛程</p>'
        '<p>主教练赛后接受了采访。</p>'
    )

    cleaned, matches = remove_promotional_blocks(body)

    assert cleaned == (
        '<p class="lead">球队在下半场完成逆转。</p>'
        '<p>主教练赛后接受了采访。</p>'
    )
    assert [(item["tag"], item["rule"], item["text"]) for item in matches] == [
        ("p", "standalone_call_to_action", "立即点击官网查看完整赛程")
    ]


def test_matches_real_aleagues_full_schedule_prompt() -> None:
    body = (
        '<p>新赛季揭幕战将在十月进行。</p>'
        '<p>点击查看2026/27赛季五十铃UTE澳超完整赛程</p>'
    )

    cleaned, matches = remove_promotional_blocks(body)

    assert cleaned == '<p>新赛季揭幕战将在十月进行。</p>'
    assert len(matches) == 1
    assert matches[0]["text"] == "点击查看2026/27赛季五十铃UTE澳超完整赛程"
    assert matches[0]["rule"] == "standalone_call_to_action"


def test_removes_all_high_confidence_blocks_in_one_pass() -> None:
    body = (
        '<p>正文第一段，介绍比赛过程。</p>'
        '<P>扫码关注获取详情</P>'
        '<div class="promotion">更多内容请点击查看</div>'
        '<p>正文最后一段，介绍赛后采访。</p>'
    )

    cleaned, matches = remove_promotional_blocks(body)

    assert cleaned == (
        '<p>正文第一段，介绍比赛过程。</p>'
        '<p>正文最后一段，介绍赛后采访。</p>'
    )
    assert len(matches) == 2
    assert [item["start"] for item in matches] == sorted(item["start"] for item in matches)


def test_images_and_surrounding_markup_are_byte_for_byte_unchanged() -> None:
    image = '<figure><img src="/fastdfs8/one.jpg" alt="赛场图" width="1200"></figure>'
    body = (
        '<h2>比赛战报</h2>'
        f"{image}"
        '<p>球队凭借终场前的进球取胜。</p>'
        '<p>立即下载官网链接获取详情</p>'
    )

    cleaned, matches = remove_promotional_blocks(body)

    assert cleaned == (
        '<h2>比赛战报</h2>'
        f"{image}"
        '<p>球队凭借终场前的进球取胜。</p>'
    )
    assert cleaned.count(image) == 1
    assert len(matches) == 1


def test_does_not_remove_non_standalone_or_rich_text_content() -> None:
    body = (
        '<p>主教练表示，点击查看回放能够帮助球员复盘比赛。</p>'
        '<p>点击查看回放能够帮助球员复盘比赛。</p>'
        '<p><strong>立即点击官网查看完整赛程</strong></p>'
        '<p><img src="/qr.jpg" alt="扫码关注获取详情"></p>'
    )

    assert remove_promotional_blocks(body) == (body, [])


def test_does_not_modify_promotion_like_markup_inside_comments_or_raw_elements() -> None:
    body = (
        '<!-- template: <p>点击查看完整赛程</p> -->'
        '<script>const card = "<p>点击查看完整赛程</p>";</script>'
        '<template><p>商务咨询：editor@example.com</p></template>'
        '<p>这是文章中真实存在且需要保留的正常正文。</p>'
    )

    assert remove_promotional_blocks(body) == (body, [])


def test_does_not_normalize_photo_marker_inside_comment_or_script() -> None:
    body = (
        '<!-- <p>球员庆祝 [照片]=Getty Images</p> -->'
        '<script>const caption = "<p>球员庆祝 [照片]=Getty Images</p>";</script>'
    )

    assert normalize_photo_credits(body) == (body, [])


def test_unmatched_body_is_byte_for_byte_unchanged() -> None:
    body = (
        " \n<!-- 点击官网查看详情 -->"
        '<ARTICLE><p class=\'lead\'>更多比赛信息将在赛后公布&nbsp;更新。</p>'
        '<img src="/one.jpg"></ARTICLE>\n '
    )

    cleaned, matches = remove_promotional_blocks(body)

    assert cleaned == body
    assert matches == []
    assert find_promotional_blocks(body) == []


def test_cleanup_is_idempotent_and_reports_matches_only_once() -> None:
    body = '<p>正文内容足够完整。</p><p>点击官网查看详情</p>'

    first_body, first_matches = remove_promotional_blocks(body)
    second_body, second_matches = remove_promotional_blocks(first_body)

    assert first_body == '<p>正文内容足够完整。</p>'
    assert len(first_matches) == 1
    assert second_body == first_body
    assert second_matches == []


def test_normalizes_getty_photo_credit_and_preserves_caption() -> None:
    body = '<p class="caption">球员在终场哨响后向球迷致意 [照片]=Getty Images</p>'

    normalized, matches = normalize_photo_credits(body)

    assert normalized == (
        '<p class="caption">球员在终场哨响后向球迷致意'
        '（图片来源：Getty Images）</p>'
    )
    assert matches == [{
        "rule": "photo_credit_marker",
        "tag": "p",
        "caption": "球员在终场哨响后向球迷致意",
        "source": "Getty Images",
        "before": "球员在终场哨响后向球迷致意 [照片]=Getty Images",
        "after": "球员在终场哨响后向球迷致意（图片来源：Getty Images）",
    }]


def test_normalizes_fullwidth_equals_without_losing_semantic_parentheses() -> None:
    caption = "悉尼队球员在主场庆祝进球（比赛第88分钟）"
    body = f"<div>{caption} [照片]＝Getty Images</div>"

    normalized, matches = normalize_photo_credits(body)

    assert normalized == f"<div>{caption}（图片来源：Getty Images）</div>"
    assert matches[0]["caption"] == caption
    assert matches[0]["source"] == "Getty Images"


def test_normalizes_photographer_credit_and_removes_feed_separator() -> None:
    body = '<p>球员在赛后向看台致意。/ 摄影：中地拓也</p>'

    normalized, matches = normalize_photo_credits(body)

    assert normalized == '<p>球员在赛后向看台致意。（图片来源：中地拓也）</p>'
    assert matches[0]["rule"] == "photographer_credit_marker"
    assert matches[0]["before"] == "球员在赛后向看台致意。/ 摄影：中地拓也"
    assert matches[0]["after"] == "球员在赛后向看台致意。（图片来源：中地拓也）"


def test_plain_photo_label_without_credit_assignment_is_unchanged() -> None:
    body = (
        '<p>球队赛前训练画面 [照片]</p>'
        '<p>Getty Images [照片] 提供了比赛资料图</p>'
    )

    assert normalize_photo_credits(body) == (body, [])


def test_photo_credit_normalization_preserves_image_count_sources_and_order() -> None:
    first_image = '<img src="/fastdfs8/first.jpg" alt="首图" width="1200">'
    second_image = '<IMG loading="lazy" SRC="https://img.example/second.jpg">'
    body = (
        f'<figure>{first_image}</figure>'
        '<p>两队球员在赛后相互致意 [照片]=Getty Images</p>'
        f'<figure class="second">{second_image}</figure>'
    )
    before = body_safety_stats(body)

    normalized, matches = normalize_photo_credits(body)
    after = body_safety_stats(normalized)

    assert len(matches) == 1
    assert first_image in normalized
    assert second_image in normalized
    assert before["image_count"] == after["image_count"] == 2
    assert before["image_sources"] == after["image_sources"] == [
        "/fastdfs8/first.jpg",
        "https://img.example/second.jpg",
    ]
    assert normalized.index(first_image) < normalized.index(second_image)


def test_tail_only_promotions_match_only_at_article_end() -> None:
    cooperation = '<p>商务咨询：editor@example.com</p>'
    trailing = (
        '<article><p>正文介绍了球队备战和新赛季安排。</p>'
        f'{cooperation}<!-- feed end --></article>'
    )
    middle = (
        '<article><p>正文介绍了球队备战。</p>'
        f'{cooperation}<p>记者随后补充了比赛信息。</p></article>'
    )

    trailing_cleaned, trailing_matches = remove_promotional_blocks(trailing)

    assert trailing_cleaned == (
        '<article><p>正文介绍了球队备战和新赛季安排。</p>'
        '<!-- feed end --></article>'
    )
    assert [item["rule"] for item in trailing_matches] == [
        "standalone_cooperation_contact"
    ]
    assert remove_promotional_blocks(middle) == (middle, [])


def test_tail_media_call_to_action_allows_only_comments_and_container_closers_after_it() -> None:
    prompt = '<p>点击收听完整播客</p>'
    at_tail = f'<section><p>节目回顾了本轮比赛。</p>{prompt}\n<!-- end --></section>'
    not_at_tail = f'<section>{prompt}<p>节目还采访了主教练。</p></section>'

    cleaned, matches = remove_promotional_blocks(at_tail)

    assert cleaned == '<section><p>节目回顾了本轮比赛。</p>\n<!-- end --></section>'
    assert [item["rule"] for item in matches] == ["standalone_media_call_to_action"]
    assert remove_promotional_blocks(not_at_tail) == (not_at_tail, [])


def test_removes_known_ge_podcast_and_watch_prompts_only_at_tail() -> None:
    body = (
        '<article><p>正文介绍了球队最新备战情况。</p>'
        '<p>🎧 收听 ge 帕尔梅拉斯播客 🎧</p>'
        '<p>+ 在 Globo、sportv 和 ge 收看帕尔梅拉斯的全部内容</p>'
        '</article>'
    )

    cleaned, matches = remove_promotional_blocks(body)

    assert cleaned == '<article><p>正文介绍了球队最新备战情况。</p></article>'
    assert [item["rule"] for item in matches] == [
        "standalone_branded_podcast_prompt",
        "standalone_branded_watch_prompt",
    ]


def test_does_not_remove_branded_watch_sentence_from_middle_of_article() -> None:
    body = (
        '<p>观看：在ge、Globo和sportv了解关于弗拉门戈的一切</p>'
        '<p>随后，主教练公布了本轮比赛的首发阵容。</p>'
    )

    assert remove_promotional_blocks(body) == (body, [])
