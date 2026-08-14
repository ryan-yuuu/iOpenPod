"""The "On iPod" library section: device truth, sorting, search, removal."""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast

from PyQt6.QtCore import Qt

from iopenpod.application.services import (
    DeviceSessionService,
    LibraryService,
    SettingsService,
)
from iopenpod.gui.widgets.podcastBrowser import (
    _COMBINED_FEED_KEY,
    _ON_IPOD_KEY,
    _SORT_LARGEST,
    _SORT_OLDEST,
    _SORT_SHOW,
    _VIEW_ON_IPOD,
    PodcastBrowser,
    _is_synthetic_feed,
)
from iopenpod.podcasts.models import (
    STATUS_NOT_DOWNLOADED,
    STATUS_ON_IPOD,
    PodcastEpisode,
    PodcastFeed,
)

PODCAST_MEDIA_TYPE = 0x04


def _track(
    db_track_id: int,
    title: str,
    album: str,
    *,
    size: int = 1_000_000,
    date_added: int = 1_700_000_000,
    media_type: int = PODCAST_MEDIA_TYPE,
) -> dict:
    return {
        "db_track_id": db_track_id,
        "Title": title,
        "Album": album,
        "Description Text": f"{title} description",
        "size": size,
        "length": 600_000,
        "date_added": date_added,
        "media_type": media_type,
    }


class _FakeCache:
    """Stands in for the runtime library cache."""

    def __init__(self, tracks: list[dict] | None, ready: bool = True) -> None:
        self._tracks = tracks or []
        self._ready = ready

    def is_ready(self) -> bool:
        return self._ready

    def get_tracks(self) -> list[dict]:
        return self._tracks


class _FakeStore:
    def __init__(self, feeds: list[PodcastFeed]) -> None:
        self._feeds = feeds
        self.updated: list[list[PodcastFeed]] = []
        self.podcast_dir = ""

    def get_feeds(self) -> list[PodcastFeed]:
        return list(self._feeds)

    def get_feed(self, feed_url: str) -> PodcastFeed | None:
        return next((f for f in self._feeds if f.feed_url == feed_url), None)

    def update_feeds(self, feeds: list[PodcastFeed]) -> int:
        self.updated.append(list(feeds))
        return len(feeds)

    def update_feed(self, feed: PodcastFeed) -> None:
        self.updated.append([feed])


def _browser(
    qtbot,
    tracks: list[dict] | None = None,
    *,
    ready: bool = True,
    feeds: list[PodcastFeed] | None = None,
) -> PodcastBrowser:
    cache = _FakeCache(tracks, ready=ready)
    browser = PodcastBrowser(
        cast(SettingsService, SimpleNamespace()),
        cast(DeviceSessionService, SimpleNamespace()),
        cast(LibraryService, SimpleNamespace(cache=lambda: cache)),
    )
    qtbot.addWidget(browser)
    browser._store = _FakeStore(feeds or [])
    return browser


def _subscribed_feed(db_track_id: int = 11) -> PodcastFeed:
    return PodcastFeed(
        feed_url="https://example.com/feed.xml",
        title="Example Show",
        episodes=[
            PodcastEpisode(
                guid="ep-1",
                title="Subscribed Episode",
                status=STATUS_ON_IPOD,
                ipod_db_track_id=db_track_id,
                size_bytes=42,
            )
        ],
    )


# ── The list reflects the device, not just the subscriptions ────────────────


def test_only_podcast_tracks_are_listed(qtbot) -> None:
    browser = _browser(
        qtbot,
        [
            _track(1, "An Episode", "A Show"),
            _track(2, "A Song", "An Album", media_type=0x01),
        ],
    )

    browser._show_on_ipod_episodes()

    assert [row["Title"] for row in browser._episode_dicts] == ["An Episode"]


def test_a_subscribed_episode_renders_as_itself(qtbot) -> None:
    feed = _subscribed_feed()
    browser = _browser(qtbot, [_track(11, "Device Title", "Example Show")], feeds=[feed])

    browser._show_on_ipod_episodes()

    row = browser._episode_dicts[0]
    assert row["Title"] == "Subscribed Episode"
    assert row["podcast_feed_title"] == "Example Show"


