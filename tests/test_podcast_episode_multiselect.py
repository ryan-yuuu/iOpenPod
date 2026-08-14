"""Checkbox episode selection and the contextual batch action bar."""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QKeyEvent

from iopenpod.application.services import (
    DeviceSessionService,
    LibraryService,
    SettingsService,
)
from iopenpod.gui.styles import paint_css
from iopenpod.gui.widgets.podcastBrowser import (
    _PODCAST_EPISODE_COLUMNS,
    PodcastBrowser,
    _PodcastEpisodeCard,
)

NO_MODIFIER = Qt.KeyboardModifier.NoModifier
SHIFT = Qt.KeyboardModifier.ShiftModifier
CMD = Qt.KeyboardModifier.ControlModifier


def _browser(qtbot) -> PodcastBrowser:
    browser = PodcastBrowser(
        cast(SettingsService, SimpleNamespace()),
        cast(DeviceSessionService, SimpleNamespace()),
        cast(LibraryService, SimpleNamespace(cache=lambda: object())),
    )
    qtbot.addWidget(browser)
    return browser


def _row(index: int, *, addable: bool = True, removable: bool = False) -> dict:
    return {
        "Title": f"Episode {index + 1}",
        "ep_status": "",
        "ep_guid": f"guid-{index}",
        "_ep_key": f"key-{index}",
        "_can_add_to_ipod": addable,
        "_can_remove_from_ipod": removable,
    }


def _with_rows(browser: PodcastBrowser, count: int = 4) -> None:
    rows = [_row(index) for index in range(count)]
    browser._episode_dicts = rows
    browser._episode_list.set_rows(rows, _PODCAST_EPISODE_COLUMNS)


def _click(browser: PodcastBrowser, row: int, modifier=NO_MODIFIER) -> None:
    browser._episode_list._on_card_clicked(row, modifier)


def _check(browser: PodcastBrowser, row: int, checked: bool = True) -> None:
    browser._episode_list._on_card_check_toggled(row, checked)


# ── Card body click follows platform convention ─────────────────────────────


def test_plain_click_selects_a_row(qtbot) -> None:
    browser = _browser(qtbot)
    _with_rows(browser)

    _click(browser, 0)

    assert browser._episode_list.selected_rows() == [0]


def test_plain_click_replaces_rather_than_accumulates(qtbot) -> None:
    browser = _browser(qtbot)
    _with_rows(browser)

    _click(browser, 0)
    _click(browser, 2)

    # An idle click must never quietly grow a batch that Remove can act on.
    assert browser._episode_list.selected_rows() == [2]


def test_cmd_click_toggles_a_row(qtbot) -> None:
    browser = _browser(qtbot)
    _with_rows(browser)

    _click(browser, 0)
    _click(browser, 2, CMD)

    assert browser._episode_list.selected_rows() == [0, 2]

    _click(browser, 2, CMD)

    assert browser._episode_list.selected_rows() == [0]


def test_shift_click_selects_a_range(qtbot) -> None:
    browser = _browser(qtbot)
    _with_rows(browser)

    _click(browser, 0)
    _click(browser, 3, SHIFT)

    assert browser._episode_list.selected_rows() == [0, 1, 2, 3]


def test_shift_click_extends_upward_from_the_anchor(qtbot) -> None:
    browser = _browser(qtbot)
    _with_rows(browser)

    _click(browser, 3)
    _click(browser, 1, SHIFT)

    # Anchored on the last clicked row, not on min(selection).
    assert browser._episode_list.selected_rows() == [1, 2, 3]


def test_shift_click_reanchors_after_a_plain_click(qtbot) -> None:
    browser = _browser(qtbot)
    _with_rows(browser)

    _click(browser, 0)
    _click(browser, 3, SHIFT)
    _click(browser, 2)
    _click(browser, 3, SHIFT)

    assert browser._episode_list.selected_rows() == [2, 3]


def test_clear_selection_empties_it(qtbot) -> None:
    browser = _browser(qtbot)
    _with_rows(browser)
    _click(browser, 0)
    _click(browser, 1, CMD)

    browser._episode_list.clear_selection()

    assert browser._episode_list.selected_rows() == []


# ── The checkbox builds batches without modifiers ───────────────────────────


def test_checkbox_adds_a_row_without_disturbing_the_rest(qtbot) -> None:
    browser = _browser(qtbot)
    _with_rows(browser)

    _check(browser, 0)
    _check(browser, 2)

    assert browser._episode_list.selected_rows() == [0, 2]


