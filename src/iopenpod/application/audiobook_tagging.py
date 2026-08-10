"""Orchestrates fetching audiobook artwork and writing tags to a PC file.

This lives in the application layer so the GUI can drive it without importing
``iopenpod.audiobooks`` directly, which the architecture rules forbid.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from iopenpod.application.sync_review_model import classify_media_type
from iopenpod.audiobooks.models import AudiobookMetadata

log = logging.getLogger(__name__)

# Only MP4 containers can carry the iTunes atoms an audiobook needs.
_MP4_SUFFIXES = frozenset({".m4b", ".m4a", ".mp4"})


@dataclass(frozen=True)
class AudiobookTagResult:
    """Outcome of one tagging attempt."""

    path: Path
    applied: bool
    cover_embedded: bool = False
    error: str = ""


def audiobook_path_for_item(item: Any) -> Path | None:
    """Return the PC file for a taggable audiobook sync item, else ``None``."""
    if classify_media_type(item) != "audiobook":
        return None

    track = getattr(item, "pc_track", None)
    if track is None:
        return None

    raw_path = str(getattr(track, "path", "") or "")
    if not raw_path:
        return None

    path = Path(raw_path)
    if path.suffix.lower() not in _MP4_SUFFIXES:
        return None
    return path


def is_taggable_audiobook(item: Any) -> bool:
    """Whether the metadata lookup action applies to ``item``."""
    return audiobook_path_for_item(item) is not None


def tag_audiobook_file(
    path: Path,
    metadata: AudiobookMetadata,
    *,
    cover_fn: Callable[[str], bytes] | None = None,
    apply_fn: Callable[..., None] | None = None,
) -> AudiobookTagResult:
    """Download artwork if available, then write ``metadata`` into ``path``.

    Never raises: failures come back on the result so the caller can show
    them. A failed artwork download does not prevent the text metadata from
    being written.
    """
    from iopenpod.audiobooks.audnexus import fetch_cover_bytes
    from iopenpod.audiobooks.tagger import apply_metadata

    fetch_cover = cover_fn or fetch_cover_bytes
    apply = apply_fn or apply_metadata

    cover_bytes: bytes | None = None
    if metadata.cover_url:
        try:
            cover_bytes = fetch_cover(metadata.cover_url) or None
        except Exception as exc:  # noqa: BLE001 - artwork is optional
            log.warning("Cover download failed for %s: %s", metadata.title, exc)
            cover_bytes = None

    try:
        apply(path, metadata, cover_bytes=cover_bytes)
    except Exception as exc:  # noqa: BLE001 - reported on the result
        log.warning("Tagging failed for %s: %s", path, exc)
        return AudiobookTagResult(path=path, applied=False, error=str(exc))

    return AudiobookTagResult(
        path=path,
        applied=True,
        cover_embedded=cover_bytes is not None,
    )


def describe_pending_changes(
    path: Path,
    metadata: AudiobookMetadata,
) -> list[tuple[str, str, str]]:
    """``(field, current, proposed)`` rows for a confirmation prompt."""
    from iopenpod.audiobooks.tagger import describe_changes

    return describe_changes(path, metadata)


@dataclass(frozen=True)
class AudiobookRunSummary:
    """What happened across a run over one or more audiobooks."""

    total: int
    applied: tuple[str, ...] = ()
    unchanged: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()
    problems: tuple[str, ...] = ()
    stopped_at: int | None = None

    @property
    def untouched(self) -> int:
        """Books never reached because the run was stopped part way."""
        if self.stopped_at is None:
            return 0
        return max(0, self.total - self.stopped_at + 1)

    @property
    def has_problems(self) -> bool:
        return bool(self.problems)


def summarize_tagging_run(summary: AudiobookRunSummary) -> str:
    """Compose the end-of-run report shown once, instead of after each book.

    Returns an empty string when there is nothing worth saying, so the caller
    can skip the dialog entirely.
    """
    lines: list[str] = []
    if summary.applied:
        lines.append(f"Updated {len(summary.applied)}: {', '.join(summary.applied)}")
    if summary.unchanged:
        lines.append(
            f"Already up to date {len(summary.unchanged)}: {', '.join(summary.unchanged)}"
        )
    if summary.skipped:
        lines.append(f"Skipped {len(summary.skipped)}: {', '.join(summary.skipped)}")
    if summary.untouched:
        lines.append(f"Stopped early — {summary.untouched} left untouched.")
    if summary.problems:
        if lines:
            lines.append("")
        lines.append("Problems:")
        lines.extend(f"  {problem}" for problem in summary.problems)

    if not lines:
        return ""
    if summary.applied:
        lines.append("")
        lines.append("Re-scan or drop the files again to refresh the sync plan.")
    return "\n".join(lines)
