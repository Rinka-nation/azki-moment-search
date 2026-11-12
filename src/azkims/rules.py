"""文脈つき補正ルール（普通の言葉とぶつかる短い崩れ方のため）。

例: 字幕の「かけん」は かなけん のことも、「かける」の口語（水かけんの?、迷惑かけんなよ）のこともある。
辞書の語に次のように書く:

```toml
[[term]]
canonical = "かなけん"
...

[[term.rules]]
surfaces = ["かけん", "カケン"]
after = ["ライブ", "社員"]                 # ① 後ろにこれが続けば、どの配信でも補正する
before = ["こん", "おつ"]                   #    前がこれでも、どの配信でも補正する（挨拶 こんかなけん）
not_before = ["水", "声", "[にもを]"]       #    前がこれ（正規表現。直前で一致）なら補正しない
not_after = ["の[?？]", "のか", "なよ"]     #    後ろがこれ（正規表現。直後で一致）なら補正しない
topic = ["天音かなた", "沙花叉クロヱ", "かなけん"]  # ② その配信の話題ならそれ以外も補正する
```

判定の順番（2026-10-30。27 件をユーザーと確認して決めた）:

1. 後ろが `after`、前が `before` → 補正（組み合わせ）
2. 前が `not_before` / 後ろが `not_after` → そのまま（普通の言葉の形）
3. 配信の話題が `topic` → 補正。話題は タイトル・タイムスタンプの文脈（Context）に topic の人・語が
   あるか、補正後の字幕に topic の人・語が `caption_topic_min` 回以上出てくるか
4. それ以外 → 補正候補（もしかして）。本文は書き換えない。
   `otherwise = "keep"` なら候補にもしない（かなけ: 長引かなければ のような普通の言葉が多い）
"""

from __future__ import annotations

import re
from dataclasses import dataclass

FIX = "fix"
MAYBE = "maybe"
KEEP = "keep"
DEFAULT_CAPTION_TOPIC_MIN = 3


@dataclass(frozen=True)
class Rule:
    canonical: str
    surfaces: tuple[str, ...]
    after: tuple[str, ...]
    not_before: re.Pattern | None
    not_after: re.Pattern | None
    topic: tuple[str, ...]
    caption_topic_min: int = DEFAULT_CAPTION_TOPIC_MIN
    before: tuple[str, ...] = ()
    otherwise: str = MAYBE  # 4 の判定（maybe / keep）


def parse_rule(canonical: str, entry: dict) -> Rule:
    def pattern(items: list[str], anchor_end: bool) -> re.Pattern | None:
        if not items:
            return None
        body = "|".join(f"(?:{x})" for x in items)
        return re.compile(f"(?:{body})$" if anchor_end else f"^(?:{body})")

    return Rule(
        canonical=canonical,
        surfaces=tuple(entry["surfaces"]),
        after=tuple(entry.get("after", [])),
        not_before=pattern(entry.get("not_before", []), anchor_end=True),
        not_after=pattern(entry.get("not_after", []), anchor_end=False),
        topic=tuple(entry.get("topic", [])),
        caption_topic_min=int(entry.get("caption_topic_min", DEFAULT_CAPTION_TOPIC_MIN)),
        before=tuple(entry.get("before", [])),
        otherwise=entry.get("otherwise", MAYBE),
    )


@dataclass(frozen=True)
class RuleMatch:
    start: int
    end: int
    surface: str
    rule: Rule
    decision: str  # fix / maybe / keep
    why: str


def find(text: str, rules: list[Rule], topical: frozenset[Rule]) -> list[RuleMatch]:
    """text の中のルールの表記を判定する。topical: その配信が話題だと分かったルール。重なりなし。"""
    out: list[RuleMatch] = []
    taken: list[tuple[int, int]] = []
    for rule in rules:
        for surface in rule.surfaces:
            start = text.find(surface)
            while start >= 0:
                end = start + len(surface)
                # 正しい表記の一部（かなけん の中の かなけ）は対象外
                correct = text.startswith(rule.canonical, start)
                if not correct and all(end <= s or start >= e for s, e in taken):
                    before, after = text[:start], text[end:]
                    if any(after.startswith(a) for a in rule.after) or any(
                        before.endswith(b) for b in rule.before
                    ):
                        decision, why = FIX, "組み合わせ"
                    elif (rule.not_before and rule.not_before.search(before)) or (
                        rule.not_after and rule.not_after.search(after)
                    ):
                        decision, why = KEEP, "普通の言葉の形"
                    elif rule in topical:
                        decision, why = FIX, "話題の配信"
                    else:
                        decision, why = rule.otherwise, "決め手なし"
                    out.append(RuleMatch(start, end, surface, rule, decision, why))
                    taken.append((start, end))
                start = text.find(surface, end)
    return sorted(out, key=lambda m: m.start)


def apply_fixes(text: str, matches: list[RuleMatch]) -> tuple[str, list[tuple[str, str]]]:
    """decision = fix の箇所を canonical に置き換える。"""
    out, applied, pos = [], [], 0
    for m in matches:
        if m.decision != FIX:
            continue
        out += [text[pos : m.start], m.rule.canonical]
        applied.append((m.surface, m.rule.canonical))
        pos = m.end
    out.append(text[pos:])
    return "".join(out), applied
