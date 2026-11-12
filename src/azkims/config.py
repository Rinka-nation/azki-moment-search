from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "config.toml"
EXAMPLE_CONFIG = PROJECT_ROOT / "config" / "config.example.toml"


@dataclass
class Config:
    channel_url: str
    data_dir: Path
    output_dir: Path
    dictionary_dir: Path
    caption_lang: str = "ja"
    sleep_seconds: float = 6.0
    sleep_jitter: float = 0.0
    max_videos_per_run: int = 20
    min_age_days: int = 0
    singing_title_patterns: list[str] = field(default_factory=list)
    no_clip_title_patterns: list[str] = field(default_factory=list)
    exclude_video_ids: set[str] = field(default_factory=set)
    include_video_ids: set[str] = field(default_factory=set)
    segment_max_chars: int = 60
    segment_gap_ms: int = 2000
    ts_author_ids: set[str] = field(default_factory=set)
    ts_since: str | None = None
    ts_max_parents: int = 60

    @property
    def db_path(self) -> Path:
        return self.data_dir / "azkims.sqlite"

    @property
    def captions_dir(self) -> Path:
        return self.data_dir / "captions"

    @property
    def comments_dir(self) -> Path:
        return self.data_dir / "comments"


def _resolve(p: str) -> Path:
    path = Path(p)
    return path if path.is_absolute() else PROJECT_ROOT / path


def load_config(path: Path | None = None) -> Config:
    if path is None:
        path = DEFAULT_CONFIG if DEFAULT_CONFIG.exists() else EXAMPLE_CONFIG
    with open(path, "rb") as f:
        raw = tomllib.load(f)

    paths = raw.get("paths", {})
    fetch = raw.get("fetch", {})
    flt = raw.get("filter", {})
    seg = raw.get("segment", {})
    ts = raw.get("timestamps", {})
    return Config(
        channel_url=raw["channel"]["url"],
        data_dir=_resolve(paths.get("data_dir", "data")),
        output_dir=_resolve(paths.get("output_dir", "output")),
        dictionary_dir=_resolve(paths.get("dictionary_dir", "config/dictionary")),
        caption_lang=fetch.get("caption_lang", "ja"),
        sleep_seconds=float(fetch.get("sleep_seconds", 6.0)),
        sleep_jitter=float(fetch.get("sleep_jitter", 0.0)),
        max_videos_per_run=int(fetch.get("max_videos_per_run", 20)),
        min_age_days=int(fetch.get("min_age_days", 0)),
        singing_title_patterns=list(flt.get("singing_title_patterns", [])),
        no_clip_title_patterns=list(flt.get("no_clip_title_patterns", [])),
        exclude_video_ids=set(flt.get("exclude_video_ids", [])),
        include_video_ids=set(flt.get("include_video_ids", [])),
        segment_max_chars=int(seg.get("max_chars", 60)),
        segment_gap_ms=int(seg.get("gap_ms", 2000)),
        ts_author_ids=set(ts.get("author_ids", [])),
        ts_since=ts.get("since") or None,
        ts_max_parents=int(ts.get("max_parents", 60)),
    )
