"""SQLite storage. The schema is kept D1-compatible (Cloudflare D1 is SQLite + FTS5)."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS streams (
    video_id        TEXT PRIMARY KEY,
    title           TEXT NOT NULL,
    list_order      INTEGER,          -- チャンネル一覧での並び（0 = 最新）
    upload_date     TEXT,             -- YYYY-MM-DD
    duration_sec    INTEGER,
    availability    TEXT,             -- public / subscriber_only / ...
    live_status     TEXT,             -- was_live / is_upcoming / ...
    excluded_reason TEXT,             -- NULL なら検索対象
    caption_status  TEXT,             -- NULL(未取得) / ok / no_captions / error
    caption_error   TEXT,
    fetched_at      TEXT
);

-- 発言系テーブルは build のたびに作り直す（UTTERANCE_SCHEMA）
-- タイムスタンプコメントの取得状況（原本は data/comments/{video_id}.json）
CREATE TABLE IF NOT EXISTS comment_fetch (
    video_id      TEXT PRIMARY KEY REFERENCES streams(video_id),
    status        TEXT NOT NULL,      -- ok / editing（（編集中）付き。次回も取り直す）/ none（該当投稿者のコメントなし）/ error
    comment_count INTEGER,
    error         TEXT,
    fetched_at    TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

# 補正の 3 段階を全部残す:
#   text_raw   字幕原文（正規化のみ）
#   text       確定補正後（辞書 aliases = 人が確定させたもの）
#   text_fuzzy 候補補正後（読みのあいまい一致。未確定なので検索では「もしかして」扱い）
#   text_ai    LLM の修正を text に当てたもの（llmfix.py。表示に AI修正 の印を付ける）
UTTERANCE_SCHEMA = """
CREATE TABLE IF NOT EXISTS utterances (
    id             INTEGER PRIMARY KEY,
    video_id       TEXT NOT NULL REFERENCES streams(video_id),
    start_ms       INTEGER NOT NULL,
    end_ms         INTEGER NOT NULL,
    text_raw       TEXT NOT NULL,
    text           TEXT NOT NULL,
    text_fuzzy     TEXT,             -- 候補が無ければ NULL
    text_orig      TEXT,             -- 補正で text が原文と変わったときだけ原文（検索用）。同じなら NULL
    text_ai        TEXT,             -- LLM の修正を当てた text。修正が無ければ NULL
    fuzzy_distance INTEGER,          -- 候補の読み編集距離の最大値（NULL = 候補なし）
    reading        TEXT NOT NULL,    -- text の読み（fold_reading 済み）。text_ai があればその読みを改行でつなぐ
    -- スパチャ読みの名前の連続（readout.py）を除いた部分。連続が無ければ NULL。
    -- 原文・補正後・候補補正後を改行でつないだもの（検索語がこの外にしかなければ スパチャ読み タブ）
    text_kept      TEXT,
    reading_kept   TEXT
);
CREATE INDEX IF NOT EXISTS idx_utterances_video ON utterances(video_id, start_ms);

-- trigram: 3 文字以上の部分一致を高速化（2 文字は読みがあれば語単位の索引 reading_words_fts で引く。索引で引けない語は検索が断る）
CREATE VIRTUAL TABLE IF NOT EXISTS utterances_fts USING fts5(
    text, text_fuzzy, text_orig, text_ai, reading, content='utterances', content_rowid='id',
    tokenize='trigram'
);

-- 語単位の読み索引（reading.word_readings）。検索語の読みと「語の境界で」一致したかの判定用。
-- trigram ではなく空白区切りの完全一致にしたいので unicode61。中身は保持しない（contentless）
CREATE VIRTUAL TABLE IF NOT EXISTS reading_words_fts USING fts5(
    words, content='', tokenize='unicode61 remove_diacritics 0'
);

