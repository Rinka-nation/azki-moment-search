"""Pipeline stages: sync (list) -> fetch (captions) -> build (utterances + corrections + index).

Raw captions are cached under data/captions and never modified, so `build` can be re-run at
any time after the dictionary changes and always produces the same result.
"""

from __future__ import annotations

import json
import random
import re
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta

from azkims import db, rules, youtube
from azkims.captions import parse_captions
from azkims.config import PROJECT_ROOT, Config
from azkims.dictionary import Dictionary, Term, load_dictionary
from azkims.filters import exclusion_reason
from azkims.llmfix import apply_fixes, load_fixes, member_names, official_names
from azkims.oshi import load_oshi_marks, near_context, stream_context, topics_by_time
from azkims.reading import ReadingConverter, Token, word_readings
from azkims.readout import strip_name_runs
from azkims.related import build_index, load_related, member_list
from azkims.review import FuzzyHit, FuzzyMatcher, apply_candidates, screen
from azkims.rules import Rule
from azkims.search import INCLUDED_ONLY, UTTERANCE_ORDER
from azkims.textnorm import normalize_text
from azkims.timestamps import parse_comments

Log = Callable[[str], None]


def _pause(cfg: Config) -> None:
    """YouTube への間隔。一定間隔だと機械的に見えるので揺らぎを足す。"""
    time.sleep(cfg.sleep_seconds + random.uniform(0, cfg.sleep_jitter))


def newest_date(cfg: Config, today: date | None = None) -> str:
    """取り込んでよい配信日の上限（YYYY-MM-DD）。これより新しい配信は次回に回す。"""
    return ((today or date.today()) - timedelta(days=cfg.min_age_days)).isoformat()


# 書きかけのタイムスタンプの印。付いていれば完成版ではないので、次回も取り直す
EDITING_MARK = re.compile(r"[（(]編集中[）)]")

# 連続でこの件数エラーになったら fetch を止める（回線断・レート制限で全件 error にしないため）
MAX_CONSECUTIVE_ERRORS = 5


def reclassify(conn: sqlite3.Connection, cfg: Config) -> None:
    rows = conn.execute("SELECT video_id, title, availability, live_status FROM streams").fetchall()
    conn.executemany(
        "UPDATE streams SET excluded_reason = ? WHERE video_id = ?",
        [
            (
                exclusion_reason(
                    r["video_id"], r["title"], r["availability"], r["live_status"], cfg
                ),
                r["video_id"],
            )
            for r in rows
        ],
    )
    # 外す配信が変わったかもしれないので、次の build までは配信との結合で外す
    conn.execute("DELETE FROM meta WHERE key = ?", (INCLUDED_ONLY,))


def sync(conn: sqlite3.Connection, cfg: Config, limit: int | None, log: Log) -> None:
    rows = youtube.list_channel(cfg.channel_url, limit=limit)
    with db.transaction(conn):
        db.upsert_listing(conn, rows)
        reclassify(conn, cfg)
    log(f"listed {len(rows)} streams")


