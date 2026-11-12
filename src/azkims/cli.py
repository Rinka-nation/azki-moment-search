from __future__ import annotations

import argparse
import sys
from pathlib import Path

from azkims import db, pipeline
from azkims.config import load_config
from azkims.dictionary import append_review_decision, load_dictionary
from azkims.reading import ReadingConverter
from azkims.reports import write_fuzzy_report, write_unknown_report
from azkims.review import collect_unknown, load_candidate_groups
from azkims.search import (
    TABS,
    TIER_LABELS,
    QueryError,
    count_tiers,
    parse_query,
    search,
    search_timestamps,
)


def _log(msg: str) -> None:
    print(msg, flush=True)


def _corpus_rows(conn, streams: int | None):
    sql = (
        "SELECT u.video_id, u.start_ms, u.text, s.title FROM utterances u "
        "JOIN streams s ON s.video_id = u.video_id "
    )
    params: list = []
    if streams:
        sql += (
            "WHERE u.video_id IN (SELECT video_id FROM streams WHERE caption_status = 'ok' "
            "ORDER BY list_order LIMIT ?) "
        )
        params.append(streams)
    sql += "ORDER BY s.list_order, u.start_ms"
    for r in conn.execute(sql, params):
        yield r["video_id"], r["start_ms"], r["text"], r["title"]


def cmd_sync(args, cfg, conn):
    pipeline.sync(conn, cfg, args.limit, _log)


def cmd_fetch(args, cfg, conn):
    pipeline.fetch(conn, cfg, _log, limit=args.limit, retry_errors=args.retry_errors, ids=args.ids)


def cmd_serve(args, cfg, conn):
    from azkims.server import serve

    conn.close()  # サーバーはリクエストごとに読み取り専用で開く
    serve(cfg, args.port, _log)


def cmd_bundle_vercel(args, cfg, conn):
    from azkims.deploy import bundle_vercel, export_for_turso

    conn.close()
    if args.turso_export:
        export_for_turso(cfg, _log)
    bundle_vercel(cfg, _log, remote_db=args.turso)


def cmd_smoke(args, cfg, conn):
    """デプロイ先の API が正しい結果を返すか確かめる。保護された環境は BYPASS の値で通す。"""
    import json
    import os
    import urllib.parse
    import urllib.request

    conn.close()
    base = args.url.rstrip("/")
    headers = {}
    if secret := os.environ.get("VERCEL_AUTOMATION_BYPASS_SECRET"):
        headers["x-vercel-protection-bypass"] = secret
    checks = [
        ("件数", "/api/stats", {}, lambda r: r["streams"] > 0 and r["utterances"] > 0),
        ("3 文字以上", "/api/search", {"q": "開拓者"}, lambda r: r["counts"]["match"] > 0),
        ("2 文字", "/api/search", {"q": "配信"}, lambda r: r["counts"]["match"] > 0),
        ("関連語", "/api/search", {"q": "天音かなた", "tab": "related"}, lambda r: r["hits"]),
        ("タイムスタンプ", "/api/search", {"q": "開拓者", "tab": "ts"}, lambda r: r["hits"]),
    ]
    failed = 0
    for label, path, params, ok in checks:
        url = f"{base}{path}?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                passed = bool(ok(json.load(response)))
            detail = ""
        except (OSError, ValueError, KeyError) as e:
            passed, detail = False, f" … {type(e).__name__}: {e}"
        failed += not passed
        print(f"{'OK  ' if passed else 'FAIL'} {label}{detail}")
    return 1 if failed else 0


def cmd_publish(args, cfg, conn):
    from azkims.publish import PublishError, run

    conn.close()
    try:
        run(cfg, _log, force=args.force)
    except PublishError as e:
        print(f"中止: {e}", file=sys.stderr)
        return 1
    return 0


def cmd_fetch_comments(args, cfg, conn):
    pipeline.fetch_comments(conn, cfg, _log, limit=args.limit, retry_errors=args.retry_errors)


def cmd_build(args, cfg, conn):
    pipeline.build(conn, cfg, _log)


def _print_hit(h) -> None:
    print(f"  [{h.label}] {h.upload_date or '????-??-??'} {h.timestamp}  {h.display_text}")
    if h.display_text != h.text_raw:
        print(f"      字幕原文: {h.text_raw}")
    for c in h.candidates:
        print(f"      補正候補: {c.surface} → {c.canonical}（読み距離 {c.distance}）")
    print(f"      {h.title[:60]}")
    print(f"      {h.url}")


