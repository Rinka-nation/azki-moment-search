"""data/ のうち作り直せないものを、日付つきの zip にまとめる（2026-11-12 霧島リンカさん。控えは Google ドライブ）。

    .venv/Scripts/python scripts/backup_data.py [出力先のフォルダ]

- 入れるもの: 字幕の原本（captions）、コメント（comments）、AI修正 の入出力（llm）、調査（research）、
  手で直した字幕（manual_fixes.jsonl）、公開中の Turso の記録（turso_active.json）、git の控え（backup）
- 入れないもの: azkims.sqlite と turso_upload.sqlite（build で作り直せる）、*.env（トークン。クラウドに置かない）
- 出力先を省くと output/backup（git 管理外）。同じフォルダの古い zip は新しい 4 個を残して消す
"""

import sys
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
INCLUDE = [
    "captions",
    "comments",
    "llm",
    "research",
    "backup",
    "manual_fixes.jsonl",
    "turso_active.json",
]
KEEP = 4
PREFIX = "azkims-data-"


def files() -> list[Path]:
    out: list[Path] = []
    for name in INCLUDE:
        p = DATA / name
        if p.is_file():
            out.append(p)
        elif p.is_dir():
            out += sorted(f for f in p.rglob("*") if f.is_file())
    return [f for f in out if not f.name.endswith(".env")]


def main() -> None:
    dest = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "output" / "backup"
    dest.mkdir(parents=True, exist_ok=True)
    target = dest / f"{PREFIX}{datetime.now():%Y%m%d-%H%M}.zip"
    tmp = target.with_suffix(".zip.part")
    items = files()
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for f in items:
            z.write(f, f.relative_to(DATA).as_posix())
    tmp.replace(target)
    raw = sum(f.stat().st_size for f in items)
    print(
        f"{target}: {len(items)} ファイル、{raw / 1e6:.0f} MB → {target.stat().st_size / 1e6:.0f} MB"
    )
    old = sorted(dest.glob(f"{PREFIX}*.zip"))[:-KEEP]
    for p in old:
        p.unlink()
        print(f"古い控えを消しました: {p.name}")


if __name__ == "__main__":
    main()
