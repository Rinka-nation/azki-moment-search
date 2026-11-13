import json

import pytest

from azkims import db, related
from azkims.captions import parse_captions, segment
from azkims.config import PROJECT_ROOT, Config
from azkims.dictionary import Dictionary, DictionaryError, Term, load_dictionary
from azkims.filters import exclusion_reason
from azkims.reading import ReadingConverter
from azkims.related import RelatedWord
from azkims.review import FuzzyMatcher, levenshtein
from azkims.search import QueryError, count_tiers, parse_query, search
from azkims.textnorm import fold_reading, normalize_text


@pytest.fixture(scope="module")
def dictionary():
    # レビュー結果（accepted.toml）に左右されないよう、同梱辞書だけで試す
    return load_dictionary(PROJECT_ROOT / "config" / "dictionary", include_accepted=False)


@pytest.fixture(scope="module")
def converter(dictionary):
    return ReadingConverter(dictionary)


def test_fold_reading_absorbs_asr_variants():
    assert fold_reading("ガッチャー") == fold_reading("がちゃ")
    assert fold_reading("ジオゲッサー") == fold_reading("じおげっさ")
    assert fold_reading("ヴィヴィ") == fold_reading("ビビ")


def test_normalize_text_removes_caption_tags():
    assert normalize_text("開拓者が作って[笑い]くれました") == "開拓者が作って くれました"


def test_bundled_dictionary_is_valid(dictionary):
    assert "星街すいせい" in dictionary.by_canonical
    assert "AZKi" in dictionary.by_canonical


def test_conflicting_alias_is_rejected():
    with pytest.raises(DictionaryError):
        Dictionary([Term("A1", "えー", ["エー"]), Term("A2", "えー", ["エー"])])


def test_apply_aliases_longest_match(dictionary):
    text, applied = dictionary.apply_aliases("昨日はホロ面とロザミアのあずきちとあずき")
    assert text == "昨日はホロメンとRosaMiAのあずきちとAZKi"
    assert ("ホロ面", "ホロメン") in applied
    assert ("あずき", "AZKi") in applied
    assert all(s != "あずきち" for s, _ in applied)


def test_reading_prefers_dictionary(converter):
    # Sudachi 単体だと 星街 = ホシガイ になる
    assert fold_reading("ほしまちすいせい") in converter.reading("星街すいせいさん")


def test_fuzzy_finds_reading_match(dictionary, converter):
    text = "今日はほしまち水星さんとコラボ"
    hits = FuzzyMatcher(dictionary).find(text, converter.tokens(text))
    assert [h.term.canonical for h in hits] == ["星街すいせい"]
    assert hits[0].surface == "ほしまち水星"


def test_levenshtein():
    assert levenshtein("ロザミア", "ロザミヤ") == 1
    assert levenshtein("", "abc") == 3


def _json3(events):
    return {"events": [{"tStartMs": 0, "dDurationMs": 999999, "id": 1}, *events]}


def test_segment_from_json3(tmp_path):
    data = _json3(
        [
            {
                "tStartMs": 1000,
                "segs": [{"utf8": "こんにちは。"}, {"utf8": "今日は", "tOffsetMs": 500}],
            },
            {"tStartMs": 1600, "aAppend": 1, "segs": [{"utf8": "\n"}]},
            {"tStartMs": 1700, "segs": [{"utf8": "ジオゲ[笑い]です"}]},
            {"tStartMs": 9000, "segs": [{"utf8": "久しぶり"}]},
        ]
    )
    path = tmp_path / "x.ja.json3"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    utts = parse_captions(path, gap_ms=2000)
    # 「こんにちは。」「今日はジオゲ」「です」は細切れなので 1 つにつながる
    assert [(u.start_ms, u.text) for u in utts] == [
        (1000, "こんにちは。今日はジオゲです"),
        (9000, "久しぶり"),
    ]


