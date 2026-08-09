"""ALAC must be recorded as ALAC, not AAC, in the SQLite database.

The .m4a container holds both AAC and ALAC, so only the codec distinguishes
them.  A previous implementation guessed from bitrate, which fails because
mutagen reports either 0 or the *uncompressed* PCM rate for ALAC — never the
compressed rate.  These tests pin the codec-driven behaviour.
"""

from __future__ import annotations

from iopenpod.sqlitedb_writer.library_writer import (
    AUDIO_FORMAT_AAC,
    AUDIO_FORMAT_AIFF,
    AUDIO_FORMAT_ALAC,
    AUDIO_FORMAT_MP3,
    AUDIO_FORMAT_WAV,
    _audio_format_for,
)
from iopenpod.sync._formats import CODEC_AAC, CODEC_ALAC, normalize_codec
from iopenpod.sync._track_conversion import track_dict_to_info

# ── normalize_codec ──────────────────────────────────────────────────────


def test_normalize_codec_maps_mutagen_mp4_object_types_to_aac() -> None:
    # mutagen reports MP4 audio object types, not bare codec names.
    assert normalize_codec("mp4a.40.2") == CODEC_AAC   # AAC-LC
    assert normalize_codec("mp4a.40.5") == CODEC_AAC   # HE-AAC


def test_normalize_codec_recognises_alac_case_insensitively() -> None:
    assert normalize_codec("alac") == CODEC_ALAC
    assert normalize_codec("ALAC") == CODEC_ALAC
    assert normalize_codec(" Alac ") == CODEC_ALAC


def test_normalize_codec_returns_empty_for_unknown_input() -> None:
    # "" means "fall back to inference", never a specific codec.
    assert normalize_codec("") == ""
    assert normalize_codec(None) == ""


def test_normalize_codec_passes_through_unrecognised_codecs() -> None:
    assert normalize_codec("opus") == "opus"


# ── codec → audio_format ─────────────────────────────────────────────────


def test_alac_in_m4a_container_is_written_as_alac() -> None:
    assert _audio_format_for("m4a", CODEC_ALAC) == AUDIO_FORMAT_ALAC


def test_aac_in_m4a_container_is_written_as_aac() -> None:
    assert _audio_format_for("m4a", CODEC_AAC) == AUDIO_FORMAT_AAC


def test_low_bitrate_alac_is_still_written_as_alac() -> None:
    """Regression: sparse ALAC can fall below any plausible bitrate threshold.

    A real 44.1/16 mono ALAC measures ~484 kbps and a quiet stereo track
    ~340 kbps — both under the 500 kbps cutoff the old heuristic used.  The
    codec is authoritative, so bitrate must not influence the result at all.
    """
    assert _audio_format_for("m4a", CODEC_ALAC) == AUDIO_FORMAT_ALAC


def test_m4b_audiobook_respects_codec() -> None:
    assert _audio_format_for("m4b", CODEC_ALAC) == AUDIO_FORMAT_ALAC
    assert _audio_format_for("m4b", CODEC_AAC) == AUDIO_FORMAT_AAC


def test_unknown_codec_falls_back_to_container_filetype() -> None:
    # Tracks restored from mapping data written before the codec was
    # recorded still need a sensible value.
    assert _audio_format_for("m4a", "") == AUDIO_FORMAT_AAC
    assert _audio_format_for("mp3", "") == AUDIO_FORMAT_MP3
    assert _audio_format_for("wav", "") == AUDIO_FORMAT_WAV
    assert _audio_format_for("aiff", "") == AUDIO_FORMAT_AIFF


def test_unrecognised_filetype_and_codec_falls_back_to_mp3() -> None:
    assert _audio_format_for("xyz", "") == AUDIO_FORMAT_MP3


# ── round-trip from an existing iPod database ────────────────────────────


def test_track_read_from_ipod_keeps_alac_via_filetype_description() -> None:
    """Tracks read back off the iPod carry no probed codec.

    The iTunesDB filetype description says "Apple Lossless audio file", so a
    re-write must preserve ALAC rather than downgrading the record to AAC.
    """
    info = track_dict_to_info({
        "Title": "Donda Chant",
        "filetype": "Apple Lossless audio file",
    })
    assert info.codec == CODEC_ALAC
    assert _audio_format_for(info.filetype, info.codec) == AUDIO_FORMAT_ALAC


def test_track_read_from_ipod_keeps_aac_as_aac() -> None:
    info = track_dict_to_info({
        "Title": "Some Single",
        "filetype": "AAC audio file",
    })
    assert info.codec == CODEC_AAC
    assert _audio_format_for(info.filetype, info.codec) == AUDIO_FORMAT_AAC


def test_explicit_codec_in_track_dict_wins_over_filetype_description() -> None:
    info = track_dict_to_info({
        "Title": "Track",
        "filetype": "AAC audio file",
        "codec": "alac",
    })
    assert info.codec == CODEC_ALAC
