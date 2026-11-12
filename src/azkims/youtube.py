"""yt-dlp adapter. Only metadata and auto-captions are fetched; no audio/video is downloaded."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlparse


class CaptionsUnavailable(Exception):
    pass


# bot 判定・レート制限のメッセージ。これが出たら続けると悪化するので止める
_BLOCK_MARKERS = ("confirm you're not a bot", "HTTP Error 429", "Too Many Requests")


def is_blocked(error: str) -> bool:
    return any(m.lower() in error.lower() for m in _BLOCK_MARKERS)


@dataclass(frozen=True)
class VideoInfo:
    video_id: str
    title: str
    upload_date: str | None  # YYYY-MM-DD
    duration_sec: int | None
    availability: str | None
    live_status: str | None


def _ydl(opts: dict):
    from yt_dlp import YoutubeDL

    base = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "socket_timeout": 30,
        "retries": 5,
        "extractor_retries": 3,
    }
    return YoutubeDL({**base, **opts})


def list_channel(url: str, limit: int | None = None) -> list[dict]:
    opts = {"extract_flat": "in_playlist"}
    if limit:
        opts["playlistend"] = limit
    with _ydl(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    rows = []
    for order, entry in enumerate(info.get("entries") or []):
        if not entry or not entry.get("id"):
            continue
        rows.append(
            {
                "video_id": entry["id"],
                "title": entry.get("title") or "",
                "list_order": order,
                "availability": entry.get("availability"),
                "live_status": entry.get("live_status"),
            }
        )
    return rows


def _fmt_date(d: str | None) -> str | None:
    if d and len(d) == 8 and d.isdigit():
        return f"{d[:4]}-{d[4:6]}-{d[6:]}"
    return None


def caption_path(captions_dir: Path, video_id: str, lang: str) -> Path:
    return captions_dir / f"{video_id}.{lang}.json3"


def _translated_from(info: dict, lang: str) -> str | None:
    """自動字幕 lang が機械翻訳（URL に tlang がある）なら、元の言語を返す。"""
    for f in (info.get("automatic_captions") or {}).get(lang) or []:
        q = parse_qs(urlparse(f.get("url") or "").query)
        if "tlang" in q:
            return (q.get("lang") or ["?"])[0]
    return None


def fetch_video(video_id: str, captions_dir: Path, lang: str = "ja") -> tuple[VideoInfo, Path]:
    """Fetch metadata and write the auto-caption json3 into captions_dir.

    Raises CaptionsUnavailable (with VideoInfo in args[1]) when no auto caption exists.
    """
    captions_dir.mkdir(parents=True, exist_ok=True)
    opts = {
        "skip_download": True,
        "writeautomaticsub": True,
        "writesubtitles": False,
        "subtitleslangs": [lang],
        "subtitlesformat": "json3",
        "outtmpl": str(captions_dir / "%(id)s.%(ext)s"),
    }
    with _ydl(opts) as ydl:
        info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
        translated = _translated_from(info, lang)
        if translated is None:
            ydl.process_ie_result(info, download=True)
    meta = VideoInfo(
        video_id=video_id,
        title=info.get("title") or "",
        upload_date=_fmt_date(info.get("release_date") or info.get("upload_date")),
        duration_sec=info.get("duration"),
        availability=info.get("availability"),
        live_status=info.get("live_status"),
    )
    if translated is not None:
        # 配信の言語が別の言語（en など）で登録されていると、'ja' は英語の自動字幕の機械翻訳になる。
        # 中身は使えず、しかもこの翻訳字幕の取得には HTTP 429 が返る（2026-10-28 に確認）
        raise CaptionsUnavailable(
            f"'{lang}' auto caption is machine-translated from '{translated}'", meta
        )
    path = caption_path(captions_dir, video_id, lang)
    if not path.exists():
        raise CaptionsUnavailable(f"no '{lang}' auto caption", meta)
    return meta, path


def fetch_comments(video_id: str, author_ids: set[str], max_parents: int = 60) -> list[dict]:
    """Top comments (+ replies) of the video, filtered to the given authors.

    返信に分けて投稿されたタイムスタンプも拾うため、返信も取得する。
    """
    opts = {
        "skip_download": True,
        "getcomments": True,
        "extractor_args": {
            "youtube": {
                "comment_sort": ["top"],
                # max-comments, max-parents, max-replies, max-replies-per-thread
                "max_comments": ["1000", str(max_parents), "1000", "100"],
            }
        },
    }
    with _ydl(opts) as ydl:
        info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
        info = ydl.sanitize_info(info)
    return [
        {
            "id": c.get("id"),
            "parent": c.get("parent"),
            "author": c.get("author"),
            "author_id": c.get("author_id"),
            "text": c.get("text") or "",
            "timestamp": c.get("timestamp"),
        }
        for c in info.get("comments") or []
        if c.get("author_id") in author_ids
    ]