def test_segment_max_chars():
    chars = [("あ", i * 10) for i in range(150)]
    utts = segment(chars, max_chars=60, gap_ms=2000)
    assert [len(u.text) for u in utts] == [80, 70]


def _cfg(**kw):
    base = {
        "channel_url": "",
        "data_dir": PROJECT_ROOT,
        "output_dir": PROJECT_ROOT,
        "dictionary_dir": PROJECT_ROOT,
        "singing_title_patterns": ["歌枠", "Singing Stream"],
    }
    base.update(kw)
    return Config(**base)


def test_exclusion_reason():
    cfg = _cfg(include_video_ids={"keep"})
    assert exclusion_reason("a", "【歌枠】うたう", "public", "was_live", cfg) == "singing"
    assert exclusion_reason("a", "x SINGING STREAM", None, "was_live", cfg) == "singing"
    assert exclusion_reason("a", "雑談", "subscriber_only", "was_live", cfg) == "members_only"
    assert exclusion_reason("a", "雑談", None, "is_upcoming", cfg) == "live_status:is_upcoming"
    assert exclusion_reason("a", "雑談", None, "was_live", cfg) is None
    assert exclusion_reason("keep", "【歌枠】", None, "was_live", cfg) is None


def test_clip_prohibited_titles_are_excluded():
    # UT-116: タイトルに切り抜き禁止とある配信は外す。全角・半角や大文字小文字の違いも同じに扱う
    cfg = _cfg(no_clip_title_patterns=["切り抜き禁止", "切り抜きNG"])
    assert (
        exclusion_reason("a", "【雑談】話す【AZKi】※切り抜き禁止※", None, "was_live", cfg)
        == "no_clip"
    )
    assert exclusion_reason("a", "雑談 切り抜きｎｇ", None, "was_live", cfg) == "no_clip"
    assert exclusion_reason("a", "【歌枠】切り抜き禁止", None, "was_live", cfg) == "no_clip"
    assert exclusion_reason("a", "切り抜き歓迎！雑談", None, "was_live", cfg) is None


@pytest.fixture()
def conn(dictionary, converter):
    from azkims.pipeline import analyze
    from azkims.review import FuzzyMatcher

    matcher = FuzzyMatcher(dictionary)
    c = db.connect(":memory:")
    streams = [
        ("v1", "【GeoGuessr】鉄道", "2026-10-01", None),
        ("v2", "雑談", "2026-10-10", None),
        ("v3", "【歌枠】", "2026-10-11", "singing"),
        ("v4", "【#ホロドリ】釣り", "2026-10-12", None),
    ]
    c.executemany(
        "INSERT INTO streams (video_id, title, upload_date, caption_status, excluded_reason) "
        "VALUES (?, ?, ?, 'ok', ?)",
        streams,
    )
    titles = {vid: title for vid, title, _, _ in streams}
    for vid, ms, raw in [
        ("v1", 1000, "ジオゲッサーじゃないとたどり着けない"),
        ("v1", 5000, "すいちゃんと遊んだ"),
        ("v2", 2000, "昨日は水星ちゃんと遊んだ"),
        ("v2", 3000, "開拓者のみんなありがとう"),
        ("v3", 1000, "開拓者のみんな歌うよ"),
        ("v4", 1000, "ホロドリで釣り"),
        ("v4", 2000, "ほろりで釣り"),  # ホロドリ と読み距離 1（タイトルに #ホロドリ）→ 候補
        ("v4", 3000, "ほろドリで釣り"),  # ホロドリ と読み距離 0 → 候補
        ("v2", 4000, "釣り堀に行く"),
        ("v2", 4500, "京都鉄道博物館に行きたい"),
        ("v2", 4600, "夏祭りです"),
        ("v2", 5000, "開拓者のみんな、鉄道の日だよ"),
    ]:
        raw = normalize_text(raw)
        a = analyze(raw, titles[vid], dictionary, converter, matcher)
        db.insert_utterance(
            c,
            vid,
            ms,
            ms + 1000,
            raw,
            a.text,
            a.reading,
            a.corrections,
            a.text_fuzzy,
            a.fuzzy,
            a.words,
        )
    c.commit()
    return c