def fetch(
    conn: sqlite3.Connection,
    cfg: Config,
    log: Log,
    limit: int | None = None,
    retry_errors: bool = False,
    ids: list[str] | None = None,
    today: date | None = None,
) -> None:
    """ids: 取得する配信を指定する（一覧に載っている未取得の配信のうち、これだけを先に取る）。

    配信日が分かっていて新しすぎる配信は取りに行かない。一覧には配信日が無いので、初めての配信は
    取ってから配信日を見て、新しすぎれば未取得に戻す（次回に取り直す）。
    """
    newest = newest_date(cfg, today)
    status_cond = "caption_status IS NULL"
    if retry_errors:
        status_cond = "(caption_status IS NULL OR caption_status = 'error')"
    id_cond, params = "", [newest]
    if ids:
        id_cond = f" AND video_id IN ({','.join('?' * len(ids))})"
        params += list(ids)
    pending = conn.execute(
        f"SELECT video_id, title FROM streams WHERE excluded_reason IS NULL AND {status_cond}"
        f" AND (upload_date IS NULL OR upload_date <= ?){id_cond} ORDER BY list_order LIMIT ?",
        (*params, limit or cfg.max_videos_per_run),
    ).fetchall()
    consecutive_errors = 0
    for n, row in enumerate(pending):
        vid = row["video_id"]
        if n:
            _pause(cfg)
        status, error, meta = "ok", None, None
        try:
            meta, _ = youtube.fetch_video(vid, cfg.captions_dir, cfg.caption_lang)
        except youtube.CaptionsUnavailable as e:
            status, error, meta = "no_captions", str(e.args[0]), e.args[1]
        except Exception as e:  # noqa: BLE001  1 配信の失敗で全体を止めない
            status, error = "error", f"{type(e).__name__}: {e}"
        consecutive_errors = consecutive_errors + 1 if status == "error" else 0
        if meta and meta.upload_date and meta.upload_date > newest:
            status, error = None, None  # 新しすぎる。未取得のまま残し、次回に取り直す
        with db.transaction(conn):
            if meta:
                conn.execute(
                    "UPDATE streams SET title = ?, upload_date = ?, duration_sec = ?, "
                    "availability = COALESCE(?, availability), "
                    "live_status = COALESCE(?, live_status) WHERE video_id = ?",
                    (
                        meta.title,
                        meta.upload_date,
                        meta.duration_sec,
                        meta.availability,
                        meta.live_status,
                        vid,
                    ),
                )
            conn.execute(
                "UPDATE streams SET caption_status = ?, caption_error = ?, fetched_at = ? "
                "WHERE video_id = ?",
                (status, error, db.now_iso(), vid),
            )
            reclassify(conn, cfg)
        shown = status or f"まだ新しい（{meta.upload_date}）ので次回"
        log(f"[{n + 1}/{len(pending)}] {vid} {shown} {row['title'][:50]}")
        if error and youtube.is_blocked(error):
            log(f"YouTube にブロックされた可能性があるため中断します: {error[:200]}")
            return
        if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
            log(
                f"{consecutive_errors} 件連続でエラーになったため中断します（--retry-errors で再開）"
            )
            return


def fetch_comments(
    conn: sqlite3.Connection,
    cfg: Config,
    log: Log,
    limit: int | None = None,
    retry_errors: bool = False,
    today: date | None = None,
) -> None:
    """字幕を取得済みの配信について、指定投稿者のタイムスタンプコメントを取得する。

    新しすぎる配信は完成版がまだ無いので取らない。（編集中）のものは毎回取り直す。
    """
    if not cfg.ts_author_ids:
        log("config の [timestamps] author_ids が空です")
        return
    status_cond = "(c.video_id IS NULL OR c.status = 'editing')"
    if retry_errors:
        status_cond = "(c.video_id IS NULL OR c.status IN ('editing', 'error'))"
    params: list = [newest_date(cfg, today)]
    since_cond = ""
    if cfg.ts_since:
        since_cond = "AND s.upload_date >= ?"
        params.append(cfg.ts_since)
    pending = conn.execute(
        "SELECT s.video_id, s.title FROM streams s "
        "LEFT JOIN comment_fetch c ON c.video_id = s.video_id "
        f"WHERE s.caption_status = 'ok' AND s.excluded_reason IS NULL AND {status_cond} "
        f"AND s.upload_date <= ? {since_cond} ORDER BY s.list_order LIMIT ?",
        [*params, limit or cfg.max_videos_per_run],
    ).fetchall()
    cfg.comments_dir.mkdir(parents=True, exist_ok=True)
    consecutive_errors = 0
    for n, row in enumerate(pending):
        vid = row["video_id"]
        if n:
            _pause(cfg)
        status, error, comments = "ok", None, []
        try:
            comments = youtube.fetch_comments(vid, cfg.ts_author_ids, cfg.ts_max_parents)
            if comments:
                (cfg.comments_dir / f"{vid}.json").write_text(
                    json.dumps(comments, ensure_ascii=False, indent=1), encoding="utf-8"
                )
                if any(EDITING_MARK.search(c["text"]) for c in comments):
                    status = "editing"  # 書きかけでも今の版は使う
            else:
                status = "none"
        except Exception as e:  # noqa: BLE001  1 配信の失敗で全体を止めない
            status, error = "error", f"{type(e).__name__}: {e}"
        consecutive_errors = consecutive_errors + 1 if status == "error" else 0
        with db.transaction(conn):
            conn.execute(
                "INSERT INTO comment_fetch (video_id, status, comment_count, error, fetched_at) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(video_id) DO UPDATE SET "
                "status = excluded.status, comment_count = excluded.comment_count, "
                "error = excluded.error, fetched_at = excluded.fetched_at",
                (vid, status, len(comments), error, db.now_iso()),
            )
        log(f"[{n + 1}/{len(pending)}] {vid} {status} ({len(comments)}) {row['title'][:40]}")
        if error and youtube.is_blocked(error):
            log(f"YouTube にブロックされた可能性があるため中断します: {error[:200]}")
            return
        if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
            log(
                f"{consecutive_errors} 件連続でエラーになったため中断します（--retry-errors で再開）"
            )
            return