def test_unchecking_removes_only_that_row(qtbot) -> None:
    browser = _browser(qtbot)
    _with_rows(browser)
    _check(browser, 0)
    _check(browser, 1)

    _check(browser, 0, False)

    assert browser._episode_list.selected_rows() == [1]


def test_shift_click_anchors_on_the_last_checked_row(qtbot) -> None:
    browser = _browser(qtbot)
    _with_rows(browser)

    _check(browser, 1)
    _click(browser, 3, SHIFT)

    assert browser._episode_list.selected_rows() == [1, 2, 3]


# ── Selection survives the list being rebuilt ───────────────────────────────


def test_selection_follows_episodes_across_a_resort(qtbot) -> None:
    browser = _browser(qtbot)
    rows = [_row(index) for index in range(4)]
    browser._episode_list.set_rows(rows, _PODCAST_EPISODE_COLUMNS)
    _check(browser, 0)

    # Same episodes, reversed order: the selection must track key-0, not row 0.
    browser._episode_list.set_rows(list(reversed(rows)), _PODCAST_EPISODE_COLUMNS)

    assert browser._episode_list.selected_keys() == {"key-0"}
    assert browser._episode_list.selected_rows() == [3]


def test_selection_drops_episodes_that_are_gone(qtbot) -> None:
    browser = _browser(qtbot)
    rows = [_row(index) for index in range(4)]
    browser._episode_list.set_rows(rows, _PODCAST_EPISODE_COLUMNS)
    _check(browser, 3)

    browser._episode_list.set_rows(rows[:2], _PODCAST_EPISODE_COLUMNS)

    assert browser._episode_list.selected_rows() == []


# ── Select all ──────────────────────────────────────────────────────────────


def test_select_all_takes_every_listed_row(qtbot) -> None:
    browser = _browser(qtbot)
    _with_rows(browser)

    browser._select_all_visible()

    assert browser._episode_list.selected_rows() == [0, 1, 2, 3]


def test_select_all_is_scoped_to_the_listed_rows(qtbot) -> None:
    # A filtered list must not let "select all" reach the rows it hid.
    browser = _browser(qtbot)
    rows = [_row(index) for index in range(4)]
    browser._episode_list.set_rows(rows[:2], _PODCAST_EPISODE_COLUMNS)

    browser._select_all_visible()

    assert browser._episode_list.selected_keys() == {"key-0", "key-1"}


def test_ctrl_a_selects_everything_listed(qtbot) -> None:
    browser = _browser(qtbot)
    _with_rows(browser)

    browser._episode_list.keyPressEvent(
        QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_A, CMD)
    )

    assert browser._episode_list.selected_rows() == [0, 1, 2, 3]


def test_escape_clears_the_selection(qtbot) -> None:
    browser = _browser(qtbot)
    _with_rows(browser)
    _check(browser, 0)

    browser._episode_list.keyPressEvent(
        QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_Escape, NO_MODIFIER)
    )

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

    _check(browser, 0)
    _check(browser, 1)
    _check(browser, 2)

    assert browser._selection_count_label.text() == "3 episodes selected"


def test_confirm_bar_hides_again_when_selection_is_emptied(qtbot) -> None:
    browser = _browser(qtbot)
    _with_rows(browser)
    _check(browser, 0)

    _check(browser, 0, False)

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

    _check(browser, 0)
    _check(browser, 1)

    assert browser._selection_apply_btn.text() == "Add 2 to iPod"


def test_apply_button_is_singular_for_one_episode(qtbot) -> None:
    browser = _browser(qtbot)
    _with_rows(browser)

    _click(browser, 0)

    assert browser._selection_apply_btn.text() == "Add 1 to iPod"


# ── The bar offers only what the selection allows ───────────────────────────


def _with_mixed_rows(browser: PodcastBrowser) -> None:
    rows = [
        _row(0, addable=True, removable=False),
        _row(1, addable=True, removable=False),
        _row(2, addable=False, removable=True),
    ]
    browser._episode_dicts = rows
    browser._episode_list.set_rows(rows, _PODCAST_EPISODE_COLUMNS)


def test_remove_button_is_hidden_when_nothing_is_on_the_ipod(qtbot) -> None:
    browser = _browser(qtbot)
    _with_rows(browser)

    _check(browser, 0)

    assert browser._selection_remove_btn.isHidden()
    assert not browser._selection_apply_btn.isHidden()


def test_add_button_is_hidden_when_nothing_can_be_added(qtbot) -> None:
    browser = _browser(qtbot)
    _with_mixed_rows(browser)

    _check(browser, 2)

    assert browser._selection_apply_btn.isHidden()
    assert not browser._selection_remove_btn.isHidden()
    assert browser._selection_remove_btn.text() == "Remove 1 from iPod"


