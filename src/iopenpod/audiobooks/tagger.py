"""Write resolved audiobook metadata into an MP4 container.

Tags are written in place with mutagen, which rewrites only the metadata
atoms — the audio stream and any existing chapter markers are untouched.

Field conventions follow Apple's audiobook layout: the author goes in
``©ART``/``aART``, the narrator in ``©wrt`` (composer), and ``stik=2``
marks the file as an audiobook, which is what routes it to the iPod's
Audiobooks menu and enables position memory.
"""

from __future__ import annotations

import logging
from pathlib import Path

from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm

from .models import AudiobookMetadata

log = logging.getLogger(__name__)

# The ``desc`` atom is a short blurb; iTunes truncates well past this, but
# 255 is the conventional limit and keeps players predictable.
_SHORT_DESC_LIMIT = 255
_PUBLISHER_ATOM = "----:com.apple.iTunes:publisher"
_AUDIOBOOK_STIK = 2
_GENRE = "Audiobook"


def short_description(summary: str, *, limit: int = _SHORT_DESC_LIMIT) -> str:
    """Trim ``summary`` to ``limit`` chars, preferring a sentence boundary."""
    text = summary.strip()
    if len(text) <= limit:
        return text
    clipped = text[:limit]
    stop = clipped.rfind(".")
    return clipped[: stop + 1] if stop > 0 else clipped.rstrip()


def _cover_format(data: bytes) -> int:
    """Detect PNG vs JPEG. mutagen needs the format declared explicitly."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return MP4Cover.FORMAT_PNG
    return MP4Cover.FORMAT_JPEG


def apply_metadata(
    path: Path,
    metadata: AudiobookMetadata,
    *,
    cover_bytes: bytes | None = None,
) -> None:
    """Write ``metadata`` into the MP4 at ``path``, in place.

    Raises ``FileNotFoundError`` if the file is missing.
    """
    if not path.is_file():
        raise FileNotFoundError(f"No such audiobook file: {path}")

    tags = MP4(str(path))

    if metadata.title:
        tags["\xa9nam"] = [metadata.title]
        tags["\xa9alb"] = [metadata.title]
    if metadata.authors:
        tags["\xa9ART"] = [metadata.author_text]
        tags["aART"] = [metadata.author_text]
    if metadata.narrators:
        tags["\xa9wrt"] = [metadata.narrator_text]
    if metadata.release_year:
        tags["\xa9day"] = [str(metadata.release_year)]
    if metadata.summary:
        tags["ldes"] = [metadata.summary]
        tags["desc"] = [short_description(metadata.summary)]
    if metadata.publisher:
        tags[_PUBLISHER_ATOM] = [MP4FreeForm(metadata.publisher.encode("utf-8"))]

    tags["\xa9gen"] = [_GENRE]
    tags["stik"] = [_AUDIOBOOK_STIK]

    if cover_bytes:
        tags["covr"] = [MP4Cover(cover_bytes, imageformat=_cover_format(cover_bytes))]

    tags.save()
    log.info("Tagged audiobook %s as %r", path.name, metadata.title)


def describe_changes(
    path: Path,
    metadata: AudiobookMetadata,
) -> list[tuple[str, str, str]]:
    """Return ``(field, current, proposed)`` rows for a pre-write preview.

    Read-only: the file is never modified.
    """
    if not path.is_file():
        raise FileNotFoundError(f"No such audiobook file: {path}")

    tags = MP4(str(path))

    def current(atom: str) -> str:
        values = tags.get(atom) or []
        if not values:
            return ""
        value = values[0]
        if isinstance(value, bytes | MP4FreeForm):
            return bytes(value).decode("utf-8", errors="replace")
        return str(value)

    rows: list[tuple[str, str, str]] = [
        ("Title", current("\xa9nam"), metadata.title),
        ("Author", current("\xa9ART"), metadata.author_text),
        ("Narrator", current("\xa9wrt"), metadata.narrator_text),
        ("Album", current("\xa9alb"), metadata.title),
        ("Year", current("\xa9day"), str(metadata.release_year or "")),
        ("Publisher", current(_PUBLISHER_ATOM), metadata.publisher),
        ("Genre", current("\xa9gen"), _GENRE),
        ("Description", current("desc"), short_description(metadata.summary)),
    ]
    return [row for row in rows if row[1] != row[2]]
