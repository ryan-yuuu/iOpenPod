from __future__ import annotations

from iopenpod.itunesdb_shared.constants import MEDIA_TYPE_PODCAST
from iopenpod.itunesdb_writer.mhit_writer import TrackInfo
from iopenpod.itunesdb_writer.mhyp_writer import PlaylistInfo
from iopenpod.sync._playlist_builder import _sync_podcast_playlist_membership


def _podcast_tracks() -> list[TrackInfo]:
    return [
        TrackInfo(
            title=f"Episode {i}",
            location=f":iPod_Control:Music:F0{i}:EP{i}.mp3",
            db_track_id=5 + i,
            media_type=MEDIA_TYPE_PODCAST,
        )
        for i in range(3)
    ]


def _podcast_playlist(track_ids: list[int] | None = None, **kwargs) -> PlaylistInfo:
    return PlaylistInfo(
        name="Podcasts",
        track_ids=list(track_ids or []),
        podcast_flag=1,
        **kwargs,
    )


def test_podcast_playlist_written_to_both_datasets() -> None:
    """The firmware resolves episodes from dataset 2, not dataset 3.

    Writing the Podcasts playlist to dataset 3 alone renders the show with no
    episodes under it, so a first-time podcast sync must populate both.
    """
    dataset2: list[PlaylistInfo] = []
    dataset3: list[PlaylistInfo] = []

    _sync_podcast_playlist_membership(dataset2, dataset3, _podcast_tracks())

    assert len(dataset2) == 1, "dataset 2 must receive the Podcasts playlist"
    assert len(dataset3) == 1, "dataset 3 must receive the Podcasts playlist"
    assert dataset2[0].track_ids == [5, 6, 7]
    assert dataset3[0].track_ids == [5, 6, 7]
    assert all(pl.podcast_flag == 1 for pl in (*dataset2, *dataset3))


def test_podcast_playlist_copies_share_one_id() -> None:
    """iTunes gives both copies the same persistent playlist ID."""
    dataset2: list[PlaylistInfo] = []
    dataset3: list[PlaylistInfo] = []

    _sync_podcast_playlist_membership(dataset2, dataset3, _podcast_tracks())

    assert dataset2[0].playlist_id is not None
    assert dataset2[0].playlist_id == dataset3[0].playlist_id


def test_podcast_playlist_missing_from_one_dataset_is_repaired() -> None:
    """Databases already written with the dataset-3-only layout self-heal."""
    dataset2: list[PlaylistInfo] = []
    dataset3 = [_podcast_playlist(playlist_id=999, track_ids=[5, 6, 7])]

    _sync_podcast_playlist_membership(dataset2, dataset3, _podcast_tracks())

    assert len(dataset2) == 1
    assert dataset2[0].playlist_id == 999, "repair reuses the existing playlist ID"
    assert dataset2[0].track_ids == [5, 6, 7]


def test_podcast_playlist_not_created_without_podcast_tracks() -> None:
    dataset2: list[PlaylistInfo] = []
    dataset3: list[PlaylistInfo] = []
    music = [
        TrackInfo(
            title="Song",
            location=":iPod_Control:Music:F00:SONG.mp3",
            db_track_id=1,
            media_type=1,
        )
    ]

    _sync_podcast_playlist_membership(dataset2, dataset3, music)

    assert dataset2 == []
    assert dataset3 == []


def test_podcast_playlist_sync_is_idempotent() -> None:
    dataset2: list[PlaylistInfo] = []
    dataset3: list[PlaylistInfo] = []
    tracks = _podcast_tracks()

    for _ in range(3):
        _sync_podcast_playlist_membership(dataset2, dataset3, tracks)

    assert len(dataset2) == 1, "repeated syncs must not duplicate the playlist"
    assert len(dataset3) == 1


def test_existing_podcast_playlists_track_membership_changes() -> None:
    """A pre-existing playlist in each dataset is re-aligned, not replaced."""
    existing2 = _podcast_playlist(playlist_id=42, track_ids=[99])
    existing3 = _podcast_playlist(playlist_id=42, track_ids=[99])
    dataset2 = [existing2]
    dataset3 = [existing3]

    _sync_podcast_playlist_membership(dataset2, dataset3, _podcast_tracks())

    assert dataset2 == [existing2], "must reuse the existing playlist object"
    assert dataset3 == [existing3]
    assert existing2.track_ids == [5, 6, 7]
    assert existing3.track_ids == [5, 6, 7]