def test_a_mixed_selection_shows_both_counts(qtbot) -> None:
    browser = _browser(qtbot)
    _with_mixed_rows(browser)

    browser._select_all_visible()

    assert browser._selection_count_label.text() == "3 episodes selected"
    assert browser._selection_apply_btn.text() == "Add 2 to iPod"
    assert browser._selection_remove_btn.text() == "Remove 1 from iPod"


def test_remove_sends_only_the_on_ipod_episodes(qtbot) -> None:
    browser = _browser(qtbot)
    _with_mixed_rows(browser)
    sent: list = []
    browser._remove_from_ipod_refs = lambda refs: sent.append(list(refs))  # type: ignore[method-assign]
    on_ipod = SimpleNamespace(status="on_ipod", ipod_db_track_id=7)
    browser._get_selected_episode_refs = lambda: [  # type: ignore[method-assign]
        (0, SimpleNamespace(status="not_downloaded", ipod_db_track_id=0), object()),
        (2, on_ipod, object()),
    ]

    browser._on_remove_episode_selection()

    assert len(sent) == 1
    assert [ref[1] for ref in sent[0]] == [on_ipod]


def test_remove_reports_when_nothing_is_removable(qtbot) -> None:
    browser = _browser(qtbot)
    _with_rows(browser)
    browser._get_selected_episode_refs = lambda: [  # type: ignore[method-assign]
        (0, SimpleNamespace(status="downloaded", ipod_db_track_id=0), object()),
    ]

    browser._on_remove_episode_selection()

    assert browser._action_status.text() == "No selected episode is on the iPod"


# ── Master checkbox ─────────────────────────────────────────────────────────


def test_master_is_partially_checked_for_a_subset(qtbot) -> None:
    browser = _browser(qtbot)
    _with_rows(browser)

    _check(browser, 0)

    assert browser._selection_master.checkState() == Qt.CheckState.PartiallyChecked


def test_master_is_checked_once_everything_is_selected(qtbot) -> None:
    browser = _browser(qtbot)
    _with_rows(browser)

    browser._select_all_visible()

    assert browser._selection_master.checkState() == Qt.CheckState.Checked


def test_clicking_a_partial_master_selects_everything(qtbot) -> None:
    browser = _browser(qtbot)
    _with_rows(browser)
    _check(browser, 0)

    browser._on_selection_master_clicked()

    assert browser._episode_list.selected_rows() == [0, 1, 2, 3]


def test_clicking_a_full_master_clears_everything(qtbot) -> None:
    browser = _browser(qtbot)
    _with_rows(browser)
    browser._select_all_visible()

    browser._on_selection_master_clicked()

    assert browser._episode_list.selected_rows() == []


# ── Style conventions ───────────────────────────────────────────────────────


def test_selection_bar_buttons_are_themed(qtbot) -> None:
    browser = _browser(qtbot)

    assert browser._selection_apply_btn.styleSheet()
    assert browser._selection_clear_btn.styleSheet()
    assert browser._selection_remove_btn.styleSheet()


def test_remove_is_styled_as_the_destructive_action(qtbot) -> None:
    browser = _browser(qtbot)

    assert paint_css("status.danger.text") in browser._selection_remove_btn.styleSheet()


def test_remove_is_never_the_default_button(qtbot) -> None:
    # Return must not be able to reach a destructive action.
    browser = _browser(qtbot)

    assert not browser._selection_remove_btn.isDefault()
    assert not browser._selection_remove_btn.autoDefault()


def test_remove_sits_left_of_the_primary_action(qtbot) -> None:
    browser = _browser(qtbot)
    layout = browser._selection_bar.layout()

    order = [layout.itemAt(i).widget() for i in range(layout.count())]

    assert order.index(browser._selection_remove_btn) < order.index(
        browser._selection_apply_btn
    )


# ── The card's box says what the row's state is ─────────────────────────────


def test_a_selected_row_shows_a_ticked_box(qtbot) -> None:
    """The box and the row's selection are the same fact, shown twice.

    The checkbox paints rely on this: an empty box is never drawn on the
    accent-tinted fill of a selected card, so only the ticked and hovered
    states are held to a contrast floor against that tint.
    """

    browser = _browser(qtbot)
    _with_rows(browser)
    _click(browser, 1)

    browser._episode_list.schedule_viewport_refresh(force=True)
    browser._episode_list._refresh_viewport()
    card = browser._episode_list._visible_widgets.get(1)

    assert card is not None
    assert card._selected
    assert card._check.isChecked()
