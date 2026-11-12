"""Text -> reading (katakana) conversion.

Dictionary terms take priority over Sudachi: Sudachi reads 星街 as ホシガイ, but the
dictionary knows 星街すいせい = ホシマチスイセイ. Everything else is read by Sudachi.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cached_property

from azkims.dictionary import NO_CONTEXT, Context, Dictionary, Term
from azkims.textnorm import fold_reading, to_katakana


@dataclass(frozen=True)
class Token:
    surface: str
    reading: str  # folded
    start: int
    end: int
    term: Term | None = None
    oov: bool = False
    pos: str = ""


class ReadingConverter:
    def __init__(self, dictionary: Dictionary):
        self.dictionary = dictionary

    @cached_property
    def _sudachi(self):
        from sudachipy import Dictionary as SudachiDictionary

        return SudachiDictionary(dict="core")

    @cached_property
    def _tokenizer(self):
        from sudachipy import SplitMode

        return self._sudachi.tokenizer(mode=SplitMode.C)

    @cached_property
    def _tokenizer_fine(self):
        from sudachipy import SplitMode

        return self._sudachi.tokenizer(mode=SplitMode.A)

    def _sudachi_tokens(self, text: str, offset: int, fine: bool = False) -> list[Token]:
        tokenizer = self._tokenizer_fine if fine else self._tokenizer
        tokens = []
        for m in tokenizer.tokenize(text):
            surface = m.surface()
            if not surface.strip():
                continue
            reading = m.reading_form() or surface
            tokens.append(
                Token(
                    surface=surface,
                    reading=fold_reading(to_katakana(reading)),
                    start=offset + m.begin(),
                    end=offset + m.end(),
                    oov=m.is_oov(),
                    pos=m.part_of_speech()[0],
                )
            )
        return tokens

    def tokens(
        self,
        text: str,
        fine: bool = False,
        context: Context = NO_CONTEXT,
        near: Context = NO_CONTEXT,
        include_games: bool = False,
    ) -> list[Token]:
        """fine=True は Sudachi の短単位（A）。複合語を分ける（釣り竿 → 釣り|竿）。辞書語は常に 1 語。"""
        out: list[Token] = []
        pos = 0
        for m in self.dictionary.find(text, context, near, include_games):
            if m.start > pos:
                out.extend(self._sudachi_tokens(text[pos : m.start], pos, fine))
            out.append(Token(m.surface, m.term.folded, m.start, m.end, term=m.term, pos="名詞"))
            pos = m.end
        if pos < len(text):
            out.extend(self._sudachi_tokens(text[pos:], pos, fine))
        return out

    def sudachi_tokens(self, text: str) -> list[Token]:
        """辞書を使わず Sudachi だけで分割（普通の日本語として読めるかの判定用）。"""
        return self._sudachi_tokens(text, 0)

    def reading(self, text: str) -> str:
        """読み。検索語の変換用なので、ゲーム別の辞書の語も使う（どの配信の語か分からないため）。"""
        return "".join(t.reading for t in self.tokens(text, include_games=True))


# 語の境界判定に使わない品詞（これらで始まる/終わる連なりは「語」として登録しない）
_EDGE_SKIP_POS = {"助詞", "助動詞", "補助記号", "空白"}
MAX_WORD_SPAN = 4


def word_readings(tokens: list[Token], max_span: int = MAX_WORD_SPAN) -> list[str]:
    """Readings of every contiguous run of 1..max_span tokens, aligned to token boundaries.

    「かいたくしゃ」で 開拓者、「てつどうのひ」で 鉄道の日 に“語単位で”一致させるための索引。
    分かち書きの粒度が検索語側と違っても、連なりを全部登録しておけば境界の一致だけで判定できる。
    """
    words: dict[str, None] = {}
    toks = [t for t in tokens if t.reading]
    for i, first in enumerate(toks):
        if first.pos in _EDGE_SKIP_POS:
            continue
        reading = ""
        for tok in toks[i : i + max_span]:
            reading += tok.reading
            if tok.pos not in _EDGE_SKIP_POS and len(reading) >= 2:
                words[reading] = None
    return list(words)