def test_search_alias_corrected_is_confirmed_tier(conn, converter):
    hits = search(conn, parse_query("GeoGuessr", converter))
    assert [(h.text, h.label) for h in hits] == [("GeoGuessrじゃないとたどり着けない", "補正")]
    assert hits[0].text_raw == "ジオゲッサーじゃないとたどり着けない"
    assert hits[0].url == "https://www.youtube.com/watch?v=v1&t=0s"
    # 原文の表記で検索すれば「原文」一致
    assert search(conn, parse_query("ジオゲッサー", converter))[0].label == "原文"


def test_kana_query_matches_kanji_word(conn, converter):
    # かな・カタカナで漢字語を探せる。語の境界で読みが一致すれば「一致」タブの「読み」
    for q in ("かいたくしゃ", "カイタクシャ"):
        hits = search(conn, parse_query(q, converter))
        assert {h.label for h in hits} == {"読み"}
        assert len(hits) == 2
    # 複合語も短単位の境界で一致（鉄道博物館 = 鉄道|博物|館）
    hits = search(conn, parse_query("てつどう", converter))
    assert [h.label for h in hits] == ["読み", "読み"]
    hits = search(conn, parse_query("てつどうのひ", converter))
    assert [(h.text, h.label) for h in hits] == [("開拓者のみんな、鉄道の日だよ", "読み")]


def test_kana_query_matches_latin_dictionary_term(conn, converter):
    # GeoGuessr は字幕上カタカナでも英字でも、どの書き方で検索しても一致タブに出る
    for q in ("GeoGuessr", "geoguessr", "ジオゲッサー", "じおげっさー"):
        hits = search(conn, parse_query(q, converter))
        assert [h.video_id for h in hits] == ["v1"], q


def test_reading_match_inside_word_goes_to_maybe_tab(conn, converter):
    terms = parse_query("つり", converter)
    match = {h.text for h in search(conn, terms)}
    maybe = [(h.text, h.label) for h in search(conn, terms, tab="maybe")]
    assert "ホロドリで釣り" in match  # 釣り = ツリ が語として一致
    # 読みが 2 文字の語は語の前方だけ引く: 釣り堀(ツリボリ) は出るが 夏祭り(ナツ|マツリ) は全件走査になるので出ない
    assert sorted(maybe) == [("釣り堀に行く", "読み(部分)")]


def test_homophone_word_is_reading_tier(conn, converter):
    # 「水星」は「すいせい」の同音語。語単位の読み一致なので「読み」として一致タブに出る
    hits = search(conn, parse_query("すいせい", converter))
    assert [(h.text, h.label) for h in hits] == [("昨日は水星ちゃんと遊んだ", "読み")]
    assert search(conn, parse_query("すいせい", converter), use_reading=False) == []


def test_fuzzy_candidates_rank_below_and_keep_raw(conn, converter):
    terms = parse_query("ホロドリ", converter)
    # ほろドリ はかなの文字種のゆれ（読みが完全一致）なので自動で補正される（fix_kana_variants）
    assert [(h.text_raw, h.text, h.label) for h in search(conn, terms)] == [
        ("ホロドリで釣り", "ホロドリで釣り", "原文"),
        ("ほろドリで釣り", "ホロドリで釣り", "補正"),
    ]
    maybe = search(conn, terms, tab="maybe")
    # 候補は本文 text を書き換えない
    assert [(h.text, h.display_text, h.label) for h in maybe] == [
        ("ほろりで釣り", "ホロドリで釣り", "候補"),
    ]
    assert [c.distance for h in maybe for c in h.candidates] == [1]
    assert count_tiers(conn, terms) == {0: 1, 1: 1, 2: 0, 3: 0, 4: 1, 5: 0, 8: 0}