def test_podcasts_with_no_subscription_are_still_listed(qtbot) -> None:
    browser = _browser(qtbot, [_track(99, "Orphan Episode", "Some Other Show")])

    browser._show_on_ipod_episodes()

    row = browser._episode_dicts[0]
    assert row["Title"] == "Orphan Episode"
    assert row["podcast_feed_title"] == "Some Other Show"
    assert row["_can_remove_from_ipod"] is True


def test_an_orphan_without_an_album_is_still_grouped(qtbot) -> None:
    browser = _browser(qtbot, [_track(99, "Orphan", "")])

    browser._show_on_ipod_episodes()

    assert browser._episode_dicts[0]["podcast_feed_title"] == "Unknown Podcast"


def test_device_size_wins_over_the_rss_enclosure_length(qtbot) -> None:
    feed = _subscribed_feed()
    browser = _browser(
        qtbot,
        [_track(11, "Device Title", "Example Show", size=5_000_000)],
        feeds=[feed],
    )

    browser._show_on_ipod_episodes()

    # The episode's own size_bytes is 42; what is on the iPod is what matters.
    assert browser._episode_dicts[0]["size"] == 5_000_000


def test_the_date_column_is_when_it_landed_on_the_ipod(qtbot) -> None:
    browser = _browser(qtbot, [_track(1, "Episode", "Show", date_added=1_650_000_000)])

    browser._show_on_ipod_episodes()

    assert browser._episode_dicts[0]["date_added"] == 1_650_000_000


# ── Loading is distinct from empty ──────────────────────────────────────────


def test_an_unread_database_shows_loading_not_empty(qtbot) -> None:
    browser = _browser(qtbot, [], ready=False)

    browser._show_on_ipod_episodes()

    # Claiming "no podcasts" while the iTunesDB is still parsing would be a lie.
    assert browser._episode_stack.currentIndex() == 1
    assert "Reading" in browser._filter_bar.summary()


def test_an_unread_database_offers_nothing_to_sort_or_search(qtbot) -> None:
    browser = _browser(qtbot, [], ready=False)

    browser._show_on_ipod_episodes()

    assert browser._filter_bar.isHidden()


def test_an_empty_device_says_so(qtbot) -> None:
    browser = _browser(qtbot, [])

    browser._show_on_ipod_episodes()

    assert browser._episode_dicts == []
    assert browser._episode_stack.currentIndex() == 1


# ── Sorting ─────────────────────────────────────────────────────────────────


def _sortable(qtbot) -> PodcastBrowser:
    return _browser(
        qtbot,
        [
            _track(1, "Middle", "Beta Show", size=200, date_added=200),
            _track(2, "Newest", "Alpha Show", size=100, date_added=300),
            _track(3, "Oldest", "Gamma Show", size=300, date_added=100),
        ],
    )


def test_the_default_sort_is_most_recently_added(qtbot) -> None:
    browser = _sortable(qtbot)

    browser._show_on_ipod_episodes()

    assert [r["Title"] for r in browser._episode_dicts] == [
        "Newest",
        "Middle",
        "Oldest",
    ]


def _sorted_by(qtbot, sort_key: str) -> PodcastBrowser:
    browser = _sortable(qtbot)
    browser._sort_by_view[_VIEW_ON_IPOD] = sort_key
    browser._show_on_ipod_episodes()
    return browser


def test_oldest_first_reverses_the_order(qtbot) -> None:
    browser = _sorted_by(qtbot, _SORT_OLDEST)

    assert [r["Title"] for r in browser._episode_dicts] == [
        "Oldest",
        "Middle",
        "Newest",
    ]


def test_largest_first_answers_what_is_eating_space(qtbot) -> None:
    browser = _sorted_by(qtbot, _SORT_LARGEST)

    assert [r["Title"] for r in browser._episode_dicts] == [
        "Oldest",
        "Middle",
        "Newest",
    ]


def test_by_show_groups_alphabetically(qtbot) -> None:
    browser = _sorted_by(qtbot, _SORT_SHOW)

    assert [r["podcast_feed_title"] for r in browser._episode_dicts] == [
        "Alpha Show",
        "Beta Show",
        "Gamma Show",
    ]


def test_choosing_a_sort_label_reorders_the_list(qtbot) -> None:
    browser = _sortable(qtbot)
    browser._show_on_ipod_episodes()

    browser._filter_bar._sort.setCurrentText("Oldest first")

    assert browser._episode_dicts[0]["Title"] == "Oldest"