-- 関連語の一致（related.py）。confident = 0 は話題でない配信の weak の語（もしかして に出す）
CREATE TABLE IF NOT EXISTS related_hits (
    tag          TEXT NOT NULL,
    word         TEXT NOT NULL,
    utterance_id INTEGER NOT NULL REFERENCES utterances(id),
    confident    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_related_hits ON related_hits(tag, confident, word);

CREATE TABLE IF NOT EXISTS related_ts_hits (
    tag       TEXT NOT NULL,
    word      TEXT NOT NULL,
    entry_id  INTEGER NOT NULL REFERENCES ts_entries(id),
    confident INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_related_ts_hits ON related_ts_hits(tag, confident, word);

-- 確定補正（alias 置換）の記録
CREATE TABLE IF NOT EXISTS corrections (
    utterance_id INTEGER NOT NULL REFERENCES utterances(id),
    surface      TEXT NOT NULL,
    canonical    TEXT NOT NULL
);

-- 補正候補（あいまい一致）の記録。review レポートの元データでもある
CREATE TABLE IF NOT EXISTS fuzzy_matches (
    utterance_id INTEGER NOT NULL REFERENCES utterances(id),
    surface      TEXT NOT NULL,
    canonical    TEXT NOT NULL,
    distance     INTEGER NOT NULL,
    in_title     INTEGER NOT NULL    -- 配信タイトルにその語が出ていたか
);
CREATE INDEX IF NOT EXISTS idx_fuzzy_pair ON fuzzy_matches(surface, canonical);

-- タイムスタンプコメントの各行（人が書いた文章。自動字幕とは別の層）
CREATE TABLE IF NOT EXISTS ts_entries (
    id       INTEGER PRIMARY KEY,
    video_id TEXT NOT NULL REFERENCES streams(video_id),
    sec      INTEGER NOT NULL,
    text     TEXT NOT NULL,
    quotes   TEXT NOT NULL,          -- 「」内を改行区切り（発言そのままの可能性が高い）
    is_note  INTEGER NOT NULL,       -- 時刻なしの補足行
    reading  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ts_video ON ts_entries(video_id, sec);
CREATE VIRTUAL TABLE IF NOT EXISTS ts_fts USING fts5(
    text, reading, content='ts_entries', content_rowid='id', tokenize='trigram'
);
CREATE VIRTUAL TABLE IF NOT EXISTS ts_words_fts USING fts5(
    words, content='', tokenize='unicode61 remove_diacritics 0'
);
"""


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def connect(path: Path | str) -> sqlite3.Connection:
    if isinstance(path, Path):
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    conn.executescript(UTTERANCE_SCHEMA)
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection):
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def upsert_listing(conn: sqlite3.Connection, rows: list[dict]) -> None:
    conn.executemany(
        """
        INSERT INTO streams (video_id, title, list_order, availability, live_status)
        VALUES (:video_id, :title, :list_order, :availability, :live_status)
        ON CONFLICT(video_id) DO UPDATE SET
            title = excluded.title,
            list_order = excluded.list_order,
            availability = COALESCE(excluded.availability, streams.availability),
            live_status = COALESCE(excluded.live_status, streams.live_status)
        """,
        rows,
    )


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def reset_utterances(conn: sqlite3.Connection) -> None:
    """Drop and recreate all utterance-derived tables (schema changes need no migration)."""
    for name in (
        "related_hits",
        "related_ts_hits",
        "ts_words_fts",
        "ts_fts",
        "ts_entries",
        "fuzzy_matches",
        "corrections",
        "reading_words_fts",
        "utterances_fts",
        "utterances",
    ):
        conn.execute(f"DROP TABLE IF EXISTS {name}")
    conn.executescript(UTTERANCE_SCHEMA)


def insert_utterance(
    conn: sqlite3.Connection,
    video_id: str,
    start_ms: int,
    end_ms: int,
    text_raw: str,
    text: str,
    reading: str,
    corrections: list[tuple[str, str]],
    text_fuzzy: str | None = None,
    fuzzy: list[tuple[str, str, int, bool]] | None = None,
    words: list[str] | None = None,
    text_kept: str | None = None,
    reading_kept: str | None = None,
    text_ai: str | None = None,
) -> int:
    """fuzzy: [(surface, canonical, distance, in_title)]"""
    fuzzy = fuzzy or []
    distance = max((d for _, _, d, _ in fuzzy), default=None)
    # 補正で変わった発言だけ原文も検索対象にする（水きち で検索 → あずきち に補正された発言も出る）
    text_orig = text_raw if text_raw != text else None
    cur = conn.execute(
        "INSERT INTO utterances "
        "(video_id, start_ms, end_ms, text_raw, text, text_fuzzy, text_orig, text_ai, "
        "fuzzy_distance, reading, text_kept, reading_kept) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            video_id,
            start_ms,
            end_ms,
            text_raw,
            text,
            text_fuzzy,
            text_orig,
            text_ai,
            distance,
            reading,
            text_kept,
            reading_kept,
        ),
    )
    uid = cur.lastrowid
    conn.execute(
        "INSERT INTO utterances_fts (rowid, text, text_fuzzy, text_orig, text_ai, reading) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (uid, text, text_fuzzy, text_orig, text_ai, reading),
    )
    if words:
        conn.execute(
            "INSERT INTO reading_words_fts (rowid, words) VALUES (?, ?)", (uid, " ".join(words))
        )
    if corrections:
        conn.executemany(
            "INSERT INTO corrections (utterance_id, surface, canonical) VALUES (?, ?, ?)",
            [(uid, s, c) for s, c in corrections],
        )
    if fuzzy:
        conn.executemany(
            "INSERT INTO fuzzy_matches (utterance_id, surface, canonical, distance, in_title) "
            "VALUES (?, ?, ?, ?, ?)",
            [(uid, s, c, d, int(t)) for s, c, d, t in fuzzy],
        )
    return uid


def insert_ts_entry(
    conn: sqlite3.Connection,
    video_id: str,
    sec: int,
    text: str,
    quotes: list[str],
    is_note: bool,
    reading: str,
    words: list[str],
) -> int:
    cur = conn.execute(
        "INSERT INTO ts_entries (video_id, sec, text, quotes, is_note, reading) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (video_id, sec, text, "\n".join(quotes), int(is_note), reading),
    )
    eid = cur.lastrowid
    conn.execute("INSERT INTO ts_fts (rowid, text, reading) VALUES (?, ?, ?)", (eid, text, reading))
    if words:
        conn.execute(
            "INSERT INTO ts_words_fts (rowid, words) VALUES (?, ?)", (eid, " ".join(words))
        )
    return eid