TAB_HEADINGS = {
    "match": "一致",
    "maybe": "もしかして",
}


def tab_header(tab: str, counts: dict[int, int]) -> str:
    """件数の見出し。内訳は search.TABS と TIER_LABELS から作り、tier の振り直しに追いつかせる（ISS-12）。"""
    if tab == "all":
        return f"■ すべて {sum(counts.values())} 件"
    lo, hi = TABS[tab]
    tiers = range(lo, hi + 1)
    parts = " / ".join(f"{TIER_LABELS[t]} {counts.get(t, 0)}" for t in tiers)
    head = f"■ {TAB_HEADINGS[tab]} {sum(counts.get(t, 0) for t in tiers)} 件（{parts}）"
    return head + (" … 自動字幕の補正候補・読みの一致。誤検出を含みます" if tab == "maybe" else "")


def cmd_search(args, cfg, conn):
    converter = ReadingConverter(load_dictionary(cfg.dictionary_dir))
    filters = {
        "title": args.title,
        "date_from": args.date_from,
        "date_to": args.date_to,
        "use_reading": not args.surface_only,
    }
    try:
        terms = parse_query(" ".join(args.query), converter)
        counts = count_tiers(conn, terms, **filters)
        tabs = ["match", "maybe"] if args.tab == "both" else [args.tab]
        ts_hits, ts_total = ([], 0)
        if args.tab in ("both", "ts"):
            ts_hits, ts_total = search_timestamps(
                conn, terms, limit=args.limit, offset=args.offset, **filters
            )
        tabs = [t for t in tabs if t != "ts"]
        results = {
            tab: search(
                conn,
                terms,
                tab=tab,
                sort=args.sort,
                limit=args.limit,
                offset=args.offset,
                **filters,
            )
            for tab in tabs
        }
    except QueryError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    readings = ", ".join(f"{t.surface}={t.reading or '-'}" for t in terms)
    print(f"読み: {readings}\n")
    if args.tab in ("both", "ts"):
        print(f"■ タイムスタンプ {ts_total} 件 … 人が書いたタイムスタンプコメント")
        for t in ts_hits:
            print(f"  [{t.label}] {t.upload_date or '????-??-??'} {t.timestamp}  {t.text}")
            print(f"      {t.title[:60]}")
            print(f"      {t.url}")
        print()
    for tab, hits in results.items():
        print(tab_header(tab, counts))
        for h in hits:
            _print_hit(h)
        print()
    return 0


def cmd_review(args, cfg, conn):
    groups = load_candidate_groups(conn, held=load_dictionary(cfg.dictionary_dir).held)
    out = cfg.output_dir / "review_candidates.md"
    write_fuzzy_report(out, groups)
    print(f"{len(groups)} candidate groups -> {out}")


def cmd_align(args, cfg, conn):
    from azkims.align import collect_quotes, collect_term_evidence
    from azkims.reports import write_alignment_report

    dictionary = load_dictionary(cfg.dictionary_dir)
    converter = ReadingConverter(dictionary)
    groups, coverage = collect_term_evidence(conn, dictionary, converter)
    quotes = collect_quotes(conn, converter)
    out = cfg.output_dir / "timestamp_alignment.md"
    write_alignment_report(out, groups, coverage, quotes)
    print(f"{len(groups)} evidence-backed candidates, {len(quotes)} quotes -> {out}")