def test_the_sort_survives_a_re_render_of_the_same_view(qtbot) -> None:
    browser = _sorted_by(qtbot, _SORT_OLDEST)

    # A listened toggle or an RSS refresh must not silently reorder the list.
    browser._refresh_current_view()

    assert browser._episode_dicts[0]["Title"] == "Oldest"


# ── Search ──────────────────────────────────────────────────────────────────


def _searched(qtbot, query: str) -> PodcastBrowser:
    """Open the view, then type — the order a person would do it in."""
    browser = _sortable(qtbot)
    browser._show_on_ipod_episodes()
    browser._filter_bar.set_query(query, notify=True)
    return browser


def test_search_matches_the_episode_title(qtbot) -> None:
    browser = _searched(qtbot, "old")

    assert [r["Title"] for r in browser._episode_dicts] == ["Oldest"]


def test_search_matches_the_show_name(qtbot) -> None:
    browser = _searched(qtbot, "alpha")

    assert [r["Title"] for r in browser._episode_dicts] == ["Newest"]


def test_search_matches_the_episode_description(qtbot) -> None:
    # The blurb is what people remember when the title is a number.
    browser = _searched(qtbot, "Middle description")

    assert [r["Title"] for r in browser._episode_dicts] == ["Middle"]


def test_search_is_case_insensitive(qtbot) -> None:
    browser = _searched(qtbot, "NEWEST")

    assert len(browser._episode_dicts) == 1


def test_every_search_term_has_to_land(qtbot) -> None:
    # "alpha" alone matches one row; adding a word no row has must narrow it
    # to nothing rather than widening the result.
    browser = _searched(qtbot, "alpha zebra")

    assert browser._episode_dicts == []


def test_a_search_with_no_hits_says_so_rather_than_looking_empty(qtbot) -> None:
    browser = _searched(qtbot, "nothing matches this")

    assert browser._episode_dicts == []
    assert browser._episode_stack.currentIndex() == 1


def test_a_search_that_hid_everything_keeps_its_own_way_out(qtbot) -> None:
    browser = _searched(qtbot, "nothing matches this")

    # Hiding the search box along with the rows would strand the user.
    assert not browser._filter_bar.isHidden()

    browser._retry_episode_state()  # the empty state's "Clear Search" button

    assert len(browser._episode_dicts) == 3
    assert browser._filter_bar.query() == ""


def test_clearing_the_search_restores_the_full_list(qtbot) -> None:
    browser = _searched(qtbot, "alpha")

    browser._filter_bar.set_query("", notify=True)

    assert len(browser._episode_dicts) == 3


def test_select_all_cannot_reach_rows_the_search_hid(qtbot) -> None:
    browser = _searched(qtbot, "alpha")

    browser._select_all_visible()

    assert len(browser._episode_list.selected_rows()) == 1


def test_a_search_survives_a_re_render_of_the_same_view(qtbot) -> None:
    browser = _searched(qtbot, "alpha")

    # Reconciliation and RSS refreshes redraw the view constantly; wiping the
    # filter under the user mid-type would be maddening.
    browser._refresh_current_view()

    assert [r["Title"] for r in browser._episode_dicts] == ["Newest"]


# ── The summary line ────────────────────────────────────────────────────────


def test_the_summary_counts_episodes_shows_and_bytes(qtbot) -> None:
    browser = _sortable(qtbot)

    browser._show_on_ipod_episodes()

    text = browser._filter_bar.summary()
    assert "3 episodes" in text
    assert "3 shows" in text


def test_one_episode_reads_in_the_singular(qtbot) -> None:
    browser = _browser(qtbot, [_track(1, "Only", "Show")])

    browser._show_on_ipod_episodes()

    assert "1 episode" in browser._filter_bar.summary()
    assert "1 episodes" not in browser._filter_bar.summary()


def test_the_summary_shows_the_filtered_share_while_searching(qtbot) -> None:
    browser = _searched(qtbot, "alpha")

    assert "1 of 3 episodes" in browser._filter_bar.summary()


# ── Removal reaches the sync plan ───────────────────────────────────────────


def test_an_orphan_can_be_removed_like_any_other_episode(qtbot) -> None:
    browser = _browser(qtbot, [_track(99, "Orphan", "Some Show")])
    browser._ipod_path = "/Volumes/iPod"
    browser._show_on_ipod_episodes()
    plans: list = []
    browser.podcast_sync_requested.connect(plans.append)

    browser._select_all_visible()
    browser._on_remove_episode_selection()

    assert len(plans) == 1
    assert [item.db_track_id for item in plans[0].to_remove] == [99]


