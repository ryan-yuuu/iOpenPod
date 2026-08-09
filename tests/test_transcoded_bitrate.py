"""A transcoded track's bitrate must describe the file written to the iPod.

Lossless sources previously carried their own bitrate into the database, but
ALAC's compression ratio is not FLAC's, so the recorded figure described a
file that does not exist on the device.  Measure the output instead.
"""

from __future__ import annotations

from pathlib import Path

from iopenpod.sync._formats import average_bitrate_kbps
from iopenpod.sync._track_conversion import pc_track_to_info
from iopenpod.sync.pc_library import PCTrack


def _pc_track(**overrides) -> PCTrack:
    defaults = dict(
        path="/music/track.flac",
        relative_path="track.flac",
        filename="track.flac",
        extension=".flac",
        mtime=0.0,
        size=13_778_353,
        title="Track",
        artist="Artist",
        album="Album",
        album_artist=None,
        genre=None,
        year=None,
        track_number=1,
        track_total=None,
        disc_number=None,
        disc_total=None,
        duration_ms=154_357,
        bitrate=699,          # the FLAC source's bitrate
        sample_rate=44_100,
        rating=None,
    )
    defaults.update(overrides)
    return PCTrack(**defaults)


def _write_output(tmp_path: Path, size_bytes: int) -> Path:
    out = tmp_path / "track.m4a"
    out.write_bytes(b"\0" * size_bytes)
    return out


# ── average_bitrate_kbps ─────────────────────────────────────────────────


def test_average_bitrate_is_size_over_duration() -> None:
    # 14_046_112 bytes over 154_357 ms == 728 kbps
    assert average_bitrate_kbps(14_046_112, 154_357) == 728


def test_average_bitrate_returns_none_on_missing_inputs() -> None:
    assert average_bitrate_kbps(0, 154_357) is None
    assert average_bitrate_kbps(14_046_112, 0) is None
    assert average_bitrate_kbps(None, None) is None


def test_average_bitrate_returns_none_on_negative_inputs() -> None:
    assert average_bitrate_kbps(-1, 154_357) is None
    assert average_bitrate_kbps(14_046_112, -1) is None


# ── transcoded lossless ──────────────────────────────────────────────────


def test_flac_to_alac_records_the_output_bitrate(tmp_path: Path) -> None:
    out = _write_output(tmp_path, 14_046_112)
    info = pc_track_to_info(_pc_track(), ":iPod:F00:X.m4a", True, ipod_file_path=out)
    assert info.bitrate == 728          # measured from the ALAC we wrote
    assert info.bitrate != 699          # not the FLAC source's figure


def test_measured_bitrate_tracks_a_smaller_output(tmp_path: Path) -> None:
    # A well-compressed ALAC output must not inherit a larger source figure.
    out = _write_output(tmp_path, 6_000_000)
    info = pc_track_to_info(_pc_track(), ":iPod:F00:X.m4a", True, ipod_file_path=out)
    assert info.bitrate == average_bitrate_kbps(6_000_000, 154_357)


def test_wav_source_also_measures_the_output(tmp_path: Path) -> None:
    out = _write_output(tmp_path, 14_046_112)
    track = _pc_track(extension=".wav", filename="track.wav", bitrate=1411)
    info = pc_track_to_info(track, ":iPod:F00:X.m4a", True, ipod_file_path=out)
    assert info.bitrate == 728


def test_falls_back_to_source_bitrate_when_output_is_unavailable() -> None:
    # Dry run: nothing was written, so there is nothing to measure.
    info = pc_track_to_info(_pc_track(), ":iPod:F00:X.m4a", True, ipod_file_path=None)
    assert info.bitrate == 699


def test_zero_duration_leaves_the_source_bitrate_alone(tmp_path: Path) -> None:
    out = _write_output(tmp_path, 14_046_112)
    track = _pc_track(duration_ms=0)
    info = pc_track_to_info(track, ":iPod:F00:X.m4a", True, ipod_file_path=out)
    assert info.bitrate == 699


# ── unchanged paths ──────────────────────────────────────────────────────


def test_direct_copy_keeps_the_source_bitrate(tmp_path: Path) -> None:
    # Not transcoded: the source file *is* the file on the iPod.
    out = _write_output(tmp_path, 14_046_112)
    track = _pc_track(extension=".m4a", filename="track.m4a", bitrate=256)
    info = pc_track_to_info(track, ":iPod:F00:X.m4a", False, ipod_file_path=out)
    assert info.bitrate == 256
