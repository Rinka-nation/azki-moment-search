"""Proper-noun dictionary (hololive members, AZKi-specific terms, game titles...).

Dictionary files are TOML files under config/dictionary/:

    [[term]]
    canonical = "星街すいせい"     # 表示・補正後の表記
    reading = "ほしまちすいせい"   # 読み（ひらがな/カタカナどちらでも可）
    aliases = ["星街彗星"]         # 確定済みの誤認識・別表記（自動で canonical に置換）
    title_aliases = ["ほろり"]     # 配信タイトルにこの語が出ている配信でだけ置換する別表記
    category = "hololive"
    fuzzy = true                   # 読みのあいまい一致で補正候補を探すか（既定: 読み4文字以上なら true）
    kana_fix = false               # 読みが同じかな書きを自動で直さない（既定: true。pipeline.fix_kana_variants）

accepted.toml は `azkims accept` / `azkims reject` が追記するレビュー結果:

    [[alias]]   surface = "ホロ面"  canonical = "ホロメン"
    [[alias]]   surface = "ほろり"  canonical = "ホロドリ"  scope = "title"
    [[alias]]   surface = "まりちゃん"  canonical = "まつりちゃん"  scope = "near"
                competitors = ["マリンちゃん"]
    [[reject]]  surface = "マイク"  canonical = "マイクラ"
    [[hold]]    surface = "赤いハート"  canonical = "赤井はあと"

hold（保留）は「補正はしないが候補としては残す」。本文は書き換えず、検索の「もしかして」には出る。
普通の言葉でもあり得る句（赤いハート、時の空）に使う。

scope = "title" の alias は、普通の言葉と同じ表記（ほろり、あ色 など）を
「その話題の配信の中でだけ」補正するためのもの。話題は配信タイトルと、タイムスタンプの
推しマーク・名前（oshi.stream_context）から決める。

ゲーム別の辞書（config/dictionary/games/*.toml）:

    [game]
    id = "pokopia"
    name = "ぽこ あ ポケモン"
    title_patterns = ["ぽこ あ ポケモン", "ぽこあポケモン"]   # 配信タイトルにこれがあればこのゲームの配信

    [[term]]  ...（書式は同じ）

ゲームの語は、そのゲームの配信（タイトルに title_patterns のどれかがある）でだけ補正に使う。
ポケモンの名前を他の配信の字幕に当てないため。検索語の読みの変換では常に使う。

scope = "near" はさらに狭く、「その発言の時刻の近くのタイムスタンプに、その人の推しマーク・名前がある」
ときだけ置換する（oshi.near_context）。まりちゃん のように複数人の愛称に近い表記用で、
competitors（紛らわしい相手: マリンちゃん 等）が近くに出ているときは置換しない。
"""

from __future__ import annotations

import tomllib
import unicodedata
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path

from azkims.rules import Rule, parse_rule
from azkims.textnorm import fold_reading

ACCEPTED_FILE = "accepted.toml"
MIN_FUZZY_READING = 4
SCOPES = ("global", "title", "near")

Context = frozenset[str]  # その配信で有効な canonical の集合（配信タイトルに出てくる語）
NO_CONTEXT: Context = frozenset()


def _is_katakana(c: str) -> bool:
    return "ァ" <= c <= "ヺ" or c == "ー"


def game_key(game_id: str) -> str:
    """文脈（Context）の中でゲームを表す要素。"""
    return f"game:{game_id}"


# チャンネルの定型（【ホロライブ / AZKi】）でほぼ全タイトルに出る語
UBIQUITOUS_TITLE_TERMS = frozenset({"ホロライブ", "AZKi"})


class DictionaryError(ValueError):
    pass


