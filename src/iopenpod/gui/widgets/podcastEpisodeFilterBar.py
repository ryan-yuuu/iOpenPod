"""Sort and search controls shared by every podcast episode view.

One strip sits directly above the episode list in all three views — a single
show, the combined feed, and On iPod — so ordering and filtering work the same
way wherever episodes are listed.

The bar owns no episode data.  It reports what the user asked for and lets the
browser re-present its own rows, which keeps the sort and filter rules in one
place instead of one copy per view.
"""

from __future__ import annotations

from collections.abc import Sequence

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QFont, QKeyEvent
from PyQt6.QtWidgets import QComboBox, QFrame, QHBoxLayout, QLineEdit, QWidget

from ..glyphs import glyph_icon
from ..styles import (
    BROWSER_SEARCH_CONTROL_SIZE,
    BROWSER_SEARCH_FIELD_WIDTH,
    FONT_FAMILY,
    LABEL_SECONDARY,
    Metrics,
    browser_search_field_css,
    combo_css,
    make_label,
    paint_css,
)

SEARCH_DEBOUNCE_MS = 120
"""Long enough to coalesce a burst of keystrokes, short enough to feel live."""

_BAR_PADDING_V = 8
_BAR_PADDING_H = 24
_SORT_MIN_WIDTH = 138


class _EpisodeSearchField(QLineEdit):
    """Search field that reports Escape instead of swallowing it."""

    escaped = pyqtSignal()

    def keyPressEvent(self, a0: QKeyEvent | None) -> None:
        if a0 is not None and a0.key() == Qt.Key.Key_Escape:
            a0.accept()
            self.escaped.emit()
            return
        super().keyPressEvent(a0)