def test_and_query_uses_weakest_tier(conn, converter):
    hits = search(conn, parse_query("ホロドリ 釣り", converter), tab="all")
    assert [h.label for h in hits] == ["原文", "補正", "候補"]


def test_search_filters_and_exclusion(conn, converter):
    hits = search(conn, parse_query("開拓者", converter))
    assert [h.video_id for h in hits] == ["v2", "v2"]  # 歌枠 v3 は出ない
    assert search(conn, parse_query("開拓者", converter), date_to="2026-10-05") == []
    assert len(search(conn, parse_query("遊んだ", converter), title="雑談", tab="all")) == 1


def test_parse_query_validation(converter):
    with pytest.raises(QueryError):
        parse_query("a", converter)
    with pytest.raises(QueryError):
        parse_query("ab cd ef gh ij kl", converter)


def test_screen_title_gates_short_terms(dictionary, converter):
    from azkims.review import FuzzyMatcher, screen

    matcher = FuzzyMatcher(dictionary)

    def run(text, title):
        ctx = dictionary.title_context(title)
        hits = screen(matcher.find(text, converter.tokens(text)), ctx)
        return [(h.surface, h.term.canonical, t) for h, t in hits]

    assert run("兄ちゃんが来た", "雑談") == []  # すいちゃん と距離 1、タイトルに無い
    assert run("ほろりで釣り", "【#ホロドリ】釣り") == [("ほろり", "ホロドリ", True)]
    assert run("あ、ずきです", "雑談") == []  # 句読点をまたぐ窓は出さない


def test_rejected_pair_is_not_a_candidate():
    from azkims.review import FuzzyMatcher

    d = Dictionary([Term("ホロライブ", "ほろらいぶ")], rejected={("ソロライブ", "ホロライブ")})
    text = "ソロライブやります"
    assert FuzzyMatcher(d).find(text, ReadingConverter(d).tokens(text)) == []


def test_suffix_terms_compare_name_part_only():
    from azkims.review import FuzzyMatcher, name_part_close

    costume = Term("3rd衣装", "さーどいしょう")
    new_year = Term("正月衣装", "しょうがついしょう")
    sui = Term("すいちゃん", "すいちゃん")
    noel = Term("ノエちゃん", "のえちゃん")

    def close(surface, term):
        d = Dictionary([term])
        return name_part_close(ReadingConverter(d).reading(surface), term)

    # 語尾（衣装・ちゃん）が同じなだけのものは落とす
    assert not close("お衣装", costume)
    assert not close("最初さ", costume)
    assert not close("じいちゃん", sui)
    assert not close("あいちゃん", sui)
    # 〇〇の部分が近いものは残す。ちゃんは頭の音が同じなら 1 音違いまで
    assert close("正月以上", new_year)
    assert close("サード以上", costume)
    assert close("スちゃん", sui)
    assert close("のいちゃん", noel)
    # 語尾の無い語には関係しない
    assert name_part_close("ロザミヤ", Term("ロザミア", "ろざみあ"))

    d = Dictionary([costume])
    text = "今日はお衣装で"
    tokens = ReadingConverter(d).tokens(text)
    assert FuzzyMatcher(d).find(text, tokens) == []
    # 裏付けのある探索（タイムスタンプ・推しマーク）は語全体で比べる
    assert [h.surface for h in FuzzyMatcher(d, name_part=False).find(text, tokens)] == ["お衣装"]


def test_title_scoped_alias_applies_only_in_matching_streams():
    term = Term("ホロドリ", "ほろどり", title_aliases=["ほろり"])
    d = Dictionary([term])
    assert d.apply_aliases("ほろりと泣いた")[0] == "ほろりと泣いた"
    ctx = d.title_context("【#ホロドリ】釣り")
    assert ctx == {"ホロドリ"}
    assert d.apply_aliases("ほろりで釣り", ctx) == ("ホロドリで釣り", [("ほろり", "ホロドリ")])