@dataclass
class Term:
    canonical: str
    reading: str
    aliases: list[str] = field(default_factory=list)
    category: str = ""
    fuzzy: bool | None = None
    source: str = ""
    title_aliases: list[str] = field(default_factory=list)
    # (表記, 紛らわしい相手の canonical) 。発言の近くでその人が話題のときだけ置換する
    near_aliases: list[tuple[str, tuple[str, ...]]] = field(default_factory=list)
    group: str = ""  # 愛称なら誰のことか（canonical）。本人の語は空
    game: str = ""  # ゲーム別の辞書の語ならゲームの id。そのゲームの配信でだけ使う
    # その語が配信の話題（タイトル・タイムスタンプ）のときだけ別表記の補正・候補探しに使う
    topic_only: bool = False
    # false: 読みが同じかな書き（じじちゃん → ジジちゃん）を自動で直さない。読みが普通の言葉・人の名前と同じ語用
    kana_fix: bool = True
    # 文脈つき補正ルール（rules.py）。普通の言葉とぶつかる崩れ方（かけん → かなけん）
    rules: list[Rule] = field(default_factory=list)

    @property
    def member(self) -> str:
        """この語が指す人・もの（愛称なら group、そうでなければ自分自身）。"""
        return self.group or self.canonical

    # 読みは作った後に変えないので、1 回だけ計算する。補正候補探しが 1 発言で何百回も読むので、毎回計算すると
    # build の時間の 2 割ほどになっていた（配信 3 本で 139 万回。2026-11-14、ISS-21）
    @cached_property
    def folded(self) -> str:
        return fold_reading(self.reading)

    @property
    def fuzzy_enabled(self) -> bool:
        if self.fuzzy is not None:
            return self.fuzzy
        return len(self.folded) >= MIN_FUZZY_READING


@dataclass(frozen=True)
class Match:
    start: int
    end: int
    surface: str
    term: Term


def _key(s: str) -> str:
    return unicodedata.normalize("NFKC", s).lower()