@dataclass(frozen=True)
class Analyzed:
    text: str  # 確定補正後
    corrections: list[tuple[str, str]]
    reading: str
    words: list[str]  # 語単位の読み（word_readings）
    text_fuzzy: str | None  # 候補補正後（候補が無ければ None）
    fuzzy: list[tuple[str, str, int, bool]]  # (surface, canonical, distance, in_title)


# かな表記ゆれの自動補正（fix_kana_variants）の対象にする語の読みの最短長
KANA_VARIANT_MIN_READING = 4
# ひらがな・カタカナだけ（語の途中の「、」「。」は自動字幕の区切り誤りとして許す: カ。ナたん）
_KANA_ONLY = re.compile(r"^[ぁ-ゖァ-ヺー](?:[ぁ-ゖァ-ヺー、。]*[ぁ-ゖァ-ヺー])?$")


def _kana_variant_targets(dictionary: Dictionary) -> dict[str, Term]:
    """読み → 語。読みが他の語と重ならない、読み KANA_VARIANT_MIN_READING 文字以上の語だけ。"""
    cached = getattr(dictionary, "_kana_variant_targets", None)
    if cached is not None:
        return cached
    by_reading: dict[str, list[Term]] = {}
    for t in dictionary.terms:
        by_reading.setdefault(t.folded, []).append(t)
    targets = {
        r: ts[0]
        for r, ts in by_reading.items()
        if len(ts) == 1 and len(r) >= KANA_VARIANT_MIN_READING and ts[0].kana_fix
    }
    dictionary._kana_variant_targets = targets  # type: ignore[attr-defined]
    return targets


def fix_kana_variants(
    text: str, tokens: list[Token], dictionary: Dictionary, context: frozenset[str] = frozenset()
) -> tuple[str, list[tuple[str, str]]]:
    """ひらがな・カタカナだけの表記で、読みが辞書語とぴったり同じものを辞書語に直す。

    かナたん・カナタン・ラデンちゃん（= かなたん・らでんちゃん）のような文字種のゆれは、読みが同じなので
    誤補正の心配が小さい。人が登録した alias が無くても自動で直す（確定補正として扱う）。
    除外: 読みが他の語と同じ語（あめちゃん = あやめちゃん? アメちゃん?）、読みの短い語、
    Sudachi の辞書にある普通の 1 語。
    """
    targets = _kana_variant_targets(dictionary)
    out: list[str] = []
    applied: list[tuple[str, str]] = []
    pos = 0
    i = 0
    while i < len(tokens):
        hit = None
        if not tokens[i].term:
            reading = ""
            for j in range(i, min(len(tokens), i + 4)):
                if tokens[j].term:
                    break
                reading += tokens[j].reading
                surface = text[tokens[i].start : tokens[j].end]
                term = targets.get(reading)
                if term is None or not _KANA_ONLY.match(surface) or surface == term.canonical:
                    continue
                if not dictionary.game_active(term, context):
                    continue
                if i == j and not tokens[i].oov and tokens[i].pos != "名詞":
                    continue  # 普通の 1 語（動詞・形容詞など）
                hit = (j, surface, term)
        if hit is None:
            i += 1
            continue
        j, surface, term = hit
        out += [text[pos : tokens[i].start], term.canonical]
        applied.append((surface, term.canonical))
        pos = tokens[j].end
        i = j + 1
    if not applied:
        return text, []
    out.append(text[pos:])
    return "".join(out), applied