def cmd_triage(args, cfg, conn):
    from azkims.align import collect_term_evidence
    from azkims.triage import classify

    dictionary = load_dictionary(cfg.dictionary_dir)
    converter = ReadingConverter(dictionary)
    # (表記, 正規形) -> [読み距離, 件数, タイトル一致, 出どころ]
    pairs: dict[tuple[str, str], list] = {}
    # --aggressive では保留にしたものも改めて採用対象にする
    held = set() if args.aggressive else dictionary.held
    for g in load_candidate_groups(conn, max_examples=0, held=held):
        if (g.surface, g.canonical) in dictionary.rejected:
            continue  # build の後に reject したもの（DB の候補にはまだ残っている）
        pairs[(g.surface, g.canonical)] = [g.distance, g.count, g.in_title, "字幕"]
    evidence, _ = collect_term_evidence(conn, dictionary, converter)
    for g in evidence:
        key = (g.surface, g.canonical)
        if key in dictionary.rejected or (key in dictionary.held and not args.aggressive):
            continue
        if key in pairs:
            pairs[key][3] = "字幕+TS"
        else:
            pairs[key] = [g.distance, g.count, 0, "TS"]

    decisions = [
        (
            classify(s, c, d, n, t, converter, ts_backed="TS" in src, aggressive=args.aggressive),
            d,
            n,
            src,
        )
        for (s, c), (d, n, t, src) in sorted(pairs.items(), key=lambda kv: (kv[1][0], -kv[1][1]))
    ]
    labels = {"accept": "採用", "hold": "保留（候補として残す）", "reject": "却下"}
    lines = ["# 補正候補の自動仕分け", ""]
    for action, label in labels.items():
        rows = [x for x in decisions if x[0].action == action]
        lines += [f"## {label}（{len(rows)} 件）", ""]
        for dec, d, n, src in rows:
            scope = "（タイトル限定）" if action == "accept" and dec.scope == "title" else ""
            lines.append(
                f"- {dec.surface} → {dec.canonical}{scope}  距離{d} / {n}件 / {src} … {dec.reason}"
            )
        lines.append("")
    out = cfg.output_dir / "triage.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    counts = {a: sum(1 for x in decisions if x[0].action == a) for a in labels}
    print(f"accept {counts['accept']} / hold {counts['hold']} / reject {counts['reject']} -> {out}")
    if args.apply:
        kinds = {"accept": "alias", "hold": "hold", "reject": "reject"}
        for dec, *_ in decisions:
            append_review_decision(
                cfg.dictionary_dir,
                kinds[dec.action],
                dec.surface,
                dec.canonical,
                dec.scope,
                source="aggressive" if args.aggressive else "",
            )
        load_dictionary(cfg.dictionary_dir)  # 追記後も辞書として正しいか検証
        print("accepted.toml に書き込みました。反映するには `azkims build` を実行してください")


def cmd_oshi(args, cfg, conn):
    from azkims.config import PROJECT_ROOT
    from azkims.oshi import collect_oshi_candidates, load_oshi_marks
    from azkims.triage import classify

    dictionary = load_dictionary(cfg.dictionary_dir)
    converter = ReadingConverter(dictionary)
    marks = load_oshi_marks(PROJECT_ROOT / "config" / "oshi_marks.toml")
    cands = [
        c
        for c in collect_oshi_candidates(conn, dictionary, converter, marks)
        if (c.surface, c.canonical) not in dictionary.rejected
        and (c.surface, c.canonical) not in dictionary.held
    ]
    # 実験的に原則すべて採用。ただし日常語 1 語は却下、動詞の活用形で終わるものは保留（従来の方針）
    decisions = []
    for c in cands:
        d = classify(c.surface, c.canonical, c.distance, c.count, 0, converter, ts_backed=True)
        action = (
            d.action if d.action in ("reject",) or d.reason == "動詞の活用形を含む" else "accept"
        )
        decisions.append((c, action, d.reason if action != "accept" else "推しマークの裏付け"))

    lines = [
        "# 推しマークで見つけたホロメン名の補正候補",
        "",
        "タイムスタンプに推しマーク（または名前）がある時刻の付近（-20〜+90 秒）の字幕から、",
        "そのホロメンの名前・愛称と読みが近い表記を探した結果です。",
        "",
    ]
    member = None
    for c, action, reason in decisions:
        if c.member != member:
            member = c.member
            lines += [f"## {member}", ""]
        mark = {"accept": "採用", "reject": "却下", "hold": "保留"}[action]
        near = (
            f" ／ 紛らわしい相手: {'・'.join(c.competitors)}（近くで話題のときだけ補正）"
            if c.competitors
            else ""
        )
        lines.append(
            f"- [{mark}] {c.surface} → {c.canonical}（{c.count} 件 / 読み距離 {c.distance}） … {reason}{near}"
        )
        for ex in c.examples[:2]:
            lines.append(f"  - TS「{ex.ts_text}」 / 字幕「{ex.caption}」")
    out = cfg.output_dir / "oshi_candidates.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    counts = {a: sum(1 for _, x, _ in decisions if x == a) for a in ("accept", "hold", "reject")}
    print(
        f"{len(decisions)} candidates: accept {counts['accept']} / hold {counts['hold']} / "
        f"reject {counts['reject']} -> {out}"
    )
    if args.apply:
        kinds = {"accept": "alias", "hold": "hold", "reject": "reject"}
        for c, action, _ in decisions:
            # 推しマーク由来の補正は「その人が話題の配信」（タイトルかタイムスタンプに出る）だけで効かせる。
            # 別人の愛称にも近い表記（まりちゃん: まつりちゃん / マリンちゃん）は、さらに
            # 「発言の近くでその人が話題、かつ紛らわしい相手は話題でない」ときだけ（scope = near）
            append_review_decision(
                cfg.dictionary_dir,
                kinds[action],
                c.surface,
                c.canonical,
                scope="near" if c.competitors else "title",
                competitors=c.competitors,
                source="oshi",
            )
        load_dictionary(cfg.dictionary_dir)  # 追記後も辞書として正しいか検証
        print("accepted.toml に書き込みました。反映するには `azkims build` を実行してください")


