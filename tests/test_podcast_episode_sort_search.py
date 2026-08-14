"""Sorting and searching episodes, in every view that lists them."""

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
    _SORT_LONGEST,
    _SORT_NEWEST,
    _SORT_SHORTEST,
    _SORT_SHOW,
    _SORT_TITLE,
    _SORT_UNPLAYED,
    _VIEW_FEED,
    _VIEW_ON_IPOD,
    _VIEW_SHOW,
    PodcastBrowser,
)
from iopenpod.podcasts.models import (
    STATUS_NOT_DOWNLOADED,
    PodcastEpisode,
    PodcastFeed,
)


class _FakeCache:
    def __init__(self, tracks: list[dict] | None = None) -> None:
        self._tracks = tracks or []

    def is_ready(self) -> bool:
        return True

    def get_tracks(self) -> list[dict]:
        return self._tracks


class _FakeStore:
    def __init__(self, feeds: list[PodcastFeed]) -> None:
        self._feeds = feeds
        self.podcast_dir = ""

    def get_feeds(self) -> list[PodcastFeed]:
        return list(self._feeds)

    def get_feed(self, feed_url: str) -> PodcastFeed | None:
        return next((f for f in self._feeds if f.feed_url == feed_url), None)

    def update_feeds(self, feeds: list[PodcastFeed]) -> int:
        return len(feeds)

    def update_feed(self, feed: PodcastFeed) -> None:
        return None


def _episode(
    guid: str,
    title: str,
    *,
    pub_date: float = 0.0,
    duration: int = 0,
    description: str = "",
    play_count: int = 0,
) -> PodcastEpisode:
    return PodcastEpisode(
        guid=guid,
        title=title,
        description=description,
        pub_date=pub_date,
        duration_seconds=duration,
        status=STATUS_NOT_DOWNLOADED,
        play_count=play_count,
    )


def _browser(qtbot, feeds: list[PodcastFeed]) -> PodcastBrowser:
    browser = PodcastBrowser(
        cast(SettingsService, SimpleNamespace()),
        cast(DeviceSessionService, SimpleNamespace()),
        cast(LibraryService, SimpleNamespace(cache=lambda: _FakeCache())),
    )
    qtbot.addWidget(browser)
    browser._store = _FakeStore(feeds)
    return browser


def _show_feed() -> PodcastFeed:
    """One show whose episodes differ in date, length, and play history."""
    return PodcastFeed(
        feed_url="https://example.com/feed.xml",
        title="Example Show",
        episodes=[
            _episode(
                "b",
                "Beta",
                pub_date=200,
                duration=1800,
                description="A talk about the mars rover",
            ),
            _episode("c", "Gamma", pub_date=100, duration=600, play_count=2),
            _episode("a", "Alpha", pub_date=300, duration=3600),
        ],
    )


def _titles(browser: PodcastBrowser) -> list[str]:
    return [row["Title"] for row in browser._episode_dicts]


def _open_show(qtbot) -> PodcastBrowser:
    feed = _show_feed()
    browser = _browser(qtbot, [feed])
    browser._selected_feed = feed
    browser._show_episodes(feed)
    return browser


# ── A show sorts by more than publication date ──────────────────────────────


def test_a_show_opens_newest_first(qtbot) -> None:
    browser = _open_show(qtbot)

    assert _titles(browser) == ["Alpha", "Beta", "Gamma"]


def test_longest_first_finds_the_deep_dive(qtbot) -> None:
    browser = _open_show(qtbot)
    browser._sort_by_view[_VIEW_SHOW] = _SORT_LONGEST

    browser._present_episode_rows()

    assert _titles(browser) == ["Alpha", "Beta", "Gamma"]


def test_shortest_first_finds_something_for_a_short_commute(qtbot) -> None:
    browser = _open_show(qtbot)
    browser._sort_by_view[_VIEW_SHOW] = _SORT_SHORTEST

    browser._present_episode_rows()

    assert _titles(browser) == ["Gamma", "Beta", "Alpha"]


def test_an_episode_with_no_published_duration_sorts_last_not_first(qtbot) -> None:
    feed = PodcastFeed(
        feed_url="https://example.com/feed.xml",
        title="Example Show",
        episodes=[
            _episode("unknown", "Unknown Length", pub_date=100),
            _episode("short", "Short", pub_date=200, duration=300),
        ],
    )
    browser = _browser(qtbot, [feed])
    browser._selected_feed = feed
    browser._show_episodes(feed)
    browser._sort_by_view[_VIEW_SHOW] = _SORT_SHORTEST

    browser._present_episode_rows()

    # A missing duration is unknown, not zero: leading with it would claim
    # the shortest episode is the one nobody knows the length of.
    assert _titles(browser) == ["Short", "Unknown Length"]


