"""Application-layer orchestration for tagging an audiobook sync item."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from iopenpod.application.audiobook_tagging import (
    audiobook_path_for_item,
    is_taggable_audiobook,
    tag_audiobook_file,
)
from iopenpod.audiobooks.models import AudiobookMetadata

METADATA = AudiobookMetadata(
    asin="B00E83NFUC",
    title="Kafka on the Shore",
    authors=("Haruki Murakami",),
    narrators=("Sean Barrett",),
    publisher="Naxos AudioBooks",
    release_year=2007,
    summary="A tale of two journeys.",
    cover_url="https://example.invalid/cover.jpg",
)

JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 32


@dataclass
class _FakePCTrack:
    path: str
    is_audiobook: bool = True
    is_podcast: bool = False
    is_video: bool = False


@dataclass
class _FakeItem:
    pc_track: Any = None
    ipod_track: Any = None


def _item(tmp_path: Path, name: str = "book.m4b", **kwargs: Any) -> _FakeItem:
    target = tmp_path / name
    target.write_bytes(b"stub")
    return _FakeItem(pc_track=_FakePCTrack(path=str(target), **kwargs))


# ── Identifying a taggable item ─────────────────────────────────────────────


def test_audiobook_item_is_taggable(tmp_path: Path) -> None:
    assert is_taggable_audiobook(_item(tmp_path))


def test_music_item_is_not_taggable(tmp_path: Path) -> None:
    assert not is_taggable_audiobook(_item(tmp_path, "song.m4a", is_audiobook=False))


def test_podcast_item_is_not_taggable(tmp_path: Path) -> None:
    item = _item(tmp_path, "ep.m4b", is_audiobook=True, is_podcast=True)

    # Podcasts classify ahead of audiobooks and have their own metadata source.
    assert not is_taggable_audiobook(item)


def test_ipod_only_item_is_not_taggable() -> None:
    # Nothing on the PC side to write to.
    assert not is_taggable_audiobook(_FakeItem(ipod_track={"media_type": 0x08}))


def test_non_mp4_audiobook_is_not_taggable(tmp_path: Path) -> None:
    # An .mp3 audiobook cannot take MP4 atoms.
    assert not is_taggable_audiobook(_item(tmp_path, "book.mp3"))


def test_item_without_a_track_is_not_taggable() -> None:
    assert not is_taggable_audiobook(_FakeItem())


def test_path_is_resolved_for_a_taggable_item(tmp_path: Path) -> None:
    item = _item(tmp_path)

    resolved = audiobook_path_for_item(item)

    assert resolved == tmp_path / "book.m4b"


def test_path_is_none_for_an_untaggable_item() -> None:
    assert audiobook_path_for_item(_FakeItem()) is None


# ── Applying ────────────────────────────────────────────────────────────────


def test_tagging_writes_metadata_and_reports_success(tmp_path: Path) -> None:
    written: dict[str, Any] = {}

    def _apply(path: Path, metadata: AudiobookMetadata, *, cover_bytes: bytes | None = None) -> None:
        written["path"] = path
        written["metadata"] = metadata
        written["cover"] = cover_bytes

    path = tmp_path / "book.m4b"
    path.write_bytes(b"stub")

    result = tag_audiobook_file(
        path,
        METADATA,
        cover_fn=lambda _url: JPEG_BYTES,
        apply_fn=_apply,
    )

    assert result.applied
    assert not result.error
    assert result.cover_embedded
    assert written["path"] == path
    assert written["cover"] == JPEG_BYTES


def test_tagging_proceeds_without_cover_when_download_fails(tmp_path: Path) -> None:
    seen: dict[str, Any] = {}

    def _apply(path: Path, metadata: AudiobookMetadata, *, cover_bytes: bytes | None = None) -> None:
        seen["cover"] = cover_bytes

    def _failing_cover(_url: str) -> bytes:
        raise OSError("network down")

    path = tmp_path / "book.m4b"
    path.write_bytes(b"stub")

    result = tag_audiobook_file(path, METADATA, cover_fn=_failing_cover, apply_fn=_apply)

    # Losing the artwork must not cost the user the text metadata.
    assert result.applied
    assert not result.cover_embedded
    assert seen["cover"] is None


def test_tagging_skips_cover_fetch_when_no_url(tmp_path: Path) -> None:
    calls: list[str] = []
    path = tmp_path / "book.m4b"
    path.write_bytes(b"stub")

    result = tag_audiobook_file(
        path,
        AudiobookMetadata(asin="X", title="No Art"),
        cover_fn=lambda url: calls.append(url) or b"",
        apply_fn=lambda *a, **k: None,
    )

    assert calls == []
    assert result.applied
    assert not result.cover_embedded


def test_tagging_reports_a_write_failure_instead_of_raising(tmp_path: Path) -> None:
    def _explode(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("read-only file system")

    path = tmp_path / "book.m4b"
    path.write_bytes(b"stub")

    result = tag_audiobook_file(path, METADATA, cover_fn=lambda _u: b"", apply_fn=_explode)

    assert not result.applied
    assert "read-only" in result.error


def test_tagging_reports_a_missing_file(tmp_path: Path) -> None:
    result = tag_audiobook_file(tmp_path / "gone.m4b", METADATA, cover_fn=lambda _u: b"")

    assert not result.applied
    assert result.error


def test_tagging_result_carries_the_path(tmp_path: Path) -> None:
    path = tmp_path / "book.m4b"
    path.write_bytes(b"stub")

    result = tag_audiobook_file(path, METADATA, cover_fn=lambda _u: b"", apply_fn=lambda *a, **k: None)

    assert result.path == path


@pytest.mark.parametrize("suffix", [".m4b", ".M4B", ".m4a", ".mp4"])
def test_mp4_family_extensions_are_accepted(tmp_path: Path, suffix: str) -> None:
    assert is_taggable_audiobook(_item(tmp_path, f"book{suffix}"))


# ── Run summary ─────────────────────────────────────────────────────────────


def _summary(**kwargs):
    from iopenpod.application.audiobook_tagging import AudiobookRunSummary

    kwargs.setdefault("total", 1)
    return AudiobookRunSummary(**kwargs)


def _text(**kwargs) -> str:
    from iopenpod.application.audiobook_tagging import summarize_tagging_run

    return summarize_tagging_run(_summary(**kwargs))


def test_summary_is_empty_when_nothing_happened() -> None:
    # Nothing to say means no dialog at all.
    assert _text() == ""


def test_summary_counts_and_names_updated_books() -> None:
    text = _text(total=2, applied=("a.m4b", "b.m4b"))

    assert "Updated 2: a.m4b, b.m4b" in text


def test_summary_mentions_rescanning_only_when_something_changed() -> None:
    assert "Re-scan" in _text(total=1, applied=("a.m4b",))
    assert "Re-scan" not in _text(total=1, skipped=("a.m4b",))


def test_summary_reports_each_outcome_separately() -> None:
    text = _text(total=3, applied=("a.m4b",), unchanged=("b.m4b",), skipped=("c.m4b",))

    assert "Updated 1: a.m4b" in text
    assert "Already up to date 1: b.m4b" in text
    assert "Skipped 1: c.m4b" in text


def test_stopping_early_counts_the_books_never_reached() -> None:
    # Cancelled on the second of five: that book and the three after it.
    text = _text(total=5, applied=("a.m4b",), stopped_at=2)

    assert "4 left untouched" in text


def test_finishing_the_run_reports_no_untouched_books() -> None:
    assert _summary(total=3, stopped_at=None).untouched == 0


def test_problems_are_listed_and_flagged() -> None:
    summary = _summary(total=1, problems=("a.m4b: permission denied",))

    assert summary.has_problems
    assert "a.m4b: permission denied" in _text(total=1, problems=("a.m4b: permission denied",))


def test_problems_alone_still_produce_a_report() -> None:
    # A missing file must not vanish just because nothing else happened.
    assert _text(total=1, problems=("gone.m4b: no longer at /x",)).startswith("Problems:")
