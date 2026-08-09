"""The 'Find Details…' audiobook action in the sync review footer."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from iopenpod.gui.widgets.syncReview import SyncReviewWidget


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


def _review(qtbot) -> SyncReviewWidget:
    widget = SyncReviewWidget(cast(Any, object()), cast(Any, object()))
    qtbot.addWidget(widget)
    return widget


def _audiobook(tmp_path: Path, name: str = "book.m4b") -> _FakeItem:
    target = tmp_path / name
    target.write_bytes(b"stub")
    return _FakeItem(pc_track=_FakePCTrack(path=str(target)))


def _music(tmp_path: Path, name: str = "song.m4a") -> _FakeItem:
    target = tmp_path / name
    target.write_bytes(b"stub")
    return _FakeItem(pc_track=_FakePCTrack(path=str(target), is_audiobook=False))


def _with_selection(widget: SyncReviewWidget, items: list[_FakeItem]) -> None:
    """Stand in for checked rows without building a whole SyncPlan."""
    widget._get_selected_items = lambda: list(items)  # type: ignore[method-assign]


# ── Button presence and styling ─────────────────────────────────────────────


def test_footer_has_an_audiobook_details_button(qtbot) -> None:
    widget = _review(qtbot)

    assert widget.audiobook_details_btn.text() == "Find Details…"


def test_button_is_themed_like_the_other_quiet_footer_buttons(qtbot) -> None:
    widget = _review(qtbot)

    style = widget.audiobook_details_btn.styleSheet()

    assert style
    assert style == widget.edit_selection_btn.styleSheet()


def test_button_starts_disabled(qtbot) -> None:
    widget = _review(qtbot)

    assert not widget.audiobook_details_btn.isEnabled()


# ── Enablement rules ────────────────────────────────────────────────────────


def test_exactly_one_selected_audiobook_enables_the_button(qtbot, tmp_path: Path) -> None:
    widget = _review(qtbot)
    _with_selection(widget, [_audiobook(tmp_path)])

    widget.refresh_audiobook_action()

    assert widget.audiobook_details_btn.isEnabled()


def test_an_audiobook_alongside_music_still_enables(qtbot, tmp_path: Path) -> None:
    widget = _review(qtbot)
    _with_selection(widget, [_music(tmp_path), _audiobook(tmp_path)])

    widget.refresh_audiobook_action()

    # The action is unambiguous: exactly one audiobook is checked.
    assert widget.audiobook_details_btn.isEnabled()


def test_two_audiobooks_disable_the_button(qtbot, tmp_path: Path) -> None:
    widget = _review(qtbot)
    _with_selection(
        widget,
        [_audiobook(tmp_path, "a.m4b"), _audiobook(tmp_path, "b.m4b")],
    )

    widget.refresh_audiobook_action()

    # Ambiguous target — the user must narrow the selection.
    assert not widget.audiobook_details_btn.isEnabled()
    assert widget.selected_audiobook_item() is None


def test_music_only_selection_disables_the_button(qtbot, tmp_path: Path) -> None:
    widget = _review(qtbot)
    _with_selection(widget, [_music(tmp_path)])

    widget.refresh_audiobook_action()

    assert not widget.audiobook_details_btn.isEnabled()


def test_empty_selection_disables_the_button(qtbot) -> None:
    widget = _review(qtbot)
    _with_selection(widget, [])

    widget.refresh_audiobook_action()

    assert not widget.audiobook_details_btn.isEnabled()


def test_selection_count_update_refreshes_the_button(qtbot, tmp_path: Path) -> None:
    widget = _review(qtbot)
    _with_selection(widget, [_audiobook(tmp_path)])

    # The debounced selection handler must keep the action in sync.
    widget._do_update_selection_count()

    assert widget.audiobook_details_btn.isEnabled()


# ── Resolving the target ────────────────────────────────────────────────────


def test_selected_audiobook_item_returns_the_single_audiobook(qtbot, tmp_path: Path) -> None:
    widget = _review(qtbot)
    book = _audiobook(tmp_path)
    _with_selection(widget, [_music(tmp_path), book])

    assert widget.selected_audiobook_item() is book


def test_selected_audiobook_item_is_none_without_one(qtbot, tmp_path: Path) -> None:
    widget = _review(qtbot)
    _with_selection(widget, [_music(tmp_path)])

    assert widget.selected_audiobook_item() is None


# ── Emitting the request ────────────────────────────────────────────────────


def test_clicking_emits_the_selected_item(qtbot, tmp_path: Path) -> None:
    widget = _review(qtbot)
    book = _audiobook(tmp_path)
    _with_selection(widget, [book])
    widget.refresh_audiobook_action()

    with qtbot.waitSignal(widget.audiobook_details_requested, timeout=1000) as blocker:
        widget.audiobook_details_btn.click()

    assert blocker.args[0] is book


def test_no_signal_when_nothing_qualifies(qtbot, tmp_path: Path) -> None:
    widget = _review(qtbot)
    _with_selection(widget, [_music(tmp_path)])
    widget.refresh_audiobook_action()

    with qtbot.assertNotEmitted(widget.audiobook_details_requested):
        widget._on_audiobook_details()


# ── Visibility follows the plan state ───────────────────────────────────────


def test_button_hides_while_loading_like_its_neighbours(qtbot) -> None:
    widget = _review(qtbot)
    widget.show_loading()

    assert widget.audiobook_details_btn.isVisible() == widget.edit_selection_btn.isVisible()