def cmd_discover(args, cfg, conn):
    converter = ReadingConverter(load_dictionary(cfg.dictionary_dir))
    words = collect_unknown(_corpus_rows(conn, args.streams), converter, min_count=args.min_count)
    out = cfg.output_dir / "unknown_terms.md"
    write_unknown_report(out, words)
    print(f"{len(words)} unknown words -> {out}")


def _decide(kind: str):
    def run(args, cfg, conn):
        dictionary = load_dictionary(cfg.dictionary_dir)
        if args.canonical not in dictionary.by_canonical:
            print(f"error: 辞書に無い canonical です: {args.canonical}", file=sys.stderr)
            return 2
        scope = getattr(args, "scope", "global")
        path = append_review_decision(cfg.dictionary_dir, kind, args.surface, args.canonical, scope)
        load_dictionary(cfg.dictionary_dir)  # 追記後も辞書として正しいか検証
        suffix = "（タイトル限定）" if scope == "title" else ""
        print(f"{kind}: {args.surface} -> {args.canonical}{suffix} ({path.name})")
        print("反映するには `azkims build` を実行してください")
        return 0

    return run


def cmd_drop_source(args, cfg, conn):
    from azkims.dictionary import drop_decisions_by_source

    n = drop_decisions_by_source(cfg.dictionary_dir, args.source)
    print(
        f"accepted.toml から source = {args.source!r} の {n} 件を消しました（反映は azkims build）"
    )