def test_a_batch_removal_covers_every_selected_episode(qtbot) -> None:
    browser = _browser(
        qtbot,
        [
            _track(1, "One", "Show A"),
            _track(2, "Two", "Show A"),
            _track(3, "Three", "Show B"),
        ],
    )
    browser._ipod_path = "/Volumes/iPod"
    browser._show_on_ipod_episodes()
    plans: list = []
    browser.podcast_sync_requested.connect(plans.append)

    browser._select_all_visible()
    browser._on_remove_episode_selection()

    assert sorted(item.db_track_id for item in plans[0].to_remove) == [1, 2, 3]


def test_removal_reports_the_sync_review_handoff(qtbot) -> None:
    browser = _browser(qtbot, [_track(1, "One", "Show A")])
    browser._ipod_path = "/Volumes/iPod"
    browser._show_on_ipod_episodes()

    browser._select_all_visible()
    browser._on_remove_episode_selection()

    # Sync Review is the confirmation step, so the toast names it.
    assert browser._action_status.text() == "1 removal sent to Sync Review"


# ── Synthetic feeds never reach the subscription store ──────────────────────


def test_orphan_feeds_are_recognisable_as_synthetic(qtbot) -> None:
    browser = _browser(qtbot, [_track(99, "Orphan", "Some Show")])
    browser._show_on_ipod_episodes()

    feeds = list(browser._episode_feed_by_key.values())

    assert feeds and all(_is_synthetic_feed(feed) for feed in feeds)


def test_a_real_feed_is_not_synthetic() -> None:
    assert not _is_synthetic_feed(PodcastFeed(feed_url="https://example.com/f.xml"))


def test_marking_an_orphan_listened_does_not_invent_a_subscription(qtbot) -> None:
    browser = _browser(qtbot, [_track(99, "Orphan", "Some Show")])
    browser._show_on_ipod_episodes()
    store = cast(_FakeStore, browser._store)

    refs = [browser._episode_ref_at_row(0)]
    browser._set_listened_refs([ref for ref in refs if ref], True)

    assert store.updated == []


def test_marking_a_subscribed_episode_listened_still_saves(qtbot) -> None:
    feed = _subscribed_feed()
    browser = _browser(qtbot, [_track(11, "Device Title", "Example Show")], feeds=[feed])
    browser._show_on_ipod_episodes()
    store = cast(_FakeStore, browser._store)

    refs = [browser._episode_ref_at_row(0)]
    browser._set_listened_refs([ref for ref in refs if ref], True)

    assert store.updated == [[feed]]


def test_removing_an_orphan_download_does_not_invent_a_subscription(qtbot) -> None:
    browser = _browser(qtbot, [_track(99, "Orphan", "Some Show")])
    browser._show_on_ipod_episodes()
    store = cast(_FakeStore, browser._store)
    ref = browser._episode_ref_at_row(0)
    assert ref is not None
    ref[1].status = STATUS_NOT_DOWNLOADED

    browser._remove_download_refs([ref])

    assert store.updated == []


# ── Sidebar ─────────────────────────────────────────────────────────────────


def _sidebar_keys(browser: PodcastBrowser) -> list:
    return [
        browser._feed_list.item(row).data(Qt.ItemDataRole.UserRole)
        for row in range(browser._feed_list.count())
    ]


def test_the_sidebar_lists_library_rows_above_the_shows(qtbot) -> None:
    browser = _browser(qtbot, [], feeds=[_subscribed_feed()])

    browser._refresh_feed_list()

    assert _sidebar_keys(browser) == [
        "",  # Library
        _COMBINED_FEED_KEY,
        _ON_IPOD_KEY,
        "",  # Shows
        "https://example.com/feed.xml",
    ]


def test_section_headers_cannot_be_selected(qtbot) -> None:
    browser = _browser(qtbot, [], feeds=[_subscribed_feed()])

    browser._refresh_feed_list()

    for row in (0, 3):
        item = browser._feed_list.item(row)
        assert item.flags() == Qt.ItemFlag.NoItemFlags


def test_a_feed_is_found_by_key_not_by_position(qtbot) -> None:
    browser = _browser(qtbot, [], feeds=[_subscribed_feed()])

    browser._refresh_feed_list()

    assert browser._row_for_key("https://example.com/feed.xml") == 4
    assert browser._row_for_key(_ON_IPOD_KEY) == 2
    assert browser._row_for_key("not a key") == -1


