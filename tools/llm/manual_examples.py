"""人が手で直した字幕（data/manual_fixes.jsonl）を、AI修正 の参考にする（2026-11-12 霧島リンカさん）。

    .venv/Scripts/python tools/llm/manual_examples.py [出力先.md]

出すもの:
- 人が直した例（字幕の原文 → 正しい文、メモ）。次に AI修正 を作るとき、指示文の「人が直した例」に入れる
- AI修正 の当たり具合: 人が直した発言のうち、AI修正 が当たっていたもの（= AI の修正を人が直し直したもの）の数

モデルは呼ばない（読むだけ）。出力先を省くと output/manual_examples.md（git 管理外）。
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from azkims import manualfix
from azkims.config import load_config
from azkims.dictionary import load_dictionary
from azkims.llmfix import load_fixes, member_names, official_names


def main() -> None:
    cfg = load_config()
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else cfg.output_dir / "manual_examples.md"
    fixes = manualfix.load(manualfix.path_for(cfg.data_dir))
    dictionary = load_dictionary(cfg.dictionary_dir)
    ai, _ = load_fixes(
        cfg.data_dir / "llm", official_names(dictionary.terms), member_names(dictionary.terms)
    )
    overruled = [f for f in fixes.values() if (f.video_id, f.start_ms) in ai]
    lines = [
        "# 人が手で直した字幕",
        "",
        f"- 件数: {len(fixes)}",
        f"- うち AI修正 があった発言（人が AI の修正を直し直したもの）: {len(overruled)}",
        "",
        "## 人が直した例（AI修正 の指示文に入れる）",
        "",
    ]
    for f in sorted(fixes.values(), key=lambda f: f.at):
        pairs = ai.get((f.video_id, f.start_ms), [])
        ai_text = f"（AI修正 あり: {'、'.join(f'{w}→{x}' for w, x in pairs)}）" if pairs else ""
        lines += [f"- 字幕: {f.raw}", f"  - 正しい文: {f.text}{ai_text}"]
        if f.note:
            lines.append(f"  - メモ: {f.note}")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"{out}: {len(fixes)} 件（AI修正 を直し直したもの {len(overruled)}）")


if __name__ == "__main__":
    main()
