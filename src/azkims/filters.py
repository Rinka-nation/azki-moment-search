"""Which streams are searchable. Returns an exclusion reason, or None if included."""

from __future__ import annotations

import unicodedata

from azkims.config import Config


def exclusion_reason(
    video_id: str, title: str, availability: str | None, live_status: str | None, cfg: Config
) -> str | None:
    if video_id in cfg.include_video_ids:
        return None
    if video_id in cfg.exclude_video_ids:
        return "manual"
    folded = unicodedata.normalize("NFKC", title).lower()
    if _has_any(folded, cfg.no_clip_title_patterns):
        # 切り抜き禁止の配信。発言を検索に出すのも切り抜きに近いので入れない（2026-11-11 霧島リンカさん）
        return "no_clip"
    if availability in ("subscriber_only", "premium_only", "needs_auth", "private"):
        return "members_only"
    if live_status and live_status != "was_live":
        # is_upcoming / is_live はまだ対象外（終了後の sync で再判定される）
        return f"live_status:{live_status}"
    if _has_any(folded, cfg.singing_title_patterns):
        return "singing"
    return None


def _has_any(folded: str, patterns: list[str]) -> bool:
    return any(unicodedata.normalize("NFKC", p).lower() in folded for p in patterns)
