"""YouTube auto-caption (json3) parsing and segmentation into utterances.

json3 auto captions are a sequence of events. Events with `aAppend` are line breaks of the
rolling caption window; the rest carry text in `segs`, optionally with per-word `tOffsetMs`.
We flatten everything into (char, time_ms) pairs and cut utterances at sentence punctuation,
silence gaps, or a maximum length.
"""

from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from azkims.textnorm import CAPTION_TAG_RE

SENTENCE_END = set("。?!？！")
SOFT_BREAK = set("、, ")
_TAG_GAP = "\x00"  # [音楽] などのタグ位置。発言の区切りとして扱う
# これより短い発言は直前につなげる（細切れ対策）
MIN_UTTERANCE_CHARS = 8
# 直前の発言とこの時間以内ならつなげてよい
JOIN_GAP_MS = 4000


@dataclass(frozen=True)
class Utterance:
    start_ms: int
    end_ms: int
    text: str


def load_json3(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def timed_chars(data: dict) -> list[tuple[str, int]]:
    chars: list[tuple[str, int]] = []
    for event in data.get("events", []):
        if event.get("aAppend") or "segs" not in event:
            continue
        base = int(event.get("tStartMs", 0))
        for seg in event["segs"]:
            text = seg.get("utf8", "")
            if not text or text == "\n":
                continue
            text = unicodedata.normalize("NFKC", text.replace("\n", ""))
            text = CAPTION_TAG_RE.sub(_TAG_GAP, text)
            t = base + int(seg.get("tOffsetMs", 0))
            chars.extend((c, t) for c in text)
    return chars


def segment(
    chars: list[tuple[str, int]], max_chars: int = 60, gap_ms: int = 2000
) -> list[Utterance]:
    out: list[Utterance] = []
    buf: list[tuple[str, int]] = []

    def flush() -> None:
        text = "".join(c for c, _ in buf).strip(" 、,")
        if text:
            out.append(Utterance(buf[0][1], buf[-1][1], " ".join(text.split())))
        buf.clear()

    for c, t in chars:
        if c == _TAG_GAP:
            flush()
            continue
        if buf and t - buf[-1][1] > gap_ms:
            flush()
        if not buf and (c.isspace() or c in SOFT_BREAK or c in SENTENCE_END):
            continue
        buf.append((c, t))
        if (
            c in SENTENCE_END
            or len(buf) >= max_chars
            and (c in SOFT_BREAK or len(buf) >= max_chars + 20)
        ):
            flush()
    flush()
    return [u for u in merge_fragments(out, max_chars=max_chars) if len(u.text) >= 2]


def merge_fragments(
    utts: list[Utterance],
    max_chars: int = 60,
    min_chars: int = MIN_UTTERANCE_CHARS,
    join_gap_ms: int = JOIN_GAP_MS,
) -> list[Utterance]:
    """細切れの発言（「れる。」「いつ」「同時に消え。」）を直前の発言につなげる。

    字幕の小さな無音や [笑い] などのタグで、文の途中で切れてしまうことが多い。
    直前との間が join_gap_ms 以内で、次のどちらかならつなげる:
    - この発言が短い（min_chars 未満）
    - 直前の発言が文末記号で終わっていない（文の途中で切れている）
    """
    out: list[Utterance] = []
    for u in utts:
        prev = out[-1] if out else None
        if (
            prev is not None
            and u.start_ms - prev.end_ms <= join_gap_ms
            and len(prev.text) + len(u.text) <= max_chars + 20
            and (len(u.text) < min_chars or prev.text[-1] not in SENTENCE_END)
        ):
            out[-1] = Utterance(prev.start_ms, u.end_ms, prev.text + u.text)
        else:
            out.append(u)
    return out


def parse_captions(path: Path, max_chars: int = 60, gap_ms: int = 2000) -> list[Utterance]:
    return segment(timed_chars(load_json3(path)), max_chars=max_chars, gap_ms=gap_ms)