def test_channel_suffix_is_not_title_context(dictionary):
    ctx = dictionary.title_context("【雑談】まったり【ホロライブ / AZKi / ロボ子さん】")
    assert "ホロライブ" not in ctx and "AZKi" not in ctx
    assert "ロボ子さん" in ctx  # コラボ相手は手がかりになる


def test_query_keeps_dictionary_terms_with_spaces(converter):
    d = Dictionary([Term("from A to Z", "ふろむえーとぅーぜっと")])
    terms = parse_query("from A to Z 歌って", ReadingConverter(d))
    assert [t.surface for t in terms] == ["from A to Z", "歌って"]


def test_merge_fragments():
    from azkims.captions import Utterance, merge_fragments

    utts = [
        Utterance(0, 1000, "あの2人が生きていたら"),  # 文の途中で切れている
        Utterance(1500, 2500, "お互いに嫉妬で狂ってしまう。"),
        Utterance(3000, 3500, "れる。"),  # 短い
        Utterance(20000, 21000, "いつ"),  # 短いが直前と離れている
        Utterance(21500, 23000, "まったく別の長い発言です。"),
    ]
    assert [u.text for u in merge_fragments(utts)] == [
        "あの2人が生きていたらお互いに嫉妬で狂ってしまう。れる。",
        "いつまったく別の長い発言です。",
    ]


def test_ruby_segments():
    from azkims.search import ruby_segments

    segs = ruby_segments("ホロ取りのジオゲッサー", "ホロ取りのGeoGuessr", "ホロドリのGeoGuessr")
    assert "".join(x["text"] for x in segs) == "ホロドリのGeoGuessr"
    changed = [(x["raw"], x["text"], x["kind"]) for x in segs if "raw" in x]
    assert ("ジオゲッサー", "GeoGuessr", "confirmed") in changed
    assert any(k == "candidate" and "ド" in t for _, t, k in changed)
    assert ruby_segments("そのまま", "そのまま") == [{"text": "そのまま"}]


def test_ruby_segments_cover_whole_katakana_word():
    from azkims.search import ruby_segments

    segs = ruby_segments(
        "やはりホールドリーかなり", "やはりホールドリーかなり", "やはりホロドリかなり"
    )
    assert segs == [
        {"text": "やはり"},
        {"text": "ホロドリ", "raw": "ホールドリー", "kind": "candidate"},
        {"text": "かなり"},
    ]
    # 「の」を挟んだ別々の補正はまとめない
    segs = ruby_segments("ホロ取りのジオゲッサー", "ホロ取りのGeoGuessr", "ホロドリのGeoGuessr")
    assert [(x.get("raw"), x["text"]) for x in segs] == [
        ("ホロ取り", "ホロドリ"),
        (None, "の"),
        ("ジオゲッサー", "GeoGuessr"),
    ]


def test_ruby_segments_cover_dictionary_term():
    from azkims.search import ruby_segments

    final = "まつりちゃんが489"
    segs = ruby_segments("まりちゃんが489", final, spans=[(0, 6)])
    assert segs == [
        {"text": "まつりちゃん", "raw": "まりちゃん", "kind": "confirmed"},
        {"text": "が489"},
    ]


def test_near_scope_alias_uses_local_topic_and_competitors():
    d = Dictionary(
        [
            Term("まつりちゃん", "まつりちゃん", near_aliases=[("まりちゃん", ("マリンちゃん",))]),
            Term("マリンちゃん", "まりんちゃん"),
        ]
    )
    text = "まりちゃんかわいい"
    assert d.apply_aliases(text)[0] == text  # 近くの話題が無ければ置換しない
    assert d.apply_aliases(text, near=frozenset({"まつりちゃん"}))[0] == "まつりちゃんかわいい"
    # 紛らわしい相手（マリンちゃん）も近くで話題なら置換しない
    both = frozenset({"まつりちゃん", "マリンちゃん"})
    assert d.apply_aliases(text, near=both)[0] == text