def test_the_on_ipod_row_carries_the_device_count(qtbot) -> None:
    browser = _browser(
        qtbot,
        [_track(1, "One", "Show A"), _track(2, "Two", "Show B")],
        feeds=[_subscribed_feed()],
    )

    browser._refresh_feed_list()

    assert "(2)" in browser._feed_list.item(2).text()


def test_the_count_falls_back_to_the_store_while_loading(qtbot) -> None:
    browser = _browser(qtbot, [], ready=False, feeds=[_subscribed_feed()])

    # The subscription store knows one episode is on the iPod.
    assert browser._on_ipod_episode_count() == 1


def test_podcasts_on_the_ipod_keep_the_page_reachable_without_subscriptions(
    qtbot,
) -> None:
    # Podcasts put on the iPod by iTunes must not be stranded behind the
    # full-page "no subscriptions" pitch.
    browser = _browser(qtbot, [_track(1, "Orphan", "Some Show")], feeds=[])

    browser._refresh_feed_list()

    assert browser._stack.currentIndex() == 1
    assert browser._row_for_key(_ON_IPOD_KEY) >= 0


def test_the_shows_header_is_omitted_when_there_are_no_subscriptions(qtbot) -> None:
    browser = _browser(qtbot, [_track(1, "Orphan", "Some Show")], feeds=[])

    browser._refresh_feed_list()

    labels = [
        browser._feed_list.item(row).text()
        for row in range(browser._feed_list.count())
    ]
    assert "Shows" not in labels


def test_without_subscriptions_the_on_ipod_view_opens_by_default(qtbot) -> None:
    browser = _browser(qtbot, [_track(1, "Orphan", "Some Show")], feeds=[])

    browser._refresh_feed_list()

    # Feed would be empty, so landing there would look like a broken page.
    assert browser._showing_on_ipod
    assert [r["Title"] for r in browser._episode_dicts] == ["Orphan"]


def test_an_empty_iPod_with_no_subscriptions_still_pitches_subscribing(qtbot) -> None:
    browser = _browser(qtbot, [], feeds=[])

    browser._refresh_feed_list()

    assert browser._stack.currentIndex() == 0


def test_a_stale_search_does_not_survive_a_device_change(qtbot) -> None:
    browser = _browser(qtbot, [_track(1, "Orphan", "Some Show")])
    browser._show_on_ipod_episodes()
    browser._filter_bar.set_query("nothing matches", notify=True)

    browser.clear()

    assert browser._episode_query == ""
    assert browser._filter_bar.query() == ""
    assert browser._library_header.isHidden()


def test_selecting_the_on_ipod_row_opens_the_view(qtbot) -> None:
    browser = _browser(qtbot, [_track(1, "One", "Show A")], feeds=[_subscribed_feed()])
    browser._refresh_feed_list()

    browser._feed_list.setCurrentRow(browser._row_for_key(_ON_IPOD_KEY))

    assert browser._showing_on_ipod
    assert not browser._library_header.isHidden()
    assert [r["Title"] for r in browser._episode_dicts] == ["One"]


def test_leaving_the_view_hides_its_header(qtbot) -> None:
    browser = _browser(qtbot, [_track(1, "One", "Show A")], feeds=[_subscribed_feed()])
    browser._refresh_feed_list()
    browser._feed_list.setCurrentRow(browser._row_for_key(_ON_IPOD_KEY))

    browser._feed_list.setCurrentRow(browser._row_for_key(_COMBINED_FEED_KEY))

    assert browser._library_header.isHidden()
    assert not browser._showing_on_ipod


# ── One re-render path for every view ───────────────────────────────────────


def test_refreshing_redraws_whichever_view_is_open(qtbot) -> None:
    browser = _browser(qtbot, [_track(1, "One", "Show A")], feeds=[_subscribed_feed()])
    browser._refresh_feed_list()
    browser._feed_list.setCurrentRow(browser._row_for_key(_ON_IPOD_KEY))

    cache = _FakeCache([_track(1, "One", "Show A"), _track(2, "Two", "Show B")])
    browser._library_cache = cache  # type: ignore[assignment]
    browser._refresh_current_view()

    assert len(browser._episode_dicts) == 2
