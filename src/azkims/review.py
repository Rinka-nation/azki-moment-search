"""Correction-candidate discovery (read-only; nothing here modifies the dictionary).

1. fuzzy: token windows whose reading is close to a dictionary term's reading but whose surface
   is not a known alias (例: ホロ面 ≈ ホロメン, ロザミヤ ≈ ロザミア). build stores them as
   *candidates* (text_fuzzy / fuzzy_matches) without touching `text`; search shows them in the
   lower-confidence "maybe" tier. Accepted ones become aliases via `azkims accept`.
2. unknown: frequent Sudachi out-of-vocabulary words that are not in the dictionary
   (例: ジオゲッサー). These hint at terms that should be added to the dictionary.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from azkims.dictionary import Context, Dictionary, Term, game_key
from azkims.reading import ReadingConverter, Token
from azkims.textnorm import fold_reading

MAX_WINDOW_TOKENS = 5
# 2026-10-25: 過剰なくらいに補正してから弱める方針で 0.25 → 0.34
MAX_DISTANCE_RATIO = 0.34
# 読みがこの長さ以下の語は、配信タイトルに出てこない限り読み完全一致（距離 0）だけを候補にする
SHORT_READING = 5
_PUNCT_RE = re.compile(r"[\s、。,.!?！？「」『』()（）]")
# 窓の先頭・末尾にしない品詞。接尾辞（ちゃん・さん）は先頭には来ないが、末尾には来てよい（まつりちゃん）
_NO_START_POS = {"助詞", "助動詞", "補助記号", "空白", "接尾辞"}
_NO_END_POS = {"助詞", "助動詞", "補助記号", "空白"}


def levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


# 「音の近い違い」の重み。濁点・半濁点の有無（ト/ド、ハ/バ/パ）と、拗音の小書きが落ちた・付いた
# （ジャ/ジ。fold_reading で小書きは大書きになるので ヤ・ユ・ヨ の挿入/削除として現れる）は 0.5。
# それ以外（別の音への置き換え、音の欠落: いちゃん ↔ すいちゃん）は 1。
NEAR_SOUND_COST = 0.5
_YOON = set("ヤユヨ")
_I_ROW = set("キシチニヒミリ")  # 濁点・半濁点は _base_kana で外してから比べる（ギ・ジ・ビ・ピ）


def _base_kana(c: str) -> str:
    """濁点・半濁点を外した文字（ド → ト、パ → ハ）。"""
    return unicodedata.normalize("NFD", c)[0]


def sound_distance(a: str, b: str) -> float:
    """読み（fold_reading 済み）の距離。音の近い違いを 0.5 として数える編集距離。"""
    if a == b:
        return 0.0

    def indel(s: str, k: int) -> float:
        # 小書きの拗音は い段の後ろにしか付かない（キャ・シュ・ジョ）。ヤスイ の ヤ のような
        # 単独の ヤ・ユ・ヨ の欠落は普通の音の欠落（安いちゃん → すいちゃん を通さない。2026-10-29）
        if s[k] in _YOON and k > 0 and _base_kana(s[k - 1]) in _I_ROW:
            return NEAR_SOUND_COST
        return 1.0

    def subst(x: str, y: str) -> float:
        if x == y:
            return 0.0
        return NEAR_SOUND_COST if _base_kana(x) == _base_kana(y) else 1.0

    prev = [0.0]
    for j in range(len(b)):
        prev.append(prev[-1] + indel(b, j))
    for i, ca in enumerate(a):
        cur = [prev[0] + indel(a, i)]
        for j, cb in enumerate(b, 1):
            cur.append(
                min(
                    prev[j] + indel(a, i),
                    cur[j - 1] + indel(b, j - 1),
                    prev[j - 1] + subst(ca, cb),
                )
            )
        prev = cur
    return prev[-1]


# 共通の語尾を持つ語（〇〇衣装・〇〇ちゃん）は、語尾が同じなだけで読みが近く見える（お衣装 ≈ 3rd衣装、
# じいちゃん ≈ すいちゃん）。語尾を除いた〇〇の部分だけで近さを測る（2026-11-03）。
# (語尾, 語尾の読み, 短い名前の 1 音違いを頭の音が同じときに許すか)
# 〇〇ちゃん は名前が 2 音（フブ・スイ）なので 1 音違いで 50% になる。スちゃん（すいちゃん）・のいちゃん（ノエちゃん）
# は残し、じいちゃん・あいちゃん は落とすために、頭の音が同じなら 1 音違いまで許す。
_NAME_SUFFIXES = (
    ("衣装", fold_reading("いしょう"), False),
    ("ちゃん", fold_reading("ちゃん"), True),
)


def name_part_close(reading: str, term: Term, max_ratio: float = MAX_DISTANCE_RATIO) -> bool:
    """共通の語尾を持つ語なら、語尾が合っていて〇〇の部分も近いか。それ以外の語は常に True。"""
    for suffix, suffix_reading, allow_head in _NAME_SUFFIXES:
        if not term.canonical.endswith(suffix):
            continue
        target = term.folded[: len(term.folded) - len(suffix_reading)]
        n = len(suffix_reading)
        if not target or len(reading) <= n:
            return False
        # 語尾は音の近い違い（イシヨウ / イジヨウ）まで
        if sound_distance(reading[-n:], suffix_reading) > NEAR_SOUND_COST:
            return False
        name = reading[:-n]
        if abs(len(name) - len(target)) > max(1, len(target) // 4):
            return False
        d = sound_distance(name, target)
        if d / len(target) <= max_ratio:
            return True
        return allow_head and d <= 1 and _base_kana(name[0]) == _base_kana(target[0])
    return True


_FUNCTION_POS = {"助詞", "助動詞", "補助記号", "空白"}


def unnatural(tokens: list[Token]) -> bool:
    """普通の日本語の分かれ方ではない（誤認識の崩れ方）か。"""
    for t in tokens:
        if t.oov:
            return True
        # 助詞・助動詞以外の 1 文字のかな（ほ / ロ / ア）は崩れた分割のしるし
        if len(t.surface) == 1 and "ぁ" <= t.surface <= "ヿ" and t.pos not in _FUNCTION_POS:
            return True
    return False


def _bigrams(s: str) -> set[str]:
    return {s[i : i + 2] for i in range(len(s) - 1)}


@dataclass(frozen=True)
class FuzzyHit:
    surface: str
    term: Term
    distance: int
    start: int
    end: int


class FuzzyMatcher:
    def __init__(
        self,
        dictionary: Dictionary,
        max_ratio: float = MAX_DISTANCE_RATIO,
        terms: list[Term] | None = None,
        name_part: bool = True,
    ):
        """terms: 探す語を限定する（fuzzy 設定に関係なく探す）。推しマークの文脈で使う。

        name_part: 〇〇衣装・〇〇ちゃん を〇〇の部分で比べる（name_part_close）。タイムスタンプや推しマークの
        裏付けがある探索では False にして、従来どおり語全体で比べる。
        """
        self.dictionary = dictionary
        self.max_ratio = max_ratio
        self.name_part = name_part
        self.terms = (
            terms if terms is not None else [t for t in dictionary.terms if t.fuzzy_enabled]
        )
        self._bigram_index: dict[str, list[int]] = defaultdict(list)
        for idx, term in enumerate(self.terms):
            for bg in _bigrams(term.folded):
                self._bigram_index[bg].append(idx)
        self._max_len = max((len(t.folded) for t in self.terms), default=0)

    def _candidate_terms(self, reading: str) -> list[Term]:
        counts: Counter[int] = Counter()
        for bg in _bigrams(reading):
            for idx in self._bigram_index.get(bg, ()):
                counts[idx] += 1
        out = []
        for idx, n in counts.items():
            need = max(1, len(_bigrams(self.terms[idx].folded)) // 2)
            if n >= need:
                out.append(self.terms[idx])
        return out

    def find(self, text: str, tokens: list[Token]) -> list[FuzzyHit]:
        found: list[FuzzyHit] = []
        for i, first in enumerate(tokens):
            if first.term or first.pos in _NO_START_POS:
                continue
            reading = ""
            for j in range(i, min(len(tokens), i + MAX_WINDOW_TOKENS)):
                tok = tokens[j]
                if tok.term:
                    break
                reading += tok.reading
                if len(reading) > self._max_len + 2:
                    break
                if len(reading) < 3 or tok.pos in _NO_END_POS:
                    continue
                surface = text[first.start : tok.end]
                if _PUNCT_RE.search(surface):
                    break
                for term in self._candidate_terms(reading):
                    target = term.folded
                    if abs(len(reading) - len(target)) > max(1, len(target) // 4):
                        continue
                    d = levenshtein(reading, target)
                    if d / len(target) > self.max_ratio:
                        continue
                    if surface == term.canonical or self.dictionary.is_known_surface(surface):
                        continue
                    if self.name_part and not name_part_close(reading, term):
                        continue
                    if (surface, term.canonical) in self.dictionary.rejected:
                        continue
                    window = tokens[i : j + 1]
                    if not unnatural(window) and sound_distance(reading, target) > 1:
                        # 普通の言葉の並び（なんか|ドラゴン、シフト|ボタン、ござる|は|ね）で音も 2 以上違う。
                        # 語の一部が偶然似ているだけなので候補にもしない（2026-10-28）
                        continue
                    found.append(FuzzyHit(surface, term, d, first.start, tok.end))
        # 重なりは「距離が小さい → 長い」ものを優先して 1 つに絞る
        found.sort(key=lambda h: (h.distance / len(h.term.folded), -(h.end - h.start)))
        chosen: list[FuzzyHit] = []
        for h in found:
            if all(h.end <= c.start or h.start >= c.end for c in chosen):
                chosen.append(h)
        return sorted(chosen, key=lambda h: h.start)


@dataclass
class Example:
    video_id: str
    start_ms: int
    text: str
    title: str


@dataclass
class CandidateGroup:
    surface: str
    canonical: str
    distance: int
    count: int = 0
    in_title: int = 0
    examples: list[Example] = field(default_factory=list)


def screen(hits: list[FuzzyHit], context: Context) -> list[tuple[FuzzyHit, bool]]:
    """Drop weak hits. Returns [(hit, in_title)].

    context は Dictionary.title_context(配信タイトル)。
    短い語（読み SHORT_READING 文字以下）は誤検出が多いので、距離 1 以上は
    配信タイトルにその語が出ている（＝その配信の話題・コラボ相手）場合だけ残す。
    """
    out = []
    for hit in hits:
        if hit.term.game and game_key(hit.term.game) not in context:
            continue  # 別のゲームの語（ポケモンの名前を他の配信に当てない）
        in_title = hit.term.canonical in context
        if hit.term.topic_only and not in_title:
            continue  # そら先輩: AZKi がそう呼ぶのはコラボ相手のとき（2026-10-30）
        if hit.distance and len(hit.term.folded) <= SHORT_READING and not in_title:
            continue
        out.append((hit, in_title))
    return out


def apply_candidates(text: str, hits: list[FuzzyHit]) -> str:
    """text の候補箇所を canonical に置き換えた「候補補正後」テキスト。hits は重なりなし。"""
    out, pos = [], 0
    for h in sorted(hits, key=lambda h: h.start):
        out += [text[pos : h.start], h.term.canonical]
        pos = h.end
    out.append(text[pos:])
    return "".join(out)


def load_candidate_groups(
    conn, max_examples: int = 3, held: set[tuple[str, str]] | None = None
) -> list[CandidateGroup]:
    """build 時に記録した fuzzy_matches を (表記, 正規形) ごとに集計する。保留済みは除く。"""
    held = held or set()
    groups = [
        CandidateGroup(r["surface"], r["canonical"], r["d"], r["n"], r["t"])
        for r in conn.execute(
            "SELECT surface, canonical, MIN(distance) AS d, COUNT(*) AS n, SUM(in_title) AS t "
            "FROM fuzzy_matches GROUP BY surface, canonical ORDER BY d, n DESC, surface"
        )
        if (r["surface"], r["canonical"]) not in held
    ]
    for g in groups:
        for r in conn.execute(
            "SELECT u.video_id, u.start_ms, u.text, s.title FROM fuzzy_matches f "
            "JOIN utterances u ON u.id = f.utterance_id JOIN streams s ON s.video_id = u.video_id "
            "WHERE f.surface = ? AND f.canonical = ? ORDER BY f.in_title DESC, u.id LIMIT ?",
            (g.surface, g.canonical, max_examples),
        ):
            g.examples.append(Example(r["video_id"], r["start_ms"], r["text"], r["title"]))
    return groups


@dataclass
class UnknownWord:
    surface: str
    count: int = 0
    streams: set[str] = field(default_factory=set)
    examples: list[Example] = field(default_factory=list)


def collect_unknown(
    rows, converter: ReadingConverter, min_count: int = 3, max_examples: int = 2
) -> list[UnknownWord]:
    words: dict[str, UnknownWord] = {}
    for video_id, start_ms, text, title in rows:
        tokens = converter.tokens(text)
        i = 0
        while i < len(tokens):
            tok = tokens[i]
            if tok.term or not tok.oov:
                i += 1
                continue
            # 連続する未知語はひとまとまりにする
            j = i + 1
            while j < len(tokens) and not tokens[j].term and tokens[j].oov:
                j += 1
            surface = text[tok.start : tokens[j - 1].end]
            followed_by = text[tokens[j - 1].end : tokens[j - 1].end + 2]
            i = j
            if len(surface) < 3 or surface.isdigit():
                continue
            if followed_by == "さん":  # スパチャ読みの視聴者名
                continue
            w = words.setdefault(surface, UnknownWord(surface))
            w.count += 1
            w.streams.add(video_id)
            if len(w.examples) < max_examples:
                w.examples.append(Example(video_id, start_ms, text, title))
    out = [w for w in words.values() if w.count >= min_count]
    return sorted(out, key=lambda w: (-len(w.streams), -w.count))