class EpisodeFilterBar(QFrame):
    """Summary, sort, and search strip above a podcast episode list."""

    sort_changed = pyqtSignal(str)      # sort key of the newly chosen order
    search_changed = pyqtSignal(str)    # debounced query text
    search_dismissed = pyqtSignal()     # Escape on an already-empty field

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("podcastEpisodeFilterBar")
        self.setFixedHeight(BROWSER_SEARCH_CONTROL_SIZE + _BAR_PADDING_V * 2)
        self.setStyleSheet(f"""
            QFrame#podcastEpisodeFilterBar {{
                background: {paint_css('canvas.default')};
                border-bottom: 1px solid {paint_css('border.subtle')};
            }}
        """)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(
            _BAR_PADDING_H,
            _BAR_PADDING_V,
            _BAR_PADDING_H,
            _BAR_PADDING_V,
        )
        layout.setSpacing(8)

        # What is listed right now — the only feedback that a search actually
        # narrowed something rather than the list being short to begin with.
        self._summary = make_label("", size=Metrics.FONT_SM, style=LABEL_SECONDARY())
        layout.addWidget(self._summary)
        layout.addStretch()

        self._sort = QComboBox()
        self._sort.setObjectName("podcastEpisodeSortCombo")
        self._sort.setFont(QFont(FONT_FAMILY, Metrics.FONT_SM))
        self._sort.setStyleSheet(combo_css())
        self._sort.setFixedHeight(BROWSER_SEARCH_CONTROL_SIZE)
        self._sort.setMinimumWidth(_SORT_MIN_WIDTH)
        # Options differ per view, so the box has to keep resizing to fit them
        # rather than settling on the width of whatever it held first.
        self._sort.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self._sort.setCursor(Qt.CursorShape.PointingHandCursor)
        self._sort.setAccessibleName("Sort episodes")
        self._sort.currentIndexChanged.connect(self._on_sort_index_changed)
        layout.addWidget(self._sort)

        self._search = _EpisodeSearchField()
        self._search.setObjectName("podcastEpisodeSearchField")
        self._search.setAccessibleName("Search episodes")
        self._search.setClearButtonEnabled(True)
        self._search.setFixedSize(
            BROWSER_SEARCH_FIELD_WIDTH,
            BROWSER_SEARCH_CONTROL_SIZE,
        )
        self._search.setStyleSheet(browser_search_field_css())
        search_icon = glyph_icon("search", 16, paint_css("text.tertiary"))
        if search_icon is not None:
            self._search.addAction(
                search_icon,
                QLineEdit.ActionPosition.LeadingPosition,
            )
        self._search.textChanged.connect(self._on_search_text_changed)
        self._search.escaped.connect(self._on_search_escaped)
        layout.addWidget(self._search)

        # Typing re-presents the whole list, so coalesce a burst of keystrokes
        # into one pass instead of one per character.
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(SEARCH_DEBOUNCE_MS)
        self._search_timer.timeout.connect(self._emit_search_changed)

        self.set_search_scope("Find an episode", describes="these episodes")
        self._update_sort_tooltip()

    # ── Sort ─────────────────────────────────────────────────────────────

    def set_sort_options(
        self,
        options: Sequence[tuple[str, str]],
        active: str,
    ) -> str:
        """Offer *options* as ``(label, key)`` pairs and select *active*.

        Returns the key actually selected, which differs from *active* when a
        view does not offer it — sorting by show means nothing inside a single
        show, so the caller has to be told what it ended up with.
        """
        self._sort.blockSignals(True)
        self._sort.clear()
        for label, key in options:
            self._sort.addItem(label, key)
        index = self._sort.findData(active)
        self._sort.setCurrentIndex(max(index, 0))
        self._sort.blockSignals(False)
        self._update_sort_tooltip()
        return self.sort_key()

    def sort_options(self) -> list[tuple[str, str]]:
        """The ``(label, key)`` orders currently on offer, in menu order."""
        return [
            (self._sort.itemText(index), str(self._sort.itemData(index) or ""))
            for index in range(self._sort.count())
        ]

    def sort_key(self) -> str:
        """The key of the currently selected order."""
        return str(self._sort.currentData() or "")

    def sort_label(self) -> str:
        """The label shown for the currently selected order."""
        return self._sort.currentText()

    def _on_sort_index_changed(self, _index: int) -> None:
        self._update_sort_tooltip()
        self.sort_changed.emit(self.sort_key())

    def _update_sort_tooltip(self) -> None:
        label = self.sort_label()
        self._sort.setToolTip(f"Sort: {label}" if label else "Sort episodes")

    # ── Search ───────────────────────────────────────────────────────────

    def query(self) -> str:
        """The text currently in the search field."""
        return self._search.text()

    def set_query(self, text: str, *, notify: bool = False) -> None:
        """Put *text* in the search field, optionally applying it at once.

        Silent by default so switching views can reset the field without
        triggering a re-render the caller is about to do anyway.
        """
        self._search_timer.stop()
        self._search.blockSignals(True)
        self._search.setText(text)
        self._search.blockSignals(False)
        if notify:
            self.search_changed.emit(text)

    def set_search_scope(self, placeholder: str, *, describes: str) -> None:
        """Name what a search covers here.

        *placeholder* has to fit the field, so the fuller phrasing goes in
        *describes*, which only has to fit a tooltip.
        """
        self._search.setPlaceholderText(placeholder)
        self._search.setToolTip(
            f"Filter {describes} by episode title, show name, or description"
        )

    def focus_search(self) -> None:
        """Move focus to the search field, ready to replace what is there."""
        self._search.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self._search.selectAll()

    def _on_search_text_changed(self, _text: str) -> None:
        self._search_timer.start()

    def _emit_search_changed(self) -> None:
        self.search_changed.emit(self._search.text())

    def _on_search_escaped(self) -> None:
        """Escape backs out one step: first the query, then the field."""
        if self._search.text():
            self.set_query("", notify=True)
            return
        self.search_dismissed.emit()

    # ── Summary ──────────────────────────────────────────────────────────

    def set_summary(self, text: str) -> None:
        """Write the line describing what the list currently holds."""
        self._summary.setText(text)

    def summary(self) -> str:
        """The line describing what the list currently holds."""
        return self._summary.text()
