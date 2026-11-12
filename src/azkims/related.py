"""ホロメン・ユニットの関連語（検索専用。字幕の補正には使わない）。

天音かなた で検索すると、天使・ゴリラ・曲名 などの発言も「関連語」タブに出す。
語は config/dictionary/related/*.toml に strong / weak で書く（形式はファイルの先頭を参照）。

天使 のような普通の言葉は他の話題でも出るので、weak の語は「その人が話題の配信」でだけ確からしいとする。
話題の配信: タイトル・タイムスタンプにその人の名前・呼び名が出るか、字幕に TOPIC_MIN 回以上出る配信。
ユニットは、ユニット名かメンバーのどれかが話題の配信。

語の一致は build のときに調べて related_hits / related_ts_hits に入れておく（検索は表を引くだけ）。
"""

from __future__ import annotations

import re
import sqlite3
import tomllib
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from azkims.dictionary import Dictionary, DictionaryError
from azkims.textnorm import normalize_text

RELATED_DIR = "related"
TOPIC_MIN = 3


@dataclass(frozen=True)
class RelatedWord:
    word: str
    forms: tuple[str, ...]  # 字幕の中で探す表記（小文字）
    strong: bool


@dataclass(frozen=True)
class Tag:
    name: str
    names: tuple[str, ...]  # 名前・呼び名。これで検索するとタグになる
    words: tuple[RelatedWord, ...]
    members: tuple[str, ...] = ()  # ユニットのメンバー


class Related:
    def __init__(self, tags: list[Tag]):
        self.tags = {t.name: t for t in tags}
        self._by_name = {_key(n): t for t in tags for n in t.names}

    def tag_of(self, surface: str) -> Tag | None:
        return self._by_name.get(_key(surface))


def _key(s: str) -> str:
    return normalize_text(s).lower()


def _to_hiragana(s: str) -> str:
    return "".join(chr(ord(c) - 0x60) if "ァ" <= c <= "ヶ" else c for c in s)


def _to_katakana(s: str) -> str:
    return "".join(chr(ord(c) + 0x60) if "ぁ" <= c <= "ゖ" else c for c in s)


def _forms(word: str, reading: str = "") -> tuple[str, ...]:
    key = _key(word)
    forms = {key}
    # かな書きはひらがな・カタカナどちらでも探す。混ざった語（かなココ）は崩すと普通の言葉になる
    pure = key == _to_hiragana(key) or key == _to_katakana(key)
    for s in [key] * pure + [reading] * bool(reading):
        if re.fullmatch(r"[ぁ-ゖァ-ヶー]+", s):
            forms |= {_to_hiragana(s), _to_katakana(s)}
    return tuple(sorted(forms))


def _words(entry: dict, strong: bool) -> list[RelatedWord]:
    out = []
    for e in entry.get("strong" if strong else "weak", []):
        word, reading = (e, "") if isinstance(e, str) else (e["word"], e.get("reading", ""))
        forms = _forms(word, reading)
        # かなの混ざった別の書き方（字幕の「アティシガに」など）。崩さずそのまま探す
        if not isinstance(e, str) and e.get("forms"):
            forms = tuple(sorted({*forms, *(_key(f) for f in e["forms"])}))
        out.append(RelatedWord(word, forms, strong))
    return out


def load_related(directory: Path, dictionary: Dictionary) -> Related:
    members: list[dict] = []
    units: list[dict] = []
    for path in sorted((directory / RELATED_DIR).glob("*.toml")):
        with open(path, "rb") as f:
            raw = tomllib.load(f)
        members += raw.get("member", [])
        units += raw.get("unit", [])

    names: dict[str, list[str]] = {}
    words: dict[str, list[RelatedWord]] = {}
    for m in members:
        nicknames = [t.canonical for t in dictionary.terms if t.group == m["name"]]
        names[m["name"]] = [m["name"], *nicknames]
        words[m["name"]] = [
            *(RelatedWord(n, _forms(n), True) for n in names[m["name"]]),
            *_words(m, True),
            *_words(m, False),
        ]
    tags = []
    for u in units:
        own = [RelatedWord(u["name"], _forms(u["name"]), True), *_words(u, True), *_words(u, False)]
        for member in u["members"]:
            # AZKi は全部の配信が本人なのでタグにしていない
            if member in words:
                words[member] += own
            elif member != "AZKi":
                raise DictionaryError(f"related: unknown member {member!r} in {u['name']!r}")
        tags.append(Tag(u["name"], (u["name"],), tuple(own), tuple(u["members"])))
    tags += [Tag(n, tuple(names[n]), tuple(words[n])) for n in words]
    return Related(tags)


def _same_kind(c: str, edge: str) -> bool:
    """c が語の端の字 edge と続いているか（カタカナ語・英単語の途中では一致させない）。"""
    if "ァ" <= edge <= "ヶ" or edge == "ー":
        return "ァ" <= c <= "ヶ" or c == "ー"
    return edge.isascii() and edge.isalpha() and c.isascii() and c.isalpha()


