"""Human-written timestamp comments (タイムスタンプコメント) parsing.

Format observed in practice:

    【タイムスタンプ】（ネタバレ注意）
    3:36  開始
    今日は鉄道の日！            ← 時刻なしの行は直前の時刻の補足
    └5:11  車両基地が400以上？      ← └ は細かい時刻
    └33:30  駅発見！「ぴぴぴぴぴ」   ← 「」内は高確率で発言そのまま
    └13:24  :_guess:               ← :_xxx: はメンバースタンプ

長いものは返信に分けて投稿されるので、同じ投稿者の本文 + 返信をまとめて読む。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from azkims.textnorm import normalize_text

_TIME_RE = re.compile(r"^(?:(\d{1,2}):)?(\d{1,2}):(\d{2})(?!\d)\s*(.*)$")
_LEAD_RE = re.compile(r"^[\s　└├┗┣・\-–—>＞]+")
_EMOJI_RE = re.compile(r":_[^:\s]{1,30}:")
QUOTE_RE = re.compile(r"「([^「」]{2,})」")


@dataclass(frozen=True)
class TimestampEntry:
    sec: int
    text: str  # 表示用（スタンプを除いて正規化）
    quotes: tuple[str, ...]  # 「」内（発言そのままの可能性が高い）
    is_note: bool  # 時刻なしの補足行


def clean(text: str) -> str:
    return normalize_text(_EMOJI_RE.sub(" ", text))


def parse_comment_text(text: str) -> list[TimestampEntry]:
    entries: list[TimestampEntry] = []
    current: int | None = None
    for line in text.splitlines():
        body = _LEAD_RE.sub("", line)
        m = _TIME_RE.match(body)
        if m:
            h, mi, s, rest = m.groups()
            current = int(h or 0) * 3600 + int(mi) * 60 + int(s)
            body, is_note = rest, False
        else:
            if current is None:
                continue  # 最初の時刻より前は見出し（【タイムスタンプ】など）
            is_note = True
        cleaned = clean(body)
        if len(cleaned) < 2:
            continue
        entries.append(
            TimestampEntry(
                current, cleaned, tuple(clean(q) for q in QUOTE_RE.findall(body)), is_note
            )
        )
    return entries


def parse_comments(comments: list[dict]) -> list[TimestampEntry]:
    """comments: 同じ投稿者のコメント（本文 + 返信）。時刻順に並べ直して返す。"""
    entries = [e for c in comments for e in parse_comment_text(c.get("text") or "")]
    return sorted(entries, key=lambda e: e.sec)
