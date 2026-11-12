import json

import pytest

from azkims import db
from azkims.config import PROJECT_ROOT
from azkims.dictionary import load_dictionary
from azkims.llmfix import (
    apply_fixes,
    load_fixes,
    member_names,
    normalize_names,
    official_names,
    swaps_member,
)
from azkims.reading import ReadingConverter, word_readings
from azkims.search import count_tiers, parse_query, ruby_segments, search


def _write_chunk(llm_dir, name, video_id, lines, rows):
    (llm_dir / "in").mkdir(parents=True, exist_ok=True)
    (llm_dir / "out").mkdir(exist_ok=True)
    body = "## 発言（番号\t時刻\t本文）\n" + "\n".join(
        f"{n}\t0:{n:02d}\t{text}" for n, (_, text) in enumerate(lines, 1)
    )
    (llm_dir / "in" / f"{name}.txt").write_text(body + "\n", encoding="utf-8")
    out = "\n".join(json.dumps(r, ensure_ascii=False) for r in rows)
    (llm_dir / "out" / f"{name}.jsonl").write_text(out, encoding="utf-8")
    entry = {"name": name, "video_id": video_id, "start_ms": [ms for ms, _ in lines]}
    with open(llm_dir / "manifest.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def test_load_fixes_maps_rows_to_utterances(tmp_path):
    lines = [(1000, "博多の一覧にしかない"), (2000, "メニューがあるそうなんだ"), (3000, "福岡")]
    rows = [
        {"n": 1, "n_end": 1, "wrong": "一覧", "fix": "一蘭"},
        {"n": 3, "n_end": 3, "wrong": "あるそう", "fix": "あるそう"},  # wrong と fix が同じ
        # 番号が 1 つずれている → 近くの発言で見つけて当てる
        {"n": 1, "n_end": 1, "wrong": "そうなんだ", "fix": "そうなのか"},
        # 発言の境目をまたぐ・どこにも無い
        {"n": 1, "n_end": 2, "wrong": "ないメニュー", "fix": "ないメニュー！"},
        {"n": 2, "n_end": 2, "wrong": "ホロケ", "fix": "ホロライブ"},
    ]
    _write_chunk(tmp_path, "c1", "v1", lines, rows)
    _write_chunk(tmp_path, "c2", "v2", [(500, "まだ")], [])
    fixes, stats = load_fixes(tmp_path)
    assert fixes == {("v1", 1000): [("一覧", "一蘭")], ("v1", 2000): [("そうなんだ", "そうなのか")]}
    assert (stats.rows, stats.used, stats.moved) == (5, 2, 1)
    assert stats.dropped == {"修正が無い": 1, "wrong が 1 つの発言の中に無い": 2}


def test_load_fixes_without_llm_output(tmp_path):
    fixes, stats = load_fixes(tmp_path / "missing")
    assert fixes == {} and stats.rows == 0


def test_apply_fixes_skips_text_changed_since():
    assert apply_fixes("博多の一覧にしかない", [("一覧", "一蘭")]) == "博多の一蘭にしかない"
    # 辞書の補正で wrong がもう無い発言には当てない
    assert apply_fixes("博多の一蘭にしかない", [("一覧", "一蘭")]) is None


@pytest.fixture(scope="module")
def converter():
    return ReadingConverter(
        load_dictionary(PROJECT_ROOT / "config" / "dictionary", include_accepted=False)
    )


@pytest.fixture()
def conn(converter):
    c = db.connect(":memory:")
    c.execute(
        "INSERT INTO streams (video_id, title, upload_date, caption_status) "
        "VALUES ('v1', '雑談', '2026-10-01', 'ok')"
    )
    for ms, text, ai in [
        (1000, "小豆生産グッズが届いた", "AZKi生誕グッズが届いた"),
        (2000, "生誕祭のグッズ", None),
    ]:
        tokens = converter.tokens(text)
        reading = "".join(t.reading for t in tokens)
        words = word_readings(tokens)
        if ai:
            ai_tokens = converter.tokens(ai)
            reading += "\n" + "".join(t.reading for t in ai_tokens)
            words += word_readings(ai_tokens)
        db.insert_utterance(c, "v1", ms, ms + 500, text, text, reading, [], words=words, text_ai=ai)
    c.commit()
    return c


def test_ai_fixed_text_is_searchable_with_its_own_tier(conn, converter):
    hits = search(conn, parse_query("生誕", converter))
    assert [(h.start_ms, h.label) for h in hits] == [(1000, "AI修正"), (2000, "原文")]
    assert hits[0].display_text == "AZKi生誕グッズが届いた"
    assert count_tiers(conn, parse_query("生誕", converter))[2] == 1
    # 読みでも探せる（AI修正 の文の読みも索引に入れている）
    assert {h.start_ms for h in search(conn, parse_query("せいたんぐっず", converter))} == {1000}


def test_original_caption_still_matches_and_shows_ai_text(conn, converter):
    hits = search(conn, parse_query("小豆生産", converter))
    assert [(h.start_ms, h.label, h.display_text) for h in hits] == [
        (1000, "原文", "AZKi生誕グッズが届いた")
    ]


def test_ruby_marks_ai_fix():
    segs = ruby_segments("博多の一覧に", "博多の一覧に", ai="博多の一蘭に")
    assert {"text": "蘭", "raw": "覧", "kind": "ai"} in segs


def test_ai_names_follow_official_notation():
    names = official_names(load_dictionary(PROJECT_ROOT / "config" / "dictionary").terms)
    reason = "兎田ぺこら（ペコラ）の呼びかけ"
    # 理由にフルネームがある・敬称が付くなら公式の表記（ぺこら はひらがな、フブキ はカタカナ）
    assert normalize_names("ペコロさん", "ペコラさん", reason, names) == "ぺこらさん"
    assert normalize_names("吹雪先輩", "ふぶき先輩", "", names) == "フブキ先輩"
    assert normalize_names("い先生", "チョコ先生", "", names) == "ちょこ先生"
    # 名前と分からないもの・普通の言葉・別の語の一部・元からあった表記は直さない
    assert normalize_names("そこ", "ここに", "", names) == "ここに"
    assert normalize_names("ぼたん", "ボタンを押す", "", names) == "ボタンを押す"
    assert normalize_names("かみおけ", "かみおき", "大神ミオ", names) == "かみおき"
    assert normalize_names("ペコラ", "ペコラ先輩", "", names) == "ペコラ先輩"
    assert "さん" not in {official for official, _ in names.values()}


def test_ai_fix_must_not_swap_members():
    members = member_names(load_dictionary(PROJECT_ROOT / "config" / "dictionary").terms)
    # んなたん（姫森ルーナ）を かなたん（天音かなた）にしない
    assert swaps_member("んなたん、あなた。", "かなたん、かなた。", members)
    # 同じ人の表記を直す・誤認識を名前に直すのはよい
    assert not swaps_member("彼たん", "かなたん", members)
    assert not swaps_member("かなたんとぺこら", "かなたんとぺこらちゃん", members)


def test_broken_propagated_rows_are_dropped_not_fatal(tmp_path):
    # NT-014（ISS-15）: 横展開（prop/out）の壊れた行は捨てて続け、理由を数える
    _write_chunk(tmp_path, "c1", "v1", [(1000, "まだ")], [])
    prop = tmp_path / "prop" / "out"
    prop.mkdir(parents=True)
    good = {"wrong": "一覧", "fix": "一蘭", "start_ms": 4000}
    rows = [
        json.dumps(good, ensure_ascii=False),
        '{"wrong": "x", "fix"',
        "[1, 2]",
        '{"wrong": "a"}',
    ]
    (prop / "v9.jsonl").write_text("\n".join(rows), encoding="utf-8")
    fixes, stats = load_fixes(tmp_path)
    assert fixes == {("v9", 4000): [("一覧", "一蘭")]}
    assert stats.propagated == 1
    assert stats.dropped == {"JSON でない": 1, "横展開の行に wrong・fix・start_ms が無い": 2}
