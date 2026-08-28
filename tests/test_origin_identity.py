from app.origin_identity import source_origin_key


def test_kbs_url_variants_share_the_same_origin_key():
    assert source_origin_key(
        "kbs", "https://news.kbs.co.kr/news/view.do?ncd=8635111"
    ) == "kbs:ncd:8635111"
    assert source_origin_key(
        "KBS", "https://news.kbs.co.kr/news/pc/view/view.do?foo=1&ncd=8635111#top"
    ) == "kbs:ncd:8635111"


def test_origin_key_requires_an_unambiguous_kbs_article_id():
    assert source_origin_key("other", "https://news.kbs.co.kr/news/view.do?ncd=8635111") is None
    assert source_origin_key("kbs", "https://example.com/news/view.do?ncd=8635111") is None
    assert source_origin_key("kbs", "https://news.kbs.co.kr/news/view.do") is None
    assert source_origin_key("kbs", "https://news.kbs.co.kr/news/view.do?ncd=abc") is None
    assert source_origin_key(
        "kbs", "https://news.kbs.co.kr/news/view.do?ncd=1&ncd=2"
    ) is None