def analyze(
    raw: str,
    title: str,
    dictionary: Dictionary,
    converter: ReadingConverter,
    matcher: FuzzyMatcher,
    extra_context: frozenset[str] = frozenset(),
    near: frozenset[str] = frozenset(),
    topical: frozenset[Rule] = frozenset(),
) -> Analyzed:
    """1 発言分: 確定補正（alias）→ 読み → 補正候補（あいまい一致、本文は書き換えない）。

    extra_context: タイトル以外から分かったその配信の話題（タイムスタンプの推しマーク等）。
    topical: 文脈つき補正ルールのうち、その配信が話題だと分かったもの。
    """
    context = dictionary.title_context(title) | extra_context
    text, applied = dictionary.apply_aliases(raw, context, near)
    text, fixed = rules.apply_fixes(text, rules.find(text, dictionary.rules, topical))
    applied = applied + fixed
    tokens = converter.tokens(text, context=context, near=near)
    text, variants = fix_kana_variants(text, tokens, dictionary, context)
    if variants:
        applied = applied + variants
        tokens = converter.tokens(text, context=context, near=near)
    fine_tokens = converter.tokens(text, fine=True, context=context, near=near)
    candidates = screen(matcher.find(text, tokens), context)
    for m in rules.find(text, dictionary.rules, topical):
        # 決め手のないルールの表記は「もしかして」にだけ出す
        if m.decision == rules.MAYBE and all(
            m.end <= h.start or m.start >= h.end for h, _ in candidates
        ):
            term = dictionary.by_canonical[m.rule.canonical]
            candidates.append((FuzzyHit(m.surface, term, 1, m.start, m.end), False))
    candidates.sort(key=lambda c: c[0].start)
    return Analyzed(
        text=text,
        corrections=applied,
        reading="".join(t.reading for t in tokens),
        # 長単位と短単位の両方の境界で「語」を登録する（鉄道博物館 も 鉄道 も語として一致させる）
        words=list(dict.fromkeys(word_readings(tokens) + word_readings(fine_tokens))),
        text_fuzzy=apply_candidates(text, [h for h, _ in candidates]) if candidates else None,
        fuzzy=[(h.surface, h.term.canonical, h.distance, t) for h, t in candidates],
    )


def analyze_kept(
    raw: str,
    title: str,
    dictionary: Dictionary,
    converter: ReadingConverter,
    matcher: FuzzyMatcher,
    extra_context: frozenset[str] = frozenset(),
    near: frozenset[str] = frozenset(),
    topical: frozenset[Rule] = frozenset(),
) -> tuple[str | None, str | None]:
    """スパチャ読みの名前の連続を除いた部分の (text_kept, reading_kept)。連続が無ければ (None, None)。

    除いた残りも本文と同じ手順で補正し、原文・補正後・候補補正後のどれで探しても残りに一致させる。
    """
    kept = strip_name_runs(raw, lambda clause: bool(dictionary.find(clause, include_games=True)))
    if kept == raw:
        return None, None
    if not kept:
        return "", ""
    a = analyze(kept, title, dictionary, converter, matcher, extra_context, near, topical)
    forms = (kept, a.text, a.text_fuzzy)
    return "\n".join(dict.fromkeys(f for f in forms if f)), a.reading


