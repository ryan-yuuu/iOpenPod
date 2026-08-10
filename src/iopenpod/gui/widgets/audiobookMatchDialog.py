"""Audiobook metadata match dialog — Audible search plus Audnexus details.

Modal dialog that looks up an audiobook by title, shows the candidate
editions so the user can pick the right one, then writes the resolved
metadata into the selected ``.m4b``.  Network work runs on background
workers to keep the UI responsive.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QFont, QImage, QPixmap
from PyQt6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from iopenpod.audiobooks.models import AudiobookCandidate, AudiobookMetadata

from ..glyphs import glyph_pixmap
from ..hidpi import scale_pixmap_for_display
from ..styles import (
    FONT_FAMILY,
    LABEL_SECONDARY,
    Metrics,
    accent_btn_css,
    btn_css,
    input_css,
    make_label,
    make_scroll_area,
    paint_css,
)
from .podcastStates import PodcastStatePanel

log = logging.getLogger(__name__)

_ART_SIZE = 56
_DETAIL_SEPARATOR = "  ·  "


class AudiobookMatchDialog(QDialog):
    """Modal dialog for matching one audiobook file to a catalog record.

    Emits ``metadata_applied(AudiobookMetadata)`` when the user confirms a
    match. Writing the tags is the caller's job, so this dialog stays free
    of file I/O.

    A caller working through several audiobooks passes ``batch_position`` to
    show progress and offer a Skip button. Skipping has to be distinct from
    cancelling: with only Qt's two exits, closing the dialog on the second of
    twenty books either abandons the remaining eighteen or cannot pass over
    one, and neither is what the user meant.
    """

    metadata_applied = pyqtSignal(object)  # AudiobookMetadata

    #: Third exit code alongside Qt's Accepted (1) and Rejected (0): leave this
    #: file alone but carry on with the rest of the batch.
    Skipped = 2

    def __init__(
        self,
        path: Path,
        parent: QWidget | None = None,
        *,
        search_fn: Callable[..., list[AudiobookCandidate]] | None = None,
        detail_fn: Callable[..., AudiobookMetadata | None] | None = None,
        cover_fn: Callable[[str], bytes] | None = None,
        batch_position: tuple[int, int] | None = None,
    ):
        """``*_fn`` default to the live clients; tests inject stubs."""
        super().__init__(parent)
        self._path = Path(path)
        self._cards: list[_CandidateCard] = []
        self._selected: AudiobookCandidate | None = None
        self._resolved: AudiobookMetadata | None = None
        self._search_fn = search_fn
        self._detail_fn = detail_fn
        self._cover_fn = cover_fn
        self._batch_position = batch_position

        self.setWindowTitle(self._window_title())
        self.setMinimumSize(560, 480)
        self.resize(640, 560)
        self.setStyleSheet(f"""
            QDialog {{
                background: {paint_css("modal.background")};
            }}
        """)

        self._build_ui()
        self._search_input.setText(self._path.stem)

    # ── Public accessors (also the test seam) ────────────────────────────

    def target_path(self) -> Path:
        return self._path

    def search_query(self) -> str:
        return self._search_input.text().strip()

    def apply_button(self) -> QPushButton:
        return self._apply_btn

    def skip_button(self) -> QPushButton:
        """Only shown in batch mode; hidden otherwise."""
        return self._skip_btn

    def batch_position(self) -> tuple[int, int] | None:
        return self._batch_position

    def candidate_cards(self) -> list[_CandidateCard]:
        return list(self._cards)

    def selected_candidate(self) -> AudiobookCandidate | None:
        return self._selected

    def resolved_metadata(self) -> AudiobookMetadata | None:
        return self._resolved

    # ── UI construction ──────────────────────────────────────────────────

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        file_label = make_label(
            f"Tagging  {self._path.name}",
            size=Metrics.FONT_SM,
            style=LABEL_SECONDARY(),
        )
        file_label.setWordWrap(True)
        layout.addWidget(file_label)

        # ── Search bar ───────────────────────────────────────────────────
        search_row = QHBoxLayout()
        search_row.setSpacing(8)

        self._search_input = QLineEdit()
        self._search_input.setPlaceholderText("Search by book title…")
        self._search_input.setFont(QFont(FONT_FAMILY, Metrics.FONT_MD))
        self._search_input.setStyleSheet(input_css(padding="8px 12px"))
        self._search_input.returnPressed.connect(self._on_search)
        search_row.addWidget(self._search_input, stretch=1)

        self._search_btn = QPushButton("Search")
        self._search_btn.setFont(QFont(FONT_FAMILY, Metrics.FONT_MD))
        self._search_btn.setStyleSheet(accent_btn_css())
        self._search_btn.setFixedHeight(36)
        self._search_btn.clicked.connect(self._on_search)
        search_row.addWidget(self._search_btn)

        layout.addLayout(search_row)

        # ── Status label ─────────────────────────────────────────────────
        self._status_label = make_label(
            "Search for this book to fill in its details",
            size=Metrics.FONT_SM,
            style=LABEL_SECONDARY(),
        )
        layout.addWidget(self._status_label)

        # ── Results ──────────────────────────────────────────────────────
        self._results_container = QWidget()
        self._results_layout = QVBoxLayout(self._results_container)
        self._results_layout.setContentsMargins(0, 0, 0, 0)
        self._results_layout.setSpacing(4)

        self._state_panel = PodcastStatePanel(compact=True)
        self._state_panel.show_empty(
            "Find this audiobook",
            "Editions differ by narrator and length — pick the one that matches your file.",
        )
        self._state_panel.action_clicked.connect(self._on_search)
        self._results_layout.addWidget(self._state_panel)
        self._results_layout.addStretch()

        scroll = make_scroll_area(
            extra_css=f"""
            QScrollArea {{
                border: 1px solid {paint_css("border.subtle")};
                border-radius: {Metrics.BORDER_RADIUS_SM}px;
            }}
        """
        )
        scroll.setWidget(self._results_container)
        layout.addWidget(scroll, stretch=1)

        # ── Actions ──────────────────────────────────────────────────────
        action_row = QHBoxLayout()
        action_row.setSpacing(8)
        action_row.addStretch()

        # Leaves this file untouched and lets the caller move to the next one.
        self._skip_btn = QPushButton("Skip")
        self._skip_btn.setFont(QFont(FONT_FAMILY, Metrics.FONT_MD))
        self._skip_btn.setStyleSheet(btn_css())
        self._skip_btn.setFixedHeight(36)
        self._skip_btn.clicked.connect(self._on_skip)
        self._skip_btn.setVisible(self._batch_position is not None)
        self._skip_btn.setToolTip("Leave this audiobook as it is and go to the next")
        action_row.addWidget(self._skip_btn)

        cancel_btn = QPushButton("Cancel")
        cancel_btn.setFont(QFont(FONT_FAMILY, Metrics.FONT_MD))
        cancel_btn.setStyleSheet(btn_css())
        cancel_btn.setFixedHeight(36)
        cancel_btn.clicked.connect(self.reject)
        if self._batch_position is not None:
            cancel_btn.setToolTip("Stop here and leave the remaining audiobooks alone")
        action_row.addWidget(cancel_btn)

        self._apply_btn = QPushButton("Apply Details")
        self._apply_btn.setFont(QFont(FONT_FAMILY, Metrics.FONT_MD))
        self._apply_btn.setStyleSheet(accent_btn_css())
        self._apply_btn.setFixedHeight(36)
        self._apply_btn.setEnabled(False)
        self._apply_btn.clicked.connect(self._on_apply)
        action_row.addWidget(self._apply_btn)

        layout.addLayout(action_row)

    # ── Result population ────────────────────────────────────────────────

    def show_candidates(self, candidates: list[AudiobookCandidate]) -> None:
        """Replace the result list, clearing any previous selection."""
        self._clear_results()
        self._selected = None
        self._resolved = None
        self._sync_apply_enabled()

        if not candidates:
            self._status_label.setText("No matching audiobooks found")
            self._state_panel.show()
            self._state_panel.show_empty(
                "No matches found",
                "Try a shorter title, or the author's name instead.",
            )
            return

        count = len(candidates)
        self._status_label.setText(f"Found {count} edition{'s' if count != 1 else ''} — choose the closest match")
        self._state_panel.hide()

        for candidate in candidates:
            card = _CandidateCard(candidate, self, cover_fn=self._cover_fn)
            card.chosen.connect(self._on_card_chosen)
            self._cards.append(card)
            self._results_layout.insertWidget(
                self._results_layout.count() - 1,  # Before stretch
                card,
            )

    def set_resolved_metadata(self, metadata: AudiobookMetadata | None) -> None:
        """Record the full record fetched for the selected candidate."""
        self._resolved = metadata
        self._sync_apply_enabled()
        if metadata is not None:
            self._status_label.setText(f"Ready to apply “{metadata.title}”")

    # ── Slots ────────────────────────────────────────────────────────────

    def _on_search(self) -> None:
        query = self.search_query()
        if not query:
            return

        self._search_btn.setEnabled(False)
        self._status_label.setText("Searching…")
        self._clear_results()
        self._state_panel.show()
        self._state_panel.show_loading(
            "Searching for this audiobook…",
            "Checking the catalog now.",
        )

        from iopenpod.application.runtime import ThreadPoolSingleton, Worker
        from iopenpod.audiobooks.audible_search import search_audiobooks

        search = self._search_fn or search_audiobooks
        worker = Worker(search, query, raise_on_error=True)
        worker.signals.result.connect(self.show_candidates)
        worker.signals.error.connect(self._on_search_error)
        worker.signals.finished.connect(lambda: self._search_btn.setEnabled(True))
        ThreadPoolSingleton.get_instance().start(worker)

    def _on_search_error(self, error_tuple: tuple) -> None:
        _, value, _ = error_tuple
        from iopenpod.audiobooks.network_errors import describe_audiobook_error

        info = describe_audiobook_error(value, action="search for audiobooks")
        self._status_label.setText(info.title)
        self._clear_results()
        self._state_panel.show()
        self._state_panel.show_error(info.title, info.message, code=info.code)
        self._search_btn.setEnabled(True)

    def _on_card_chosen(self, candidate: object) -> None:
        if not isinstance(candidate, AudiobookCandidate):
            return
        self._selected = candidate
        self._resolved = None
        for card in self._cards:
            card.set_selected(card.candidate() is candidate)
        self._sync_apply_enabled()
        self._fetch_details(candidate)

    def _fetch_details(self, candidate: AudiobookCandidate) -> None:
        self._status_label.setText(f"Loading details for “{candidate.title}”…")

        from iopenpod.application.runtime import ThreadPoolSingleton, Worker
        from iopenpod.audiobooks.audnexus import fetch_audiobook

        detail = self._detail_fn or fetch_audiobook
        worker = Worker(detail, candidate.asin, raise_on_error=True)
        worker.signals.result.connect(self.set_resolved_metadata)
        worker.signals.error.connect(self._on_details_error)
        ThreadPoolSingleton.get_instance().start(worker)

    def _on_details_error(self, error_tuple: tuple) -> None:
        _, value, _ = error_tuple
        from iopenpod.audiobooks.network_errors import describe_audiobook_error

        info = describe_audiobook_error(value, action="load audiobook details")
        self._status_label.setText(info.title)
        self.set_resolved_metadata(None)

    def _on_apply(self) -> None:
        if self._resolved is not None:
            self.metadata_applied.emit(self._resolved)

    def _on_skip(self) -> None:
        self.done(self.Skipped)

    def _window_title(self) -> str:
        base = "Find Audiobook Details"
        if self._batch_position is None:
            return base
        index, total = self._batch_position
        return f"{base} — {index} of {total}"

    # ── Helpers ──────────────────────────────────────────────────────────

    def _sync_apply_enabled(self) -> None:
        self._apply_btn.setEnabled(self._selected is not None and self._resolved is not None)

    def _clear_results(self) -> None:
        self._cards.clear()
        for index in range(self._results_layout.count() - 1, -1, -1):
            item = self._results_layout.itemAt(index)
            if item is None:
                continue
            widget = item.widget()
            if widget is None or widget is self._state_panel:
                continue
            self._results_layout.takeAt(index)
            widget.deleteLater()


class _CandidateCard(QFrame):
    """A single candidate edition, showing what distinguishes it."""

    chosen = pyqtSignal(object)  # AudiobookCandidate

    def __init__(
        self,
        candidate: AudiobookCandidate,
        parent: QWidget | None = None,
        *,
        cover_fn: Callable[[str], bytes] | None = None,
    ):
        super().__init__(parent)
        self._candidate = candidate
        self._cover_fn = cover_fn
        self._selected = False
        self.setProperty("selected", False)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._apply_style()

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(10)

        # Artwork
        self._art_label = QLabel()
        self._art_label.setFixedSize(_ART_SIZE, _ART_SIZE)
        self._art_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._art_label.setStyleSheet(f"""
            background: {paint_css("surface.raised")};
            border-radius: {Metrics.BORDER_RADIUS_SM}px;
            color: {paint_css("text.tertiary")};
        """)
        book_px = glyph_pixmap("book", 24, paint_css("text.tertiary"))
        if book_px:
            self._art_label.setPixmap(book_px)
        layout.addWidget(self._art_label)

        if candidate.cover_url:
            self._load_artwork(candidate.cover_url)

        # Info column
        info = QVBoxLayout()
        info.setSpacing(2)

        title_lbl = make_label(
            candidate.title,
            size=Metrics.FONT_MD,
            weight=QFont.Weight.DemiBold,
        )
        title_lbl.setWordWrap(True)
        title_lbl.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Preferred,
        )
        info.addWidget(title_lbl)

        self._detail_text = self._build_detail_text(candidate)
        detail_lbl = make_label(
            self._detail_text,
            size=Metrics.FONT_SM,
            style=LABEL_SECONDARY(),
        )
        detail_lbl.setWordWrap(True)
        detail_lbl.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Preferred,
        )
        info.addWidget(detail_lbl)

        layout.addLayout(info, stretch=1)

        self._choose_btn = QPushButton("Choose")
        self._choose_btn.setFont(QFont(FONT_FAMILY, Metrics.FONT_SM))
        self._choose_btn.setStyleSheet(btn_css())
        self._choose_btn.setFixedSize(90, 32)
        self._choose_btn.clicked.connect(self.select)
        layout.addWidget(self._choose_btn, alignment=Qt.AlignmentFlag.AlignVCenter)

    # ── Public accessors ─────────────────────────────────────────────────

    def candidate(self) -> AudiobookCandidate:
        return self._candidate

    def detail_text(self) -> str:
        return self._detail_text

    def is_selected(self) -> bool:
        return self._selected

    def select(self) -> None:
        """Choose this candidate, marking it and notifying the dialog.

        The card owns its own visual state so it is correct standalone; the
        dialog then clears the other cards to enforce single selection.
        """
        self.set_selected(True)
        self.chosen.emit(self._candidate)

    def set_selected(self, selected: bool) -> None:
        self._selected = selected
        self.setProperty("selected", selected)
        self._choose_btn.setText("Chosen" if selected else "Choose")
        self._choose_btn.setStyleSheet(accent_btn_css() if selected else btn_css())
        self._apply_style()

    # ── Presentation ─────────────────────────────────────────────────────

    @staticmethod
    def _build_detail_text(candidate: AudiobookCandidate) -> str:
        """Join only the parts we actually know, so no stray separators."""
        parts = [
            part
            for part in (
                candidate.author_text,
                f"Read by {candidate.narrator_text}" if candidate.narrators else "",
                candidate.runtime_text,
            )
            if part
        ]
        return _DETAIL_SEPARATOR.join(parts)

    def _apply_style(self) -> None:
        """Style as a selectable card, reusing the grid card selection paints."""
        if self._selected:
            background = paint_css("grid.card.selected_fill")
            border = paint_css("grid.card.selected_border")
            hover = paint_css("grid.card.selected_hover_fill")
        else:
            background = paint_css("surface.inset")
            border = paint_css("border.subtle")
            hover = paint_css("surface.hover")
        self.setStyleSheet(f"""
            _CandidateCard {{
                background: {background};
                border: 1px solid {border};
                border-radius: {Metrics.BORDER_RADIUS_SM}px;
            }}
            _CandidateCard:hover {{
                background: {hover};
            }}
        """)

    def _load_artwork(self, url: str) -> None:
        from iopenpod.application.runtime import ThreadPoolSingleton, Worker
        from iopenpod.audiobooks.audnexus import fetch_cover_bytes

        fetch = self._cover_fn or fetch_cover_bytes
        worker = Worker(fetch, url)
        worker.signals.result.connect(self._on_artwork_loaded)
        ThreadPoolSingleton.get_instance().start(worker)

    def _on_artwork_loaded(self, data: bytes) -> None:
        if not data:
            return
        img = QImage()
        if img.loadFromData(data):
            pm = scale_pixmap_for_display(
                QPixmap.fromImage(img),
                _ART_SIZE,
                _ART_SIZE,
                widget=self._art_label,
                aspect_mode=Qt.AspectRatioMode.KeepAspectRatio,
                transform_mode=Qt.TransformationMode.SmoothTransformation,
            )
            self._art_label.setPixmap(pm)
