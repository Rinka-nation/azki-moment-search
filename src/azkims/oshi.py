"""Member-name correction candidates from oshi marks in timestamp comments (read-only).

タイムスタンプ（霧島リンカさん）には、ホロメンの話が出た時刻に推しマーク（🌲 = 大神ミオ など）が
書かれている。その時刻の付近の字幕には、そのホロメンの名前・愛称が崩れた形で出ていることが多い
（マスりちゃん = まつりちゃん、猫股たおか = 猫又おかゆ）。

通常の補正候補探し（review.FuzzyMatcher）より条件を緩める:
- 探す語を、その時刻に話題になっているホロメンの名前と愛称だけに絞る
- 短い愛称（fuzzy = false の語）も探す
- 読みの距離の上限を緩める（MAX_RATIO）
"""

from __future__ import annotations

import sqlite3
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from azkims.dictionary import Dictionary, Term
from azkims.reading import ReadingConverter
from azkims.review import FuzzyHit, FuzzyMatcher, levenshtein
from azkims.textnorm import fold_reading

WINDOW_BEFORE_MS = 20_000
WINDOW_AFTER_MS = 90_000
MAX_RATIO = 0.4
_VS = "️"  # 絵文字の異体字セレクタ。付いたり付かなかったりするので除いて比べる


def load_oshi_marks(path: Path, keep_variation: bool = False) -> dict[str, str]:
    """推しマーク → ホロメン。照合用は異体字セレクタを外す。keep_variation=True は表示用（書いたまま）。"""
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    marks = raw.get("marks", {})
    return dict(marks) if keep_variation else {k.replace(_VS, ""): v for k, v in marks.items()}


def members_in(text: str, marks: dict[str, str], dictionary: Dictionary) -> set[str]:
    """タイムスタンプの 1 行から、話題のホロメン（canonical）を集める。推しマーク + 名前・愛称。"""
    plain = text.replace(_VS, "")
    found = {member for mark, member in marks.items() if mark in plain}
    found |= {m.term.member for m in dictionary.find(text) if m.term.category in _PERSON}
    return found


_PERSON = {"hololive", "nickname", "azki", "staff"}

# 敬称・愛称の語尾。これで終わる愛称は、候補も同じ語尾で終わり、名前の部分が近いことを求める
# （「ワンチャン」「あちゃん」が みこちゃん・ミオちゃん に当たるのを防ぐ）
_SUFFIXES = [
    fold_reading(x) for x in ("ちゃん", "さん", "せんぱい", "たん", "さま", "くん", "せんせい")
]
STEM_MAX_DISTANCE = 1


def max_distance(target_len: int) -> int:
    """読みの長さごとの距離の上限。短い名前ほど厳しく（3 文字以下は読み完全一致のみ）。"""
    if target_len <= 3:
        return 0
    if target_len <= 6:
        return 1
    return 2


def plausible(hit: FuzzyHit, converter: ReadingConverter) -> bool:
    """緩い条件で拾った候補のうち、名前の崩れとしてありそうなものだけ残す。"""
    target = hit.term.folded
    surface = fold_reading(converter.reading(hit.surface))
    if hit.distance > max_distance(len(target)):
        return False
    if any(c.isdigit() for c in hit.surface) or hit.surface[-1] in "っッの":
        return False  # 「この3」「おかゆのっ」のような切れ端
    if len(target) <= 4 and len(surface) < len(target):
        return False  # 短い名前で文字が欠けたもの（まりん ↔ マリリン）は別の語形
    if hit.distance and (surface.startswith(target) or target.startswith(surface)):
        return False  # カナタ ↔ かなたん、みおちゃん見 のような「付け足し・切れ端」は別の語形
    for suf in _SUFFIXES:
        if target.endswith(suf) and len(target) > len(suf):
            if not surface.endswith(suf):
                return False
            stem_t, stem_s = target[: -len(suf)], surface[: -len(suf)]
            return len(stem_s) >= 2 and levenshtein(stem_s, stem_t) <= STEM_MAX_DISTANCE
    return True


def stream_context(
    texts: list[str], marks: dict[str, str], dictionary: Dictionary
) -> frozenset[str]:
    """配信のタイムスタンプ全体から、その配信で話題になったホロメンの語（本人 + 愛称の canonical）。

    タイトル限定（scope = "title"）の alias を「タイトルかタイムスタンプに出てくる人の配信」で
    効かせるために、Dictionary.title_context と合わせて使う。
    """
    members: set[str] = set()
    for t in texts:
        members |= members_in(t, marks, dictionary)
    return _expand(members, dictionary)


def _expand(members: set[str], dictionary: Dictionary) -> frozenset[str]:
    """人（member）の集合 → その人の語（本人 + 愛称）の canonical の集合。"""
    return frozenset(t.canonical for t in dictionary.terms if t.member in members) | frozenset(
        members
    )


