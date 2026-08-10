"""Audiobook match dialog behaviour and style conventions.

Network clients are injected as stubs; these tests never reach the network.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PyQt6.QtWidgets import QPushButton

from iopenpod.audiobooks.models import AudiobookCandidate, AudiobookMetadata
from iopenpod.gui.widgets.audiobookMatchDialog import (
    AudiobookMatchDialog,
    _CandidateCard,
)

CANDIDATE = AudiobookCandidate(
    asin="B00E83NFUC",
    title="Kafka on the Shore",
    authors=("Haruki Murakami",),
    narrators=("Sean Barrett", "Oliver Le Sueur"),
    runtime_min=1148,
    cover_url="https://example.invalid/cover.jpg",
)

METADATA = AudiobookMetadata(
    asin="B00E83NFUC",
    title="Kafka on the Shore",
    authors=("Haruki Murakami",),
    narrators=("Sean Barrett", "Oliver Le Sueur"),
    publisher="Naxos AudioBooks",
    release_year=2007,
    summary="A tale of two journeys.",
    runtime_min=1148,
)


def _no_search(*_args: object, **_kwargs: object) -> list[AudiobookCandidate]:
    return []


def _no_detail(*_args: object, **_kwargs: object) -> AudiobookMetadata | None:
    return None


def _no_cover(*_args: object, **_kwargs: object) -> bytes:
    return b""


@pytest.fixture
def book(tmp_path: Path) -> Path:
    path = tmp_path / "book.m4b"
    path.write_bytes(b"not really an mp4")
    return path


def _make_dialog(
    qtbot, path: Path, *, batch_position: tuple[int, int] | None = None
) -> AudiobookMatchDialog:
    dlg = AudiobookMatchDialog(
        path,
        search_fn=_no_search,
        detail_fn=_no_detail,
        cover_fn=_no_cover,
        batch_position=batch_position,
    )
    qtbot.addWidget(dlg)
    return dlg


def _make_card(qtbot, candidate: AudiobookCandidate) -> _CandidateCard:
    card = _CandidateCard(candidate, cover_fn=_no_cover)
    qtbot.addWidget(card)
    return card


@pytest.fixture
def dialog(qtbot, book: Path) -> AudiobookMatchDialog:
    return _make_dialog(qtbot, book)


# ── Construction ────────────────────────────────────────────────────────────


def test_dialog_seeds_the_query_from_the_filename(qtbot, tmp_path: Path) -> None:
    path = tmp_path / "Kafka on the Shore.m4b"
    path.write_bytes(b"x")

    dlg = _make_dialog(qtbot, path)

    assert dlg.search_query() == "Kafka on the Shore"


def test_dialog_starts_with_apply_disabled(dialog: AudiobookMatchDialog) -> None:
    # Nothing is selected yet, so there is nothing to write.
    assert not dialog.apply_button().isEnabled()


def test_dialog_exposes_the_target_file(dialog: AudiobookMatchDialog, book: Path) -> None:
    assert dialog.target_path() == book


# ── Results ─────────────────────────────────────────────────────────────────


def test_showing_results_creates_one_card_per_candidate(
    dialog: AudiobookMatchDialog,
) -> None:
    dialog.show_candidates([CANDIDATE, CANDIDATE])

    assert len(dialog.candidate_cards()) == 2


def test_selecting_a_candidate_records_it(dialog: AudiobookMatchDialog) -> None:
    dialog.show_candidates([CANDIDATE])
    dialog.candidate_cards()[0].select()

    assert dialog.selected_candidate() == CANDIDATE


def test_only_one_candidate_is_selected_at_a_time(dialog: AudiobookMatchDialog) -> None:
    other = AudiobookCandidate(asin="B0OTHER", title="Something Else")
    dialog.show_candidates([CANDIDATE, other])
    cards = dialog.candidate_cards()

    cards[0].select()
    cards[1].select()

    assert dialog.selected_candidate() == other
    assert not cards[0].is_selected()
    assert cards[1].is_selected()


def test_empty_results_clear_selection_and_disable_apply(
    dialog: AudiobookMatchDialog,
) -> None:
    dialog.show_candidates([CANDIDATE])
    dialog.candidate_cards()[0].select()

    dialog.show_candidates([])

    assert dialog.selected_candidate() is None
    assert not dialog.apply_button().isEnabled()


def test_new_search_replaces_previous_cards(dialog: AudiobookMatchDialog) -> None:
    dialog.show_candidates([CANDIDATE, CANDIDATE, CANDIDATE])
    dialog.show_candidates([CANDIDATE])

    assert len(dialog.candidate_cards()) == 1


# ── Card presentation ───────────────────────────────────────────────────────


def test_card_shows_narrator_and_runtime_to_distinguish_editions(qtbot) -> None:
    card = _make_card(qtbot, CANDIDATE)

    text = card.detail_text()

    assert "Sean Barrett" in text
    assert "19h 08m" in text


def test_card_omits_missing_detail_fields(qtbot) -> None:
    card = _make_card(qtbot, AudiobookCandidate(asin="X", title="Bare"))

    # No trailing separators when narrator and runtime are unknown.
    assert not card.detail_text().strip().endswith("·")


# ── Applying ────────────────────────────────────────────────────────────────


def test_apply_enables_once_selection_and_metadata_are_both_present(
    dialog: AudiobookMatchDialog,
) -> None:
    dialog.show_candidates([CANDIDATE])
    dialog.candidate_cards()[0].select()
    dialog.set_resolved_metadata(METADATA)

    assert dialog.apply_button().isEnabled()


def test_apply_stays_disabled_until_metadata_resolves(
    dialog: AudiobookMatchDialog,
) -> None:
    dialog.show_candidates([CANDIDATE])
    dialog.candidate_cards()[0].select()
    dialog.set_resolved_metadata(None)

    # A chosen edition is not enough — there must be a record to write.
    assert not dialog.apply_button().isEnabled()


def test_apply_emits_the_resolved_metadata(qtbot, dialog: AudiobookMatchDialog) -> None:
    dialog.show_candidates([CANDIDATE])
    dialog.candidate_cards()[0].select()
    dialog.set_resolved_metadata(METADATA)

    with qtbot.waitSignal(dialog.metadata_applied, timeout=1000) as blocker:
        dialog.apply_button().click()

    assert blocker.args[0] == METADATA


def test_choosing_a_different_edition_clears_stale_metadata(
    dialog: AudiobookMatchDialog,
) -> None:
    other = AudiobookCandidate(asin="B0OTHER", title="Something Else")
    dialog.show_candidates([CANDIDATE, other])
    cards = dialog.candidate_cards()

    cards[0].select()
    dialog.set_resolved_metadata(METADATA)
    cards[1].select()

    # The old record must not be applied to a newly chosen edition.
    assert dialog.resolved_metadata() is None
    assert not dialog.apply_button().isEnabled()


# ── Style conventions ───────────────────────────────────────────────────────


def test_dialog_uses_a_themed_modal_background(dialog: AudiobookMatchDialog) -> None:
    # Themed paints only — hard-coded colours break community themes.
    assert "background:" in dialog.styleSheet()


def test_buttons_are_themed_not_default_styled(dialog: AudiobookMatchDialog) -> None:
    for button in dialog.findChildren(QPushButton):
        assert button.styleSheet(), f"{button.text()!r} has no themed stylesheet"


# ── Working through several audiobooks ──────────────────────────────────────


def test_single_use_shows_no_skip_button(dialog: AudiobookMatchDialog) -> None:
    # Shown first: an unshown widget reports invisible whatever its own flag.
    dialog.show()

    # Nothing to skip to, so the control would only add noise.
    assert not dialog.skip_button().isVisible()


def test_single_use_title_has_no_position(dialog: AudiobookMatchDialog) -> None:
    assert dialog.windowTitle() == "Find Audiobook Details"


def test_batch_mode_offers_a_skip_button(qtbot, book: Path) -> None:
    dlg = _make_dialog(qtbot, book, batch_position=(2, 5))
    dlg.show()

    assert dlg.skip_button().isVisible()


def test_batch_mode_shows_progress_in_the_title(qtbot, book: Path) -> None:
    dlg = _make_dialog(qtbot, book, batch_position=(2, 5))

    assert "2 of 5" in dlg.windowTitle()


def test_skipping_closes_with_its_own_code(qtbot, book: Path) -> None:
    dlg = _make_dialog(qtbot, book, batch_position=(1, 3))
    codes: list[int] = []
    dlg.finished.connect(codes.append)

    dlg.skip_button().click()

    # Skip must be distinguishable from cancel: one passes over this file,
    # the other abandons the whole run.
    assert codes == [AudiobookMatchDialog.Skipped]
    assert AudiobookMatchDialog.Skipped not in (
        int(AudiobookMatchDialog.DialogCode.Accepted),
        int(AudiobookMatchDialog.DialogCode.Rejected),
    )


def test_skipping_emits_no_metadata(qtbot, book: Path) -> None:
    dlg = _make_dialog(qtbot, book, batch_position=(1, 3))

    with qtbot.assertNotEmitted(dlg.metadata_applied):
        dlg.skip_button().click()


def test_skip_button_is_themed(qtbot, book: Path) -> None:
    dlg = _make_dialog(qtbot, book, batch_position=(1, 3))

    assert dlg.skip_button().styleSheet()


def test_card_marks_selection_with_an_accessible_state(qtbot) -> None:
    card = _make_card(qtbot, CANDIDATE)

    assert not card.is_selected()
    card.select()

    assert card.is_selected()
    # Selection is conveyed by a queryable property, not colour alone.
    assert card.property("selected") is True
