import pytest

from azkims import db
from azkims.config import PROJECT_ROOT
from azkims.dictionary import load_dictionary
from azkims.reading import ReadingConverter, word_readings
from azkims.search import parse_query, search_timestamps
from azkims.timestamps import parse_comment_text, parse_comments

SAMPLE = """【タイムスタンプ】（ネタバレ注意）

3:36  開始
　　今日は鉄道の日！
└5:11  車両基地が400以上あるということ？
└13:24  :_guess:
└33:30  駅発見！「ぴぴぴぴぴ」かわいい
1:02:03  GeoGuessrのUIが変わっている
"""


def test_parse_comment_text():
    entries = parse_comment_text(SAMPLE)
    assert [(e.sec, e.text, e.is_note) for e in entries] == [
        (216, "開始", False),
        (216, "今日は鉄道の日!", True),
        (311, "車両基地が400以上あるということ?", False),
        (2010, "駅発見!「ぴぴぴぴぴ」かわいい", False),
        (3723, "GeoGuessrのUIが変わっている", False),
    ]  # スタンプだけの行（:_guess:）と見出しは捨てる
    assert entries[3].quotes == ("ぴぴぴぴぴ",)


def test_parse_comments_merges_replies_and_skips_chatter():
    comments = [
        {"text": "5:00  後半"},
        {"text": "1:00  前半"},
        {"text": "@someone 付け忘れていました。ご指摘感謝いたします。"},  # 時刻なしの返信
    ]
    assert [(e.sec, e.text) for e in parse_comments(comments)] == [(60, "前半"), (300, "後半")]


@pytest.fixture()
def conn():
    dictionary = load_dictionary(PROJECT_ROOT / "config" / "dictionary", include_accepted=False)
    converter = ReadingConverter(dictionary)
    c = db.connect(":memory:")
    c.execute(
        "INSERT INTO streams (video_id, title, upload_date, caption_status) "
        "VALUES ('v1', '【GeoGuessr】鉄道', '2026-10-14', 'ok')"
    )
    for e in parse_comment_text(SAMPLE):
        tokens = converter.tokens(e.text)
        db.insert_ts_entry(
            c,
            "v1",
            e.sec,
            e.text,
            list(e.quotes),
            e.is_note,
            "".join(t.reading for t in tokens),
            word_readings(tokens) + word_readings(converter.tokens(e.text, fine=True)),
        )
    c.commit()
    return c, converter


def test_search_timestamps_surface_and_reading(conn):
    conn, converter = conn
    hits, total = search_timestamps(conn, parse_query("鉄道", converter))
    assert total == 1 and hits[0].label == "原文"
    assert hits[0].url == "https://www.youtube.com/watch?v=v1&t=216s"
    hits, _ = search_timestamps(conn, parse_query("じおげっさー", converter))
    assert [(h.text, h.label) for h in hits] == [("GeoGuessrのUIが変わっている", "読み")]
