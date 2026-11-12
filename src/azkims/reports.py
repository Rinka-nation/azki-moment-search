"""Markdown reports for human review (output/*.md)."""

from __future__ import annotations

from pathlib import Path

from azkims.review import CandidateGroup, Example, UnknownWord


def _link(ex: Example) -> str:
    sec = max(0, ex.start_ms // 1000 - 2)
    return f"https://www.youtube.com/watch?v={ex.video_id}&t={sec}s"


def _q(s: str) -> str:
    return '"' + s.replace('"', '\\"') + '"'


def write_fuzzy_report(path: Path, groups: list[CandidateGroup]) -> None:
    lines = [
        "# 補正候補レビュー",
        "",
        "読みが辞書の語に近いが、まだ登録されていない表記の一覧です。",
        "これらは検索では「もしかして」（候補）として出ます。正しい補正なら `accept` で確定補正に昇格、",
        "誤検出なら `reject` で候補から外れます（どちらも次回の build から反映）。",
        "",
        "- 距離: 読みの編集距離（0 = 読みが完全一致）",
        "- タイトル一致: 配信タイトルにその語が含まれていた件数（コラボ相手など。採用してよい可能性が高い）",
        "",
    ]
    for g in groups:
        lines += [
            f"## {g.surface} → {g.canonical}",
            "",
            f"- 件数: {g.count} / 距離: {g.distance} / タイトル一致: {g.in_title}",
            f"- 採用: `azkims accept {_q(g.surface)} {_q(g.canonical)}`"
            + (" （普通の言葉と紛らわしいなら `--scope title`）" if g.distance else ""),
            f"- 却下: `azkims reject {_q(g.surface)} {_q(g.canonical)}`",
        ]
        for ex in g.examples:
            lines.append(f"  - 「{ex.text}」 [{ex.title[:40]}]({_link(ex)})")
        lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def write_unknown_report(path: Path, words: list[UnknownWord]) -> None:
    lines = [
        "# 未知語レポート",
        "",
        "形態素解析辞書に無い語（固有名詞・ゲーム名・誤認識の可能性）を、登場配信数の多い順に並べています。",
        "固有名詞なら `config/dictionary/*.toml` に `[[term]]` として追加してください。",
        "",
        "| 語 | 配信数 | 件数 | 例 |",
        "|---|---:|---:|---|",
    ]
    for w in words:
        ex = w.examples[0]
        context = ex.text.replace("|", "｜")
        lines.append(f"| {w.surface} | {len(w.streams)} | {w.count} | [{context}]({_link(ex)}) |")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _ts_link(video_id: str, sec: int) -> str:
    return f"https://www.youtube.com/watch?v={video_id}&t={max(0, sec - 2)}s"


def write_alignment_report(
    path: Path, groups, coverage, quotes, max_quote_examples: int = 80
) -> None:
    from azkims.align import quote_summary

    lines = [
        "# タイムスタンプ × 自動字幕 照合レポート",
        "",
        "タイムスタンプ（人が書いた表記）を正解として、その時刻付近（-20秒〜+60秒）の自動字幕と照合した結果です。",
        "",
        "## 1. 用語の拾えている率",
        "",
        "タイムスタンプで言及された辞書語が、付近の字幕（補正後）に出てくるか。",
        "",
        "| 語 | 言及 | 字幕にあり | 別表記あり | 見つからず |",
        "|---|---:|---:|---:|---:|",
    ]
    for c in coverage:
        lines.append(
            f"| {c.canonical} | {c.mentions} | {c.in_captions} | {c.variant} | {c.missing} |"
        )
    lines += [
        "",
        "## 2. 裏付けのある補正候補",
        "",
        "タイムスタンプにその語があり、付近の字幕で読みの近い表記が見つかったもの。",
        "",
    ]
    for g in groups:
        lines += [
            f"### {g.surface} → {g.canonical}（{g.count} 件 / 読み距離 {g.distance}）",
            "",
            f"- 採用: `azkims accept {_q(g.surface)} {_q(g.canonical)}`",
            f"- 却下: `azkims reject {_q(g.surface)} {_q(g.canonical)}`",
        ]
        for ex in g.examples[:3]:
            lines.append(
                f"  - TS「{ex.ts_text}」 / 字幕「{ex.caption}」 ({_ts_link(ex.video_id, ex.sec)})"
            )
        lines.append("")

    summary = quote_summary(quotes)
    total = sum(summary.values()) or 1
    labels = {
        "surface": "表記も一致",
        "reading": "読みは一致（表記違い）",
        "near": "読みが近い",
        "missing": "見つからず",
    }
    lines += ["## 3. 「」発言と字幕の照合", ""]
    for kind, label in labels.items():
        n = summary.get(kind, 0)
        lines.append(f"- {label}: {n} 件（{n * 100 // total}%）")
    lines += ["", "### 表記が食い違う例（辞書に足す語・誤認識パターンの発見用）", ""]
    shown = [q for q in quotes if q.kind in ("reading", "near")][:max_quote_examples]
    for q in shown:
        mark = "読み一致" if q.kind == "reading" else f"読み距離 {q.distance}"
        lines.append(
            f"- 「{q.quote}」 ⇔ 字幕「{q.caption}」（{mark}） {_ts_link(q.video_id, q.sec)}"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
