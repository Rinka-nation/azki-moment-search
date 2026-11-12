"""Text normalization shared by indexing and querying.

Two levels:
- normalize_text: display/search surface form (NFKC, whitespace cleanup, caption tags removed)
- fold_reading: aggressive katakana folding used only for the reading column, so that
  ASR spelling variants (ガッチャー / ガチャ, ジオゲッサー / ジオゲッサ) compare equal.
"""

from __future__ import annotations

import re
import unicodedata

# YouTube 自動字幕の音声タグ: [笑い] [鼻息] [音楽] など
CAPTION_TAG_RE = re.compile(r"\[[^\[\]]{1,12}\]")

_HIRA_START, _HIRA_END = ord("ぁ"), ord("ゖ")
_SMALL_TO_LARGE = str.maketrans("ァィゥェォャュョヮヵヶ", "アイウエオヤユヨワカケ")
_V_SOUNDS = {"ヴァ": "バ", "ヴィ": "ビ", "ヴェ": "ベ", "ヴォ": "ボ", "ヴュ": "ビュ"}
_V_RE = re.compile("|".join(_V_SOUNDS))
_KANA_MERGE = str.maketrans({"ヴ": "ブ", "ヂ": "ジ", "ヅ": "ズ", "ヲ": "オ"})
_FOLD_DROP_RE = re.compile(r"[ーｰ‐\-・･ッ\s、。,.!?！？「」『』()（）~〜]")


def normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = CAPTION_TAG_RE.sub(" ", text)
    text = text.replace("\n", "")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def to_katakana(text: str) -> str:
    return "".join(chr(ord(c) + 0x60) if _HIRA_START <= ord(c) <= _HIRA_END else c for c in text)


def fold_reading(text: str) -> str:
    """Fold a reading (or any kana text) into a canonical comparison key."""
    text = unicodedata.normalize("NFKC", text)
    text = to_katakana(text).upper()
    text = _V_RE.sub(lambda m: _V_SOUNDS[m.group()], text)
    text = text.translate(_KANA_MERGE).translate(_SMALL_TO_LARGE)
    return _FOLD_DROP_RE.sub("", text)