def test_unplayed_first_puts_what_is_left_at_the_top(qtbot) -> None:
    browser = _open_show(qtbot)
    browser._sort_by_view[_VIEW_SHOW] = _SORT_UNPLAYED

    browser._present_episode_rows()

    # Gamma has been played, so it drops below the two that have not.
    assert _titles(browser) == ["Alpha", "Beta", "Gamma"]
    assert browser._episode_dicts[-1]["Title"] == "Gamma"


def test_title_order_is_alphabetical(qtbot) -> None:
    browser = _open_show(qtbot)
    browser._sort_by_view[_VIEW_SHOW] = _SORT_TITLE

    browser._present_episode_rows()

    assert _titles(browser) == ["Alpha", "Beta", "Gamma"]


def test_a_show_cannot_be_sorted_by_show(qtbot) -> None:
    browser = _open_show(qtbot)

    keys = {key for _label, key in browser._filter_bar.sort_options()}

    # Grouping one show by show name would be an option that does nothing.
    assert _SORT_SHOW not in keys


# ── Searching a show ────────────────────────────────────────────────────────


def test_a_show_search_matches_the_description(qtbot) -> None:
    browser = _open_show(qtbot)

    browser._filter_bar.set_query("mars rover", notify=True)

    assert _titles(browser) == ["Beta"]


def test_a_show_search_reports_what_it_held_back(qtbot) -> None:
    browser = _open_show(qtbot)

    browser._filter_bar.set_query("alpha", notify=True)

    assert "1 of 3 episodes" in browser._filter_bar.summary()


def test_a_show_with_no_episodes_offers_no_controls(qtbot) -> None:
    feed = PodcastFeed(feed_url="https://example.com/feed.xml", title="Empty Show")
    browser = _browser(qtbot, [feed])
    browser._selected_feed = feed

    browser._show_episodes(feed)

    assert browser._filter_bar.isHidden()


# ── The combined feed ───────────────────────────────────────────────────────


def _two_shows() -> list[PodcastFeed]:
    return [
        PodcastFeed(
            feed_url="https://example.com/zeta.xml",
            title="Zeta Show",
            episodes=[_episode("z1", "Zeta One", pub_date=300)],
        ),
        PodcastFeed(
            feed_url="https://example.com/alpha.xml",
            title="Alpha Show",
            episodes=[_episode("a1", "Alpha One", pub_date=100)],
        ),
    ]


def test_the_combined_feed_opens_newest_first(qtbot) -> None:
    browser = _browser(qtbot, _two_shows())

    browser._show_combined_feed()

    assert _titles(browser) == ["Zeta One", "Alpha One"]


def test_the_combined_feed_can_group_by_show(qtbot) -> None:
    browser = _browser(qtbot, _two_shows())
    browser._show_combined_feed()
    browser._sort_by_view[_VIEW_FEED] = _SORT_SHOW

    browser._present_episode_rows()

    assert _titles(browser) == ["Alpha One", "Zeta One"]


def test_the_combined_feed_search_narrows_to_one_show(qtbot) -> None:
    browser = _browser(qtbot, _two_shows())
    browser._show_combined_feed()

    browser._filter_bar.set_query("zeta show", notify=True)

    assert _titles(browser) == ["Zeta One"]


# ── The controls follow the view ────────────────────────────────────────────


def _with_device(qtbot) -> PodcastBrowser:
    browser = PodcastBrowser(
        cast(SettingsService, SimpleNamespace()),
        cast(DeviceSessionService, SimpleNamespace()),
        cast(
            LibraryService,
            SimpleNamespace(cache=lambda: _FakeCache([])),
        ),
    )
    qtbot.addWidget(browser)
    browser._store = _FakeStore([_show_feed()])
    # Opening a show would otherwise kick off a real RSS fetch.
    browser._session_refreshed = {"https://example.com/feed.xml"}
    browser._refresh_feed_list()
    return browser


def _select(browser: PodcastBrowser, key: str) -> None:
    browser._feed_list.setCurrentRow(browser._row_for_key(key))


def test_the_sort_options_change_with_the_view(qtbot) -> None:
    browser = _with_device(qtbot)

    _select(browser, _COMBINED_FEED_KEY)
    feed_labels = [label for label, _key in browser._filter_bar.sort_options()]
    _select(browser, _ON_IPOD_KEY)
    on_ipod_labels = [label for label, _key in browser._filter_bar.sort_options()]

    # The date means publication in one view and arrival in the other, so the
    # label has to say which.
    assert "Newest first" in feed_labels
    assert "Recently added" in on_ipod_labels


def test_a_search_does_not_follow_the_user_to_another_view(qtbot) -> None:
    browser = _with_device(qtbot)
    _select(browser, _COMBINED_FEED_KEY)
    browser._filter_bar.set_query("alpha", notify=True)

    _select(browser, "https://example.com/feed.xml")

    # A filter carried into a different list would hide rows for no visible
    # reason, which is how people conclude the app lost their episodes.
    assert browser._episode_query == ""
    assert browser._filter_bar.query() == ""
    assert len(browser._episode_dicts) == 3