class Dictionary:
    def __init__(
        self,
        terms: list[Term],
        rejected: set[tuple[str, str]] | None = None,
        held: set[tuple[str, str]] | None = None,
        games: dict[str, list[str]] | None = None,
    ):
        """games: ゲーム id → 配信タイトルの手がかり（title_patterns）。"""
        self.terms = terms
        self.games = games or {}
        self.rejected = rejected or set()
        # 保留: 候補（text_fuzzy / もしかして）としては残すが、レビュー一覧には出さない
        self.held = held or set()
        self.by_canonical: dict[str, Term] = {}
        # 配信タイトル -> title_context の結果。build は 1 配信の発言の数だけ同じタイトルで呼ぶ（ISS-21）
        self._title_contexts: dict[str, Context] = {}
        # 表記キー -> (語, scope, 紛らわしい相手)
        self._surface: dict[str, tuple[Term, str, tuple[str, ...]]] = {}
        self.rules: list[Rule] = [r for t in terms for r in t.rules]
        for term in terms:
            if term.canonical in self.by_canonical:
                raise DictionaryError(f"duplicate canonical: {term.canonical}")
            self.by_canonical[term.canonical] = term
        for term in terms:
            for surface in [term.canonical, *term.aliases]:
                self._add_surface(surface, term, "global")
            for surface in term.title_aliases:
                self._add_surface(surface, term, "title")
            for surface, competitors in term.near_aliases:
                self._add_surface(surface, term, "near", competitors)
        # 先頭文字 -> 長い順の表記キー（最長一致用）
        self._index: dict[str, list[str]] = {}
        for key in sorted(self._surface, key=len, reverse=True):
            self._index.setdefault(key[0], []).append(key)

    def _add_surface(
        self, surface: str, term: Term, scope: str, competitors: tuple[str, ...] = ()
    ) -> None:
        key = _key(surface)
        if len(key) < 2:
            raise DictionaryError(f"surface too short: {surface!r} ({term.canonical})")
        owner = self._surface.get(key)
        if owner is not None and owner[0] is not term:
            raise DictionaryError(
                f"surface {surface!r} is claimed by both {owner[0].canonical!r} "
                f"and {term.canonical!r}"
            )
        # 同じ語に複数の scope で登録されたら、広い方（global > title > near）を使う
        if owner is None or SCOPES.index(scope) < SCOPES.index(owner[1]):
            self._surface[key] = (term, scope, competitors)

    def is_known_surface(self, surface: str) -> bool:
        return _key(surface) in self._surface

    def title_context(self, title: str) -> Context:
        """配信タイトルに canonical か（全体有効な）alias が出てくる語の集合。

        ほぼ全タイトルに付く「【ホロライブ / AZKi】」の語は話題の手がかりにならないので除く。
        辞書の語は作った後に変えないので、タイトルごとに覚えておく。
        """
        cached = self._title_contexts.get(title)
        if cached is not None:
            return cached
        low = _key(title)
        words = {
            t.canonical
            for t in self.terms
            if t.canonical not in UBIQUITOUS_TITLE_TERMS
            and not t.game
            and any(_key(s) in low for s in [t.canonical, *t.aliases])
        }
        games = {
            game_key(gid)
            for gid, patterns in self.games.items()
            if any(_key(p) in low for p in patterns)
        }
        context = frozenset(words | games)
        self._title_contexts[title] = context
        return context

    def game_active(self, term: Term, context: Context, include_games: bool = False) -> bool:
        return not term.game or include_games or game_key(term.game) in context

    def topic_terms(self, rule: Rule) -> frozenset[str]:
        """ルールの topic に挙げた人・語と、その人の愛称の canonical。"""
        topic = set(rule.topic)
        return frozenset(
            t.canonical for t in self.terms if t.member in topic or t.canonical in topic
        )

    def find(
        self,
        text: str,
        context: Context = NO_CONTEXT,
        near: Context = NO_CONTEXT,
        include_games: bool = False,
    ) -> list[Match]:
        """Left-to-right longest matches of canonical/alias surfaces.

        `text` is expected to be NFKC-normalized already (textnorm.normalize_text).
        scope = title の alias は、その語が `context`（配信の話題）に含まれるときだけ、
        scope = near の alias は `near`（発言の近くの話題）に含まれ、紛らわしい相手が含まれないときだけ一致させる。
        """
        lowered = text.lower()
        if len(lowered) != len(text):
            lowered = text
        matches: list[Match] = []
        i = 0
        while i < len(text):
            for key in self._index.get(lowered[i], ()):
                if not lowered.startswith(key, i):
                    continue
                term, scope, competitors = self._surface[key]
                if not self.game_active(term, context, include_games):
                    continue
                if scope == "title" and term.canonical not in context:
                    continue
                if scope == "near" and (
                    term.canonical not in near or any(c in near for c in competitors)
                ):
                    continue
                is_alias = key != term.canonical.lower()
                if is_alias and term.topic_only and term.canonical not in context:
                    continue
                if is_alias and i > 0 and _is_katakana(key[0]) and _is_katakana(text[i - 1]):
                    # カタカナ語の途中から始まる別表記は一致させない（ペコ|ラ先輩 → そら先輩 を防ぐ。2026-10-30）
                    continue
                end = i + len(key)
                matches.append(Match(i, end, text[i:end], term))
                i = end
                break
            else:
                i += 1
        return matches

    def apply_aliases(
        self, text: str, context: Context = NO_CONTEXT, near: Context = NO_CONTEXT
    ) -> tuple[str, list[tuple[str, str]]]:
        """Replace confirmed aliases with canonical forms. Returns (text, [(alias, canonical)])."""
        out: list[str] = []
        applied: list[tuple[str, str]] = []
        pos = 0
        for m in self.find(text, context, near):
            out.append(text[pos : m.start])
            out.append(m.term.canonical)
            if m.surface != m.term.canonical:
                applied.append((m.surface, m.term.canonical))
            pos = m.end
        out.append(text[pos:])
        return "".join(out), applied


def _load_terms(path: Path, game: str = "") -> list[Term]:
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    terms = []
    for entry in raw.get("term", []):
        try:
            terms.append(
                Term(
                    canonical=entry["canonical"],
                    reading=entry["reading"],
                    aliases=list(entry.get("aliases", [])),
                    title_aliases=list(entry.get("title_aliases", [])),
                    near_aliases=[(s, ()) for s in entry.get("near_aliases", [])],
                    topic_only=bool(entry.get("topic_only", False)),
                    kana_fix=bool(entry.get("kana_fix", True)),
                    rules=[parse_rule(entry["canonical"], r) for r in entry.get("rules", [])],
                    group=entry.get("group", ""),
                    game=game,
                    category=entry.get("category", ""),
                    fuzzy=entry.get("fuzzy"),
                    source=path.name,
                )
            )
        except KeyError as e:
            raise DictionaryError(f"{path.name}: term missing {e}: {entry}") from e
    return terms