def build(conn: sqlite3.Connection, cfg: Config, log: Log) -> None:
    dictionary = load_dictionary(cfg.dictionary_dir)
    converter = ReadingConverter(dictionary)
    matcher = FuzzyMatcher(dictionary)
    marks_path = PROJECT_ROOT / "config" / "oshi_marks.toml"
    marks = load_oshi_marks(marks_path) if marks_path.exists() else {}
    ai_fixes, ai_stats = load_fixes(
        cfg.data_dir / "llm", official_names(dictionary.terms), member_names(dictionary.terms)
    )
    # 発言の番号を検索の「新しい順」と同じ並びにする（番号順に取れば一致を全部読まずに済む。search.UTTERANCE_ORDER）
    streams = conn.execute(
        "SELECT video_id, title FROM streams WHERE caption_status = 'ok' AND excluded_reason IS NULL "
        "ORDER BY upload_date DESC, video_id"
    ).fetchall()
    total_utt = total_fix = total_cand = total_readout = total_ai = 0
    with db.transaction(conn):
        db.reset_utterances(conn)
        for row in streams:
            vid = row["video_id"]
            path = youtube.caption_path(cfg.captions_dir, vid, cfg.caption_lang)
            if not path.exists():
                log(f"missing caption cache: {vid}")
                continue
            oshi_ctx, ts_topics = _stream_oshi_context(cfg, vid, dictionary, marks)
            utts = [
                (utt, raw)
                for utt in parse_captions(path, cfg.segment_max_chars, cfg.segment_gap_ms)
                if len(raw := normalize_text(utt.text)) >= 2
            ]
            utts.sort(key=lambda x: x[0].start_ms)
            topical = stream_topical(
                [raw for _, raw in utts],
                row["title"],
                dictionary,
                oshi_ctx,
                ts_topics,
                [u.start_ms for u, _ in utts],
            )
            for utt, raw in utts:
                near = near_context(ts_topics, utt.start_ms // 1000)
                a = analyze(
                    raw, row["title"], dictionary, converter, matcher, oshi_ctx, near, topical
                )
                text_kept, reading_kept = analyze_kept(
                    raw, row["title"], dictionary, converter, matcher, oshi_ctx, near, topical
                )
                text_ai, reading, words = None, a.reading, a.words
                fixes = ai_fixes.get((vid, utt.start_ms))
                if fixes and text_kept is None:
                    text_ai = apply_fixes(a.text, fixes)
                if text_ai is not None:
                    # 直した文の読みも索引に入れ、読みでも探せるようにする
                    ctx = dictionary.title_context(row["title"]) | oshi_ctx
                    tokens = converter.tokens(text_ai, context=ctx, near=near)
                    reading = a.reading + "\n" + "".join(t.reading for t in tokens)
                    words = list(dict.fromkeys(words + word_readings(tokens)))
                    total_ai += 1
                db.insert_utterance(
                    conn,
                    vid,
                    utt.start_ms,
                    utt.end_ms,
                    raw,
                    a.text,
                    reading,
                    a.corrections,
                    text_fuzzy=a.text_fuzzy,
                    fuzzy=a.fuzzy,
                    words=words,
                    text_kept=text_kept,
                    reading_kept=reading_kept,
                    text_ai=text_ai,
                )
                total_utt += 1
                total_fix += len(a.corrections)
                total_cand += len(a.fuzzy)
                total_readout += text_kept is not None
        total_ts = _build_timestamps(conn, cfg, dictionary, converter)
        related = load_related(cfg.dictionary_dir, dictionary)
        rel_utt, rel_ts = build_index(conn, related, marks)
        # 画面の「ホロメンから探す」の一覧（/api/members）。推しマークは表示用に元の表記（異体字セレクタ付き）
        display_marks = (
            load_oshi_marks(marks_path, keep_variation=True) if marks_path.exists() else {}
        )
        db.set_meta(
            conn,
            "members",
            json.dumps(member_list(conn, related, display_marks), ensure_ascii=False),
        )
        db.set_meta(conn, "built_at", db.now_iso())
        db.set_meta(conn, "utterance_order", UTTERANCE_ORDER)
        # 外した配信の発言・タイムスタンプは作らない（件数を数えるとき配信との結合を省ける。search.INCLUDED_ONLY）
        db.set_meta(conn, INCLUDED_ONLY, "1")
        db.set_meta(conn, "dictionary_terms", str(len(dictionary.terms)))
    log(
        f"built {total_utt} utterances from {len(streams)} streams "
        f"({total_fix} corrections, {total_cand} candidates, {total_readout} readouts), "
        f"{total_ts} timestamp entries, "
        f"{rel_utt} + {rel_ts} related hits"
    )
    if ai_stats.rows:
        dropped = ", ".join(f"{k} {v}" for k, v in ai_stats.dropped.items()) or "なし"
        log(
            f"AI修正: 修正案 {ai_stats.rows} 件のうち {ai_stats.used} 件を発言に対応"
            f"（番号のずれを直したもの {ai_stats.moved}、名前を公式の表記にしたもの {ai_stats.renamed}、"
            f"横展開 {ai_stats.propagated}、"
            f"捨てたもの: {dropped}）、"
            f"{total_ai} 発言に適用"
        )


def stream_topical(
    raws: list[str],
    title: str,
    dictionary: Dictionary,
    oshi_ctx: frozenset[str],
    ts_topics,
    starts_ms: list[int] | None = None,
) -> frozenset[Rule]:
    """文脈つき補正ルールのうち、この配信が話題のもの。rules.py の 3。

    タイトル・タイムスタンプの文脈に topic の人・語があるか、確定補正（alias）後の字幕に
    topic の人・語が caption_topic_min 回以上出てくるか。
    """
    if not dictionary.rules:
        return frozenset()
    context = dictionary.title_context(title) | oshi_ctx
    topical: set[Rule] = set()
    pending = []
    for rule in dictionary.rules:
        words = dictionary.topic_terms(rule)
        if not words:
            continue  # topic の無いルール（組み合わせだけで補正する）
        if context & words:
            topical.add(rule)
        else:
            pending.append((rule, words))
    if not pending:
        return frozenset(topical)
    counts = dict.fromkeys([r for r, _ in pending], 0)
    for i, raw in enumerate(raws):
        near = near_context(ts_topics, starts_ms[i] // 1000) if starts_ms else frozenset()
        text, _ = dictionary.apply_aliases(raw, context, near)
        found = [m.term.canonical for m in dictionary.find(text, context, near)]
        for rule, words in pending:
            counts[rule] += sum(1 for c in found if c in words)
    for rule, _ in pending:
        if counts[rule] >= rule.caption_topic_min:
            topical.add(rule)
    return frozenset(topical)


def _stream_oshi_context(cfg: Config, video_id: str, dictionary, marks):
    """その配信のタイムスタンプ（推しマーク・名前）から分かる話題。

    戻り値: (配信全体の話題の語, 時刻ごとの話題 [(秒, 語の集合)])
    """
    path = cfg.comments_dir / f"{video_id}.json"
    if not marks or not path.exists():
        return frozenset(), []
    entries = parse_comments(json.loads(path.read_text(encoding="utf-8")))
    return (
        stream_context([e.text for e in entries], marks, dictionary),
        topics_by_time([(e.sec, e.text) for e in entries], marks, dictionary),
    )


def _build_timestamps(conn, cfg: Config, dictionary, converter: ReadingConverter) -> int:
    total = 0
    rows = conn.execute(
        "SELECT c.video_id FROM comment_fetch c JOIN streams s ON s.video_id = c.video_id "
        "WHERE c.status IN ('ok', 'editing') AND s.excluded_reason IS NULL"  # 書きかけ（editing）も今の版を使う
    ).fetchall()
    for row in rows:
        path = cfg.comments_dir / f"{row['video_id']}.json"
        if not path.exists():
            continue
        comments = json.loads(path.read_text(encoding="utf-8"))
        for e in parse_comments(comments):
            tokens = converter.tokens(e.text)
            fine = converter.tokens(e.text, fine=True)
            db.insert_ts_entry(
                conn,
                row["video_id"],
                e.sec,
                e.text,
                list(e.quotes),
                e.is_note,
                "".join(t.reading for t in tokens),
                list(dict.fromkeys(word_readings(tokens) + word_readings(fine))),
            )
            total += 1
    return total