def test_near_context_window():
    from azkims.oshi import near_context

    topics = [(100, frozenset({"夏色まつり"})), (500, frozenset({"宝鐘マリン"}))]
    assert near_context(topics, 150) == {"夏色まつり"}  # タイムスタンプの 50 秒後
    assert near_context(topics, 90) == {"夏色まつり"}  # 10 秒前
    assert near_context(topics, 300) == frozenset()


def test_fix_kana_variants(dictionary, converter):
    from azkims.pipeline import fix_kana_variants

    def run(text):
        return fix_kana_variants(text, converter.tokens(text), dictionary)

    # 文字種のゆれ（読みが辞書語とぴったり同じ）は直す
    assert run("かナたんのカニ") == ("かなたんのカニ", [("かナたん", "かなたん")])
    assert run("ラデンちゃんが来た")[0] == "らでんちゃんが来た"
    # 漢字を含む表記・読みの違う表記は直さない
    assert run("彼たんのカニ")[1] == []
    # 読みが別の語と同じ（あめちゃん = アメちゃん）ものは、その語自身にしか直さない
    assert run("あめちゃん")[0] in ("あめちゃん", "アメちゃん")
    # kana_fix = false の語（ジジちゃん、オリーちゃん）は、読みが同じでも直さない
    assert run("じじちゃんが来た")[1] == []
    assert run("おりちゃんさんありがとう")[1] == []


def test_drop_decisions_by_source(tmp_path):
    from azkims.dictionary import append_review_decision, drop_decisions_by_source

    append_review_decision(tmp_path, "alias", "あ", "AZKi")
    append_review_decision(tmp_path, "alias", "いい", "AZKi", source="aggressive")
    append_review_decision(tmp_path, "reject", "うう", "AZKi", source="aggressive")
    assert drop_decisions_by_source(tmp_path, "aggressive") == 2
    text = (tmp_path / "accepted.toml").read_text(encoding="utf-8")
    assert 'surface = "あ"' in text and "aggressive" not in text


def test_search_finds_corrected_utterance_by_original_spelling(dictionary, converter):
    from azkims.pipeline import analyze

    d = Dictionary([Term("あずきち", "あずきち", aliases=["水きち"])])
    c = db.connect(":memory:")
    c.execute(
        "INSERT INTO streams (video_id, title, upload_date, caption_status) "
        "VALUES ('v1', '雑談', '2026-10-01', 'ok')"
    )
    conv = ReadingConverter(d)
    raw = "水きちかわいい"
    a = analyze(raw, "雑談", d, conv, FuzzyMatcher(d))
    db.insert_utterance(c, "v1", 0, 1000, raw, a.text, a.reading, a.corrections)
    c.commit()
    hits = search(c, parse_query("水きち", conv))
    # 表示は補正後（あずきち）だが、原文の表記でも見つかる
    assert [(h.text, h.label) for h in hits] == [("あずきちかわいい", "原文")]
    assert [h.text for h in search(c, parse_query("あずきち", conv))] == ["あずきちかわいい"]


def test_game_terms_only_in_that_games_streams():
    t = Term("ピカチュウ", "ぴかちゅう", aliases=["ぴかちゅー"], game="pokopia")
    d = Dictionary([t], games={"pokopia": ["ぽこ あ ポケモン"]})
    text = "ぴかちゅーかわいい"
    assert d.apply_aliases(text, d.title_context("【雑談】まったり"))[0] == text
    ctx = d.title_context("【ぽこ あ ポケモン】島づくり")
    assert d.apply_aliases(text, ctx)[0] == "ピカチュウかわいい"
    # 検索語の読みの変換では、どの配信か分からないので常に使う
    assert ReadingConverter(d).reading("ぴかちゅー") == fold_reading("ぴかちゅう")