def load_dictionary(directory: Path, include_accepted: bool = True) -> Dictionary:
    """include_accepted=False はレビュー結果（accepted.toml）を除いた同梱辞書だけ（テスト用）。"""
    terms: list[Term] = []
    for path in sorted(directory.glob("*.toml")):
        if path.name != ACCEPTED_FILE:
            terms.extend(_load_terms(path))
    games: dict[str, list[str]] = {}
    for path in sorted((directory / "games").glob("*.toml")):
        with open(path, "rb") as f:
            meta = tomllib.load(f).get("game", {})
        gid = meta.get("id") or path.stem
        if gid in games:
            raise DictionaryError(f"duplicate game id: {gid}")
        games[gid] = list(meta.get("title_patterns", []))
        terms.extend(_load_terms(path, game=gid))

    rejected: set[tuple[str, str]] = set()
    held: set[tuple[str, str]] = set()
    accepted_path = directory / ACCEPTED_FILE
    if include_accepted and accepted_path.exists():
        with open(accepted_path, "rb") as f:
            raw = tomllib.load(f)
        by_canonical = {t.canonical: t for t in terms}
        for entry in raw.get("alias", []):
            term = by_canonical.get(entry["canonical"])
            if term is None:
                raise DictionaryError(f"{ACCEPTED_FILE}: unknown canonical {entry['canonical']!r}")
            scope = entry.get("scope", "global")
            if scope not in SCOPES:
                raise DictionaryError(f"{ACCEPTED_FILE}: unknown scope {scope!r}")
            if scope == "near":
                competitors = tuple(entry.get("competitors", []))
                if entry["surface"] not in [s for s, _ in term.near_aliases]:
                    term.near_aliases.append((entry["surface"], competitors))
                continue
            target = term.title_aliases if scope == "title" else term.aliases
            if entry["surface"] not in target:
                target.append(entry["surface"])
        for entry in raw.get("reject", []):
            rejected.add((entry["surface"], entry["canonical"]))
        for entry in raw.get("hold", []):
            held.add((entry["surface"], entry["canonical"]))
    return Dictionary(terms, rejected, held, games)


def _toml_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def append_review_decision(
    directory: Path,
    kind: str,
    surface: str,
    canonical: str,
    scope: str = "global",
    competitors: list[str] | None = None,
    source: str = "",
) -> Path:
    """source: どの仕組みで追加したか（"oshi" 等）。作り直すときに対象を見分けるため。"""
    """Append an [[alias]] (accept), [[reject]] or [[hold]] entry to accepted.toml."""
    if kind not in ("alias", "reject", "hold"):
        raise ValueError(kind)
    if scope not in SCOPES:
        raise ValueError(scope)
    path = directory / ACCEPTED_FILE
    if not path.exists():
        path.write_text(
            "# azkims accept / reject が追記するレビュー結果。手で編集してもよい。\n",
            encoding="utf-8",
        )
    lines = [
        f"\n[[{kind}]]",
        f"surface = {_toml_str(surface)}",
        f"canonical = {_toml_str(canonical)}",
    ]
    if kind == "alias" and scope != "global":
        lines.append(f"scope = {_toml_str(scope)}")
    if kind == "alias" and competitors:
        lines.append("competitors = [" + ", ".join(_toml_str(c) for c in competitors) + "]")
    if source:
        lines.append(f"source = {_toml_str(source)}")
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return path


def drop_decisions_by_source(directory: Path, source: str) -> int:
    """accepted.toml から source が一致する項目（[[alias]] / [[hold]] / [[reject]]）を消す。

    強め補正（source = "aggressive"）や推しマーク由来（"oshi"）をまとめて取り消す・作り直すため。
    """
    path = directory / ACCEPTED_FILE
    if not path.exists():
        return 0
    text = path.read_text(encoding="utf-8")
    blocks = text.split("\n[[")
    head, rest = blocks[0], blocks[1:]
    marker = f"source = {_toml_str(source)}"
    kept = [b for b in rest if marker not in b.splitlines()]
    path.write_text("\n[[".join([head, *kept]), encoding="utf-8")
    return len(rest) - len(kept)
