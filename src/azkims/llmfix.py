"""LLM（Haiku）による発言ごとの修正案を読み、build で別の層（text_ai）として当てる。

修正案は data/llm にキャッシュしたもの（build のたびに LLM は呼ばない）:
    manifest.jsonl   1 行 1 塊: name, video_id, start_ms[]（塊の n 番目の発言 = start_ms[n - 1]）
    in/{name}.txt    LLM に渡した発言（番号\t時刻\t本文）。本文は作成時点の text（辞書補正後）
    out/{name}.jsonl 修正案: n, n_end, wrong（本文から写した誤り）, fix, reason, confidence
    prop/out/{video_id}.jsonl 横展開: start_ms, wrong, fix, reason（同じ配信で直した誤りが、ほかの発言に
                     残っていたもの。AI修正 を当てた後の本文に対する修正なので、ほかの修正の後に当てる）

発言は (video_id, start_ms) で対応させる（build で発言の id は振り直される）。
当てるのは wrong が 1 つの発言の中にあるものだけ。LLM が示した番号が 1〜3 ずれていることがあるので、
近くの発言から探し直す。発言の境目をまたぐもの・どこにも無いもの・wrong と fix が同じものは捨てる。

LLM が書き足したホロメンの名前は、公式の表記（辞書のフルネームの名前の部分）にそろえる
（ペコラ → ぺこら、ふぶき → フブキ。2026-11-07 霧島リンカさん）。
ホロメンの呼び名を別の人の呼び名に変える修正（んなたん → かなたん）は捨てる（2026-11-09 霧島リンカさん）。
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from azkims.dictionary import Term

NEARBY = 3
# 名前の直後に続いてよい語（ここ以外で同じ文字種が続くなら、別の語の一部とみなして直さない）
_HONORIFICS = ("ちゃん", "さん", "せんぱい", "せんせい", "先輩", "先生", "たん", "様")
_LINE = re.compile(r"^(\d+)\t[\d:]+\t(.*)$", re.MULTILINE)

Fixes = dict[tuple[str, int], list[tuple[str, str]]]


@dataclass
class LoadStats:
    rows: int = 0
    used: int = 0
    moved: int = 0  # 番号のずれを近くの発言で直したもの
    renamed: int = 0  # ホロメンの名前を公式の表記にそろえたもの
    propagated: int = 0  # 横展開の修正
    dropped: dict[str, int] = field(default_factory=dict)

    def drop(self, reason: str) -> None:
        self.dropped[reason] = self.dropped.get(reason, 0) + 1


def _locate(utts: dict[int, str], n: int, n_end: int, wrong: str) -> tuple[int | None, bool]:
    """wrong を含む発言の番号と、示された範囲の外で見つけたか。"""
    for i in range(n, n_end + 1):
        if wrong in utts.get(i, ""):
            return i, False
    near = sorted(
        (i for i in range(n - NEARBY, n_end + NEARBY + 1) if not n <= i <= n_end),
        key=lambda i: min(abs(i - n), abs(i - n_end)),
    )
    for i in near:
        if wrong in utts.get(i, ""):
            return i, True
    return None, False


def _script(c: str) -> str:
    if "ぁ" <= c <= "ゖ":
        return "hira"
    if "ァ" <= c <= "ヺ" or c == "ー":
        return "kata"
    return ""


def _swap_script(s: str) -> str:
    """ひらがな ↔ カタカナ（長音などはそのまま）。"""
    return "".join(
        chr(ord(c) + 0x60)
        if _script(c) == "hira" and c != "ー"
        else chr(ord(c) - 0x60)
        if _script(c) == "kata" and c != "ー" and "ァ" <= c <= "ヶ"
        else c
        for c in s
    )


def official_names(terms: Iterable[Term]) -> dict[str, tuple[str, str]]:
    """公式と違う文字種の表記 → (公式の名前, フルネーム)。フルネーム（兎田ぺこら）の末尾のかな（ぺこら）から作る。

    全部かなの名前（ときのそら）は名前の部分が分からないので対象にしない。
    """
    names: dict[str, tuple[str, str]] = {}
    for t in terms:
        if t.category != "hololive" or t.group:
            continue
        m = re.search(r"[ぁ-ゖァ-ヺー]+$", t.canonical)
        if not m or m.start() == 0 or len(m[0]) < 2 or m[0] in _HONORIFICS:
            continue
        given = m[0]
        variant = _swap_script(given)
        if variant != given:
            names[variant] = (given, t.canonical)
    return names


def member_names(terms: Iterable[Term]) -> dict[str, str]:
    """ホロメンの名前・呼び名 → その人（Term.member）。"""
    return {
        t.canonical: t.member
        for t in terms
        if t.category in ("hololive", "nickname") and len(t.canonical) >= 2
    }


def swaps_member(wrong: str, fix: str, members: dict[str, str]) -> bool:
    """wrong にある誰かの呼び名を消し、fix に別の人の呼び名を書き足しているか。"""
    gone = {who for name, who in members.items() if name in wrong and name not in fix}
    added = {who for name, who in members.items() if name in fix and name not in wrong}
    return bool(gone and added - gone)


def normalize_names(wrong: str, fix: str, reason: str, names: dict[str, tuple[str, str]]) -> str:
    """fix で書き足された（wrong に無い）名前を公式の表記にする。

    名前と分かるときだけ直す: 修正の理由にフルネームがある（「兎田ぺこら（ペコラ）の呼びかけ」）か、
    直後に敬称が付く（チョコ先生）。普通の言葉（ここ、ボタン、チョコ）や別の語の一部（かみおき の みお）は直さない。
    """
    for variant, (official, full) in names.items():
        if variant not in fix or variant in wrong:
            continue
        by_reason = full in reason
        out, pos = [], 0
        for m in re.finditer(re.escape(variant), fix):
            before = fix[m.start() - 1] if m.start() else ""
            after = fix[m.end() :]
            joined_before = before and _script(before) == _script(variant[0])
            joined_after = (
                after
                and _script(after[0]) == _script(variant[-1])
                and not after.startswith(_HONORIFICS)
            )
            if joined_before or joined_after or not (by_reason or after.startswith(_HONORIFICS)):
                continue
            out.append(fix[pos : m.start()] + official)
            pos = m.end()
        fix = "".join(out) + fix[pos:]
    return fix


def load_fixes(
    llm_dir: Path,
    names: dict[str, tuple[str, str]] | None = None,
    members: dict[str, str] | None = None,
) -> tuple[Fixes, LoadStats]:
    fixes: Fixes = {}
    stats = LoadStats()
    manifest = llm_dir / "manifest.jsonl"
    if not manifest.exists():
        return fixes, stats
    for line in manifest.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        chunk = json.loads(line)
        out = llm_dir / "out" / f"{chunk['name']}.jsonl"
        if not out.exists():
            continue
        text = (llm_dir / "in" / f"{chunk['name']}.txt").read_text(encoding="utf-8")
        utts = {int(m[1]): m[2] for m in _LINE.finditer(text)}
        starts = chunk["start_ms"]
        for row in out.read_text(encoding="utf-8").splitlines():
            if not row.strip():
                continue
            stats.rows += 1
            try:
                r = json.loads(row)
            except json.JSONDecodeError:
                stats.drop("JSON でない")
                continue
            n, n_end = r.get("n"), r.get("n_end") or r.get("n")
            wrong = (r.get("wrong") or "").replace("\n", "")
            fix = (r.get("fix") or "").replace("\n", "")
            if not isinstance(n, int) or not isinstance(n_end, int) or not wrong:
                stats.drop("番号か wrong が無い")
                continue
            if not fix or fix == wrong:
                stats.drop("修正が無い")
                continue
            if members and swaps_member(wrong, fix, members):
                stats.drop("ホロメンの呼び名を別の人に変える")
                continue
            i, moved = _locate(utts, n, n_end, wrong)
            if i is None or not 1 <= i <= len(starts):
                stats.drop("wrong が 1 つの発言の中に無い")
                continue
            if names:
                renamed = normalize_names(wrong, fix, r.get("reason") or "", names)
                stats.renamed += renamed != fix
                fix = renamed
            fixes.setdefault((chunk["video_id"], starts[i - 1]), []).append((wrong, fix))
            stats.used += 1
            stats.moved += moved
    for out in sorted((llm_dir / "prop" / "out").glob("*.jsonl")):
        for row in out.read_text(encoding="utf-8").splitlines():
            if not row.strip():
                continue
            stats.rows += 1
            # 壊れた行は捨てて続ける（1 行で build と週 1 回の公開が止まらないように。ISS-15、2026-11-11）
            try:
                r = json.loads(row)
            except json.JSONDecodeError:
                stats.drop("JSON でない")
                continue
            r = r if isinstance(r, dict) else {}
            wrong, fix, start = r.get("wrong"), r.get("fix"), r.get("start_ms")
            if not isinstance(wrong, str) or not isinstance(fix, str) or not isinstance(start, int):
                stats.drop("横展開の行に wrong・fix・start_ms が無い")
                continue
            if members and swaps_member(wrong, fix, members):
                stats.drop("ホロメンの呼び名を別の人に変える")
                continue
            if names:
                renamed = normalize_names(wrong, fix, r.get("reason") or "", names)
                stats.renamed += renamed != fix
                fix = renamed
            fixes.setdefault((out.stem, start), []).append((wrong, fix))
            stats.used += 1
            stats.propagated += 1
    return fixes, stats


def apply_fixes(text: str, fixes: list[tuple[str, str]]) -> str | None:
    """text に修正案を当てる。変わらなければ None。

    wrong が今の text に無い修正案は当てない（作成後に辞書の補正で直った・変わった発言）。
    """
    out = text
    for wrong, fix in fixes:
        if wrong in out:
            out = out.replace(wrong, fix)
    return out if out != text else None