def test_sound_distance():
    from azkims.review import sound_distance

    f = fold_reading
    assert sound_distance(f("ほろとり"), f("ほろどり")) == 0.5  # 濁点の違い
    assert sound_distance(f("ぽかじん"), f("ぽかじゃん")) == 0.5  # 拗音の小書き
    assert sound_distance(f("いちゃん"), f("すいちゃん")) == 1.0  # 音の欠落
    assert sound_distance(f("まいちゃん"), f("すいちゃん")) == 1.0  # 別の音
    assert sound_distance(f("すいちゃん"), f("すいちゃん")) == 0.0


def test_context_rules_kanaken():
    from azkims import rules

    rule = rules.parse_rule(
        "かなけん",
        {
            "surfaces": ["かけん"],
            "after": ["ライブ"],
            "not_before": ["水", "[にもを]"],
            "not_after": ["の[?？]", "なよ"],
            "topic": ["天音かなた"],
        },
    )

    def decide(text, topical=frozenset()):
        return [m.decision for m in rules.find(text, [rule], topical)]

    # 組み合わせは話題でなくても補正
    assert decide("かけんライブ良かった") == ["fix"]
    # 普通の言葉の形は話題の配信でも補正しない
    assert decide("水かけんのかな", frozenset({rule})) == ["keep"]
    assert decide("迷惑かけんなよ", frozenset({rule})) == ["keep"]
    # それ以外は話題の配信なら補正、そうでなければ候補
    assert decide("かけん", frozenset({rule})) == ["fix"]
    assert decide("かけん") == ["maybe"]
    text, applied = rules.apply_fixes(
        "かけんライブ", rules.find("かけんライブ", [rule], frozenset())
    )
    assert text == "かなけんライブ" and applied == [("かけん", "かなけん")]

    greeting = rules.parse_rule(
        "かなけん",
        {"surfaces": ["かなけ"], "before": ["こん"], "not_after": ["ん"], "otherwise": "keep"},
    )
    assert [m.decision for m in rules.find("こんかなけ", [greeting], frozenset())] == ["fix"]
    assert [m.decision for m in rules.find("長引かなければ", [greeting], frozenset())] == ["keep"]
    assert [m.decision for m in rules.find("こんかなけん", [greeting], frozenset())] == []


def _related_conn(tmp_path, utterances, title="雑談", ts=()):
    conn = db.connect(tmp_path / "r.sqlite")
    conn.execute(
        "INSERT INTO streams (video_id, title, upload_date, caption_status) "
        "VALUES ('v1', ?, '2026-10-14', 'ok')",
        (title,),
    )
    for i, text in enumerate(utterances):
        db.insert_utterance(conn, "v1", i * 1000, i * 1000 + 500, text, text, "", [], None, [], [])
    for sec, text in ts:
        db.insert_ts_entry(conn, "v1", sec, text, [], False, "", [])
    return conn


def _related():
    def word(w, strong, reading=""):
        return RelatedWord(w, related._forms(w, reading), strong)

    return related.Related(
        [
            related.Tag(
                "天音かなた",
                ("天音かなた", "かなたん"),
                (
                    word("天音かなた", True),
                    word("かなたん", True),
                    word("中空の庭", True),
                    word("天使", False),
                    word("comet", False, "こめっと"),
                ),
            ),
            related.Tag(
                "かなけん",
                ("かなけん",),
                (word("かなけん", True), word("天使", False)),
                ("天音かなた",),
            ),
        ]
    )


def _related_rows(conn):
    return {tuple(r) for r in conn.execute("SELECT tag, word, confident FROM related_hits")}


