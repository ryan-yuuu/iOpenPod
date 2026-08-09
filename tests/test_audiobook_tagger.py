"""Writing fetched audiobook metadata into an .m4b file."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from mutagen.mp4 import MP4

from iopenpod.audiobooks.models import AudiobookMetadata
from iopenpod.audiobooks.tagger import apply_metadata, describe_changes

pytestmark = pytest.mark.skipif(
    subprocess.run(["ffmpeg", "-version"], capture_output=True).returncode != 0,
    reason="ffmpeg is required to synthesise a test .m4b",
)

METADATA = AudiobookMetadata(
    asin="B00E83NFUC",
    title="Kafka on the Shore",
    subtitle="",
    authors=("Haruki Murakami",),
    narrators=("Sean Barrett", "Oliver Le Sueur"),
    publisher="Naxos AudioBooks",
    release_year=2007,
    isbn="9789629546250",
    summary="A tale of two journeys.",
    genres=("Literature & Fiction",),
    cover_url="https://example.invalid/cover.jpg",
    runtime_min=1148,
)

# Smallest valid JPEG the decoder will accept, used as stand-in artwork.
JPEG_BYTES = bytes.fromhex(
    "ffd8ffe000104a46494600010100000100010000ffdb004300ffffffffffffff"
    "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
    "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
    "ffffffffffffffffc00011080001000103012200021101031101ffc4001f0000"
    "010501010101010100000000000000000102030405060708090a0bffc400b510"
    "0002010303020403050504040000017d01020300041105122131410613516107"
    "227114328191a1082342b1c11552d1f02433627282090a161718191a25262728"
    "292a3435363738393a434445464748494a535455565758595a63646566676869"
    "6a737475767778797a838485868788898a92939495969798999aa2a3a4a5a6a7"
    "a8a9aab2b3b4b5b6b7b8b9bac2c3c4c5c6c7c8c9cad2d3d4d5d6d7d8d9dae1e2"
    "e3e4e5e6e7e8e9eaf1f2f3f4f5f6f7f8f9faffda0008010100003f00fbfeffd9"
)


@pytest.fixture
def book(tmp_path: Path) -> Path:
    """A tiny real .m4b with an existing chapter, to prove nothing is lost."""
    path = tmp_path / "book.m4b"
    meta = tmp_path / "chapters.txt"
    meta.write_text(
        ";FFMETADATA1\n\n[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=1000\ntitle=One\n\n[CHAPTER]\nTIMEBASE=1/1000\nSTART=1000\nEND=2000\ntitle=Two\n",
        encoding="utf-8",
    )
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=f=440:d=2",
            "-f",
            "ffmetadata",
            "-i",
            str(meta),
            "-map",
            "0:a",
            "-map_metadata",
            "1",
            "-c:a",
            "aac",
            "-b:a",
            "32k",
            str(path),
        ],
        check=True,
        capture_output=True,
    )
    return path


def test_apply_metadata_writes_standard_atoms(book: Path) -> None:
    apply_metadata(book, METADATA)

    tags = MP4(book)
    assert tags["\xa9nam"] == ["Kafka on the Shore"]
    assert tags["\xa9ART"] == ["Haruki Murakami"]
    assert tags["aART"] == ["Haruki Murakami"]
    assert tags["\xa9alb"] == ["Kafka on the Shore"]
    assert tags["\xa9day"] == ["2007"]
    assert tags["\xa9gen"] == ["Audiobook"]


def test_apply_metadata_records_narrator_as_composer(book: Path) -> None:
    apply_metadata(book, METADATA)

    assert MP4(book)["\xa9wrt"] == ["Sean Barrett, Oliver Le Sueur"]


def test_apply_metadata_flags_file_as_audiobook(book: Path) -> None:
    apply_metadata(book, METADATA)

    # stik=2 is what routes the file to the iPod Audiobooks menu and
    # enables position memory.
    assert MP4(book)["stik"] == [2]


def test_apply_metadata_writes_publisher_freeform_atom(book: Path) -> None:
    apply_metadata(book, METADATA)

    value = MP4(book)["----:com.apple.iTunes:publisher"][0]
    assert bytes(value).decode("utf-8") == "Naxos AudioBooks"


def test_apply_metadata_writes_short_and_long_descriptions(book: Path) -> None:
    apply_metadata(book, METADATA)

    tags = MP4(book)
    assert tags["ldes"] == ["A tale of two journeys."]
    assert tags["desc"][0].startswith("A tale of two journeys.")


def test_short_description_is_truncated_on_a_sentence_boundary(book: Path) -> None:
    long_summary = ("Sentence one is here. " * 40).strip()
    apply_metadata(book, METADATA.__class__(**{**METADATA.__dict__, "summary": long_summary}))

    desc = MP4(book)["desc"][0]
    assert len(desc) <= 255
    assert desc.endswith(".")


def test_apply_metadata_embeds_cover_art(book: Path) -> None:
    apply_metadata(book, METADATA, cover_bytes=JPEG_BYTES)

    covers = MP4(book)["covr"]
    assert len(covers) == 1
    assert bytes(covers[0])[:2] == b"\xff\xd8"  # JPEG SOI marker


def test_apply_metadata_without_cover_leaves_artwork_absent(book: Path) -> None:
    apply_metadata(book, METADATA)

    assert "covr" not in MP4(book)


def test_apply_metadata_preserves_existing_chapters(book: Path) -> None:
    apply_metadata(book, METADATA, cover_bytes=JPEG_BYTES)

    # Tagging must never disturb chapter markers already in the file.
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_chapters", "-of", "csv=p=0", str(book)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert len([line for line in probe.stdout.splitlines() if line.strip()]) == 2


def test_apply_metadata_preserves_audio_stream(book: Path) -> None:
    before = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(book)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    apply_metadata(book, METADATA, cover_bytes=JPEG_BYTES)

    after = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(book)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert before == after


def test_apply_metadata_rejects_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        apply_metadata(tmp_path / "nope.m4b", METADATA)


# ── Change preview ──────────────────────────────────────────────────────────


def test_describe_changes_reports_old_and_new_values(book: Path) -> None:
    changes = {field: (old, new) for field, old, new in describe_changes(book, METADATA)}

    assert changes["Title"][1] == "Kafka on the Shore"
    assert changes["Narrator"][1] == "Sean Barrett, Oliver Le Sueur"


def test_describe_changes_shows_current_value_before_writing(book: Path) -> None:
    tags = MP4(book)
    tags["\xa9nam"] = ["Old Title"]
    tags.save()

    changes = {field: (old, new) for field, old, new in describe_changes(book, METADATA)}

    assert changes["Title"][0] == "Old Title"


def test_describe_changes_does_not_modify_the_file(book: Path) -> None:
    before = book.read_bytes()

    describe_changes(book, METADATA)

    assert book.read_bytes() == before