def cmd_stats(args, cfg, conn):
    q = lambda sql: conn.execute(sql).fetchone()[0]
    print(f"streams listed     : {q('SELECT COUNT(*) FROM streams')}")
    for r in conn.execute(
        "SELECT COALESCE(excluded_reason, '(included)') AS r, COUNT(*) AS n FROM streams "
        "GROUP BY 1 ORDER BY 2 DESC"
    ):
        print(f"  {r['r']:<18}: {r['n']}")
    for r in conn.execute(
        "SELECT COALESCE(caption_status, '(pending)') AS c, COUNT(*) AS n FROM streams "
        "WHERE excluded_reason IS NULL GROUP BY 1 ORDER BY 2 DESC"
    ):
        print(f"  captions {r['c']:<9}: {r['n']}")
    print(f"utterances         : {q('SELECT COUNT(*) FROM utterances')}")
    print(f"corrections        : {q('SELECT COUNT(*) FROM corrections')}")
    lo, hi = conn.execute(
        "SELECT MIN(upload_date), MAX(upload_date) FROM streams WHERE caption_status = 'ok'"
    ).fetchone()
    print(f"date range         : {lo} .. {hi}")
    print(f"built at           : {db.get_meta(conn, 'built_at')}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="azkims", description="AZKi Moment Search pipeline")
    p.add_argument("--config", type=Path, help="config.toml のパス")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("sync", help="チャンネルの配信一覧を取得し、除外判定する")
    s.add_argument("--limit", type=int, help="新しい順に N 件だけ取得")
    s.set_defaults(func=cmd_sync)

    s = sub.add_parser("fetch", help="未取得の配信の自動字幕を取得する")
    s.add_argument("--limit", type=int)
    s.add_argument("--retry-errors", action="store_true")
    s.add_argument("--ids", nargs="+", help="この配信だけ取得する（一覧にある未取得の配信）")
    s.set_defaults(func=cmd_fetch)

    s = sub.add_parser(
        "fetch-comments", help="字幕取得済みの配信のタイムスタンプコメントを取得する"
    )
    s.add_argument("--limit", type=int)
    s.add_argument("--retry-errors", action="store_true")
    s.set_defaults(func=cmd_fetch_comments)

    s = sub.add_parser("build", help="字幕キャッシュ + 辞書から検索 DB を作り直す")
    s.set_defaults(func=cmd_build)

    s = sub.add_parser("search", help="発言を検索する")
    s.add_argument("query", nargs="+")
    s.add_argument("--title")
    s.add_argument("--from", dest="date_from", help="YYYY-MM-DD")
    s.add_argument("--to", dest="date_to", help="YYYY-MM-DD")
    s.add_argument("--sort", choices=["new", "old", "relevance"], default="new")
    s.add_argument(
        "--tab",
        choices=["both", "match", "maybe", "all", "ts"],
        default="both",
        help="match=字幕の一致 / maybe=補正候補・部分的な読み一致 / ts=タイムスタンプコメント"
        "（既定: すべて分けて表示）",
    )
    s.add_argument("--limit", type=int, default=20)
    s.add_argument("--offset", type=int, default=0)
    s.add_argument("--surface-only", action="store_true", help="読みでの一致を使わない")
    s.set_defaults(func=cmd_search)

    s = sub.add_parser("review", help="build 時に検出した補正候補を集計する -> output/")
    s.set_defaults(func=cmd_review)

    s = sub.add_parser("align", help="タイムスタンプと字幕を照合し、裏付けのある補正候補を出す")
    s.set_defaults(func=cmd_align)

    s = sub.add_parser("triage", help="補正候補をルールで 採用 / 保留 / 却下 に仕分ける -> output/")
    s.add_argument("--apply", action="store_true", help="仕分け結果を accepted.toml に書き込む")
    s.add_argument(
        "--aggressive",
        action="store_true",
        help="強め補正: 日常語 1 語と動詞の活用形以外は保留分も含めて採用（source = aggressive）",
    )
    s.set_defaults(func=cmd_triage)

    s = sub.add_parser("oshi", help="推しマークの時刻付近からホロメン名の補正候補を探す -> output/")
    s.add_argument("--apply", action="store_true", help="候補を accepted.toml に書き込む")
    s.set_defaults(func=cmd_oshi)

    s = sub.add_parser("discover", help="辞書に無い未知語を集計する -> output/")
    s.add_argument("--streams", type=int)
    s.add_argument("--min-count", type=int, default=3)
    s.set_defaults(func=cmd_discover)

    for name, kind, help_ in (
        ("accept", "alias", "補正候補を採用（エイリアスとして登録）"),
        ("reject", "reject", "補正候補を却下（候補からも外す）"),
        ("hold", "hold", "補正候補を保留（補正はせず候補として残す。レビューには出さない）"),
    ):
        s = sub.add_parser(name, help=help_)
        s.add_argument("surface")
        s.add_argument("canonical")
        if kind == "alias":
            s.add_argument(
                "--scope",
                choices=["global", "title"],
                default="global",
                help="title = 配信タイトルにその語が出ている配信でだけ置換する",
            )
        s.set_defaults(func=_decide(kind))

    s = sub.add_parser("serve", help="Web UI 用の検索 API を起動する（127.0.0.1 のみ）")
    s.add_argument("--port", type=int, default=8765)
    s.set_defaults(func=cmd_serve)

    s = sub.add_parser("bundle-vercel", help="Vercel に載せる一式を deploy/ に組み立てる")
    s.add_argument("--turso", action="store_true", help="DB を含めない（Turso を読む）")
    s.add_argument(
        "--turso-export", action="store_true", help="Turso にアップロードする DB を data/ に作る"
    )
    s.set_defaults(func=cmd_bundle_vercel)

    s = sub.add_parser(
        "publish", help="公開用 DB を Turso に入れ替え、プレビューと本番（β版）を切り替える"
    )
    s.add_argument("--force", action="store_true", help="中身が同じでも入れ替える")
    s.set_defaults(func=cmd_publish)

    s = sub.add_parser("smoke", help="デプロイ先の API が正しく動くか確かめる")
    s.add_argument("url")
    s.set_defaults(func=cmd_smoke)

    s = sub.add_parser(
        "drop-source",
        help="accepted.toml から指定 source（aggressive / oshi 等）の項目をまとめて消す",
    )
    s.add_argument("source")
    s.set_defaults(func=cmd_drop_source)

    s = sub.add_parser("stats", help="DB の状況")
    s.set_defaults(func=cmd_stats)
    return p


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    cfg = load_config(args.config)
    conn = db.connect(cfg.db_path)
    try:
        return args.func(args, cfg, conn) or 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