def test_related_weak_word_needs_topical_stream(tmp_path):
    conn = _related_conn(tmp_path, ["マジ天使だから", "中空の庭いいよね"])
    related.build_index(conn, _related())
    assert _related_rows(conn) == {
        ("天音かなた", "天使", 0),
        ("天音かなた", "中空の庭", 1),
        ("かなけん", "天使", 0),
    }


def test_related_topic_from_title_ts_captions_and_unit_member(tmp_path):
    for i, (title, texts, ts) in enumerate(
        [
            ("かなたんとコラボ", ["天使だ"], ()),
            ("雑談", ["天使だ"], [(1, "かなたん登場")]),
            ("雑談", ["天使だ", "かなたん", "かなたんが", "かなたんの"], ()),
        ]
    ):
        path = tmp_path / str(i)
        path.mkdir()
        conn = _related_conn(path, texts, title, ts)
        related.build_index(conn, _related())
        assert ("天音かなた", "天使", 1) in _related_rows(conn)
        assert ("かなけん", "天使", 1) in _related_rows(conn)  # メンバーが話題ならユニットも


def test_related_oshi_mark_in_timestamp_is_a_confident_hit(tmp_path):
    conn = _related_conn(tmp_path, [], ts=[(1, "💫️のコラボ"), (2, "⚒の話")])
    related.build_index(conn, _related(), {"💫": "天音かなた", "⚒": "AZKi"})
    rows = {tuple(r) for r in conn.execute("SELECT tag, word, confident FROM related_ts_hits")}
    assert rows == {("天音かなた", "💫", 1)}  # 関連語の無い人（AZKi）の印は使わない


def test_related_word_extra_forms(tmp_path):
    entry = {"word": "あてぃしがに", "forms": ["アティシガに"]}
    (w,) = related._words({"strong": [entry]}, True)
    assert set(w.forms) == {"あてぃしがに", "アティシガニ", "アティシガに"}


def test_related_scanner_word_boundaries():
    scanner = related.Scanner(_related())
    assert ("天音かなた", "comet") in scanner.find("コメットだ")
    assert ("天音かなた", "comet") in scanner.find("cometだ")
    assert scanner.find("ミニコメットだ") == set()
    assert scanner.find("miCometだ") == set()


def test_related_forms():
    assert set(related._forms("ござる")) == {"ござる", "ゴザル"}
    assert related._forms("かなココ") == ("かなココ",)
    assert "すてらすてら" in related._forms("Stellar Stellar", "すてらすてら")


def test_related_data_loads_with_nicknames():
    dictionary = load_dictionary(PROJECT_ROOT / "config" / "dictionary")
    rel = related.load_related(PROJECT_ROOT / "config" / "dictionary", dictionary)
    tag = rel.tag_of("かなたん")
    assert tag.name == "天音かなた"
    by_word = {w.word: w for w in tag.words}
    assert by_word["かなた"].strong and by_word["かなけん"].strong
    assert not by_word["天使"].strong
    assert rel.tag_of("すいちゃん").name == "星街すいせい"
    assert rel.tag_of("AZKi") is None
    assert "Star Flower" not in rel.tags


def test_unit_name_is_written_as_the_search_normalizes_it():
    # 正式は アソビ★まわり隊！ だが、検索の語は半角の ! に正規化されるので辞書も半角にする（2026-11-13）
    dictionary = load_dictionary(PROJECT_ROOT / "config" / "dictionary")
    rel = related.load_related(PROJECT_ROOT / "config" / "dictionary", dictionary)
    scanner = related.Scanner(rel)
    unit = "アソビ★まわり隊!"
    assert normalize_text("アソビ★まわり隊！") == unit
    assert dictionary.apply_aliases("遊び回り隊がね。")[0] == "アソビ★まわり隊!がね。"
    assert (unit, unit) in scanner.find("新ユニット アソビ★まわり隊!")
    assert rel.tags[unit].members == ("百灯キョーコ", "熱千めら", "鈴鳴つづり", "宙科そぴあ")