class Scanner:
    """全タグの語をまとめて探す。長い表記を優先する（ミコメット の中の コメット は数えない）。"""

    def __init__(self, related: Related):
        self._owners: dict[str, list[tuple[str, str]]] = {}
        for tag in related.tags.values():
            for w in tag.words:
                for form in w.forms:
                    self._owners.setdefault(form, []).append((tag.name, w.word))
        ordered = sorted(self._owners, key=len, reverse=True)
        self._regex = re.compile("|".join(map(re.escape, ordered))) if ordered else None

    def find(self, text: str) -> set[tuple[str, str]]:
        """text にある (タグ, 語)。"""
        found: set[tuple[str, str]] = set()
        if self._regex is None:
            return found
        lower = text.lower()
        for m in self._regex.finditer(lower):
            s, e = m.span()
            if s > 0 and _same_kind(lower[s - 1], lower[s]):
                continue
            if e < len(lower) and _same_kind(lower[e], lower[e - 1]):
                continue
            found.update(self._owners[m.group()])
        return found


def build_index(
    conn: sqlite3.Connection, related: Related, marks: dict[str, str] | None = None
) -> tuple[int, int]:
    """related_hits / related_ts_hits を作り直す。(発言の件数, タイムスタンプの件数) を返す。

    marks（推しマーク → ホロメン。異体字セレクタを外したもの）があれば、推しマークのある
    タイムスタンプも、そのホロメンの確かな関連語の一致にする（2026-11-10 霧島リンカさん）。
    """
    marks = {m: name for m, name in (marks or {}).items() if name in related.tags}
    scanner = Scanner(related)
    is_name = {(t.name, n) for t in related.tags.values() for n in t.names}
    strong = {(t.name, w.word) for t in related.tags.values() for w in t.words if w.strong}
    strong |= {(name, mark) for mark, name in (marks or {}).items()}

    topical: set[tuple[str, str]] = set()  # (タグ, video_id)
    for vid, title in conn.execute("SELECT video_id, title FROM streams"):
        topical |= {(tag, vid) for tag, w in scanner.find(title) if (tag, w) in is_name}

    ts_rows = []
    for entry_id, vid, text in conn.execute("SELECT id, video_id, text FROM ts_entries"):
        for tag, word in scanner.find(text):
            ts_rows.append((tag, word, entry_id, vid))
            if (tag, word) in is_name:
                topical.add((tag, vid))
        plain = text.replace("️", "")
        ts_rows += [(name, mark, entry_id, vid) for mark, name in marks.items() if mark in plain]

    utt_rows = []
    mentions: Counter[tuple[str, str]] = Counter()
    # スパチャ読みの名前の連続は話題ではないので、除いた残りだけを見る
    for uid, vid, text, raw in conn.execute(
        "SELECT id, video_id, COALESCE(text_kept, text), "
        "CASE WHEN text_kept IS NULL THEN text_raw ELSE '' END FROM utterances"
    ):
        found = scanner.find(text) | scanner.find(raw)
        utt_rows += [(tag, word, uid, vid) for tag, word in found]
        for tag in {tag for tag, word in found if (tag, word) in is_name}:
            mentions[tag, vid] += 1
    topical |= {k for k, n in mentions.items() if n >= TOPIC_MIN}

    for tag in related.tags.values():
        for member in tag.members:
            topical |= {(tag.name, vid) for t, vid in topical.copy() if t == member}

    def rows(items: list[tuple[str, str, int, str]]):
        for tag, word, row_id, vid in items:
            yield tag, word, row_id, int((tag, word) in strong or (tag, vid) in topical)

    conn.execute("DELETE FROM related_hits")
    conn.execute("DELETE FROM related_ts_hits")
    conn.executemany("INSERT INTO related_hits VALUES (?, ?, ?, ?)", rows(utt_rows))
    conn.executemany("INSERT INTO related_ts_hits VALUES (?, ?, ?, ?)", rows(ts_rows))
    return len(utt_rows), len(ts_rows)


def member_list(conn: sqlite3.Connection, related: Related, marks: dict[str, str]) -> list[dict]:
    """画面の「ホロメンから探す」の一覧。推しマークの表の順（期生順）、無い人はその後ろ。

    nickname は名前・呼び名のうち AZKi の字幕にいちばん多く出るもの（related_hits の件数）。
    これで検索すると関連語タブが出る。字幕に 1 回も出ない人は載せない（2026-11-10 霧島リンカさん）。
    """
    order = {name: i for i, name in enumerate(dict.fromkeys(marks.values()))}
    mark_of: dict[str, str] = {}
    for mark, name in marks.items():
        mark_of.setdefault(name, mark)
    out = []
    for tag in related.tags.values():
        if tag.members:  # ユニットは載せない
            continue
        counts = dict(
            conn.execute(
                "SELECT word, COUNT(*) FROM related_hits WHERE tag = ? AND word IN "
                f"({','.join('?' * len(tag.names))}) GROUP BY word",
                (tag.name, *tag.names),
            ).fetchall()
        )
        if not counts:
            continue
        nickname = max(tag.names, key=lambda n: (counts.get(n, 0), -tag.names.index(n)))
        out.append(
            {
                "name": tag.name,
                "mark": mark_of.get(tag.name, ""),
                "nickname": nickname,
                "count": sum(counts.values()),
            }
        )
    out.sort(key=lambda m: (order.get(m["name"], len(order)), m["name"]))
    return out
