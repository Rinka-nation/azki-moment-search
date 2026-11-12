"""Rule-based triage of correction candidates (accept / title-scoped accept / hold / reject).

方針（2026-10-25 時点の運用ルール）:

- 日常語は補正しない: 候補の表記が「Sudachi の辞書にある 1 語」（マイク、方面、続き、麻雀）→ reject
- 動詞の活用形で終わる（開拓し(て)、開拓知ら(ない)）→ hold
- 読み距離 2 以上 → hold
- 不自然な分かれ方（ほ|ロ|面、ア|ず|色）や辞書に無い語（ポカジン）を含む＝誤認識らしい → accept
  （距離 1 で、出現がすべてその語のタイトルの配信なら --scope title）
- 普通の言葉の句としても読める（白上吹雪、赤いハート）
  - 読みが完全一致（距離 0）→ accept
  - 距離 1 で、タイムスタンプの裏付けがある → accept
  - 距離 1 で、その語のタイトルの配信で出ている → accept --scope title
  - それ以外 → hold（本文は字幕のまま、候補＝もしかして としては残る）
"""

from __future__ import annotations

from dataclasses import dataclass

from azkims.reading import ReadingConverter
from azkims.review import sound_distance
from azkims.review import unnatural as _unnatural
from azkims.textnorm import fold_reading

# 自動で補正してよい音の距離の上限（濁点・半濁点・拗音の小書きの違い 1 つまで）
MAX_SOUND_DISTANCE = 0.5

ACCEPT_MAX_DISTANCE = 1

_VERB_POS = {"動詞", "助動詞"}


@dataclass(frozen=True)
class Decision:
    surface: str
    canonical: str
    action: str  # accept / reject / hold
    scope: str  # global / title（accept のときだけ意味がある）
    reason: str


def classify(
    surface: str,
    canonical: str,
    distance: int,
    count: int,
    in_title: int,
    converter: ReadingConverter,
    ts_backed: bool = False,
    aggressive: bool = False,
) -> Decision:
    """ts_backed: タイムスタンプ（人が書いた正しい表記）の時刻付近の字幕で見つかった候補。"""
    tokens = [t for t in converter.sudachi_tokens(surface) if t.surface.strip()]
    if len(tokens) == 1 and not tokens[0].oov:
        return Decision(surface, canonical, "reject", "global", "日常語（辞書にある 1 語）")
    if tokens and tokens[-1].pos in _VERB_POS:
        # 開拓し(て) / 開拓知ら(ない) のように動詞の活用が付いた形。名詞への補正は文を壊す
        return Decision(surface, canonical, "hold", "global", "動詞の活用形を含む")
    # 音の距離（濁点・半濁点・拗音の小書きの違いは 0.5、それ以外は 1）が MAX_SOUND_DISTANCE を超えるものは
    # 自動では補正しない（2026-10-27）。いちゃん → すいちゃん（音の欠落）、まいちゃん → すいちゃん（別の音）
    # のような距離 1 の誤りが多かったため。ホロ取り → ホロドリ（ト/ド）、ポカジン → ポカジャン（ャ）は通る
    term = converter.dictionary.by_canonical.get(canonical)
    if term is not None:
        sd = sound_distance(fold_reading(converter.reading(surface)), term.folded)
        if sd > MAX_SOUND_DISTANCE:
            return Decision(surface, canonical, "hold", "global", f"音の距離 {sd:g}")
    natural = not _unnatural(tokens)
    if natural and count < 2 and in_title == 0 and not ts_backed:
        # 普通の言葉の句としても読めて（サード以上 = 3rd衣装？）、1 回しか出ず裏付けも無い
        return Decision(surface, canonical, "hold", "global", "普通の言葉の句で、裏付けが無い")
    if aggressive:
        # 過剰補正の実験（2026-10-25）: 日常語 1 語と動詞の活用形以外はすべて採用。
        # ただし読み距離 2 以上は「ちゃんと → エンチャント」「するウイルス → Tウイルス」のように
        # 普通の言葉を壊すものが大半だったので保留（2026-10-27）
        if distance >= 2:
            return Decision(surface, canonical, "hold", "global", f"読み距離 {distance}")
        scope = "title" if distance > 0 and in_title >= count else "global"
        return Decision(surface, canonical, "accept", scope, "強め補正（aggressive）")
    if distance > ACCEPT_MAX_DISTANCE:
        return Decision(surface, canonical, "hold", "global", f"読み距離 {distance}")
    title_only = in_title >= count
    if _unnatural(tokens):
        scope = "title" if distance > 0 and title_only else "global"
        return Decision(surface, canonical, "accept", scope, "誤認識らしい表記")
    # 以下、普通の言葉の句としても読めるもの（白上吹雪、赤いハート）
    if distance == 0:
        return Decision(surface, canonical, "accept", "global", "読みが完全一致")
    if ts_backed:
        return Decision(surface, canonical, "accept", "global", "タイムスタンプの裏付けあり")
    if in_title > 0:
        return Decision(
            surface, canonical, "accept", "title", "句だが、その語のタイトルの配信で出現"
        )
    return Decision(surface, canonical, "hold", "global", "普通の言葉の句としても読める")
