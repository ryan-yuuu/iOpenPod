"""Click-to-toggle episode selection and the batch confirm bar."""

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
    _PODCAST_EPISODE_COLUMNS,
    PodcastBrowser,
    _PodcastEpisodeCard,
)

NO_MODIFIER = Qt.KeyboardModifier.NoModifier
SHIFT = Qt.KeyboardModifier.ShiftModifier


def _browser(qtbot) -> PodcastBrowser:
    browser = PodcastBrowser(
        cast(SettingsService, SimpleNamespace()),
        cast(DeviceSessionService, SimpleNamespace()),
        cast(LibraryService, SimpleNamespace(cache=lambda: object())),
    )
    qtbot.addWidget(browser)
    return browser


def _with_rows(browser: PodcastBrowser, count: int = 4) -> None:
    rows = [
        {
            "Title": f"Episode {index + 1}",
            "ep_status": "",
            "ep_guid": f"guid-{index}",
            "_can_add_to_ipod": True,
            "_can_remove_from_ipod": False,
        }
        for index in range(count)
    ]
    browser._episode_list.set_rows(rows, _PODCAST_EPISODE_COLUMNS)


def _click(browser: PodcastBrowser, row: int, modifier=NO_MODIFIER) -> None:
    browser._episode_list._on_card_clicked(row, modifier)


# ── Click toggles selection, no modifier needed ─────────────────────────────


def test_plain_click_selects_a_row(qtbot) -> None:
    browser = _browser(qtbot)
    _with_rows(browser)

    _click(browser, 0)

    assert browser._episode_list.selected_rows() == [0]


def test_plain_click_on_a_second_row_adds_it(qtbot) -> None:
    browser = _browser(qtbot)
    _with_rows(browser)

    _click(browser, 0)
    _click(browser, 2)

    # No modifier held — this is the whole point of the change.
    assert browser._episode_list.selected_rows() == [0, 2]


def test_plain_click_on_a_selected_row_deselects_it(qtbot) -> None:
    browser = _browser(qtbot)
    _with_rows(browser)

    _click(browser, 1)
    _click(browser, 1)

    assert browser._episode_list.selected_rows() == []


def test_shift_click_still_selects_a_range(qtbot) -> None:
    browser = _browser(qtbot)
    _with_rows(browser)

    _click(browser, 0)
    _click(browser, 3, SHIFT)

    assert browser._episode_list.selected_rows() == [0, 1, 2, 3]


def test_clear_selection_empties_it(qtbot) -> None:
    browser = _browser(qtbot)
    _with_rows(browser)
    _click(browser, 0)
    _click(browser, 1)

    browser._episode_list.clear_selection()

    assert browser._episode_list.selected_rows() == []


# ── The per-card add button is gone ─────────────────────────────────────────


def test_episode_card_has_no_add_button(qtbot) -> None:
    card = _PodcastEpisodeCard()
    qtbot.addWidget(card)

    # Selection plus the confirm bar is now the only way to add episodes.
    assert not hasattr(card, "_add_btn")


def test_episode_card_no_longer_emits_add_requested(qtbot) -> None:
    card = _PodcastEpisodeCard()
    qtbot.addWidget(card)

    assert not hasattr(card, "add_requested")


# ── Confirm bar ─────────────────────────────────────────────────────────────


def test_confirm_bar_is_hidden_with_no_selection(qtbot) -> None:
    browser = _browser(qtbot)
    _with_rows(browser)

    browser._refresh_episode_selection_bar()

    assert browser._selection_bar.isHidden()


def test_confirm_bar_appears_once_something_is_selected(qtbot) -> None:
    browser = _browser(qtbot)
    _with_rows(browser)

    _click(browser, 0)

    assert not browser._selection_bar.isHidden()


def test_confirm_bar_reports_a_singular_count(qtbot) -> None:
    browser = _browser(qtbot)
    _with_rows(browser)

    _click(browser, 0)

    assert browser._selection_count_label.text() == "1 episode selected"


def test_confirm_bar_reports_a_plural_count(qtbot) -> None:
    browser = _browser(qtbot)
    _with_rows(browser)

    _click(browser, 0)
    _click(browser, 1)
    _click(browser, 2)

    assert browser._selection_count_label.text() == "3 episodes selected"


def test_confirm_bar_hides_again_when_selection_is_emptied(qtbot) -> None:
    browser = _browser(qtbot)
    _with_rows(browser)
    _click(browser, 0)

    _click(browser, 0)

    assert browser._selection_bar.isHidden()


def test_confirm_bar_follows_a_programmatic_select_row(qtbot) -> None:
    # The right-click path clears then selects a single row without a click.
    browser = _browser(qtbot)
    _with_rows(browser)

    browser._episode_list.select_row(2)

    assert browser._episode_list.selected_rows() == [2]
    assert not browser._selection_bar.isHidden()
    assert browser._selection_count_label.text() == "1 episode selected"


def test_clear_button_empties_the_selection(qtbot) -> None:
    browser = _browser(qtbot)
    _with_rows(browser)
    _click(browser, 0)
    _click(browser, 1)

    browser._selection_clear_btn.click()

    assert browser._episode_list.selected_rows() == []
    assert browser._selection_bar.isHidden()


# ── Applying the batch ──────────────────────────────────────────────────────


def test_apply_sends_every_selected_episode(qtbot) -> None:
    browser = _browser(qtbot)
    _with_rows(browser)
    sent: list = []
    browser._add_to_ipod_refs = lambda refs: sent.append(list(refs))  # type: ignore[method-assign]
    browser._get_selected_episode_refs = lambda: [  # type: ignore[method-assign]
        (0, object(), object()),
        (2, object(), object()),
    ]

    _click(browser, 0)
    browser._selection_apply_btn.click()

    assert len(sent) == 1
    assert len(sent[0]) == 2


def test_apply_does_nothing_without_a_selection(qtbot) -> None:
    browser = _browser(qtbot)
    _with_rows(browser)
    sent: list = []
    browser._add_to_ipod_refs = lambda refs: sent.append(list(refs))  # type: ignore[method-assign]

    browser._on_apply_episode_selection()

    assert sent == []


def test_apply_button_carries_the_count(qtbot) -> None:
    browser = _browser(qtbot)
    _with_rows(browser)

    _click(browser, 0)
    _click(browser, 1)

    assert browser._selection_apply_btn.text() == "Add 2 to iPod"


def test_apply_button_is_singular_for_one_episode(qtbot) -> None:
    browser = _browser(qtbot)
    _with_rows(browser)

    _click(browser, 0)

    assert browser._selection_apply_btn.text() == "Add 1 to iPod"


# ── Style conventions ───────────────────────────────────────────────────────


def test_selection_bar_buttons_are_themed(qtbot) -> None:
    browser = _browser(qtbot)

    assert browser._selection_apply_btn.styleSheet()
    assert browser._selection_clear_btn.styleSheet()
