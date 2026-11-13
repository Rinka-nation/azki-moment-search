"""人が手で直した字幕（2026-11-12 霧島リンカさん）。

辞書（全部の配信に当たる alias）でも AI修正 でも直せない、1 か所だけの誤りを直すための層。
手元の `azkims serve` の画面からだけ書き込む（公開側には書き込みの API を置かない）。

保存先は data/manual_fixes.jsonl（git 管理外。公開しない）。1 行 1 件:
    {"video_id", "start_ms", "raw", "text", "note", "at", "by": "human"}
同じ発言（video_id, start_ms）は後の行が勝つ。text が null の行は取り消し。

build での優先順は 手で直したもの → AI修正 → 辞書。raw は保存したときの字幕原文で、
今の原文と違えば（字幕を取り直した・区切り方を変えたなど）当てずに数えて知らせる。
今後 AI修正 を作るときの参考（人が直した例）と、AI修正 の当たり具合を測るのにも使う。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

FILE = "manual_fixes.jsonl"

Key = tuple[str, int]


@dataclass(frozen=True)
class ManualFix:
    video_id: str
    start_ms: int
    raw: str
    text: str | None  # None は取り消し
    note: str = ""
    at: str = ""
    by: str = "human"


@dataclass
class ApplyStats:
    used: int = 0
    used_keys: set[Key] = field(default_factory=set)
    mismatched: set[Key] = field(default_factory=set)

    @property
    def raw_changed(self) -> list[Key]:
        """原文が保存したときと違って当てられなかった直し。

        字幕には同じ開始時刻の発言が複数あることがある（長さ 0 の発言。2026-11-13 の DB で 37,173 組）。
        そのうち 1 つに当たれば、ほかの発言の原文が違っても知らせない。
        """
        return sorted(self.mismatched - self.used_keys)


def path_for(data_dir: Path) -> Path:
    return data_dir / FILE


def load(path: Path) -> dict[Key, ManualFix]:
    """壊れた行は飛ばす（1 行で build と週 1 回の公開が止まらないように）。"""
    fixes: dict[Key, ManualFix] = {}
    if not path.exists():
        return fixes
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
            fix = ManualFix(
                video_id=str(r["video_id"]),
                start_ms=int(r["start_ms"]),
                raw=str(r["raw"]),
                text=None if r.get("text") is None else str(r["text"]),
                note=str(r.get("note") or ""),
                at=str(r.get("at") or ""),
                by=str(r.get("by") or "human"),
            )
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            continue
        key = (fix.video_id, fix.start_ms)
        if fix.text is None:
            fixes.pop(key, None)
        else:
            fixes[key] = fix
    return fixes


def append(path: Path, fix: ManualFix) -> ManualFix:
    """1 件を書き足す。at が空なら今の時刻（UTC）を入れる。"""
    if not fix.at:
        fix = ManualFix(**{**asdict(fix), "at": datetime.now(UTC).isoformat(timespec="seconds")})
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(asdict(fix), ensure_ascii=False) + "\n")
    return fix


def lookup(
    fixes: dict[Key, ManualFix], video_id: str, start_ms: int, raw: str, stats: ApplyStats
) -> str | None:
    """この発言の手で直した文。無い、または原文が保存したときと違えば None。"""
    fix = fixes.get((video_id, start_ms))
    if fix is None:
        return None
    if fix.raw != raw:
        stats.mismatched.add((video_id, start_ms))
        return None
    stats.used += 1
    stats.used_keys.add((video_id, start_ms))
    return fix.text