def test_the_sort_choice_carries_between_shows(qtbot) -> None:
    browser = _with_device(qtbot)
    _select(browser, "https://example.com/feed.xml")
    browser._filter_bar._sort.setCurrentText("Title A–Z")

    _select(browser, _COMBINED_FEED_KEY)
    _select(browser, "https://example.com/feed.xml")

    # How somebody likes to read an episode list is a habit, not a property of
    # one show.
    assert browser._sort_by_view[_VIEW_SHOW] == _SORT_TITLE
    assert _titles(browser) == ["Alpha", "Beta", "Gamma"]


def test_a_view_that_lacks_the_remembered_sort_falls_back_visibly(qtbot) -> None:
    browser = _with_device(qtbot)
    _select(browser, _COMBINED_FEED_KEY)
    browser._filter_bar._sort.setCurrentText("By show")

    _select(browser, "https://example.com/feed.xml")

    # The combo cannot show "By show" here, so the remembered key must not
    # stay behind and quietly order the list by something unlisted.
    assert browser._sort_by_view[_VIEW_SHOW] == _SORT_NEWEST
    assert browser._filter_bar.sort_key() == _SORT_NEWEST


def test_each_view_keeps_its_own_sort(qtbot) -> None:
    browser = _with_device(qtbot)
    _select(browser, _COMBINED_FEED_KEY)
    browser._filter_bar._sort.setCurrentText("By show")

    _select(browser, _ON_IPOD_KEY)
    _select(browser, _COMBINED_FEED_KEY)

    assert browser._sort_by_view[_VIEW_FEED] == _SORT_SHOW
    assert browser._sort_by_view[_VIEW_ON_IPOD] == _SORT_NEWEST


# ── A selection is about episodes, not row numbers ──────────────────────────


def test_re_sorting_keeps_the_same_episodes_selected(qtbot) -> None:
    browser = _open_show(qtbot)
    browser._episode_list.select_row(0)  # "Alpha", the newest
    browser._sort_by_view[_VIEW_SHOW] = _SORT_TITLE

    browser._present_episode_rows()

    selected = [browser._episode_dicts[row]["Title"]
                for row in browser._episode_list.selected_rows()]
    assert selected == ["Alpha"]


def test_searching_keeps_a_hidden_selection_out_of_the_way(qtbot) -> None:
    browser = _open_show(qtbot)
    browser._episode_list.select_all()

    browser._filter_bar.set_query("alpha", notify=True)

    # Acting on the bar must not reach episodes the search hid.
    assert len(browser._episode_list.selected_rows()) == 1


# ── Keyboard ────────────────────────────────────────────────────────────────


def test_the_find_shortcut_reaches_the_search_box(qtbot) -> None:
    browser = _with_device(qtbot)
    _select(browser, "https://example.com/feed.xml")
    browser.show()
    qtbot.waitExposed(browser)

    browser._focus_episode_search()

    assert browser.focusWidget() is browser._filter_bar._search


def test_the_find_shortcut_does_nothing_where_there_is_nothing_to_search(
    qtbot,
) -> None:
    feed = PodcastFeed(feed_url="https://example.com/feed.xml", title="Empty Show")
    browser = _browser(qtbot, [feed])
    browser._selected_feed = feed
    browser._show_episodes(feed)
    browser.show()
    qtbot.waitExposed(browser)

    browser._focus_episode_search()

    # Focus must not land in a control the view is not offering.
    assert browser.focusWidget() is not browser._filter_bar._search


def test_escape_clears_the_search_before_leaving_it(qtbot) -> None:
    browser = _open_show(qtbot)
    browser._filter_bar.set_query("alpha", notify=True)

    qtbot.keyClick(browser._filter_bar._search, Qt.Key.Key_Escape)

    assert browser._filter_bar.query() == ""
    assert len(browser._episode_dicts) == 3


def test_escape_on_an_empty_box_hands_focus_back_to_the_list(qtbot) -> None:
    browser = _open_show(qtbot)
    browser.show()
    qtbot.waitExposed(browser)
    browser._focus_episode_search()

    qtbot.keyClick(browser._filter_bar._search, Qt.Key.Key_Escape)

    assert browser.focusWidget() is browser._episode_list.table


# ── Typing does not re-read the device ──────────────────────────────────────


def test_searching_re_presents_rows_instead_of_rebuilding_them(qtbot) -> None:
    browser = _open_show(qtbot)
    builds = 0
    original = browser._show_episodes

    def _counting(feed):
        nonlocal builds
        builds += 1
        original(feed)

    browser._show_episodes = _counting  # type: ignore[method-assign]

    browser._filter_bar.set_query("alpha", notify=True)
    browser._filter_bar.set_query("al", notify=True)
    browser._filter_bar.set_query("", notify=True)

    assert builds == 0
    assert len(browser._episode_dicts) == 3