def topics_by_time(
    entries: list[tuple[int, str]], marks: dict[str, str], dictionary: Dictionary
) -> list[tuple[int, frozenset[str]]]:
    """タイムスタンプの各行 → (秒, その行で話題の人の語 + 人以外の語)。話題の無い行は除く。

    人以外の語（ユニット名など）も入れるのは、その語の near_aliases（かけん → かなけん）を
    「近くのタイムスタンプにその語が書いてある発言」でだけ効かせるため（2026-10-29）。
    """
    out = []
    for sec, text in entries:
        members = members_in(text, marks, dictionary)
        others = {m.term.canonical for m in dictionary.find(text) if m.term.category not in _PERSON}
        if members or others:
            out.append((sec, _expand(members, dictionary) | others))
    return out


def near_context(topics: list[tuple[int, frozenset[str]]], sec: int) -> frozenset[str]:
    """発言の時刻 sec の近くのタイムスタンプで話題の語。

    推しマークの時刻 t の付近 [t - 20 秒, t + 90 秒] の発言を探したのと同じ範囲を、発言側から見る。
    """
    lo, hi = sec - WINDOW_AFTER_MS // 1000, sec + WINDOW_BEFORE_MS // 1000
    found: set[str] = set()
    for t, words in topics:
        if lo <= t <= hi:
            found |= words
    return frozenset(found)


@dataclass
class OshiExample:
    video_id: str
    sec: int
    ts_text: str
    caption: str


@dataclass
class OshiCandidate:
    surface: str
    canonical: str
    member: str
    distance: int
    # 補正先と同じかそれ以上に読みが近い別人の語（まりちゃん → まつりちゃん に対する マリンちゃん）。
    # あれば scope = near で使う
    competitors: list[str] = field(default_factory=list)
    utterances: set[int] = field(default_factory=set)
    examples: list[OshiExample] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.utterances)


def collect_oshi_candidates(
    conn: sqlite3.Connection,
    dictionary: Dictionary,
    converter: ReadingConverter,
    marks: dict[str, str],
    max_examples: int = 3,
) -> list[OshiCandidate]:
    by_member: dict[str, list[Term]] = {}
    for t in dictionary.terms:
        by_member.setdefault(t.member, []).append(t)
    # 他の語と読みが同じ表記（ここちゃん＝ココ、スいちゃん＝すいちゃん）は別人の呼び名なので候補にしない
    readings = {t.folded: t.canonical for t in dictionary.terms}
    matchers: dict[str, FuzzyMatcher] = {}

    found: dict[tuple[str, str], OshiCandidate] = {}
    entries = conn.execute("SELECT video_id, sec, text FROM ts_entries ORDER BY video_id, sec")
    for e in entries.fetchall():
        members = members_in(e["text"], marks, dictionary) - {"AZKi"}  # 本人は除く（地の文で頻出）
        if not members:
            continue
        ms = e["sec"] * 1000
        window = conn.execute(
            "SELECT id, text FROM utterances WHERE video_id = ? AND start_ms BETWEEN ? AND ?",
            (e["video_id"], ms - WINDOW_BEFORE_MS, ms + WINDOW_AFTER_MS),
        ).fetchall()
        for member in members:
            terms = by_member.get(member)
            if not terms:
                continue
            matcher = matchers.get(member)
            if matcher is None:
                matcher = matchers[member] = FuzzyMatcher(
                    dictionary, MAX_RATIO, terms=terms, name_part=False
                )
            for u in window:
                if any(t.canonical in u["text"] for t in terms):
                    continue  # もう正しい表記で出ている
                for hit in matcher.find(u["text"], converter.tokens(u["text"])):
                    if not plausible(hit, converter):
                        continue
                    other = readings.get(fold_reading(converter.reading(hit.surface)))
                    if other is not None and other != hit.term.canonical:
                        continue
                    key = (hit.surface, hit.term.canonical)
                    c = found.get(key)
                    if c is None:
                        c = found[key] = OshiCandidate(
                            hit.surface, hit.term.canonical, member, hit.distance
                        )
                    if u["id"] in c.utterances:
                        continue
                    c.utterances.add(u["id"])
                    if len(c.examples) < max_examples:
                        c.examples.append(
                            OshiExample(e["video_id"], e["sec"], e["text"], u["text"])
                        )
    # 同じ表記が複数の語の候補になったもの（ワンチャン → みこちゃん / ミオちゃん …）は決められないので除く
    claimed: dict[str, int] = {}
    for c in found.values():
        claimed[c.surface] = claimed.get(c.surface, 0) + 1
    result = [c for c in found.values() if claimed[c.surface] == 1]
    person_terms = [t for t in dictionary.terms if t.category in _PERSON]
    for c in result:
        surface = fold_reading(converter.reading(c.surface))
        c.competitors = sorted(
            t.canonical
            for t in person_terms
            if t.member != c.member
            and len(t.folded) >= 3
            and levenshtein(surface, t.folded) <= min(c.distance, max_distance(len(t.folded)))
        )
    return sorted(result, key=lambda c: (c.member, -c.count, c.distance))
