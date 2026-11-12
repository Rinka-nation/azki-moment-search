"""Align human-written timestamp comments with auto captions (read-only).

タイムスタンプ（人が書いた正しい表記）を「答え」として、その時刻付近の自動字幕と突き合わせる。

1. 用語の裏付け: タイムスタンプに辞書語（GeoGuessr など）が書かれているのに、付近の字幕に
   その語が無い → 字幕側で読みの近い表記を探す。見つかれば「人の裏付けがある誤認識パターン」。
   短い語でもタイトル条件なしで、通常より緩い距離（MAX_RATIO）で探す。
2. 「」発言の照合: 「」内は発言そのままのことが多い。付近の字幕で表記一致 / 読み一致 /
   読みが近い / 見つからない に分類し、表記が食い違う例を一覧にする（辞書に足す語の発見用）。
"""

from __future__ import annotations

import re
import sqlite3
from collections import Counter
from dataclasses import dataclass, field

from azkims.dictionary import Dictionary
from azkims.reading import ReadingConverter
from azkims.review import FuzzyMatcher, levenshtein

# タイムスタンプは発言の少し前に打たれることが多く、補足行は後の内容も含むので後ろを広めに
WINDOW_BEFORE_MS = 20_000
WINDOW_AFTER_MS = 60_000
MAX_RATIO = 0.4
QUOTE_NEAR_RATIO = 0.3
MIN_QUOTE_READING = 3


@dataclass
class Evidence:
    video_id: str
    sec: int
    ts_text: str
    caption: str
    caption_ms: int


@dataclass
class EvidenceGroup:
    surface: str
    canonical: str
    distance: int
    examples: list[Evidence] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.examples)


@dataclass
class TermCoverage:
    canonical: str
    mentions: int = 0  # タイムスタンプでの言及数
    in_captions: int = 0  # 付近の字幕（補正後）にその語がある
    variant: int = 0  # 付近の字幕に読みの近い未登録表記がある
    missing: int = 0  # どちらも無い（字幕が取れていない・話していない・言い換え等）


@dataclass
class QuoteResult:
    video_id: str
    sec: int
    quote: str
    kind: str  # surface / reading / near / missing
    caption: str = ""
    caption_ms: int = 0
    distance: int = 0


def _window(conn: sqlite3.Connection, video_id: str, sec: int) -> list[sqlite3.Row]:
    ms = sec * 1000
    return conn.execute(
        "SELECT start_ms, text_raw, text FROM utterances "
        "WHERE video_id = ? AND start_ms BETWEEN ? AND ? ORDER BY start_ms",
        (video_id, ms - WINDOW_BEFORE_MS, ms + WINDOW_AFTER_MS),
    ).fetchall()


def _ts_entries(conn: sqlite3.Connection):
    return conn.execute(
        "SELECT e.video_id, e.sec, e.text, e.quotes FROM ts_entries e ORDER BY e.video_id, e.sec"
    ).fetchall()


def collect_term_evidence(
    conn: sqlite3.Connection, dictionary: Dictionary, converter: ReadingConverter
) -> tuple[list[EvidenceGroup], list[TermCoverage]]:
    matcher = FuzzyMatcher(dictionary, max_ratio=MAX_RATIO, name_part=False)
    groups: dict[tuple[str, str], EvidenceGroup] = {}
    coverage: dict[str, TermCoverage] = {}
    for e in _ts_entries(conn):
        terms = {m.term.canonical: m.term for m in dictionary.find(e["text"])}
        if not terms:
            continue
        window = _window(conn, e["video_id"], e["sec"])
        for canonical in terms:
            cov = coverage.setdefault(canonical, TermCoverage(canonical))
            cov.mentions += 1
            if any(canonical.lower() in u["text"].lower() for u in window):
                cov.in_captions += 1
                continue
            found = False
            for u in window:
                for hit in matcher.find(u["text"], converter.tokens(u["text"])):
                    if hit.term.canonical != canonical:
                        continue
                    found = True
                    key = (hit.surface, canonical)
                    g = groups.setdefault(key, EvidenceGroup(hit.surface, canonical, hit.distance))
                    g.distance = min(g.distance, hit.distance)
                    g.examples.append(
                        Evidence(e["video_id"], e["sec"], e["text"], u["text"], u["start_ms"])
                    )
            if found:
                cov.variant += 1
            else:
                cov.missing += 1
    ordered = sorted(groups.values(), key=lambda g: (-g.count, g.distance))
    return ordered, sorted(coverage.values(), key=lambda c: -c.mentions)


_SYMBOL_RE = re.compile(r"[\W_]+")


def _surface_key(text: str) -> str:
    """表記比較用: 記号・句読点・空白を除いて小文字化（「やめてっ!」と「やめてっ。」を同じとみなす）。"""
    return _SYMBOL_RE.sub("", text).lower()


def _best_substring_distance(needle: str, hay: str) -> int:
    """needle に最も近い hay の部分文字列との編集距離（長さ ±2 の窓で探す）。"""
    if not hay:
        return len(needle)
    best = len(needle)
    n = len(needle)
    for size in range(max(1, n - 2), n + 3):
        for i in range(max(1, len(hay) - size + 1)):
            d = levenshtein(needle, hay[i : i + size])
            if d < best:
                best = d
                if best == 0:
                    return 0
    return best


def collect_quotes(conn: sqlite3.Connection, converter: ReadingConverter) -> list[QuoteResult]:
    results: list[QuoteResult] = []
    for e in _ts_entries(conn):
        quotes = [q for q in e["quotes"].split("\n") if q]
        if not quotes:
            continue
        window = _window(conn, e["video_id"], e["sec"])
        readings = [(u, converter.reading(u["text"])) for u in window]
        for q in quotes:
            q_read = converter.reading(q)
            if len(q_read) < MIN_QUOTE_READING:
                continue
            r = QuoteResult(e["video_id"], e["sec"], q, "missing")
            q_key = _surface_key(q)
            surface = next((u for u in window if q_key in _surface_key(u["text"])), None)
            if surface is not None:
                r.kind, r.caption, r.caption_ms = "surface", surface["text"], surface["start_ms"]
            else:
                exact = next((u for u, rd in readings if q_read in rd), None)
                if exact is not None:
                    r.kind, r.caption, r.caption_ms = "reading", exact["text"], exact["start_ms"]
                else:
                    best = min(
                        ((u, _best_substring_distance(q_read, rd)) for u, rd in readings),
                        key=lambda x: x[1],
                        default=(None, len(q_read)),
                    )
                    if best[0] is not None and best[1] / len(q_read) <= QUOTE_NEAR_RATIO:
                        r.kind, r.caption, r.caption_ms = (
                            "near",
                            best[0]["text"],
                            best[0]["start_ms"],
                        )
                        r.distance = best[1]
            results.append(r)
    return results


def quote_summary(results: list[QuoteResult]) -> Counter:
    return Counter(r.kind for r in results)
