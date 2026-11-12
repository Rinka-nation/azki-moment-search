from datetime import date

import pytest

from azkims import db, pipeline, youtube
from azkims.config import Config
from azkims.youtube import VideoInfo

TODAY = date(2026, 11, 10)


@pytest.fixture()
def cfg(tmp_path):
    return Config(
        channel_url="",
        data_dir=tmp_path,
        output_dir=tmp_path,
        dictionary_dir=tmp_path,
        sleep_seconds=0,
        max_videos_per_run=10,
        min_age_days=3,
        ts_author_ids={"me"},
    )


@pytest.fixture()
def conn():
    c = db.connect(":memory:")
    for i, vid in enumerate(["old", "new"]):
        c.execute(
            "INSERT INTO streams (video_id, title, list_order) VALUES (?, ?, ?)", (vid, vid, i)
        )
    c.commit()
    return c


def test_fetch_leaves_too_recent_streams_for_next_time(cfg, conn, monkeypatch):
    dates = {"old": "2026-11-07", "new": "2026-11-08"}
    calls = []

    def fake_fetch(vid, captions_dir, lang):
        calls.append(vid)
        return VideoInfo(vid, vid, dates[vid], 60, "public", "was_live"), None

    monkeypatch.setattr(youtube, "fetch_video", fake_fetch)
    pipeline.fetch(conn, cfg, lambda _: None, today=TODAY)
    rows = dict(conn.execute("SELECT video_id, caption_status FROM streams").fetchall())
    assert rows == {"old": "ok", "new": None}
    # 配信日が分かったので、次は日数が過ぎるまで取りに行かない
    calls.clear()
    pipeline.fetch(conn, cfg, lambda _: None, today=TODAY)
    assert calls == []
    pipeline.fetch(conn, cfg, lambda _: None, today=date(2026, 11, 11))
    assert calls == ["new"]


def test_fetch_comments_refetches_editing_and_skips_recent(cfg, conn, monkeypatch):
    conn.execute("UPDATE streams SET caption_status = 'ok', upload_date = '2026-11-07'")
    conn.execute("UPDATE streams SET upload_date = '2026-11-08' WHERE video_id = 'new'")
    texts = {"old": "【タイムスタンプ】（編集中）\n1:00 開始"}
    calls = []

    def fake_comments(vid, authors, max_parents):
        calls.append(vid)
        return [
            {"id": "c", "parent": "root", "author": "me", "author_id": "me", "text": texts[vid]}
        ]

    monkeypatch.setattr(youtube, "fetch_comments", fake_comments)
    pipeline.fetch_comments(conn, cfg, lambda _: None, today=TODAY)
    assert calls == ["old"]
    status = conn.execute("SELECT status FROM comment_fetch WHERE video_id = 'old'").fetchone()[0]
    assert status == "editing"
    # 完成版になるまで毎回取り直し、完成版になったら止める
    texts["old"] = "【タイムスタンプ】\n1:00 開始"
    pipeline.fetch_comments(conn, cfg, lambda _: None, today=TODAY)
    pipeline.fetch_comments(conn, cfg, lambda _: None, today=TODAY)
    assert calls == ["old", "old"]
