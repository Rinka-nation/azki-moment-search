"""スパチャ読みで名前だけを読み上げている部分を見分ける。

自動字幕の読み上げ部分は「親方さん、マコールさん、宮本さん、ありがとう」のように人の名前が並ぶ。
ここに一致すると話題の検索の邪魔になるので、名前の連続だけを切り出し、検索では別のタブに分ける。
発言ごとは落とさない。同じ発言に読まれたメッセージや話題が含まれるため（表示も原文も変えない）。

AZKi はリスナーを さん 付けで呼び、ホロメンは ちゃん 付けが多いので、さん だけを見る。
辞書にある語（ホロメンの名前など）を含む区切りは名前の読み上げとして扱わない。
"""

from __future__ import annotations

import re
from collections.abc import Callable

MIN_NAMES = 3  # 異なる名前がこれだけ続いたら読み上げとする
UNPUNCTUATED_MIN_SAN = 5  # 句読点のない発言は、さん がこれだけあるときだけ さん で区切る
UNPUNCTUATED_MAX_COMMAS = 1
MAX_NAME_CHARS = 14

_SEPARATOR = re.compile(r"([、。!?！？\s]+)")
_FILLER = re.compile(r"(?:え+|えー+|えっと|あ+|あー+|ま|まあ|で|と|あと|はい)")
_THANKS = re.compile(r"(?:え|えー)?(?:ありがとう(?:ございます|ございました)?|サンキュー)")
_NAME = re.compile(rf".{{1,{MAX_NAME_CHARS}}}さん")
_NOT_NAMES = frozenset({"皆さん", "みなさん", "みんなさん", "さん"})


def _kind(clause: str, is_known: Callable[[str], bool]) -> str:
    """n = 名前、f = 間をつなぐ語、x = それ以外（読み上げの連続を切る）。"""
    if not clause or _FILLER.fullmatch(clause) or _THANKS.fullmatch(clause):
        return "f"
    if clause not in _NOT_NAMES and _NAME.fullmatch(clause) and not is_known(clause):
        return "n"
    return "x"


def strip_name_runs(text: str, is_known: Callable[[str], bool]) -> str:
    """名前の連続を除いた文字列を返す。連続が無ければ text をそのまま返す。

    発言の全体が読み上げなら空文字列。呼び出し側は戻り値が text と違うかで判定する。
    """
    if text.count("さん") < MIN_NAMES:
        return text
    source = text
    if source.count("さん") >= UNPUNCTUATED_MIN_SAN and source.count("、") <= (
        UNPUNCTUATED_MAX_COMMAS
    ):
        source = re.sub(r"さん(?![、。])", "さん、", source)
    parts = _SEPARATOR.split(source)
    clauses, seps = parts[0::2], [*parts[1::2], ""]
    kinds = [_kind(c, is_known) for c in clauses]

    drop = [False] * len(clauses)
    i = 0
    found = False
    while i < len(clauses):
        if kinds[i] != "n":
            i += 1
            continue
        end = i
        while end < len(clauses) and kinds[end] != "x":
            end += 1
        if len({c for c, k in zip(clauses[i:end], kinds[i:end], strict=True) if k == "n"}) >= (
            MIN_NAMES
        ):
            found = True
            drop[i:end] = [True] * (end - i)  # 最後の名前の後ろのお礼も読み上げの一部
        i = end
    if not found:
        return text
    kept = "".join(c + s for c, s, gone in zip(clauses, seps, drop, strict=True) if not gone)
    kept = kept.strip("、。!?！？ ")
    return "" if _FILLER.fullmatch(kept) else kept
