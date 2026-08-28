"""Seed data copied from the material API document and the legacy tab list."""

from __future__ import annotations


# ``backend_tab_id`` values are the IDs used by the DQD publishing backend.
DEFAULT_TABS = (
    {"backend_tab_id": 1, "name": "头条"},
    {"backend_tab_id": 284, "name": "快讯"},
    {"backend_tab_id": 110, "name": "转会"},
    {"backend_tab_id": 253, "name": "足球"},
    {"backend_tab_id": 247, "name": "篮球"},
    {"backend_tab_id": 248, "name": "体坛"},
    {"backend_tab_id": 249, "name": "电竞"},
    {"backend_tab_id": 55, "name": "深度"},
    {"backend_tab_id": 114, "name": "世界杯"},
    {"backend_tab_id": 58, "name": "精选"},
    {"backend_tab_id": -2, "name": "耗时"},
    {"backend_tab_id": -100, "name": "gif合集"},
    {"backend_tab_id": 335, "name": "苏超"},
    {"backend_tab_id": 240, "name": "小酒馆"},
    # Confirmed event columns used by the user_name league routing rules.
    # Keeping them in the first-run catalog lets a fresh deployment receive
    # the same editable defaults as an upgraded production database.
    {"backend_tab_id": 12, "name": "法甲"},
    {"backend_tab_id": 56, "name": "中超"},
    {"backend_tab_id": 289, "name": "NBA"},
    {"backend_tab_id": 290, "name": "CBA"},
    {"backend_tab_id": 348, "name": "澳超"},
    {"backend_tab_id": 349, "name": "日职联"},
    {"backend_tab_id": 357, "name": "欧冠"},
    {"backend_tab_id": 358, "name": "欧联"},
    {"backend_tab_id": 359, "name": "韩K"},
    {"backend_tab_id": 360, "name": "世俱杯"},
    {"backend_tab_id": 361, "name": "瑞典超"},
    {"backend_tab_id": 362, "name": "美职联"},
    {"backend_tab_id": 363, "name": "挪超"},
    {"backend_tab_id": 370, "name": "德乙"},
    {"backend_tab_id": 373, "name": "英冠"},
    {"backend_tab_id": 376, "name": "日职乙"},
    {"backend_tab_id": 377, "name": "土超"},
    {"backend_tab_id": 378, "name": "巴甲"},
    {"backend_tab_id": 379, "name": "葡超"},
    {"backend_tab_id": 380, "name": "荷甲"},
    {"backend_tab_id": 383, "name": "墨甲"},
)


# ``display_name`` is the human-readable short name shown in configuration.
# The codes and names are the complete source enum in the supplied PDF.
DEFAULT_SOURCES = (
    ("aleagues", "澳超官方"),
    ("as", "阿斯"),
    ("bild_de", "图片报"),
    ("br", "B/R"),
    ("bvb_de", "多特官方"),
    ("chosun", "韩媒"),
    ("corsport", "罗体"),
    ("dmail", "邮报"),
    ("espn", "ESPN"),
    ("f1com", "F1官网"),
    ("fbldn", "英媒"),
    ("fcinter", "意媒"),
    ("fifa", "FIFA官方"),
    ("gazzetta", "米体"),
    ("ge_globo", "环球体育"),
    ("goal", "进球网"),
    ("jleague", "J联赛官方"),
    ("kbs", "韩媒"),
    ("lequipe", "队报"),
    ("lvpecho", "回声报"),
    ("marca", "马卡"),
    ("mevenews", "曼晚"),
    ("mnews", "意媒"),
    ("mundod", "世体"),
    ("naversp", "韩媒"),
    ("nikkan", "日刊体育"),
    ("nytimes", "TA"),
    ("repub", "共和报"),
    ("rmcsport", "RMC"),
    ("sky", "天空体育"),
    ("sport1", "德媒"),
    ("sport_es", "每体"),
    ("spmset", "意媒"),
    ("sponichi", "体育日本"),
    ("talksp", "英媒"),
    ("tele", "电讯报"),
    ("thesun", "太阳报"),
    ("tmkt", "德转"),
    ("tmw", "全市场"),
    ("tnscom", "网球杂志"),
    ("tsport", "都体"),
    ("yahoojp", "日媒"),
    ("skj", "日媒"),
    ("bbking", "篮球王"),
    ("mcsai", "日媒"),
    ("arsenal", "阿森纳官方"),
    ("mancity", "曼城官方"),
    ("manutd", "曼联官方"),
    ("lfcofcl", "利物浦官方"),
    ("cfc", "切尔西官方"),
    ("thfc", "热刺官方"),
    ("rmadrid", "皇马官方"),
    ("fcb", "巴萨官方"),
    ("fcbayern", "拜仁官方"),
    ("acmilan", "米兰官方"),
    ("inter", "国米官方"),
    ("juve", "尤文官方"),
    ("psg", "巴黎官方"),
    ("skyde", "德国天空体育"),
    ("kicker", "踢球者"),
    ("aips", "AIPS"),
    ("ansa", "意媒"),
    ("bbc", "BBC"),
    ("calciomercato", "意媒"),
    ("chinanews", "中新网"),
    ("dfb", "德足协"),
    ("footmercato", "法媒"),
    ("gocards", "美媒"),
    ("guardian", "卫报"),
    ("inews", "英媒"),
    ("jinwanbao", "今晚报"),
    ("leparisien", "巴黎人报"),
    ("mirror", "镜报"),
    ("paper_xinmin", "新民晚报"),
    ("rtvslo", "斯媒"),
    ("skynews", "天空"),
    ("sportsroad", "运动路"),
    ("sslazio", "拉齐奥官方"),
    ("theifab", "IFAB"),
    ("theringer", "美媒"),
    ("tribalfootball", "英媒"),
    ("ttplus", "乒乓世界"),
    ("tv7dias", "葡媒"),
    ("wy0538", "我爱足球"),
    ("x", "社媒"),
)


def source_catalog():
    """Return source dictionaries, useful to callers that need stable keys."""

    return [{"code": code, "display_name": name} for code, name in DEFAULT_SOURCES]


def tab_catalog():
    """Return a copy of the default tab dictionaries."""

    return [dict(item) for item in DEFAULT_TABS]


def seed_catalog(connection=None) -> dict:
    """Insert missing tabs and sources while preserving user configuration.

    New sources start disabled and unassigned, so a first run cannot fetch or
    publish an unexpected source before an operator chooses its tab.
    """

    from . import repository
    from .db import get_db, seed_event_tab_rules

    conn = connection or get_db()
    already_seeded = bool(repository.get_setting("default_catalog_seeded", False, conn))
    result = repository.seed_catalog(
        conn,
        tabs=() if already_seeded else DEFAULT_TABS,
        sources=DEFAULT_SOURCES,
    )
    if not already_seeded:
        repository.set_setting("default_catalog_seeded", True, conn)
    # init_db() runs before the catalog is populated on a fresh database.
    # Run the idempotent rule seed again now that target tabs exist.
    seed_event_tab_rules(conn)
    return result
