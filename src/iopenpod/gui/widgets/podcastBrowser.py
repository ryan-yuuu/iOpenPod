"""Podcast browser — two-panel widget for managing podcast subscriptions.

Layout:
    ┌──────────────────────────────────────────────────────────────┐
    │  Toolbar: [Add Podcast] [Refresh All]             status    │
    ├─────────────────┬────────────────────────────────────────────┤
    │  Feed list      │  Feed header (artwork · title · meta)     │
    │  (left panel)   ├────────────────────────────────────────────┤
    │  ┌───────────┐  │  Filter bar: count · [Sort ▾] · [Search]   │
    │  │ ▍art Feed │  ├────────────────────────────────────────────┤
    │  │ ▍art Feed │  │  Episode table (row-select, right-click)  │
    │  └───────────┘  │   Title        Duration   Date   Status   │
    │                 ├────────────────────────────────────────────┤
    │                 │  Action bar: [Add to iPod]                 │
    └─────────────────┴────────────────────────────────────────────┘

    The right-hand panel serves three views — one show, the combined feed of
    every subscription, and what is on the iPod. They share the episode list
    and the filter bar above it; only the header swaps.

    When no feeds exist, a full-page empty state with a prominent CTA
    replaces the splitter.

Select episodes → click "Add to iPod" → automatic download + sync.
"""

from __future__ import annotations

import html
import logging
import re
import time
from collections.abc import Callable
from datetime import UTC
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple, cast

from PyQt6.QtCore import (
    QEvent,
    QObject,
    QPoint,
    QRect,
    QSize,
    Qt,
    QTimer,
    pyqtSignal,
)
from PyQt6.QtGui import (
    QColor,
    QContextMenuEvent,
    QFont,
    QFontMetrics,
    QIcon,
    QImage,
    QKeyEvent,
    QKeySequence,
    QMouseEvent,
    QPainter,
    QPalette,
    QPixmap,
    QResizeEvent,
    QShortcut,
)
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from iopenpod.infrastructure.theme_renderer import render_content_hero_paints
from iopenpod.search import SearchText, matches_search, prepare_search_text

from ..artwork_rendering import dominant_artwork_color_from_pixmap
from ..glyphs import glyph_icon, glyph_pixmap
from ..hidpi import scale_pixmap_for_display
from ..styles import (
    BROWSER_SEARCH_CONTROL_SIZE,
    CHECKBOX_INDICATOR_SIZE,
    FONT_FAMILY,
    LABEL_SECONDARY,
    Metrics,
    accent_btn_css,
    btn_css,
    checkbox_css,
    combo_css,
    context_menu_css,
    current_theme,
    danger_btn_css,
    make_label,
    make_separator,
    paint_css,
    progress_bar_css,
    sidebar_item_view_css,
    spin_css,
)
from .browserChrome import (
    BrowserHeroHeader,
    BrowserPane,
    chrome_action_btn_css,
    style_browser_splitter,
)
from .formatters import format_size
from .podcastEpisodeFilterBar import EpisodeFilterBar
from .podcastStates import PodcastStatePanel

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from iopenpod.application.services import (
        DeviceSessionService,
        LibraryCacheLike,
        LibraryService,
        SettingsService,
    )
    from iopenpod.podcasts.models import PodcastFeed


# ── Column definitions ───────────────────────────────────────────────────────
_COL_TITLE = 0
_COL_DURATION = 1
_COL_DATE = 2
_COL_STATUS = 3
_COL_COUNT = 4


def _fmt_duration(seconds: int) -> str:
    """Compact H:MM:SS or M:SS for episode durations."""
    if not seconds or seconds <= 0:
        return ""
    h, remainder = divmod(seconds, 3600)
    m, s = divmod(remainder, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _fmt_date(ts: float) -> str:
    if not ts or ts <= 0:
        return ""
    from datetime import datetime
    try:
        return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%d")
    except (OSError, ValueError):
        return ""


def _coerce_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _episode_listened_override(ep) -> bool | None:
    override = getattr(ep, "listened_override", None)
    if override is None:
        return None
    return bool(override)


def _episode_is_listened(ep) -> bool:
    override = _episode_listened_override(ep)
    if override is not None:
        return override
    return (
        _coerce_int(getattr(ep, "play_count", 0)) > 0
        or _coerce_int(getattr(ep, "last_played", 0)) > 0
    )


def _set_episode_listened(ep, listened: bool) -> None:
    if listened:
        ep.listened_override = True
        ep.play_count = max(1, _coerce_int(getattr(ep, "play_count", 0)))
        if _coerce_int(getattr(ep, "last_played", 0)) <= 0:
            ep.last_played = int(time.time())
        return

    from iopenpod.podcasts.models import STATUS_ON_IPOD

    ep.play_count = 0
    ep.last_played = 0
    ep.listened_override = (
        False
        if getattr(ep, "status", "") == STATUS_ON_IPOD
        and bool(getattr(ep, "ipod_db_track_id", 0))
        else None
    )


# ── Podcast episode list ─────────────────────────────────────────────────────

_PODCAST_EPISODE_COLUMNS = [
    "Title",
    "Description Text",
    "ep_status",
    "length",
    "date_added",
    "size",
]
_COMBINED_FEED_COLUMNS = [
    "Title",
    "podcast_feed_title",
    "Description Text",
    "ep_status",
    "length",
    "date_added",
    "size",
]
_COMBINED_FEED_KEY = "__iopenpod_combined_feed__"
_ON_IPOD_KEY = "__iopenpod_on_ipod__"

# ── Episode view modes ───────────────────────────────────────────────────────
# Which of the three episode views the right-hand panel is currently showing.
# Every re-render funnels through PodcastBrowser._refresh_current_view() so a
# view can never be forgotten by one of the many status-change call sites.
_VIEW_SHOW = "show"        # A single subscribed feed.
_VIEW_FEED = "feed"        # Every subscribed episode, newest first.
_VIEW_ON_IPOD = "on_ipod"  # Every podcast episode present on the device.

# ── On iPod view ─────────────────────────────────────────────────────────────
_PODCAST_MEDIA_TYPE_BIT = 0x04

# Feeds fabricated to host podcasts found on the iPod that belong to no
# subscription. They exist only to render and remove those episodes, and must
# never reach the subscription store — see _is_synthetic_feed().
_ORPHAN_FEED_PREFIX = "__iopenpod_orphan__:"

# ── Episode ordering ─────────────────────────────────────────────────────────
_SORT_NEWEST = "newest"
_SORT_OLDEST = "oldest"
_SORT_UNPLAYED = "unplayed"
_SORT_LONGEST = "longest"
_SORT_SHORTEST = "shortest"
_SORT_LARGEST = "largest"
_SORT_SHOW = "show"
_SORT_TITLE = "title"

# Episodes whose feed never published a duration would otherwise lead
# "Shortest first", claiming a length nobody knows.
_UNKNOWN_DURATION = float("inf")


def _row_date(row: dict) -> int:
    return _coerce_int(row.get("date_added"))


def _row_length(row: dict) -> int:
    return _coerce_int(row.get("length"))


def _row_size(row: dict) -> int:
    return _coerce_int(row.get("size"))


def _row_show(row: dict) -> str:
    return str(row.get("podcast_feed_title") or "").casefold()


def _row_title(row: dict) -> str:
    return str(row.get("Title") or "").casefold()


def _row_listened(row: dict) -> bool:
    return bool(row.get("_was_listened"))


def _row_search_text(row: dict) -> SearchText:
    """The prepared text a search runs against: title, show, and blurb."""
    prepared = row.get("_search_text")
    if isinstance(prepared, SearchText):
        return prepared
    return prepare_search_text(
        "\n".join(
            str(row.get(key) or "")
            for key in ("Title", "podcast_feed_title", "Description Text")
        )
    )


# Every order ends on the title so that ties — same day, same length, same
# show — keep a stable, readable sequence instead of feed order.
_EPISODE_SORT_KEYS: dict[str, Callable[[dict], tuple[Any, ...]]] = {
    _SORT_NEWEST: lambda row: (-_row_date(row), _row_title(row)),
    _SORT_OLDEST: lambda row: (_row_date(row), _row_title(row)),
    _SORT_UNPLAYED: lambda row: (_row_listened(row), -_row_date(row), _row_title(row)),
    _SORT_LONGEST: lambda row: (-_row_length(row), _row_title(row)),
    _SORT_SHORTEST: lambda row: (_row_length(row) or _UNKNOWN_DURATION, _row_title(row)),
    _SORT_LARGEST: lambda row: (-_row_size(row), _row_title(row)),
    _SORT_SHOW: lambda row: (_row_show(row), -_row_date(row), _row_title(row)),
    _SORT_TITLE: lambda row: (_row_title(row), -_row_date(row)),
}

# Sort options per view, in menu order. Labels name the outcome rather than the
# field, and the date labels differ because the date itself does: a show lists
# publication dates, the iPod lists when a file landed on the device.
_SHOW_SORT_OPTIONS: tuple[tuple[str, str], ...] = (
    ("Newest first", _SORT_NEWEST),
    ("Oldest first", _SORT_OLDEST),
    ("Unplayed first", _SORT_UNPLAYED),
    ("Longest first", _SORT_LONGEST),
    ("Shortest first", _SORT_SHORTEST),
    ("Title A–Z", _SORT_TITLE),
)
_FEED_SORT_OPTIONS: tuple[tuple[str, str], ...] = (
    ("Newest first", _SORT_NEWEST),
    ("Oldest first", _SORT_OLDEST),
    ("Unplayed first", _SORT_UNPLAYED),
    ("By show", _SORT_SHOW),
    ("Longest first", _SORT_LONGEST),
    ("Title A–Z", _SORT_TITLE),
)
_ON_IPOD_SORT_OPTIONS: tuple[tuple[str, str], ...] = (
    ("Recently added", _SORT_NEWEST),
    ("Oldest first", _SORT_OLDEST),
    ("Unplayed first", _SORT_UNPLAYED),
    ("By show", _SORT_SHOW),
    ("Largest first", _SORT_LARGEST),
    ("Title A–Z", _SORT_TITLE),
)
_SORT_OPTIONS_BY_VIEW: dict[str, tuple[tuple[str, str], ...]] = {
    _VIEW_SHOW: _SHOW_SORT_OPTIONS,
    _VIEW_FEED: _FEED_SORT_OPTIONS,
    _VIEW_ON_IPOD: _ON_IPOD_SORT_OPTIONS,
}

# What a search covers, per view: the field placeholder, then the longer
# phrasing for the tooltip. The placeholder has to survive a 190px field.
_SEARCH_SCOPE_BY_VIEW: dict[str, tuple[str, str]] = {
    _VIEW_SHOW: ("Find in this show", "this show"),
    _VIEW_FEED: ("Find an episode", "every subscribed episode"),
    _VIEW_ON_IPOD: ("Find on iPod", "the episodes on this iPod"),
}


def _is_synthetic_feed(feed: object) -> bool:
    """True for the placeholder feeds standing in for unsubscribed podcasts."""
    return str(getattr(feed, "feed_url", "") or "").startswith(_ORPHAN_FEED_PREFIX)

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_CSS_RGBA_RE = re.compile(r"rgba?\((\d+),(\d+),(\d+)(?:,(\d+))?\)")
_EPISODE_DESCRIPTION_MAX_CHARS = 1600
_EPISODE_CARD_MARGIN_X = 12
_EPISODE_CARD_MARGIN_Y = 6
_EPISODE_CARD_PADDING = 14
_EPISODE_CARD_RADIUS = 8
_EPISODE_CARD_ARTWORK_SIZE = 54
_EPISODE_CARD_VPAD = 10
_EPISODE_CARD_SPACING = 4
_EPISODE_TOP_ROW_GAP = 10
_EPISODE_TITLE_LABEL_GAP = 2
_EPISODE_ACTION_ROW_HEIGHT = 24
# Taken from the shared control so the card always reserves the whole box.
# Anything smaller and Qt clips the indicator's border off.
_EPISODE_CHECKBOX_SIZE = CHECKBOX_INDICATOR_SIZE
_EPISODE_CHECKBOX_GAP = 10
_EPISODE_DESC_COLLAPSED_LINES = 2
_EPISODE_STATUS_MAX_WIDTH = 132
_EPISODE_TITLE_MAX_HEIGHT = 42
_EPISODE_EXPANDED_MAX_LINES = 14
_EPISODE_ROW_GAP = 8
_EPISODE_ROW_BUFFER = 4


def _episode_description_text(description: str) -> str:
    """Return compact plain text suitable for the episode table."""
    text = _HTML_TAG_RE.sub(" ", str(description or ""))
    text = html.unescape(text)
    text = " ".join(text.split())
    if len(text) > _EPISODE_DESCRIPTION_MAX_CHARS:
        return f"{text[:_EPISODE_DESCRIPTION_MAX_CHARS - 3].rstrip()}..."
    return text


def _episode_key(feed, episode) -> str:
    """Stable table key for an episode within a feed."""
    return f"{getattr(feed, 'feed_url', '')}\0{getattr(episode, 'guid', '')}"


def _qcolor(value: str) -> QColor:
    """Parse theme CSS colors for direct QPainter usage."""
    text = str(value or "").replace(" ", "")
    match = _CSS_RGBA_RE.fullmatch(text)
    if match:
        r, g, b, a = match.groups()
        return QColor(int(r), int(g), int(b), int(a or 255))
    return QColor(value)


def _is_remote_artwork_source(source: str) -> bool:
    from iopenpod.podcasts.artwork import is_remote_artwork_source

    return is_remote_artwork_source(source)


def _resolve_local_artwork_path(source: str) -> Path | None:
    from iopenpod.podcasts.artwork import resolve_local_artwork_path

    return resolve_local_artwork_path(source)


def _read_local_artwork_bytes(source: str) -> bytes | None:
    from iopenpod.podcasts.artwork import read_local_artwork_bytes

    return read_local_artwork_bytes(source)


def _load_artwork_bytes(source: str) -> bytes | None:
    from iopenpod.podcasts.artwork import load_artwork_bytes

    return load_artwork_bytes(source)


def _status_accent(status: str) -> str:
    if status == "On iPod":
        return paint_css("status.success.text")
    if status == "Downloaded":
        return paint_css("control.primary.fill")
    if status == "Listened":
        return paint_css("status.warning.text")
    if "Downloading" in status:
        return paint_css("status.warning.text")
    return paint_css("text.tertiary")


def _is_state_status(status: str) -> bool:
    return status in {"On iPod", "Downloaded", "Listened"} or "Downloading" in status


def _episode_meta_text(row: dict) -> str:
    parts = []
    date_text = _fmt_date(float(row.get("date_added") or 0))
    if date_text:
        parts.append(date_text)
    duration_ms = int(row.get("length") or 0)
    if duration_ms > 0:
        parts.append(_fmt_duration(duration_ms // 1000))
    size = int(row.get("size") or 0)
    if size > 0:
        parts.append(format_size(size))
    status = str(row.get("ep_status") or "")
    if row.get("_was_listened") and status != "Listened":
        parts.append("Listened")
    if status and not _is_state_status(status) and status not in parts:
        parts.append(status)
    return "  |  ".join(parts) if parts else "Episode"


def _wrap_lines(
    text: str,
    metrics: QFontMetrics,
    width: int,
    max_lines: int | None = None,
) -> tuple[list[str], bool]:
    """Word-wrap plain text into measured lines."""
    clean = " ".join(str(text or "").split())
    if not clean or width <= 0:
        return [], False
    if max_lines is not None and max_lines <= 0:
        return [], True

    words = clean.split(" ")
    lines: list[str] = []
    line = ""
    truncated = False

    for idx, word in enumerate(words):
        candidate = word if not line else f"{line} {word}"
        if metrics.horizontalAdvance(candidate) <= width:
            line = candidate
            continue

        if line:
            lines.append(line)
            line = word
        else:
            lines.append(metrics.elidedText(word, Qt.TextElideMode.ElideRight, width))
            line = ""

        if max_lines is not None and len(lines) >= max_lines:
            rest = " ".join(([line] if line else []) + words[idx + 1:])
            if rest:
                lines[-1] = metrics.elidedText(
                    f"{lines[-1]} {rest}",
                    Qt.TextElideMode.ElideRight,
                    width,
                )
                truncated = True
            return lines[:max_lines], truncated

    if line:
        lines.append(line)

    if max_lines is not None and len(lines) > max_lines:
        visible = lines[:max_lines]
        visible[-1] = metrics.elidedText(
            " ".join(lines[max_lines - 1:]),
            Qt.TextElideMode.ElideRight,
            width,
        )
        return visible, True

    return lines, truncated


def _clamp_desc_lines(lines: int) -> int:
    """Lines a collapsed description occupies, never more than the cap."""
    return max(1, min(_EPISODE_DESC_COLLAPSED_LINES, lines))


def _wrapped_line_count(text: str) -> int:
    """Line count of text already wrapped by :func:`_wrap_lines`."""
    return str(text or "").count("\n") + 1


class _CardVerticalMetrics(NamedTuple):
    """Vertical geometry of one collapsed episode card."""

    top_h: int        # Artwork beside the show name and wrapped title.
    meta_h: int
    desc_min_h: int
    action_h: int     # Zero when the action row has nothing to show.
    total: int        # Full card frame height.


def _card_vertical_metrics(
    *,
    card_width: int,
    show_artwork: bool,
    has_podcast_label: bool,
    title_text: str,
    show_status: bool,
    show_action_row: bool,
    desc_lines: int = _EPISODE_DESC_COLLAPSED_LINES,
) -> _CardVerticalMetrics:
    """Measure a collapsed card so it is exactly as tall as its content.

    The pooled list needs a row's height before any widget exists for it, and
    the card lays itself out inside whatever height it is given. Deriving both
    from this one function keeps them from disagreeing — a disagreement shows
    up either as a band of dead space under every card or as a clipped
    action row.
    """
    small = QFontMetrics(QFont(FONT_FAMILY, Metrics.FONT_SM))
    title_metrics = QFontMetrics(
        QFont(FONT_FAMILY, Metrics.FONT_MD, QFont.Weight.DemiBold)
    )

    left = _EPISODE_CARD_PADDING + _EPISODE_CHECKBOX_SIZE + _EPISODE_CHECKBOX_GAP
    width = max(1, card_width - left - _EPISODE_CARD_PADDING)
    art_size = _EPISODE_CARD_ARTWORK_SIZE if show_artwork else 0
    title_x = left + (art_size + _EPISODE_TOP_ROW_GAP if show_artwork else 0)
    status_w = _EPISODE_STATUS_MAX_WIDTH if show_status else 0
    status_gap = _EPISODE_TOP_ROW_GAP if show_status else 0
    title_w = max(1, left + width - title_x - status_gap - status_w)

    podcast_h = small.lineSpacing() if has_podcast_label else 0
    bounds = title_metrics.boundingRect(
        QRect(0, 0, title_w, 200),
        Qt.TextFlag.TextWordWrap,
        title_text or "Untitled Episode",
    )
    title_h = min(
        max(title_metrics.lineSpacing(), bounds.height()),
        max(title_metrics.lineSpacing(), _EPISODE_TITLE_MAX_HEIGHT),
    )
    title_block_h = (
        podcast_h
        + (_EPISODE_TITLE_LABEL_GAP if has_podcast_label else 0)
        + title_h
    )
    top_h = max(art_size, title_block_h)

    meta_h = small.lineSpacing()
    desc_min_h = _clamp_desc_lines(desc_lines) * small.lineSpacing()
    action_h = _EPISODE_ACTION_ROW_HEIGHT if show_action_row else 0

    total = (
        _EPISODE_CARD_VPAD
        + top_h
        + _EPISODE_CARD_SPACING
        + meta_h
        + _EPISODE_CARD_SPACING
        + desc_min_h
        + (_EPISODE_CARD_SPACING + action_h if show_action_row else 0)
        + _EPISODE_CARD_VPAD
    )
    return _CardVerticalMetrics(top_h, meta_h, desc_min_h, action_h, total)


class _PodcastCardMouseButton(QPushButton):
    """Button that does not steal the row's selection state."""

    def mousePressEvent(self, e: QMouseEvent | None) -> None:
        if e is not None:
            e.accept()
        super().mousePressEvent(e)


class _PodcastEpisodeCard(QFrame):
    clicked = pyqtSignal(int, object)
    more_requested = pyqtSignal(int)
    check_toggled = pyqtSignal(int, bool)
    context_requested = pyqtSignal(int, QPoint)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._row_index = -1
        self._row_key = ""
        self._artwork_source = ""
        self._selected = False
        self._selection_active = False
        self._hovered = False

        self.setObjectName("podcastEpisodeCard")
        self.setMouseTracking(True)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._emit_context_menu)

        # The explicit multi-select affordance. It stays out of the way while
        # browsing and appears on hover, or for every row once a selection
        # exists, so building a batch never depends on a modifier key.
        self._check = QCheckBox(self)
        self._check.setObjectName("podcastEpisodeCheck")
        self._check.setStyleSheet(checkbox_css(Metrics.FONT_SM))
        self._check.setFixedSize(
            _EPISODE_CHECKBOX_SIZE,
            _EPISODE_CHECKBOX_SIZE,
        )
        self._check.setCursor(Qt.CursorShape.PointingHandCursor)
        self._check.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        # `clicked`, not `toggled`: these cards are pooled and rebound
        # constantly, and `toggled` also fires for programmatic state changes
        # and during teardown — re-entering the list to rebind widgets that may
        # already be gone.
        self._check.clicked.connect(self._emit_check_toggled)
        self._check.hide()

        self._art_label = QLabel(self)
        self._art_label.setObjectName("podcastEpisodeArtwork")
        self._art_label.setFixedSize(
            _EPISODE_CARD_ARTWORK_SIZE,
            _EPISODE_CARD_ARTWORK_SIZE,
        )
        self._art_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._art_label.setStyleSheet(f"""
            QLabel#podcastEpisodeArtwork {{
                background: {paint_css('surface.default')};
                border: 1px solid {paint_css('border.subtle')};
                border-radius: 7px;
            }}
        """)
        self._art_label.hide()

        self._podcast_label = make_label(
            "",
            size=Metrics.FONT_SM,
            weight=QFont.Weight.DemiBold,
            style=f"color: {paint_css('control.primary.hover_fill')};",
        )
        self._podcast_label.setParent(self)
        self._podcast_label.setObjectName("podcastEpisodePodcast")
        self._podcast_label.setWordWrap(False)

        self._title_label = make_label(
            "",
            size=Metrics.FONT_MD,
            weight=QFont.Weight.DemiBold,
        )
        self._title_label.setParent(self)
        self._title_label.setObjectName("podcastEpisodeTitle")
        self._title_label.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
        )
        self._title_label.setWordWrap(True)
        self._title_label.setMaximumHeight(_EPISODE_TITLE_MAX_HEIGHT)

        self._status_label = make_label(
            "",
            size=Metrics.FONT_SM,
            weight=QFont.Weight.DemiBold,
        )
        self._status_label.setParent(self)
        self._status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._status_label.setMinimumWidth(86)
        self._status_label.setMaximumWidth(_EPISODE_STATUS_MAX_WIDTH)

        self._meta_label = make_label("", size=Metrics.FONT_SM, style=LABEL_SECONDARY())
        self._meta_label.setParent(self)
        self._meta_label.setObjectName("podcastEpisodeMeta")
        self._meta_label.setWordWrap(False)
        self._meta_label.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
        )
        self._meta_label.setFixedHeight(QFontMetrics(self._meta_label.font()).lineSpacing())

        self._description_label = make_label(
            "",
            size=Metrics.FONT_SM,
            style=LABEL_SECONDARY(),
            wrap=True,
        )
        self._description_label.setParent(self)
        self._description_label.setObjectName("podcastEpisodeDescription")
        self._description_label.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
        )
        self._description_label.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Expanding,
        )

        self._action_row = QWidget(self)
        self._action_row.setObjectName("podcastEpisodeActionRow")
        self._action_row.setFixedHeight(_EPISODE_ACTION_ROW_HEIGHT)

        # Both adding and removing are driven by row selection plus the batch
        # action bar, so the card carries no add or remove button of its own.
        self._more_btn = _PodcastCardMouseButton("More", self._action_row)
        self._more_btn.setObjectName("podcastEpisodeMoreButton")
        self._more_btn.setFont(QFont(FONT_FAMILY, Metrics.FONT_SM, QFont.Weight.DemiBold))
        self._more_btn.setStyleSheet(
            btn_css(padding="3px 10px", radius=Metrics.BORDER_RADIUS_SM)
        )
        btn_metrics = QFontMetrics(self._more_btn.font())
        self._more_btn.setFixedSize(
            max(
                btn_metrics.horizontalAdvance("More"),
                btn_metrics.horizontalAdvance("Show less"),
            )
            + 24,
            _EPISODE_ACTION_ROW_HEIGHT,
        )
        self._more_btn.clicked.connect(lambda: self.more_requested.emit(self._row_index))

        self._install_child_event_filters()
        self._apply_style()

    def _install_child_event_filters(self) -> None:
        for child in (
            self._art_label,
            self._podcast_label,
            self._title_label,
            self._status_label,
            self._meta_label,
            self._description_label,
            self._action_row,
            self._more_btn,
        ):
            child.installEventFilter(self)

    def _emit_check_toggled(self, checked: bool) -> None:
        if self._row_index >= 0:
            self.check_toggled.emit(self._row_index, checked)

    def _update_check_visibility(self) -> None:
        """Reveal the checkbox on hover, and whenever a selection is active."""
        self._check.setVisible(
            self._selected or self._selection_active or self._hovered
        )

    def bind(
        self,
        *,
        row_index: int,
        row: dict,
        row_key: str,
        selected: bool,
        expanded: bool,
        selection_active: bool = False,
        description_text: str,
        show_more: bool,
        show_artwork: bool,
        artwork_source: str,
        artwork_pixmap: QPixmap | None,
    ) -> None:
        self._row_index = row_index
        self._row_key = row_key
        self._artwork_source = artwork_source if show_artwork else ""
        self._selected = selected
        self._selection_active = selection_active

        self._check.setChecked(selected)
        self._check.setAccessibleName(
            f"Select {row.get('Title') or 'episode'}"
        )
        self._update_check_visibility()

        self._art_label.setVisible(show_artwork)
        if show_artwork:
            if artwork_pixmap is not None:
                self._set_artwork_pixmap(artwork_pixmap)
            else:
                self._art_label.clear()
                self._art_label.setText("◎")
        else:
            self._art_label.clear()
            self._art_label.setText("")

        podcast_title = str(row.get("podcast_feed_title") or "")
        self._podcast_label.setText(podcast_title)
        self._podcast_label.setVisible(bool(podcast_title))
        self._title_label.setText(str(row.get("Title") or "Untitled Episode"))
        self._meta_label.setText(_episode_meta_text(row))
        self._description_label.setText(description_text or "No description available.")
        self._set_description_height(description_text, expanded)

        status = str(row.get("ep_status") or "")
        if _is_state_status(status):
            self._status_label.setText(status)
            self._status_label.show()
        else:
            self._status_label.hide()

        self._more_btn.setText("Show less" if expanded else "More")
        self._more_btn.setVisible(show_more)
        self._update_card_layout()
        self._apply_style()

    def set_artwork(self, source: str, pixmap: QPixmap) -> None:
        if not self._art_label.isVisible() or source != self._artwork_source:
            return
        self._set_artwork_pixmap(pixmap)

    def _set_artwork_pixmap(self, pixmap: QPixmap) -> None:
        size = _EPISODE_CARD_ARTWORK_SIZE
        pm = scale_pixmap_for_display(
            pixmap,
            size,
            size,
            widget=self._art_label,
            aspect_mode=Qt.AspectRatioMode.KeepAspectRatio,
            transform_mode=Qt.TransformationMode.SmoothTransformation,
        )
        self._art_label.setPixmap(pm)
        self._art_label.setText("")

    def _set_description_height(self, text: str, expanded: bool) -> None:
        metrics = QFontMetrics(self._description_label.font())
        if expanded:
            line_count = max(
                1,
                min(_EPISODE_EXPANDED_MAX_LINES, _wrapped_line_count(text)),
            )
        else:
            # Only the lines actually used, so a one-line summary does not
            # reserve a second line's worth of blank card.
            line_count = _clamp_desc_lines(_wrapped_line_count(text))
        self._description_label.setMinimumHeight(line_count * metrics.lineSpacing())
        self._description_label.setMaximumHeight(16777215)

    def _title_height_for_width(self, width: int) -> int:
        metrics = QFontMetrics(self._title_label.font())
        if width <= 0:
            return metrics.lineSpacing()
        text = self._title_label.text() or "Untitled Episode"
        bounds = metrics.boundingRect(
            QRect(0, 0, width, 200),
            Qt.TextFlag.TextWordWrap,
            text,
        )
        return min(
            max(metrics.lineSpacing(), bounds.height()),
            max(metrics.lineSpacing(), self._title_label.maximumHeight()),
        )

    def _update_card_layout(self) -> None:
        top = _EPISODE_CARD_VPAD

        # The checkbox owns a fixed leading column so rows stay aligned whether
        # or not it is currently revealed.
        self._check.setGeometry(
            _EPISODE_CARD_PADDING,
            top + (_EPISODE_CARD_ARTWORK_SIZE - _EPISODE_CHECKBOX_SIZE) // 2,
            _EPISODE_CHECKBOX_SIZE,
            _EPISODE_CHECKBOX_SIZE,
        )
        left = _EPISODE_CARD_PADDING + _EPISODE_CHECKBOX_SIZE + _EPISODE_CHECKBOX_GAP
        width = max(1, self.width() - left - _EPISODE_CARD_PADDING)

        art_visible = not self._art_label.isHidden()
        art_size = _EPISODE_CARD_ARTWORK_SIZE if art_visible else 0
        if art_visible:
            self._art_label.setGeometry(left, top, art_size, art_size)
            title_x = left + art_size + _EPISODE_TOP_ROW_GAP
        else:
            self._art_label.setGeometry(left, top, 0, 0)
            title_x = left

        status_visible = not self._status_label.isHidden()
        status_w = self._status_label.maximumWidth() if status_visible else 0
        status_gap = _EPISODE_TOP_ROW_GAP if status_visible else 0
        title_w = max(1, left + width - title_x - status_gap - status_w)

        podcast_visible = not self._podcast_label.isHidden()
        podcast_h = (
            QFontMetrics(self._podcast_label.font()).lineSpacing()
            if podcast_visible
            else 0
        )
        title_h = self._title_height_for_width(title_w)
        title_y = top
        if podcast_visible:
            self._podcast_label.setGeometry(title_x, top, title_w, podcast_h)
            title_y = top + podcast_h + _EPISODE_TITLE_LABEL_GAP
        else:
            self._podcast_label.setGeometry(title_x, top, title_w, 0)
        self._title_label.setGeometry(title_x, title_y, title_w, title_h)

        if status_visible:
            status_h = self._status_label.sizeHint().height()
            self._status_label.setGeometry(
                left + width - status_w,
                top,
                status_w,
                status_h,
            )
        else:
            self._status_label.setGeometry(left + width, top, 0, 0)

        title_block_h = (
            podcast_h
            + (_EPISODE_TITLE_LABEL_GAP if podcast_visible else 0)
            + title_h
        )
        top_h = max(art_size, title_block_h)

        meta_y = top + top_h + _EPISODE_CARD_SPACING
        meta_h = self._meta_label.minimumHeight()
        self._meta_label.setGeometry(left, meta_y, width, meta_h)

        desc_y = meta_y + meta_h + _EPISODE_CARD_SPACING
        # With nothing to put in it, the action row claims no height — its
        # reserved band was the dead space under every short episode.
        action_h = (
            _EPISODE_ACTION_ROW_HEIGHT if not self._more_btn.isHidden() else 0
        )
        action_y = max(
            desc_y + self._description_label.minimumHeight() + _EPISODE_CARD_SPACING,
            self.height() - _EPISODE_CARD_VPAD - action_h,
        )
        self._action_row.setGeometry(left, action_y, width, action_h)

        self._more_btn.setGeometry(
            max(0, width - self._more_btn.width()),
            0,
            self._more_btn.width(),
            _EPISODE_ACTION_ROW_HEIGHT,
        )

        desc_h = max(
            self._description_label.minimumHeight(),
            action_y - desc_y - _EPISODE_CARD_SPACING,
        )
        self._description_label.setGeometry(left, desc_y, width, desc_h)

    def _apply_style(self) -> None:
        bg = paint_css("podcast.episode.selected_fill") if self._selected else paint_css("podcast.episode.fill")
        border = paint_css("podcast.episode.selected_border") if self._selected else paint_css("podcast.episode.border")
        self.setStyleSheet(f"""
            QFrame#podcastEpisodeCard {{
                background: {bg};
                border: 1px solid {border};
                border-radius: {_EPISODE_CARD_RADIUS}px;
            }}
        """)

        status = self._status_label.text()
        accent = _status_accent(status)
        self._status_label.setStyleSheet(f"""
            QLabel {{
                color: {accent};
                background: {paint_css('podcast.episode.status_fill')};
                border: 1px solid {accent};
                border-radius: 7px;
                padding: 2px 8px;
            }}
        """)

    def resizeEvent(self, a0: QResizeEvent | None) -> None:
        super().resizeEvent(a0)
        self._update_card_layout()

    def enterEvent(self, event) -> None:  # type: ignore[override]
        self._hovered = True
        self._update_check_visibility()
        super().enterEvent(event)

    def leaveEvent(self, a0: QEvent | None) -> None:
        self._hovered = False
        self._update_check_visibility()
        super().leaveEvent(a0)

    def contextMenuEvent(self, a0: QContextMenuEvent | None) -> None:
        if a0 is not None:
            self._emit_context_menu(a0.pos())
            a0.accept()
            return
        super().contextMenuEvent(a0)

    def eventFilter(self, a0: QObject | None, a1: QEvent | None) -> bool:
        if a1 is None:
            return super().eventFilter(a0, a1)

        if a1.type() == QEvent.Type.ContextMenu:
            context_event = cast(QContextMenuEvent, a1)
            if isinstance(a0, QWidget):
                pos = self.mapFromGlobal(context_event.globalPos())
            else:
                pos = context_event.pos()
            self._emit_context_menu(pos)
            context_event.accept()
            return True

        if a1.type() == QEvent.Type.MouseButtonPress:
            mouse_event = cast(QMouseEvent, a1)
            if a0 is self._more_btn:
                return super().eventFilter(a0, a1)
            if mouse_event.button() == Qt.MouseButton.LeftButton:
                self.clicked.emit(self._row_index, mouse_event.modifiers())
                mouse_event.accept()
                return True

        return super().eventFilter(a0, a1)

    def mousePressEvent(self, a0: QMouseEvent | None) -> None:
        if a0 is not None and a0.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self._row_index, a0.modifiers())
            a0.accept()
            return
        super().mousePressEvent(a0)

    def _emit_context_menu(self, pos: QPoint) -> None:
        self.context_requested.emit(self._row_index, pos)


class _PodcastEpisodeScrollArea(QScrollArea):
    """Compatibility wrapper around the pooled episode renderer."""

    def __init__(self, episode_list: _PodcastEpisodeList) -> None:
        super().__init__(episode_list)
        self._episode_list = episode_list
        self.setWidgetResizable(False)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.setAutoFillBackground(False)
        pal = self.palette()
        pal.setColor(QPalette.ColorRole.Window, QColor(0, 0, 0, 0))
        pal.setColor(QPalette.ColorRole.Base, QColor(0, 0, 0, 0))
        self.setPalette(pal)
        viewport = self.viewport()
        if viewport is not None:
            viewport.setPalette(pal)
            viewport.setAutoFillBackground(False)

    def rowAt(self, y: int) -> int:
        return self._episode_list.row_at_viewport_y(y)

    def resizeEvent(self, a0: QResizeEvent | None) -> None:
        super().resizeEvent(a0)
        self._episode_list.schedule_viewport_refresh(force=True)


class _PodcastEpisodeContent(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.setMinimumWidth(0)


class _PodcastEpisodeList(QFrame):
    """Pooled, lazy episode card list with in-card description expansion."""

    def __init__(self, owner: PodcastBrowser):
        super().__init__(owner)
        self._owner = owner
        self._columns = _PODCAST_EPISODE_COLUMNS.copy()
        self._all_tracks: list[dict] = []
        self._tracks: list[dict] = []
        self._is_playlist_mode = False
        self._current_filter = None
        self._load_id = 0

        self._expanded_keys: set[str] = set()
        self._selected_rows: set[int] = set()
        self._selection_anchor: int | None = None
        self._selection_was_active = False
        self._row_heights: list[int] = []
        self._row_offsets: list[int] = [0]
        self._expanded_text_cache: dict[tuple[str, int], tuple[str, int]] = {}
        self._collapsed_height_cache: dict[tuple[str, int], int] = {}

        self._widget_pool: list[_PodcastEpisodeCard] = []
        self._visible_widgets: dict[int, _PodcastEpisodeCard] = {}
        self._refresh_scheduled = False
        self._refresh_force = False
        self._last_visible_range: tuple[int, int, int] | None = None
        self._requested_artwork_sources: set[str] = set()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.table = _PodcastEpisodeScrollArea(self)
        self._content = _PodcastEpisodeContent()
        self.table.setWidget(self._content)
        self.table.customContextMenuRequested.connect(owner._on_episode_context_menu)
        self.table.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        layout.addWidget(self.table)

        bar = self.table.verticalScrollBar()
        if bar is not None:
            bar.valueChanged.connect(lambda _value: self.schedule_viewport_refresh())

    @staticmethod
    def build(owner: PodcastBrowser) -> _PodcastEpisodeList:
        return _PodcastEpisodeList(owner)

    def set_rows(self, rows: list[dict], columns: list[str]) -> None:
        # Carry the selection across by identity, not by position. Row indices
        # shift whenever the list is re-sorted, filtered, or shrunk by a
        # removal, and reusing them would silently reselect other episodes.
        previously_selected = self.selected_keys()

        self._columns = columns.copy()
        self._all_tracks = rows
        self._tracks = rows
        self._is_playlist_mode = False
        self._current_filter = None
        self._load_id += 1
        valid_keys = {self._row_key(row) for row in rows}
        self._expanded_keys.intersection_update(valid_keys)
        self._selected_rows = {
            index
            for index, row in enumerate(rows)
            if self._row_key(row) in previously_selected
        }
        self._selection_anchor = None
        self._selection_was_active = bool(self._selected_rows)
        self._expanded_text_cache.clear()
        self._collapsed_height_cache.clear()
        self._requested_artwork_sources.clear()
        self._rebuild_heights()
        self._reset_scroll_position()
        self.schedule_viewport_refresh(force=True)
        self._notify_selection_changed()

    def _notify_selection_changed(self) -> None:
        """Tell the owner the selection moved, if it cares.

        Matched to how card action handlers are resolved: the list stays
        usable with owners that do not implement the hook.
        """
        handler = getattr(self._owner, "_refresh_episode_selection_bar", None)
        if callable(handler):
            handler()

    def selected_rows(self) -> list[int]:
        return sorted(row for row in self._selected_rows if row < len(self._tracks))

    def selected_keys(self) -> set[str]:
        """Stable identities of the selected rows, safe across re-sorts."""
        return {
            self._row_key(self._tracks[row])
            for row in self._selected_rows
            if 0 <= row < len(self._tracks)
        }

    def row_count(self) -> int:
        return len(self._tracks)

    def clear_selection(self) -> None:
        if not self._selected_rows:
            return
        old_rows = set(self._selected_rows)
        self._selected_rows.clear()
        self._selection_anchor = None
        self._update_selection_for_rows(old_rows)
        self._notify_selection_changed()

    def select_row(self, row: int) -> None:
        if not (0 <= row < len(self._tracks)):
            return
        old_rows = set(self._selected_rows)
        self._selected_rows = {row}
        self._selection_anchor = row
        self._update_selection_for_rows(old_rows | {row})
        self._notify_selection_changed()

    def select_all(self) -> None:
        """Select every row currently listed, respecting any active filter."""
        if not self._tracks:
            return
        old_rows = set(self._selected_rows)
        self._selected_rows = set(range(len(self._tracks)))
        self._update_selection_for_rows(old_rows | self._selected_rows)
        self._notify_selection_changed()

    def row_at_viewport_y(self, y: int) -> int:
        bar = self.table.verticalScrollBar()
        scroll = bar.value() if bar is not None else 0
        return self._row_at_content_y(scroll + y)

    def _reset_scroll_position(self) -> None:
        bar = self.table.verticalScrollBar()
        if bar is not None:
            bar.setValue(0)

    def schedule_viewport_refresh(self, *, force: bool = False) -> None:
        if force:
            self._refresh_force = True
            self._last_visible_range = None
        if self._refresh_scheduled:
            return
        self._refresh_scheduled = True
        QTimer.singleShot(0, self._refresh_viewport)

    def _row_key(self, row: dict) -> str:
        return str(row.get("_ep_key") or row.get("_ep_guid") or id(row))

    def _shows_podcast_artwork(self) -> bool:
        return "podcast_feed_title" in self._columns

    def _collapsed_height_for_row(self, row: dict) -> int:
        """Height of one collapsed row, sized to that row's own content."""
        card_width = self._card_width()
        cache_key = (self._row_key(row), card_width)
        cached = self._collapsed_height_cache.get(cache_key)
        if cached is not None:
            return cached

        text, truncated = self._collapsed_description(row, card_width)
        metrics = _card_vertical_metrics(
            card_width=card_width,
            show_artwork=self._shows_podcast_artwork(),
            has_podcast_label=bool(row.get("podcast_feed_title")),
            title_text=str(row.get("Title") or ""),
            show_status=_is_state_status(str(row.get("ep_status") or "")),
            show_action_row=truncated,
            desc_lines=_wrapped_line_count(text),
        )
        # Row pitch includes the gap the viewport subtracts back off again.
        height = metrics.total + _EPISODE_ROW_GAP
        self._collapsed_height_cache[cache_key] = height
        return height

    def _rebuild_heights(self) -> None:
        self._row_heights = [
            self._collapsed_height_for_row(row) for row in self._tracks
        ]
        for i, row in enumerate(self._tracks):
            if self._row_key(row) in self._expanded_keys:
                self._row_heights[i] = self._height_for_row(i, row)
        self._row_offsets = [0]
        total = 0
        for height in self._row_heights:
            total += height + _EPISODE_ROW_GAP
            self._row_offsets.append(total)
        self._content.setMinimumHeight(total)
        self._content.resize(self._content_width(), total)

    def _content_width(self) -> int:
        viewport = self.table.viewport()
        width = viewport.width() if viewport is not None else self.width()
        return max(240, width)

    def _card_width(self) -> int:
        return max(180, self._content_width() - 2 * _EPISODE_CARD_MARGIN_X)

    def _height_for_row(self, _index: int, row: dict) -> int:
        key = self._row_key(row)
        if key not in self._expanded_keys:
            return self._collapsed_height_for_row(row)
        _text, height = self._expanded_description(row, self._card_width())
        return height

    def _expanded_description(self, row: dict, card_width: int) -> tuple[str, int]:
        key = self._row_key(row)
        text_width = max(80, card_width - 2 * _EPISODE_CARD_PADDING)
        cache_key = (key, text_width)
        cached = self._expanded_text_cache.get(cache_key)
        if cached is not None:
            return cached

        metrics = QFontMetrics(QFont(FONT_FAMILY, Metrics.FONT_SM))
        lines, truncated = _wrap_lines(
            row.get("Description Text", ""),
            metrics,
            text_width,
            max_lines=_EPISODE_EXPANDED_MAX_LINES,
        )
        display = "\n".join(lines)
        if truncated and display and not display.endswith("..."):
            display = f"{display.rstrip()}..."
        line_count = max(1, len(lines))
        base_height = self._collapsed_height_for_row(row)
        height = max(
            base_height,
            116 + line_count * (metrics.height() + 3),
        )
        height = min(height, 420)
        cached = (display, height)
        self._expanded_text_cache[cache_key] = cached
        return cached

    def _collapsed_description(self, row: dict, card_width: int) -> tuple[str, bool]:
        text_width = max(80, card_width - 2 * _EPISODE_CARD_PADDING)
        metrics = QFontMetrics(QFont(FONT_FAMILY, Metrics.FONT_SM))
        lines, truncated = _wrap_lines(
            row.get("Description Text", ""),
            metrics,
            text_width,
            max_lines=_EPISODE_DESC_COLLAPSED_LINES,
        )
        return "\n".join(lines), truncated

    def _row_at_content_y(self, y: int) -> int:
        import bisect

        if not self._tracks:
            return -1
        index = bisect.bisect_right(self._row_offsets, max(0, y)) - 1
        return min(max(index, 0), len(self._tracks) - 1)

    def _visible_range(self) -> tuple[int, int]:
        if not self._tracks:
            return 0, 0
        bar = self.table.verticalScrollBar()
        scroll = bar.value() if bar is not None else 0
        viewport = self.table.viewport()
        viewport_height = viewport.height() if viewport is not None else self.height()
        start = max(0, self._row_at_content_y(scroll) - _EPISODE_ROW_BUFFER)
        end = min(
            len(self._tracks),
            self._row_at_content_y(scroll + viewport_height) + _EPISODE_ROW_BUFFER + 1,
        )
        return start, end

    def _refresh_viewport(self) -> None:
        self._refresh_scheduled = False
        width = self._content_width()
        total_height = self._row_offsets[-1] if self._row_offsets else 0
        if self._content.width() != width:
            self._expanded_text_cache.clear()
            self._collapsed_height_cache.clear()
            self._rebuild_heights()
            width = self._content_width()
            total_height = self._row_offsets[-1] if self._row_offsets else 0
        self._content.resize(width, total_height)

        start, end = self._visible_range()
        view_state = (start, end, width)
        if self._last_visible_range == view_state and not self._refresh_force:
            return
        self._last_visible_range = view_state
        self._refresh_force = False

        needed = set(range(start, end))
        for row_index in list(self._visible_widgets.keys()):
            if row_index not in needed:
                self._release_widget(row_index)

        card_width = self._card_width()
        for row_index in range(start, end):
            row = self._tracks[row_index]
            widget = self._visible_widgets.get(row_index)
            if widget is None:
                widget = self._acquire_widget()
                self._visible_widgets[row_index] = widget
            self._bind_widget(widget, row_index, row)
            widget.setGeometry(
                QRect(
                    _EPISODE_CARD_MARGIN_X,
                    self._row_offsets[row_index] + _EPISODE_CARD_MARGIN_Y,
                    card_width,
                    max(1, self._row_heights[row_index] - _EPISODE_ROW_GAP),
                )
            )
            widget.show()

    def _acquire_widget(self) -> _PodcastEpisodeCard:
        if self._widget_pool:
            widget = self._widget_pool.pop()
            widget.setParent(self._content)
            return widget
        widget = _PodcastEpisodeCard(self._content)
        widget.clicked.connect(self._on_card_clicked)
        widget.more_requested.connect(self._toggle_expanded)
        widget.check_toggled.connect(self._on_card_check_toggled)
        widget.context_requested.connect(self._on_card_context_menu)
        return widget

    def _release_widget(self, row_index: int) -> None:
        widget = self._visible_widgets.pop(row_index, None)
        if widget is None:
            return
        widget.hide()
        self._widget_pool.append(widget)

    def _bind_widget(self, widget: _PodcastEpisodeCard, row_index: int, row: dict) -> None:
        key = self._row_key(row)
        expanded = key in self._expanded_keys
        card_width = self._card_width()
        if expanded:
            description_text, _height = self._expanded_description(row, card_width)
            show_more = True
        else:
            description_text, show_more = self._collapsed_description(row, card_width)
        show_artwork = self._shows_podcast_artwork()
        artwork_source = (
            str(row.get("_podcast_artwork_source") or "") if show_artwork else ""
        )
        artwork_pixmap = None
        if show_artwork:
            artwork_pixmap = _artwork_cache.get(artwork_source)
            if artwork_pixmap is None:
                artwork_pixmap = self._owner._artwork_placeholder_pixmap(
                    _EPISODE_CARD_ARTWORK_SIZE,
                )
        widget.bind(
            row_index=row_index,
            row=row,
            row_key=key,
            selected=row_index in self._selected_rows,
            selection_active=bool(self._selected_rows),
            expanded=expanded,
            description_text=description_text,
            show_more=show_more or expanded,
            show_artwork=show_artwork,
            artwork_source=artwork_source,
            artwork_pixmap=artwork_pixmap,
        )
        if (
            show_artwork
            and artwork_source
            and artwork_source not in _artwork_cache
            and artwork_source not in self._requested_artwork_sources
        ):
            self._requested_artwork_sources.add(artwork_source)
            self._owner._request_artwork(
                artwork_source,
                self._apply_artwork_to_visible_cards,
            )

    def _apply_artwork_to_visible_cards(self, source: str, pixmap: QPixmap) -> None:
        for widget in self._visible_widgets.values():
            widget.set_artwork(source, pixmap)

    def _toggle_expanded(self, row_index: int) -> None:
        if not (0 <= row_index < len(self._tracks)):
            return
        key = self._row_key(self._tracks[row_index])
        if key in self._expanded_keys:
            self._expanded_keys.remove(key)
        else:
            self._expanded_keys.add(key)
        old_height = self._row_heights[row_index]
        new_height = self._height_for_row(row_index, self._tracks[row_index])
        if new_height != old_height:
            delta = new_height - old_height
            self._row_heights[row_index] = new_height
            for i in range(row_index + 1, len(self._row_offsets)):
                self._row_offsets[i] += delta
            total_height = self._row_offsets[-1] if self._row_offsets else 0
            self._content.setMinimumHeight(total_height)
            self._content.resize(self._content_width(), total_height)
        self.schedule_viewport_refresh(force=True)

    def _on_card_clicked(
        self,
        row_index: int,
        modifiers: Qt.KeyboardModifier,
    ) -> None:
        """Platform-standard click semantics for the card body.

        Plain click selects one row rather than accumulating a batch: with a
        destructive action in the batch bar, an idle click must never quietly
        grow the set of episodes about to be removed. Batches are built with
        the per-card checkbox, or with the usual modifiers.
        """
        if not (0 <= row_index < len(self._tracks)):
            return

        old_rows = set(self._selected_rows)
        shift = bool(modifiers & Qt.KeyboardModifier.ShiftModifier)
        toggle = bool(
            modifiers
            & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.MetaModifier)
        )

        if shift and self._selection_anchor is not None:
            lo, hi = sorted((self._selection_anchor, row_index))
            self._selected_rows = set(range(lo, hi + 1))
        elif toggle:
            if row_index in self._selected_rows:
                self._selected_rows.remove(row_index)
            else:
                self._selected_rows.add(row_index)
            self._selection_anchor = row_index
        else:
            self._selected_rows = {row_index}
            self._selection_anchor = row_index

        self._update_selection_for_rows(old_rows | self._selected_rows)
        self._notify_selection_changed()

    def _on_card_check_toggled(self, row_index: int, checked: bool) -> None:
        """Checkbox clicks touch exactly one row and nothing else."""
        if not (0 <= row_index < len(self._tracks)):
            return
        if checked:
            self._selected_rows.add(row_index)
        else:
            self._selected_rows.discard(row_index)
        self._selection_anchor = row_index
        self._update_selection_for_rows({row_index})
        self._notify_selection_changed()

    def _on_card_context_menu(self, row_index: int, pos: QPoint) -> None:
        widget = self._visible_widgets.get(row_index)
        if widget is None:
            return
        viewport = self.table.viewport()
        if viewport is None:
            return
        viewport_pos = viewport.mapFromGlobal(widget.mapToGlobal(pos))
        self.table.customContextMenuRequested.emit(viewport_pos)

    def _update_selection_for_rows(self, rows: set[int]) -> None:
        # Whether *any* row is selected drives checkbox visibility on every
        # card, so crossing that boundary has to rebind the whole viewport.
        active = bool(self._selected_rows)
        if active != self._selection_was_active:
            self._selection_was_active = active
            rows = set(self._visible_widgets) | rows

        for row in rows:
            widget = self._visible_widgets.get(row)
            if widget is not None and 0 <= row < len(self._tracks):
                self._bind_widget(widget, row, self._tracks[row])

    def _recycle_all_visible_widgets(self) -> None:
        for row_index in list(self._visible_widgets):
            self._release_widget(row_index)

    def keyPressEvent(self, a0: QKeyEvent | None) -> None:
        """Standard selection keys for a multi-select list."""
        if a0 is None:
            super().keyPressEvent(a0)
            return

        key = a0.key()
        select_all = bool(
            a0.modifiers()
            & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.MetaModifier)
        ) and key == Qt.Key.Key_A

        if select_all:
            self.select_all()
            a0.accept()
            return
        if key == Qt.Key.Key_Escape and self._selected_rows:
            self.clear_selection()
            a0.accept()
            return
        if key in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace) and self._selected_rows:
            handler = getattr(self._owner, "_on_remove_episode_selection", None)
            if callable(handler):
                handler()
                a0.accept()
                return

        super().keyPressEvent(a0)


# ── Feed artwork cache ───────────────────────────────────────────────────────
# Maps artwork source path/URL → QPixmap so that repeated list refreshes don't re-download.
_artwork_cache: dict[str, QPixmap] = {}
_artwork_color_cache: dict[str, tuple[int, int, int]] = {}


class PodcastBrowser(QFrame):
    """Full podcast management widget.

    Must be initialised with ``set_device(serial, ipod_path)`` before use.
    """

    # Emitted when the user confirms podcast sync — carries a SyncPlan
    podcast_sync_requested = pyqtSignal(object)

    def __init__(
        self,
        settings_service: SettingsService,
        device_sessions: DeviceSessionService,
        libraries: LibraryService,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self._settings_service = settings_service
        self._device_sessions = device_sessions
        self._library_cache: LibraryCacheLike = libraries.cache()
        self._device_serial: str = ""
        self._ipod_path: str = ""
        self._store = None          # SubscriptionStore (lazy)
        self._selected_feed = None  # Current PodcastFeed
        self._view_mode = _VIEW_SHOW
        self._deferred_reconcile_tracks: list[dict] | None = None
        self._episode_by_guid: dict[str, object] = {}
        self._episode_feed_by_key: dict[str, object] = {}
        self._episode_dicts: list[dict] = []
        self._artwork_inflight: dict[str, list[Callable[[str, QPixmap], None]]] = {}
        self._episode_state_retry: Callable[[], None] | None = None

        # Everything the current view could list, before search and sort. Rows
        # are built once per view change and re-presented from here, so typing
        # never costs another read of the iPod or the subscription store.
        self._episode_source_rows: list[dict] = []
        self._episode_columns: list[str] = _PODCAST_EPISODE_COLUMNS.copy()
        self._episode_empty_state = ("", "", "broadcast")
        self._episode_query = ""
        # The view the current query belongs to. A filter left behind on a
        # different show would silently hide rows with no visible cause.
        self._filtered_view_key = ""
        self._sort_by_view: dict[str, str] = {}

        self._status_clear_text = ""
        self._status_clear_timer = QTimer(self)
        self._status_clear_timer.setSingleShot(True)
        self._status_clear_timer.timeout.connect(self._clear_status_timeout)
        self._action_status_clear_text = ""
        self._action_status_clear_timer = QTimer(self)
        self._action_status_clear_timer.setSingleShot(True)
        self._action_status_clear_timer.timeout.connect(self._clear_action_timeout)

        self._build_ui()

    def _current_ipod_tracks(self) -> list[dict] | None:
        try:
            if not self._library_cache.is_ready():
                return None
            return self._library_cache.get_tracks() or []
        except Exception:
            return None

    # ── Current view ─────────────────────────────────────────────────────

    @property
    def _showing_combined_feed(self) -> bool:
        """True while the plain chronological feed of all shows is visible."""
        return self._view_mode == _VIEW_FEED

    @property
    def _showing_on_ipod(self) -> bool:
        """True while the device-truth "On iPod" view is visible."""
        return self._view_mode == _VIEW_ON_IPOD

    def _current_view_key(self) -> str:
        """The feed-list ``UserRole`` key identifying the visible view."""
        if self._view_mode == _VIEW_FEED:
            return _COMBINED_FEED_KEY
        if self._view_mode == _VIEW_ON_IPOD:
            return _ON_IPOD_KEY
        return getattr(self._selected_feed, "feed_url", "") or ""

    def _refresh_current_view(self) -> None:
        """Re-render whichever episode view is active.

        Every status change — reconciliation, RSS refresh, listened toggles,
        download removal, post-sync refresh — routes through here so a newly
        added view can never be missed by one of those call sites.
        """
        if self._view_mode == _VIEW_ON_IPOD:
            self._show_on_ipod_episodes()
            return
        if self._view_mode == _VIEW_FEED:
            self._show_combined_feed()
            return
        if self._selected_feed is None:
            return
        if self._store is not None:
            refreshed = self._store.get_feed(self._selected_feed.feed_url)
            if refreshed is not None:
                self._selected_feed = refreshed
        self._show_episodes(self._selected_feed)

    # ── Public API ───────────────────────────────────────────────────────

    def set_device(self, serial: str, ipod_path: str) -> None:
        """Bind to a specific iPod device.  Loads subscriptions."""
        normalized_serial = serial or "_default"
        normalized_path = ipod_path or ""
        same_device = (
            self._store is not None
            and self._device_serial == normalized_serial
            and self._ipod_path == normalized_path
        )

        # Fast path for tab switches: avoid rebuilding store + list when the
        # selected iPod has not changed.
        if same_device:
            if self._deferred_reconcile_tracks is not None:
                deferred = self._deferred_reconcile_tracks
                self._deferred_reconcile_tracks = None
                self.reconcile_ipod_statuses(deferred)
            return

        self._device_serial = normalized_serial
        self._ipod_path = normalized_path

        from iopenpod.podcasts.subscription_store import SubscriptionStore
        settings = self._settings_service.get_effective_settings()
        session = self._device_sessions.current_session()
        storage = session.storage
        self._store = SubscriptionStore(
            ipod_path,
            download_cache_dir=settings.transcode_cache_dir,
            reported_volume_format=(
                storage.reported_volume_format if storage is not None else ""
            ),
            expected_volume_identity_key=(
                storage.volume_identity_key if storage is not None else ""
            ),
        )
        try:
            self._store.load()
        except Exception as exc:
            log.exception("Could not safely load podcast subscriptions")
            self._store = None
            self._feed_list.clear()
            self._episode_list.set_rows([], _PODCAST_EPISODE_COLUMNS)
            self._stack.setCurrentIndex(0)
            self._set_status("Podcast data could not be loaded safely")
            QMessageBox.critical(
                self,
                "Podcast Data Not Loaded",
                "iOpenPod could not safely read the podcast subscriptions on "
                f"this iPod and left them unchanged.\n\n{exc}\n\nReconnect and "
                "reload the iPod before making podcast changes.",
            )
            return

        # Apply any deferred reconciliation captured before the Podcasts
        # view/store was initialized (e.g. app.py data-ready timing).
        if self._deferred_reconcile_tracks is not None:
            deferred = self._deferred_reconcile_tracks
            self._deferred_reconcile_tracks = None
            self.reconcile_ipod_statuses(deferred)
        else:
            self.reconcile_ipod_statuses()

        self._refresh_feed_list()

        # Eagerly refresh all feeds from RSS so the full episode catalog
        # is available (the store only persists on-iPod/downloaded episodes).
        if self._store.get_feeds():
            self._refresh_all_feeds_bg()

    def clear(self) -> None:
        """Reset all state (called on device change)."""
        global _artwork_cache, _artwork_color_cache
        _artwork_cache.clear()
        _artwork_color_cache.clear()
        self._artwork_inflight.clear()

        self._store = None
        self._selected_feed = None
        self._view_mode = _VIEW_SHOW
        self._deferred_reconcile_tracks = None
        self._episode_by_guid.clear()
        self._episode_feed_by_key.clear()
        if hasattr(self, '_session_refreshed'):
            self._session_refreshed.clear()

        # A filter left over from the previous iPod would silently hide rows
        # on the next one, with no visible cause.
        self._reset_episode_filters()
        self._library_header.hide()

        self._feed_list.clear()
        self._episode_list.set_rows([], _PODCAST_EPISODE_COLUMNS)
        self._episode_dicts = []
        self._status_label.setText("")
        self._stack.setCurrentIndex(0)

    def _persist_subscription_change(
        self,
        action: str,
        operation: Callable[[], object],
    ) -> bool:
        """Run one device-store mutation and alert instead of masking refusal."""
        try:
            operation()
            return True
        except Exception as exc:
            log.exception("Could not %s", action)
            if self._store is not None:
                try:
                    self._store.load()
                except Exception:
                    log.exception("Could not reload podcast subscriptions after failure")
            QMessageBox.critical(
                self,
                "Podcast Changes Not Saved",
                f"iOpenPod stopped before it could {action}.\n\n{exc}\n\n"
                "Reconnect and reload the iPod before trying again.",
            )
            self._set_status("Podcast changes were not saved")
            return False

    def reconcile_ipod_statuses(self, ipod_tracks: list[dict] | None = None) -> None:
        """Reconcile stored episode state with the current iPod track list.

        This keeps "Downloaded" / "On iPod" statuses accurate even when
        feeds are loaded after iTunesDB parsing or tracks were removed.
        """
        store = self._store
        if store is None:
            # Store tracks for later reconciliation when set_device() creates
            # the SubscriptionStore after the Podcasts tab is opened.
            if ipod_tracks is not None:
                self._deferred_reconcile_tracks = list(ipod_tracks)
            return

        if ipod_tracks is None:
            current_tracks = self._current_ipod_tracks()
            if current_tracks is None:
                return
            ipod_tracks = current_tracks

        from iopenpod.podcasts.podcast_sync import PodcastTrackMatcher

        feeds = store.get_feeds()
        matcher = PodcastTrackMatcher(ipod_tracks)
        changed_feeds: list = []

        for feed in feeds:
            if matcher.match_feed(feed):
                changed_feeds.append(feed)

        if changed_feeds:
            if not self._persist_subscription_change(
                "update podcast status",
                lambda: store.update_feeds(changed_feeds),
            ):
                return

        self._refresh_current_view()

    # ── UI construction ──────────────────────────────────────────────────

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── Toolbar ──────────────────────────────────────────────────────
        toolbar = self._build_toolbar()
        root.addWidget(toolbar)

        # ── Stacked widget: empty state vs. main content ─────────────────
        self._stack = QStackedWidget()

        # Page 0: Empty state
        self._empty_page = self._build_empty_page()
        self._stack.addWidget(self._empty_page)

        # Page 1: Main splitter
        self._main_page = self._build_main_page()
        self._stack.addWidget(self._main_page)

        self._stack.setCurrentIndex(0)
        root.addWidget(self._stack, stretch=1)

    def _build_toolbar(self) -> QWidget:
        bar = BrowserHeroHeader("Podcasts", self)
        layout = bar.actions_layout

        self._add_btn = QPushButton("Add Podcast")
        self._add_btn.setFont(QFont(FONT_FAMILY, (Metrics.FONT_SM)))
        self._add_btn.setStyleSheet(chrome_action_btn_css())
        _add_ic = glyph_icon("plus", (14), paint_css("text.primary"))
        if _add_ic:
            self._add_btn.setIcon(_add_ic)
            self._add_btn.setIconSize(QSize((14), (14)))
        self._add_btn.clicked.connect(self._on_search)
        layout.addWidget(self._add_btn)

        self._refresh_btn = QPushButton("Refresh All")
        self._refresh_btn.setFont(QFont(FONT_FAMILY, (Metrics.FONT_SM)))
        self._refresh_btn.setStyleSheet(chrome_action_btn_css())
        _refresh_ic = glyph_icon("refresh", (14), paint_css("text.primary"))
        if _refresh_ic:
            self._refresh_btn.setIcon(_refresh_ic)
            self._refresh_btn.setIconSize(QSize((14), (14)))
        self._refresh_btn.clicked.connect(self._on_refresh_all)
        layout.addWidget(self._refresh_btn)

        self._sync_btn = QPushButton("Sync Podcasts")
        self._sync_btn.setFont(QFont(FONT_FAMILY, (Metrics.FONT_SM)))
        self._sync_btn.setStyleSheet(chrome_action_btn_css())
        _sync_ic = glyph_icon("refresh", (14), paint_css("text.primary"))
        if _sync_ic:
            self._sync_btn.setIcon(_sync_ic)
            self._sync_btn.setIconSize(QSize((14), (14)))
        self._sync_btn.setToolTip(
            "Apply per-feed settings: remove listened/old episodes, "
            "fill empty slots with new episodes"
        )
        self._sync_btn.clicked.connect(self._on_sync_podcasts)
        layout.addWidget(self._sync_btn)

        layout.addStretch()

        self._status_label = make_label(
            "",
            size=(Metrics.FONT_SM),
            style=LABEL_SECONDARY(),
        )
        layout.addWidget(self._status_label)

        return bar

    def _build_empty_page(self) -> QWidget:
        """Full-page empty state shown when there are no subscriptions."""
        page = QWidget()
        page.setStyleSheet("background: transparent;")

        layout = QVBoxLayout(page)
        layout.setContentsMargins((48), (48), (48), (48))
        layout.addStretch()

        icon_lbl = QLabel()
        _px = glyph_pixmap("broadcast", Metrics.FONT_ICON_XL, paint_css("text.tertiary"))
        if _px:
            icon_lbl.setPixmap(_px)
        else:
            icon_lbl.setText("◎")
            icon_lbl.setFont(QFont(FONT_FAMILY, Metrics.FONT_ICON_XL))
        icon_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon_lbl.setStyleSheet(f"color: {paint_css('text.tertiary')}; background: transparent;")
        layout.addWidget(icon_lbl)

        layout.addSpacing(12)

        heading = make_label(
            "No Podcast Subscriptions",
            size=(Metrics.FONT_PAGE_TITLE),
            weight=QFont.Weight.DemiBold,
        )
        heading.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(heading)

        layout.addSpacing(6)

        desc = make_label(
            "Search for podcasts or add an RSS feed to get started.\n"
            "Episodes can be downloaded and synced to your iPod.",
            size=(Metrics.FONT_LG),
            style=LABEL_SECONDARY(),
            wrap=True,
        )
        desc.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(desc)

        layout.addSpacing(16)

        cta_btn = QPushButton("Add Your First Podcast")
        cta_btn.setFont(QFont(FONT_FAMILY, (Metrics.FONT_MD), QFont.Weight.DemiBold))
        cta_btn.setStyleSheet(accent_btn_css())
        cta_btn.setFixedHeight(38)
        cta_btn.setFixedWidth(240)
        _cta_ic = glyph_icon("plus", (16), paint_css("control.primary.text"))
        if _cta_ic:
            cta_btn.setIcon(_cta_ic)
            cta_btn.setIconSize(QSize((16), (16)))
        cta_btn.clicked.connect(self._on_search)
        layout.addWidget(cta_btn, alignment=Qt.AlignmentFlag.AlignCenter)

        layout.addStretch()
        return page

    def _build_main_page(self) -> QWidget:
        """The main splitter containing feed list and episode panel."""
        splitter = QSplitter(Qt.Orientation.Horizontal)
        style_browser_splitter(splitter)

        # Left: feed list
        left = self._build_feed_panel()
        splitter.addWidget(left)

        # Right: episode table + action bar
        right = self._build_episode_panel()
        splitter.addWidget(right)

        splitter.setSizes([(240), (600)])
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)

        return splitter

    def _build_feed_panel(self) -> QWidget:
        # "Subscriptions" no longer covers it: the pane leads with library
        # views that are not subscriptions.
        panel = BrowserPane(
            "Podcasts",
            min_width=220,
            body_margins=(8, 2, 8, 8),
        )

        self._feed_list = QListWidget()
        self._feed_list.setFont(QFont(FONT_FAMILY, Metrics.FONT_SIDEBAR))
        self._feed_list.setIconSize(QSize((36), (36)))
        self._feed_list.setSpacing(2)
        self._feed_list.setTextElideMode(Qt.TextElideMode.ElideRight)
        self._feed_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._feed_list.customContextMenuRequested.connect(self._on_feed_context_menu)
        self._feed_list.currentRowChanged.connect(self._on_feed_selected)
        self._feed_list.setStyleSheet(sidebar_item_view_css(background="transparent"))

        panel.addWidget(self._feed_list, 1)
        return panel

    def _build_episode_panel(self) -> QWidget:
        panel = QWidget()

        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # ── Feed hero header ─────────────────────────────────────────────
        self._feed_header = QFrame()
        self._feed_header.setObjectName("heroHeader")
        self._feed_header.setMaximumHeight(375)
        self._feed_header.setStyleSheet(f"""
            QFrame#heroHeader {{
                background: {paint_css('canvas.default')};
                border-bottom: 1px solid {paint_css('border.subtle')};
            }}
        """)

        hdr_layout = QVBoxLayout(self._feed_header)
        hdr_layout.setContentsMargins(0, 0, 0, 0)
        hdr_layout.setSpacing(0)

        # Main hero body: art left, info right
        hero_body = QFrame()
        hero_body.setStyleSheet("background: transparent; border: none;")
        body_lay = QHBoxLayout(hero_body)
        body_lay.setContentsMargins(24, 16, 24, 16)
        body_lay.setSpacing(20)

        art_size = 120
        self._feed_art = QLabel()
        self._feed_art.setFixedSize(art_size, art_size)
        self._feed_art.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._feed_art.setStyleSheet(f"""
            background: {paint_css('surface.default')};
            border-radius: {Metrics.BORDER_RADIUS}px;
            border: 1px solid {paint_css('border.subtle')};
        """)
        self._set_feed_art_placeholder()
        body_lay.addWidget(self._feed_art, 0, Qt.AlignmentFlag.AlignTop)

        # Info column
        info_col = QVBoxLayout()
        info_col.setContentsMargins(0, 4, 0, 0)
        info_col.setSpacing(4)

        self._feed_title_label = make_label(
            "Select a podcast",
            size=Metrics.FONT_PAGE_TITLE,
            weight=QFont.Weight.DemiBold,
        )
        self._feed_title_label.setWordWrap(True)
        info_col.addWidget(self._feed_title_label)

        self._feed_author_label = make_label(
            "",
            size=Metrics.FONT_MD,
            style=LABEL_SECONDARY(),
        )
        self._feed_author_label.setWordWrap(True)
        info_col.addWidget(self._feed_author_label)

        self._feed_description_label = make_label(
            "",
            size=Metrics.FONT_SM,
            style=LABEL_SECONDARY(),
            wrap=True,
        )
        self._feed_description_label.setMaximumHeight(44)
        info_col.addWidget(self._feed_description_label)

        info_col.addSpacing(4)

        # Stats row: episodes · downloaded · on iPod
        stats_row = QHBoxLayout()
        stats_row.setSpacing(12)
        self._feed_stat_episodes = make_label("", size=Metrics.FONT_SM,
                                              style=f"color: {paint_css('text.secondary')};")
        self._feed_stat_downloaded = make_label("", size=Metrics.FONT_SM,
                                                style=f"color: {paint_css('control.primary.fill')};")
        self._feed_stat_on_ipod = make_label("", size=Metrics.FONT_SM,
                                             style=f"color: {paint_css('status.success.text')};")
        # hidden ghost label kept for _show_episodes compat
        self._feed_stat_extra = make_label("", size=Metrics.FONT_SM)
        self._feed_stat_extra.hide()

        stats_row.addWidget(self._feed_stat_episodes)
        stats_row.addWidget(self._feed_stat_downloaded)
        stats_row.addWidget(self._feed_stat_on_ipod)
        stats_row.addStretch()
        info_col.addLayout(stats_row)

        self._feed_detail_label = make_label("", size=Metrics.FONT_SM, style=LABEL_SECONDARY())
        info_col.addWidget(self._feed_detail_label)

        info_col.addStretch()
        body_lay.addLayout(info_col, 1)
        hdr_layout.addWidget(hero_body)

        self._hero_btns: list[QPushButton] = []
        self._reset_feed_hero_color()  # apply initial default styling

        # ── Per-feed settings strip ────────────────────────────────────
        hdr_layout.addWidget(make_separator())

        _lbl_css = (
            f"color: {paint_css('text.secondary')}; background: transparent; border: none;"
        )
        _combo_style = combo_css()
        _spin_style = spin_css(padding="2px 6px", font_size=Metrics.FONT_SM)

        def _make_setting_combo(options: list[str], width: int = 110) -> QComboBox:
            cb = QComboBox()
            cb.addItems(options)
            cb.setFixedWidth(width)
            cb.setFont(QFont(FONT_FAMILY, Metrics.FONT_SM))
            cb.setStyleSheet(_combo_style)
            return cb

        def _make_setting_label(text: str) -> QLabel:
            lbl = QLabel(text)
            lbl.setFont(QFont(FONT_FAMILY, Metrics.FONT_SM))
            lbl.setStyleSheet(_lbl_css)
            return lbl

        def _make_pair(label_text: str, control: QWidget) -> QWidget:
            """Wrap a label + control into a single flow-layout item."""
            w = QWidget()
            w.setStyleSheet("background: transparent;")
            lay = QHBoxLayout(w)
            lay.setContentsMargins(0, 0, 0, 0)
            lay.setSpacing(6)
            lay.addWidget(_make_setting_label(label_text))
            lay.addWidget(control)
            return w

        from .flowLayout import FlowLayout as _SettingsFlow
        settings_strip = QFrame()
        settings_strip.setStyleSheet("background: transparent; border: none;")
        flow = _SettingsFlow(settings_strip, spacing=12)
        flow.setContentsMargins(24, 8, 24, 10)

        self._feed_episode_slots = QSpinBox()
        self._feed_episode_slots.setRange(1, 50)
        self._feed_episode_slots.setValue(3)
        self._feed_episode_slots.setFixedWidth(60)
        self._feed_episode_slots.setFont(QFont(FONT_FAMILY, Metrics.FONT_SM))
        self._feed_episode_slots.setStyleSheet(_spin_style)

        self._feed_fill_mode = _make_setting_combo(["Newest Episode", "Next Episode"])
        self._feed_clear_method = _make_setting_combo(
            ["Remove Immediately", "Mark for Replacement"], width=140)
        self._feed_clear_listened = _make_setting_combo(["Yes", "No"], width=70)
        self._feed_clear_older = _make_setting_combo([
            "Immediately", "1 Day", "3 Days", "1 Week", "2 Weeks",
            "1 Month", "2 Months", "3 Months", "Never",
        ])

        flow.addWidget(_make_pair("Episodes:", self._feed_episode_slots))
        flow.addWidget(_make_pair("Fill with:", self._feed_fill_mode))
        flow.addWidget(_make_pair("Clear method:", self._feed_clear_method))
        flow.addWidget(_make_pair("Clear when listened:", self._feed_clear_listened))
        flow.addWidget(_make_pair("Clear older than:", self._feed_clear_older))

        # Connect setting changes to save handler
        self._feed_episode_slots.valueChanged.connect(self._on_feed_setting_changed)
        self._feed_fill_mode.currentTextChanged.connect(self._on_feed_setting_changed)
        self._feed_clear_listened.currentTextChanged.connect(self._on_feed_setting_changed)
        self._feed_clear_older.currentTextChanged.connect(self._on_feed_setting_changed)
        self._feed_clear_method.currentTextChanged.connect(self._on_feed_setting_changed)

        hdr_layout.addWidget(settings_strip)

        layout.addWidget(self._feed_header)
        layout.addWidget(self._build_library_header())

        # ── Sort and search ─────────────────────────────────────────────
        # Directly above the list it acts on, and identical in every view.
        self._filter_bar = EpisodeFilterBar()
        self._filter_bar.sort_changed.connect(self._on_episode_sort_changed)
        self._filter_bar.search_changed.connect(self._on_episode_search_changed)
        self._filter_bar.search_dismissed.connect(self._focus_episode_list)
        self._filter_bar.hide()
        layout.addWidget(self._filter_bar)

        find_shortcut = QShortcut(QKeySequence(QKeySequence.StandardKey.Find), self)
        find_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        find_shortcut.activated.connect(self._focus_episode_search)

        # ── Episode list ────────────────────────────────────────────────
        self._episode_list = _PodcastEpisodeList.build(self)
        self._episode_stack = QStackedWidget()
        self._episode_stack.setStyleSheet("background: transparent; border: none;")
        self._episode_stack.addWidget(self._episode_list)  # index 0: list

        self._episode_state = PodcastStatePanel()
        self._episode_state.action_clicked.connect(self._retry_episode_state)
        self._episode_stack.addWidget(self._episode_state)  # index 1: visual state
        self._episode_stack.setCurrentIndex(0)
        layout.addWidget(self._episode_stack, stretch=1)

        # ── Batch selection bar (hidden until rows are selected) ─────────
        self._selection_bar = QFrame()
        self._selection_bar.setObjectName("podcastSelectionBar")
        self._selection_bar.setFixedHeight(44)
        self._selection_bar.setStyleSheet(
            f"QFrame#podcastSelectionBar {{"
            f" background: {paint_css('surface.raised')};"
            f" border-top: 1px solid {paint_css('border.subtle')};"
            f" }}"
        )
        selection_lay = QHBoxLayout(self._selection_bar)
        selection_lay.setContentsMargins(12, 0, 12, 0)
        selection_lay.setSpacing(8)

        # Master checkbox: reflects the whole visible list and drives it.
        self._selection_master = QCheckBox()
        self._selection_master.setObjectName("podcastSelectionMaster")
        self._selection_master.setStyleSheet(checkbox_css(Metrics.FONT_SM))
        self._selection_master.setTristate(True)
        self._selection_master.setAccessibleName("Select all listed episodes")
        self._selection_master.setToolTip("Select or deselect every listed episode")
        self._selection_master.setCursor(Qt.CursorShape.PointingHandCursor)
        self._selection_master.clicked.connect(self._on_selection_master_clicked)
        selection_lay.addWidget(self._selection_master)

        self._selection_count_label = make_label(
            "",
            size=Metrics.FONT_SM,
            weight=QFont.Weight.DemiBold,
        )
        selection_lay.addWidget(self._selection_count_label)
        selection_lay.addStretch()

        self._selection_clear_btn = QPushButton("Clear")
        self._selection_clear_btn.setFont(QFont(FONT_FAMILY, Metrics.FONT_SM))
        self._selection_clear_btn.setStyleSheet(btn_css(radius=Metrics.BORDER_RADIUS_SM))
        self._selection_clear_btn.setFixedHeight(28)
        self._selection_clear_btn.clicked.connect(self._episode_list.clear_selection)
        selection_lay.addWidget(self._selection_clear_btn)

        # Destructive action sits left of the primary and is never the default
        # button, so Return can only ever trigger the additive one.
        self._selection_remove_btn = QPushButton("Remove from iPod")
        self._selection_remove_btn.setFont(
            QFont(FONT_FAMILY, Metrics.FONT_SM, QFont.Weight.DemiBold)
        )
        self._selection_remove_btn.setStyleSheet(danger_btn_css("sm"))
        self._selection_remove_btn.setFixedHeight(28)
        self._selection_remove_btn.setAutoDefault(False)
        self._selection_remove_btn.setDefault(False)
        self._selection_remove_btn.clicked.connect(
            self._on_remove_episode_selection
        )
        selection_lay.addWidget(self._selection_remove_btn)

        self._selection_apply_btn = QPushButton("Add to iPod")
        self._selection_apply_btn.setFont(
            QFont(FONT_FAMILY, Metrics.FONT_SM, QFont.Weight.DemiBold)
        )
        self._selection_apply_btn.setStyleSheet(accent_btn_css("sm"))
        self._selection_apply_btn.setFixedHeight(28)
        self._selection_apply_btn.clicked.connect(self._on_apply_episode_selection)
        selection_lay.addWidget(self._selection_apply_btn)

        self._selection_bar.hide()
        layout.addWidget(self._selection_bar)

        # ── Download progress bar (hidden by default) ────────────────────
        self._progress_bar = QProgressBar()
        self._progress_bar.setFixedHeight(3)
        self._progress_bar.setTextVisible(False)
        self._progress_bar.setRange(0, 100)
        self._progress_bar.setStyleSheet(
            progress_bar_css(height=3, radius=1, bg=paint_css("surface.default"))
        )
        self._progress_bar.hide()
        layout.addWidget(self._progress_bar)

        # ── Status toast (hidden until a message is set) ─────────────────
        self._status_toast = QFrame()
        self._status_toast.setFixedHeight(32)
        self._status_toast.setStyleSheet(
            f"background: {paint_css('surface.raised')};"
            f" border-top: 1px solid {paint_css('border.subtle')};"
        )
        toast_lay = QHBoxLayout(self._status_toast)
        toast_lay.setContentsMargins(12, 0, 12, 0)
        self._action_status = make_label("", size=Metrics.FONT_SM, style=LABEL_SECONDARY())
        toast_lay.addWidget(self._action_status)
        toast_lay.addStretch()
        self._status_toast.hide()
        layout.addWidget(self._status_toast)

        return panel

    def _build_library_header(self) -> QWidget:
        """Title strip for the On iPod view.

        Occupies the same slot as the per-show hero header; exactly one of the
        two is visible at a time. Sorting and searching live in the shared
        filter bar below, which serves every view.
        """
        header = QFrame()
        header.setObjectName("podcastLibraryHeader")
        header.setStyleSheet(f"""
            QFrame#podcastLibraryHeader {{
                background: {paint_css('canvas.default')};
                border-bottom: 1px solid {paint_css('border.subtle')};
            }}
        """)

        layout = QHBoxLayout(header)
        layout.setContentsMargins(24, 14, 24, 12)
        layout.setSpacing(8)

        title = make_label(
            "On iPod",
            size=Metrics.FONT_PAGE_TITLE,
            weight=QFont.Weight.DemiBold,
        )
        layout.addWidget(title)
        layout.addStretch()

        self._on_ipod_select_all_btn = QPushButton("Select All")
        self._on_ipod_select_all_btn.setFont(
            QFont(FONT_FAMILY, Metrics.FONT_SM, QFont.Weight.DemiBold)
        )
        self._on_ipod_select_all_btn.setStyleSheet(
            btn_css(radius=Metrics.BORDER_RADIUS_SM, padding="0px 12px")
        )
        self._on_ipod_select_all_btn.setFixedHeight(BROWSER_SEARCH_CONTROL_SIZE)
        self._on_ipod_select_all_btn.setToolTip(
            "Select every episode currently listed"
        )
        self._on_ipod_select_all_btn.clicked.connect(self._select_all_visible)
        layout.addWidget(self._on_ipod_select_all_btn)

        self._library_header = header
        header.hide()
        return header

    # ── Episode state visuals ───────────────────────────────────────────

    def _show_episode_content(self) -> None:
        self._episode_state_retry = None
        if hasattr(self, "_episode_stack"):
            self._episode_stack.setCurrentIndex(0)

    def _show_episode_loading(self, title: str, message: str) -> None:
        self._episode_state_retry = None
        self._episode_state.show_loading(title, message)
        self._episode_stack.setCurrentIndex(1)

    def _show_episode_empty(
        self,
        title: str,
        message: str,
        *,
        glyph: str = "broadcast",
        action_text: str = "",
        action: Callable[[], None] | None = None,
    ) -> None:
        self._episode_state_retry = action
        self._episode_state.show_empty(
            title,
            message,
            glyph=glyph,
            action_text=action_text if action is not None else "",
        )
        self._episode_stack.setCurrentIndex(1)

    def _show_episode_error(
        self,
        error: BaseException,
        *,
        action: str,
        retry: Callable[[], None] | None = None,
    ) -> None:
        from iopenpod.podcasts.network_errors import describe_podcast_error

        info = describe_podcast_error(error, action=action)
        self._episode_state_retry = retry
        self._episode_state.show_error(
            info.title,
            info.message,
            code=info.code,
            action_text="Try Again" if retry else "",
        )
        self._episode_stack.setCurrentIndex(1)

    def _retry_episode_state(self) -> None:
        retry = self._episode_state_retry
        if retry is not None:
            retry()

    # ── Feed list management ─────────────────────────────────────────────

    def _add_feed_section_header(self, text: str) -> None:
        """Append an inert grouping label to the feed list."""
        item = QListWidgetItem(text)
        # No flags at all: the row cannot be selected, and Qt's arrow-key
        # navigation steps straight over it.
        item.setFlags(Qt.ItemFlag.NoItemFlags)
        item.setData(Qt.ItemDataRole.UserRole, "")
        item.setFont(
            QFont(FONT_FAMILY, Metrics.FONT_SIDEBAR_SECTION, QFont.Weight.DemiBold)
        )
        item.setForeground(_qcolor(paint_css("text.tertiary")))
        item.setSizeHint(QSize(0, 26))
        self._feed_list.addItem(item)

    def _add_library_row(self, label: str, key: str, glyph: str) -> None:
        """Append one of the synthetic library rows (Feed / On iPod)."""
        item = QListWidgetItem(label)
        item.setData(Qt.ItemDataRole.UserRole, key)
        item.setSizeHint(QSize(0, 40))
        tile = self._artwork_placeholder_pixmap(36, glyph=glyph)
        if tile:
            item.setIcon(QIcon(tile))
        self._feed_list.addItem(item)

    def _row_for_key(self, key: str) -> int:
        """Find a feed-list row by its ``UserRole`` key, or -1."""
        if not key:
            return -1
        for row in range(self._feed_list.count()):
            item = self._feed_list.item(row)
            if item is not None and item.data(Qt.ItemDataRole.UserRole) == key:
                return row
        return -1

    def _refresh_feed_list(self) -> None:
        """Repopulate the feed list widget from the subscription store."""
        if not self._store:
            return

        self._feed_list.blockSignals(True)
        prev_key = self._current_view_key()
        self._feed_list.clear()

        feeds = self._store.get_feeds()
        on_ipod_count = self._on_ipod_episode_count()

        # The full-page "no subscriptions" pitch is only the truth when the
        # iPod is also empty. Podcasts put there by iTunes still need a way in.
        if not feeds and not on_ipod_count:
            self._stack.setCurrentIndex(0)
            self._feed_list.blockSignals(False)
            self._selected_feed = None
            self._view_mode = _VIEW_SHOW
            self._show_episodes(None)
            return
        self._stack.setCurrentIndex(1)

        self._add_feed_section_header("Library")
        self._add_library_row("Feed", _COMBINED_FEED_KEY, "broadcast")
        self._add_library_row(
            f"On iPod  ({on_ipod_count})",
            _ON_IPOD_KEY,
            "download",
        )
        if feeds:
            self._add_feed_section_header("Shows")

        for feed in feeds:
            ep_count = len(feed.episodes)
            label = feed.title or "Untitled"
            item = QListWidgetItem(f"{label}  ({ep_count})")
            item.setData(Qt.ItemDataRole.UserRole, feed.feed_url)
            item.setSizeHint(QSize(0, 40))

            # Feed artwork thumbnail in list
            artwork_source = self._feed_artwork_source(feed)
            item.setIcon(QIcon(self._artwork_placeholder_pixmap(36)))
            if artwork_source and artwork_source in _artwork_cache:
                icon_pm = scale_pixmap_for_display(
                    _artwork_cache[artwork_source],
                    36,
                    36,
                    widget=self._feed_list,
                    aspect_mode=Qt.AspectRatioMode.KeepAspectRatio,
                    transform_mode=Qt.TransformationMode.SmoothTransformation,
                )
                item.setIcon(QIcon(icon_pm))
            elif artwork_source:
                self._load_feed_list_artwork(artwork_source)

            self._feed_list.addItem(item)

        self._feed_list.blockSignals(False)

        select_row = self._row_for_key(prev_key)
        if select_row < 0:
            # With no subscriptions there is nothing for Feed to show, so open
            # on the one view that has content.
            select_row = self._row_for_key(
                _COMBINED_FEED_KEY if feeds else _ON_IPOD_KEY
            )
        if select_row >= 0:
            self._feed_list.setCurrentRow(select_row)
        else:
            self._selected_feed = None
            self._view_mode = _VIEW_SHOW
            self._show_episodes(None)

    def _on_feed_selected(self, row: int) -> None:
        if row < 0 or not self._store:
            self._selected_feed = None
            self._view_mode = _VIEW_SHOW
            self._show_episodes(None)
            return

        item = self._feed_list.item(row)
        if not item:
            return

        feed_url = item.data(Qt.ItemDataRole.UserRole)
        if not feed_url:
            return  # A section header — not selectable, but stay defensive.
        if feed_url == _COMBINED_FEED_KEY:
            self._selected_feed = None
            self._view_mode = _VIEW_FEED
            self._show_combined_feed()
            return
        if feed_url == _ON_IPOD_KEY:
            self._selected_feed = None
            self._view_mode = _VIEW_ON_IPOD
            self._show_on_ipod_episodes()
            return

        self._selected_feed = self._store.get_feed(feed_url)
        self._view_mode = _VIEW_SHOW
        self._show_episodes(self._selected_feed)

        # Auto-refresh from RSS if this feed only has persisted episodes
        # (on-iPod / downloaded) and hasn't been refreshed this session.
        if self._selected_feed and not self._is_feed_refreshed_this_session(feed_url):
            self._refresh_single_feed(self._selected_feed)

    def _is_feed_refreshed_this_session(self, feed_url: str) -> bool:
        """Check if a feed has been RSS-refreshed during this app session."""
        if not hasattr(self, '_session_refreshed'):
            self._session_refreshed: set[str] = set()
        return feed_url in self._session_refreshed

    def _mark_feed_refreshed(self, feed_url: str) -> None:
        if not hasattr(self, '_session_refreshed'):
            self._session_refreshed: set[str] = set()
        self._session_refreshed.add(feed_url)

    def _on_feed_context_menu(self, pos):
        item = self._feed_list.itemAt(pos)
        if not item or not self._store:
            return

        feed_url = item.data(Qt.ItemDataRole.UserRole)
        if feed_url == _COMBINED_FEED_KEY:
            return
        feed = self._store.get_feed(feed_url)
        if not feed:
            return

        menu = QMenu(self)
        menu.setStyleSheet(context_menu_css())

        refresh_action = menu.addAction("Refresh Feed")
        menu.addSeparator()
        unsub_action = menu.addAction("Unsubscribe")

        action = menu.exec(self._feed_list.mapToGlobal(pos))
        if action == refresh_action:
            self._refresh_single_feed(feed)
        elif action == unsub_action:
            self._unsubscribe_feed(feed)

    # ── Episode context menu ─────────────────────────────────────────────

    def _on_episode_context_menu(self, pos) -> None:
        """Right-click on episode rows → Add/Remove actions."""
        t = self._episode_list.table
        # If right-clicked row is not already selected, target that row only.
        row = t.rowAt(pos.y())
        if row >= 0:
            selected_rows = set(self._episode_list.selected_rows())
            if row not in selected_rows:
                self._episode_list.clear_selection()
                self._episode_list.select_row(row)

        selected = self._get_selected_episode_refs()
        if not selected:
            return

        from iopenpod.podcasts.models import (
            STATUS_DOWNLOADED,
            STATUS_DOWNLOADING,
            STATUS_ON_IPOD,
        )

        can_add = [
            (row, ep, feed)
            for row, ep, feed in selected
            if ep.status not in (STATUS_ON_IPOD, STATUS_DOWNLOADING)
        ]
        can_remove_dl = [
            (row, ep, feed)
            for row, ep, feed in selected
            if ep.status in (STATUS_DOWNLOADED,) and ep.downloaded_path
        ]
        can_remove_ipod = [
            (row, ep, feed)
            for row, ep, feed in selected
            if ep.status == STATUS_ON_IPOD and ep.ipod_db_track_id
        ]
        can_mark_listened = [
            (row, ep, feed)
            for row, ep, feed in selected
            if not _episode_is_listened(ep)
        ]
        can_mark_unlistened = [
            (row, ep, feed)
            for row, ep, feed in selected
            if _episode_is_listened(ep)
        ]

        if not any((
            can_add,
            can_remove_dl,
            can_remove_ipod,
            can_mark_listened,
            can_mark_unlistened,
        )):
            return

        menu = QMenu(self)
        menu.setStyleSheet(context_menu_css())

        add_action = remove_dl_action = remove_ipod_action = None
        mark_listened_action = mark_unlistened_action = None

        if can_add:
            n = len(can_add)
            suffix = f" ({n})" if n > 1 else ""
            add_action = menu.addAction(f"Add to iPod{suffix}")

        if can_remove_dl:
            if add_action:
                menu.addSeparator()
            n = len(can_remove_dl)
            suffix = f" ({n})" if n > 1 else ""
            remove_dl_action = menu.addAction(f"Remove Download{suffix}")

        if can_remove_ipod:
            if add_action or remove_dl_action:
                menu.addSeparator()
            n = len(can_remove_ipod)
            suffix = f" ({n})" if n > 1 else ""
            remove_ipod_action = menu.addAction(f"Remove from iPod{suffix}")

        if can_mark_listened or can_mark_unlistened:
            if add_action or remove_dl_action or remove_ipod_action:
                menu.addSeparator()
            if can_mark_listened:
                n = len(can_mark_listened)
                suffix = f" ({n})" if n > 1 else ""
                mark_listened_action = menu.addAction(f"Mark as Listened{suffix}")
            if can_mark_unlistened:
                n = len(can_mark_unlistened)
                suffix = f" ({n})" if n > 1 else ""
                mark_unlistened_action = menu.addAction(f"Mark as Unlistened{suffix}")

        viewport = self._episode_list.table.viewport()
        if not viewport:
            return
        action = menu.exec(viewport.mapToGlobal(pos))
        if action is None:
            return
        if action == add_action:
            self._add_to_ipod_refs(can_add)
        elif action == remove_dl_action:
            self._remove_download_refs(can_remove_dl)
        elif action == remove_ipod_action:
            self._remove_from_ipod_refs(can_remove_ipod)
        elif action == mark_listened_action:
            self._set_listened_refs(can_mark_listened, True)
        elif action == mark_unlistened_action:
            self._set_listened_refs(can_mark_unlistened, False)

    # ── Episode table ────────────────────────────────────────────────────

    @staticmethod
    def _ep_to_dict(ep, status_text: str, feed=None) -> dict:
        """Convert a PodcastEpisode to a MusicBrowserList-compatible dict."""
        from iopenpod.podcasts.models import STATUS_DOWNLOADING, STATUS_ON_IPOD

        ep_key = _episode_key(feed, ep) if feed is not None else ep.guid
        status = str(getattr(ep, "status", ""))
        play_count = _coerce_int(getattr(ep, "play_count", 0))
        last_played = _coerce_int(getattr(ep, "last_played", 0))
        title = ep.title or ep.guid or ""
        show_title = getattr(feed, "title", "") if feed is not None else ""
        description = _episode_description_text(ep.description)
        return {
            "Title": title,
            "podcast_feed_title": show_title,
            "Description Text": description,
            "ep_status": status_text,
            "length": (ep.duration_seconds or 0) * 1000,
            "date_added": int(ep.pub_date or 0),
            "size": ep.size_bytes or 0,
            "play_count_1": play_count,
            "last_played": last_played,
            "_ep_guid": ep.guid,
            "_ep_key": ep_key,
            # Prepared once per row rather than per keystroke: searching a big
            # feed re-scans every episode on every character typed.
            "_search_text": prepare_search_text(
                "\n".join(part for part in (title, show_title, description) if part)
            ),
            "_was_listened": _episode_is_listened(ep),
            "_listened_override": _episode_listened_override(ep),
            "_can_add_to_ipod": status not in (STATUS_ON_IPOD, STATUS_DOWNLOADING),
            "_can_remove_from_ipod": (
                status == STATUS_ON_IPOD
                and bool(getattr(ep, "ipod_db_track_id", 0))
            ),
        }

    def _set_episode_rows(self, rows: list[dict], columns: list[str]) -> None:
        self._episode_list.set_rows(rows, columns)

    # ── Sort and search ──────────────────────────────────────────────────

    def _begin_episode_view(self, view_mode: str) -> None:
        """Arm the filter bar for the view about to be rendered.

        The search resets when the user moves to a different show or library
        view, but survives a re-render of the same one — status changes and
        feed refreshes must not wipe what somebody is in the middle of typing.
        The sort is remembered per view instead, since preferring newest-first
        or by-show is a habit rather than a property of one show.
        """
        self._view_mode = view_mode

        view_key = self._current_view_key()
        if view_key != self._filtered_view_key:
            self._filtered_view_key = view_key
            self._episode_query = ""
            self._filter_bar.set_query("")

        placeholder, describes = _SEARCH_SCOPE_BY_VIEW[view_mode]
        self._filter_bar.set_search_scope(placeholder, describes=describes)
        # The bar reports what it could actually select: a view that does not
        # offer the remembered order falls back, and must say so.
        self._sort_by_view[view_mode] = self._filter_bar.set_sort_options(
            _SORT_OPTIONS_BY_VIEW[view_mode],
            self._sort_by_view.get(view_mode, _SORT_NEWEST),
        )

    def _set_episode_source_rows(
        self,
        rows: list[dict],
        columns: list[str],
        *,
        empty_title: str,
        empty_message: str,
        empty_glyph: str = "broadcast",
    ) -> None:
        """Hand the view's full row set to the presenter and draw it."""
        self._episode_source_rows = rows
        self._episode_columns = columns
        self._episode_empty_state = (empty_title, empty_message, empty_glyph)
        self._present_episode_rows()

    def _present_episode_rows(self) -> None:
        """Draw the current view's rows through the active search and sort.

        Searching and sorting re-present these rows rather than rebuilding
        them, so a keystroke costs one filter pass instead of another read of
        the iTunesDB or the subscription store.
        """
        source = self._episode_source_rows
        rows = self._sorted_episode_rows(self._matching_episode_rows(source))

        self._episode_dicts = rows
        self._set_episode_rows(rows, self._episode_columns)
        self._filter_bar.set_summary(self._episode_summary_text(source, rows))
        # Controls with nothing to act on are noise; controls that hid every
        # row must stay, or there is no way back to the full list.
        self._filter_bar.setVisible(bool(source))

        if rows:
            self._show_episode_content()
        elif source:
            self._show_no_matching_episodes()
        else:
            title, message, glyph = self._episode_empty_state
            self._show_episode_empty(title, message, glyph=glyph)

    def _matching_episode_rows(self, rows: list[dict]) -> list[dict]:
        """Keep the rows matching the search box, by title, show, or blurb."""
        query = self._episode_query.strip()
        if not query:
            return rows
        return [
            row
            for row in rows
            # Every term has to land somewhere, so "mars rover" narrows instead
            # of pulling in every episode that ever said "mars".
            if matches_search(query, _row_search_text(row), match_all_terms=True)
        ]

    def _sorted_episode_rows(self, rows: list[dict]) -> list[dict]:
        """Order rows by the sort chosen for the current view."""
        sort_key = _EPISODE_SORT_KEYS.get(
            self._sort_by_view.get(self._view_mode, _SORT_NEWEST),
            _EPISODE_SORT_KEYS[_SORT_NEWEST],
        )
        return sorted(rows, key=sort_key)

    def _episode_summary_text(
        self,
        source: list[dict],
        visible: list[dict],
    ) -> str:
        """Describe what is listed, and what a search is holding back."""
        filtering = len(visible) != len(source)
        counted = visible if filtering else source

        if filtering:
            parts = [f"{len(visible)} of {len(source)} episodes"]
        else:
            count = len(source)
            parts = [f"{count} episode{'s' if count != 1 else ''}"]

        if self._view_mode != _VIEW_SHOW:
            shows = {str(row.get("podcast_feed_title") or "") for row in counted}
            shows.discard("")
            if shows:
                parts.append(f"{len(shows)} show{'s' if len(shows) != 1 else ''}")

        # Only the device reports sizes it has actually written; an RSS
        # enclosure length is a claim, and summing claims would mislead.
        if self._view_mode == _VIEW_ON_IPOD:
            total_bytes = sum(_row_size(row) for row in counted)
            if total_bytes > 0:
                parts.append(format_size(total_bytes))

        return "  ·  ".join(parts)

    def _show_no_matching_episodes(self) -> None:
        """The list is empty because of the search, and says so."""
        self._show_episode_empty(
            "No matching episodes",
            f"Nothing here matches “{self._episode_query.strip()}”.",
            glyph="search",
            action_text="Clear Search",
            action=self._clear_episode_search,
        )

    def _on_episode_sort_changed(self, sort_key: str) -> None:
        if not sort_key:
            return
        self._sort_by_view[self._view_mode] = sort_key
        self._present_episode_rows()

    def _on_episode_search_changed(self, text: str) -> None:
        if text == self._episode_query:
            return
        self._episode_query = text
        self._present_episode_rows()

    def _clear_episode_search(self) -> None:
        self._filter_bar.set_query("", notify=True)

    def _focus_episode_search(self) -> None:
        """Jump to the search box, unless this view has nothing to search."""
        if self._filter_bar.isVisible():
            self._filter_bar.focus_search()

    def _focus_episode_list(self) -> None:
        self._episode_list.table.setFocus(Qt.FocusReason.ShortcutFocusReason)

    def _reset_episode_filters(self) -> None:
        """Drop the query and the remembered orders, for a new device."""
        self._episode_query = ""
        self._filtered_view_key = ""
        self._sort_by_view.clear()
        self._episode_source_rows = []
        self._filter_bar.set_query("")
        self._filter_bar.set_summary("")
        self._filter_bar.hide()

    # ── Combined feed view ───────────────────────────────────────────────

    def _show_combined_feed(self) -> None:
        """Show every subscribed episode as one list across all shows."""
        if not self._store:
            self._show_episodes(None)
            return

        self._selected_feed = None
        self._begin_episode_view(_VIEW_FEED)
        self._feed_header.hide()
        self._library_header.hide()
        self._episode_by_guid.clear()
        self._episode_feed_by_key.clear()

        rows = []
        artwork_sources: dict[str, str] = {}
        for feed in self._store.get_feeds():
            for ep in feed.episodes:
                key = _episode_key(feed, ep)
                self._episode_by_guid[key] = ep
                self._episode_feed_by_key[key] = feed
                feed_key = str(getattr(feed, "feed_url", "") or id(feed))
                if feed_key not in artwork_sources:
                    artwork_sources[feed_key] = self._feed_artwork_source(feed)
                row = self._ep_to_dict(ep, self._episode_status_display(ep)[0], feed)
                row["_podcast_artwork_source"] = artwork_sources[feed_key]
                rows.append(row)

        self._set_episode_source_rows(
            rows,
            _COMBINED_FEED_COLUMNS,
            empty_title="Waiting for episodes",
            empty_message=(
                "Subscribed shows will appear here after their feeds refresh."
            ),
        )

    # ── On iPod view ────────────────────────────────────────────────────

    def _device_podcast_tracks(self) -> list[dict] | None:
        """Podcast tracks actually present on the iPod, or None if unknown.

        None means the iTunesDB has not finished parsing — distinct from an
        empty list, which means the device genuinely holds no podcasts.
        """
        tracks = self._current_ipod_tracks()
        if tracks is None:
            return None
        return [
            track
            for track in tracks
            if _coerce_int(track.get("media_type")) & _PODCAST_MEDIA_TYPE_BIT
        ]

    def _on_ipod_episode_count(self) -> int:
        """Count for the sidebar row.

        Falls back to the subscription store's own tally while the device
        database is still loading, so the row is never blank on first paint.
        """
        tracks = self._device_podcast_tracks()
        if tracks is not None:
            return len(tracks)
        if self._store is None:
            return 0
        return sum(feed.on_ipod_count for feed in self._store.get_feeds())

    def _orphan_feed_for(self, show_title: str, cache: dict) -> object:
        """Return the placeholder feed grouping one unsubscribed show."""
        from iopenpod.podcasts.models import PodcastFeed

        title = show_title or "Unknown Podcast"
        feed = cache.get(title)
        if feed is None:
            feed = PodcastFeed(
                feed_url=f"{_ORPHAN_FEED_PREFIX}{title}",
                title=title,
            )
            cache[title] = feed
        return feed

    def _build_on_ipod_rows(self, tracks: list[dict]) -> list[dict]:
        """Build episode rows from the device's own podcast track list.

        Tracks that match a subscribed episode render as that episode. Anything
        else — podcasts put on the iPod by iTunes or another tool — is wrapped
        in a throwaway episode so it is still visible and still removable.
        """
        from iopenpod.podcasts.models import (
            STATUS_ON_IPOD,
            PodcastEpisode,
        )

        subscribed: dict[int, tuple[object, object]] = {}
        if self._store is not None:
            for feed in self._store.get_feeds():
                for ep in feed.episodes:
                    if ep.status != STATUS_ON_IPOD:
                        continue
                    track_id = _coerce_int(getattr(ep, "ipod_db_track_id", 0))
                    if track_id:
                        subscribed[track_id] = (feed, ep)

        orphan_feeds: dict[str, object] = {}
        artwork_sources: dict[str, str] = {}
        rows: list[dict] = []

        for track in tracks:
            track_id = _coerce_int(
                track.get("db_track_id") or track.get("db_id")
            )
            match = subscribed.get(track_id) if track_id else None
            if match is not None:
                feed, ep = match
            else:
                feed = self._orphan_feed_for(
                    str(track.get("Album") or ""),
                    orphan_feeds,
                )
                ep = PodcastEpisode(
                    guid=f"{_ORPHAN_FEED_PREFIX}{track_id}",
                    title=str(track.get("Title") or "Untitled Episode"),
                    description=str(track.get("Description Text") or ""),
                    duration_seconds=_coerce_int(track.get("length")) // 1000,
                    size_bytes=_coerce_int(track.get("size")),
                    status=STATUS_ON_IPOD,
                    ipod_db_track_id=track_id,
                    play_count=_coerce_int(track.get("play_count_1")),
                    last_played=_coerce_int(track.get("last_played")),
                )

            key = _episode_key(feed, ep)
            self._episode_by_guid[key] = ep
            self._episode_feed_by_key[key] = feed
            feed_key = str(getattr(feed, "feed_url", "") or id(feed))
            if feed_key not in artwork_sources:
                artwork_sources[feed_key] = self._feed_artwork_source(feed)

            row = self._ep_to_dict(ep, self._episode_status_display(ep)[0], feed)
            row["_podcast_artwork_source"] = artwork_sources[feed_key]
            # The device is authoritative for both of these. An RSS enclosure
            # length can disagree with what was actually written, and "added to
            # the iPod" is the date that matters when reclaiming space.
            row["size"] = _coerce_int(track.get("size")) or row.get("size") or 0
            row["date_added"] = _coerce_int(track.get("date_added"))
            rows.append(row)

        return rows

    def _show_on_ipod_episodes(self) -> None:
        """Show every podcast episode stored on the connected iPod."""
        self._selected_feed = None
        self._begin_episode_view(_VIEW_ON_IPOD)
        self._feed_header.hide()
        self._library_header.show()
        self._episode_by_guid.clear()
        self._episode_feed_by_key.clear()

        tracks = self._device_podcast_tracks()
        if tracks is None:
            # The iTunesDB is still being read. Claiming "nothing on this iPod"
            # here would be a lie the user has no way to distinguish.
            self._episode_source_rows = []
            self._episode_dicts = []
            self._set_episode_rows([], _COMBINED_FEED_COLUMNS)
            self._filter_bar.set_summary("Reading the iPod…")
            self._filter_bar.hide()
            self._show_episode_loading(
                "Reading the iPod…",
                "Listing the podcast episodes stored on this iPod.",
            )
            return

        self._set_episode_source_rows(
            self._build_on_ipod_rows(tracks),
            _COMBINED_FEED_COLUMNS,
            empty_title="No podcasts on this iPod",
            empty_message=(
                "Open a show, pick the episodes you want, and add them to "
                "your iPod. They will be listed here."
            ),
            empty_glyph="download",
        )

    # ── Single show view ─────────────────────────────────────────────────

    def _show_episodes(self, feed) -> None:
        """Populate the episode list for the given feed."""
        self._episode_by_guid.clear()
        self._episode_feed_by_key.clear()
        self._episode_dicts = []

        self._library_header.hide()

        if not feed:
            self._feed_header.show()
            self._feed_title_label.setText("Select a podcast")
            self._feed_author_label.setText("")
            self._feed_description_label.setText("")
            self._feed_detail_label.setText("")
            self._feed_stat_episodes.setText("")
            self._feed_stat_downloaded.setText("")
            self._feed_stat_on_ipod.setText("")
            self._feed_stat_extra.setText("")
            self._load_feed_settings(None)
            self._set_feed_art_placeholder()
            self._episode_source_rows = []
            self._set_episode_rows([], _PODCAST_EPISODE_COLUMNS)
            self._filter_bar.hide()
            self._show_episode_content()
            return

        self._begin_episode_view(_VIEW_SHOW)
        self._feed_header.show()
        self._feed_title_label.setText(feed.title or "Untitled Podcast")
        self._feed_author_label.setText(feed.author or "Unknown Author")

        desc_text = (feed.description or "").replace("\n", " ").strip()
        if len(desc_text) > 170:
            desc_text = f"{desc_text[:167].rstrip()}..."
        self._feed_description_label.setText(desc_text)

        detail_parts = []
        if feed.language:
            detail_parts.append(feed.language.upper())
        refreshed = _fmt_date(feed.last_refreshed)
        if refreshed:
            detail_parts.append(f"Updated {refreshed}")
        if feed.feed_url:
            detail_parts.append("RSS feed linked")
        self._feed_detail_label.setText("  ·  ".join(detail_parts))

        self._feed_stat_episodes.setText(f"Episodes: {len(feed.episodes)}")
        self._feed_stat_downloaded.setText(f"Downloaded: {feed.downloaded_count}")
        self._feed_stat_on_ipod.setText(f"On iPod: {feed.on_ipod_count}")

        extra_parts = []
        if feed.category:
            extra_parts.append(feed.category)
        if feed.language:
            extra_parts.append(feed.language.upper())
        self._feed_stat_extra.setText(" · ".join(extra_parts))

        self._load_feed_settings(feed)

        # Load header artwork
        self._set_feed_art_placeholder()
        artwork_source = self._feed_artwork_source(feed)
        if artwork_source:
            self._load_feed_artwork(artwork_source)

        # Feed order is whatever the RSS listed; the filter bar decides how it
        # is actually shown.
        rows = []
        for ep in feed.episodes:
            key = _episode_key(feed, ep)
            self._episode_by_guid[key] = ep
            self._episode_feed_by_key[key] = feed
            rows.append(self._ep_to_dict(ep, self._episode_status_display(ep)[0], feed))

        self._set_episode_source_rows(
            rows,
            _PODCAST_EPISODE_COLUMNS,
            empty_title="No episodes found",
            empty_message=(
                "This podcast loaded, but its feed did not list any episodes."
            ),
        )

    @staticmethod
    def _episode_status_display(ep):
        """Return (text, QColor|None) for episode status."""
        from PyQt6.QtGui import QColor as _QC

        from iopenpod.podcasts.models import (
            STATUS_DOWNLOADED,
            STATUS_DOWNLOADING,
            STATUS_ON_IPOD,
        )
        if ep.status == STATUS_ON_IPOD:
            return ("On iPod", _QC(paint_css("status.success.text")))
        if ep.status == STATUS_DOWNLOADED:
            return ("Downloaded", _QC(paint_css("control.primary.fill")))
        if ep.status == STATUS_DOWNLOADING:
            return ("Downloading…", _QC(paint_css("status.warning.text")))
        if _episode_is_listened(ep):
            return ("Listened", _QC(paint_css("status.warning.text")))
        if ep.size_bytes and ep.size_bytes > 0:
            return (format_size(ep.size_bytes), None)
        return ("", None)

    # ── Toolbar actions ──────────────────────────────────────────────────

    def _on_search(self) -> None:
        """Open the podcast search dialog."""
        from .podcastSearchDialog import PodcastSearchDialog

        dialog = PodcastSearchDialog(self)
        dialog.subscribed.connect(self._subscribe_to_feed)
        dialog.exec()

    def _refresh_all_feeds_bg(self) -> None:
        """Silently refresh all feeds from RSS in the background.

        Called automatically on device load so the full episode catalog
        is available.  Unlike ``_on_refresh_all`` this does not disable
        buttons or show a status bar message.
        """
        if not self._store:
            return
        feeds = self._store.get_feeds()
        if not feeds:
            return

        if not self._episode_dicts:
            self._show_episode_loading(
                "Loading episodes…",
                "",
            )

        from iopenpod.application.runtime import ThreadPoolSingleton, Worker
        from iopenpod.podcasts.feed_parser import fetch_feed

        store = self._store

        def _refresh():
            refreshed_feeds = []
            failures = []
            for feed in feeds:
                try:
                    refreshed = fetch_feed(feed.feed_url, existing=feed)
                    store.cache_feed_artwork(refreshed)
                    refreshed_feeds.append(refreshed)
                except Exception as exc:
                    log.warning("Background refresh failed for %s: %s", feed.title, exc)
                    failures.append((feed.title, exc))
            return store.update_feeds(refreshed_feeds), failures

        worker = Worker(_refresh)
        worker.signals.result.connect(self._on_refresh_done)
        worker.signals.error.connect(self._on_refresh_error)
        ThreadPoolSingleton.get_instance().start(worker)

    def _on_refresh_all(self) -> None:
        """Refresh all subscribed feeds in background."""
        if not self._store:
            return

        feeds = self._store.get_feeds()
        if not feeds:
            self._set_status("No subscriptions to refresh")
            return

        self._refresh_btn.setEnabled(False)
        self._set_status(f"Refreshing {len(feeds)} feeds…")
        self._show_episode_loading(
            "Refreshing podcasts…",
            "Checking subscribed feeds for new episodes.",
        )
        self._episode_state_retry = self._on_refresh_all

        from iopenpod.application.runtime import ThreadPoolSingleton, Worker
        from iopenpod.podcasts.feed_parser import fetch_feed

        store = self._store

        def _refresh_all():
            refreshed_feeds = []
            failures = []
            for feed in feeds:
                try:
                    refreshed = fetch_feed(feed.feed_url, existing=feed)
                    store.cache_feed_artwork(refreshed)
                    refreshed_feeds.append(refreshed)
                except Exception as exc:
                    log.warning("Failed to refresh %s: %s", feed.title, exc)
                    failures.append((feed.title, exc))
            return store.update_feeds(refreshed_feeds), failures

        worker = Worker(_refresh_all)
        worker.signals.result.connect(self._on_refresh_done)
        worker.signals.error.connect(self._on_refresh_error)
        worker.signals.finished.connect(lambda: self._refresh_btn.setEnabled(True))
        ThreadPoolSingleton.get_instance().start(worker)

    def _on_refresh_done(self, result) -> None:
        if isinstance(result, tuple):
            count = int(result[0] or 0)
            failures = list(result[1] or [])
        else:
            count = int(result or 0)
            failures = []

        # Mark all feeds as refreshed this session
        if self._store:
            for f in self._store.get_feeds():
                self._mark_feed_refreshed(f.feed_url)
        if count:
            self._set_status(f"Refreshed {count} feed{'s' if count != 1 else ''}")

        # Reconcile episode statuses after RSS merge so that episodes
        # present on the iPod (but only known from RSS, not yet stored)
        # are correctly marked as "On iPod".
        self.reconcile_ipod_statuses()

        self._refresh_feed_list()

        # Re-display the current view's episodes with the full catalog
        self._refresh_current_view()

        if failures:
            if count:
                self._set_status(
                    f"Refreshed {count}; {len(failures)} feed"
                    f"{'s' if len(failures) != 1 else ''} could not update"
                )
            elif not self._episode_dicts:
                _feed_title, error = failures[0]
                self._show_episode_error(
                    error,
                    action="refresh podcasts",
                    retry=self._on_refresh_all,
                )
                self._set_status("Podcasts could not refresh")
            else:
                self._set_status("Some podcasts could not refresh")

    def _on_refresh_error(self, error_tuple) -> None:
        _, value, _ = error_tuple
        self._show_episode_error(
            value,
            action="refresh podcasts",
            retry=self._episode_state_retry or self._on_refresh_all,
        )
        self._set_status("Refresh failed")

    # ── Managed podcast sync ─────────────────────────────────────────────

    def _on_sync_podcasts(self) -> None:
        """Refresh all feeds, then build a managed sync plan.

        The plan applies each feed's slot settings: removing listened/old
        episodes and filling empty slots with new ones.
        """
        if not self._store:
            return
        caps = self._device_sessions.current_session().capabilities
        if caps is not None and not caps.supports_podcast:
            self._set_status("This iPod does not support podcasts")
            return

        feeds = self._store.get_feeds()
        if not feeds:
            self._set_status("No subscriptions to sync")
            return

        self._sync_btn.setEnabled(False)
        self._set_status("Refreshing feeds for sync…", timeout_ms=0)
        self._show_episode_loading(
            "Preparing podcast sync…",
            "Refreshing feeds before building the sync plan.",
        )
        self._episode_state_retry = self._on_sync_podcasts

        from iopenpod.application.runtime import ThreadPoolSingleton, Worker
        from iopenpod.podcasts.feed_parser import fetch_feed

        store = self._store

        def _refresh_and_plan():
            # Phase 1: Refresh all feeds from RSS
            refreshed = []
            for feed in feeds:
                try:
                    refreshed_feed = fetch_feed(feed.feed_url, existing=feed)
                    store.cache_feed_artwork(refreshed_feed)
                    refreshed.append(refreshed_feed)
                except Exception as exc:
                    log.warning("Failed to refresh %s: %s", feed.title, exc)
                    refreshed.append(feed)  # Keep existing data
            store.update_feeds(refreshed)
            return refreshed

        worker = Worker(_refresh_and_plan)
        worker.signals.result.connect(self._on_sync_feeds_refreshed)
        worker.signals.error.connect(self._on_sync_error)
        ThreadPoolSingleton.get_instance().start(worker)

    def _on_sync_feeds_refreshed(self, refreshed_feeds: list) -> None:
        """Feeds refreshed — build podcast sync plan and emit for review."""
        if not self._store:
            self._sync_btn.setEnabled(True)
            return

        # Mark all as refreshed this session
        for f in refreshed_feeds:
            self._mark_feed_refreshed(f.feed_url)
        self._refresh_feed_list()

        # Get iPod tracks for plan building
        ipod_tracks = self._current_ipod_tracks() or []

        # Reconcile episode statuses against actual iPod tracks before
        # building the plan.  This ensures episodes synced in a prior run
        # are correctly marked as "On iPod" even if the subscription store
        # on disk was stale (e.g. NOT_DOWNLOADED episodes from RSS that
        # were synced but never persisted with ON_IPOD status).
        self.reconcile_ipod_statuses(ipod_tracks)
        self._refresh_current_view()

        from iopenpod.podcasts.podcast_sync import build_podcast_managed_plan

        # Re-read feeds from store (they were just updated by reconcile)
        feeds = self._store.get_feeds()
        plan = build_podcast_managed_plan(feeds, ipod_tracks, self._store)

        if not plan.has_changes:
            self._set_status("All podcasts are up to date")
            self._sync_btn.setEnabled(True)
            return

        # Emit the plan (pending episodes will download during sync)
        summary_parts = []
        if plan.to_remove:
            summary_parts.append(f"{len(plan.to_remove)} to remove")
        if plan.to_add:
            summary_parts.append(f"{len(plan.to_add)} to add")
        self._set_status(f"Podcast sync: {', '.join(summary_parts)}")
        self._sync_btn.setEnabled(True)
        self.podcast_sync_requested.emit(plan)

    def _on_sync_error(self, error_tuple) -> None:
        self._progress_bar.hide()
        _, value, _ = error_tuple
        self._show_episode_error(
            value,
            action="prepare podcast sync",
            retry=self._episode_state_retry or self._on_sync_podcasts,
        )
        self._set_status("Sync failed")
        self._sync_btn.setEnabled(True)

    # ── Subscribe / unsubscribe ──────────────────────────────────────────

    def _subscribe_to_feed(self, feed_url: str, artwork_url: str = "") -> None:
        """Subscribe to a feed by URL (called from search dialog)."""
        if not self._store:
            return

        # Check if already subscribed
        if self._store.get_feed(feed_url):
            self._set_status("Already subscribed")
            return

        self._set_status("Fetching feed…")
        self._stack.setCurrentIndex(1)
        self._show_episode_loading(
            "Adding podcast…",
            "Fetching the feed and latest episodes.",
        )
        self._episode_state_retry = (
            lambda feed_url=feed_url, artwork_url=artwork_url: self._subscribe_to_feed(
                feed_url,
                artwork_url,
            )
        )

        from iopenpod.application.runtime import ThreadPoolSingleton, Worker

        worker = Worker(self._fetch_subscribed_feed, feed_url, artwork_url)
        worker.signals.result.connect(self._on_feed_fetched)
        worker.signals.error.connect(self._on_subscribe_error)
        ThreadPoolSingleton.get_instance().start(worker)

    def _fetch_subscribed_feed(self, feed_url: str, artwork_url: str = ""):
        from iopenpod.podcasts.feed_parser import fetch_feed
        from iopenpod.podcasts.models import normalize_artwork_url

        feed = fetch_feed(feed_url)
        fallback_url = normalize_artwork_url(artwork_url)
        if fallback_url and not feed.artwork_url:
            feed.artwork_url = fallback_url
        if self._store:
            self._store.cache_feed_artwork(
                feed,
                fallback_urls=[fallback_url] if fallback_url else [],
            )
        return feed

    def _cache_artwork_file(self, feed_url: str, artwork_url: str) -> str:
        if not self._store or not artwork_url:
            return ""

        from iopenpod.podcasts.models import PodcastFeed

        feed = PodcastFeed(feed_url=feed_url, artwork_url=artwork_url)
        return self._store.cache_feed_artwork(feed)

    def _on_feed_fetched(self, feed) -> None:
        store = self._store
        if store is None:
            return
        if not self._persist_subscription_change(
            "save the podcast subscription",
            lambda: store.add_feed(feed),
        ):
            return
        self._mark_feed_refreshed(feed.feed_url)
        self._set_status(f"Subscribed to {feed.title}")
        self._view_mode = _VIEW_SHOW
        self._refresh_feed_list()

        # Select the new feed
        new_row = self._row_for_key(feed.feed_url)
        if new_row >= 0:
            self._feed_list.setCurrentRow(new_row)

        self._selected_feed = store.get_feed(feed.feed_url) or feed
        self._show_episodes(self._selected_feed)

    def _on_subscribe_error(self, error_tuple) -> None:
        _, value, _ = error_tuple
        self._show_episode_error(
            value,
            action="add podcast",
            retry=self._episode_state_retry,
        )
        self._set_status("Could not add podcast")

    def _unsubscribe_feed(self, feed) -> None:
        store = self._store
        if store is None:
            return
        if not self._persist_subscription_change(
            "remove the podcast subscription",
            lambda: store.remove_feed(feed.feed_url),
        ):
            return
        self._set_status(f"Unsubscribed from {feed.title}")
        self._selected_feed = None
        self._view_mode = _VIEW_SHOW
        self._refresh_feed_list()

    def _refresh_single_feed(self, feed) -> None:
        """Refresh a single feed in the background."""
        self._set_status(f"Refreshing {feed.title}…")
        self._show_episode_loading(
            "Refreshing this podcast…",
            "Checking the feed for the latest episodes.",
        )
        self._episode_state_retry = lambda feed=feed: self._refresh_single_feed(feed)

        from iopenpod.application.runtime import ThreadPoolSingleton, Worker
        from iopenpod.podcasts.feed_parser import fetch_feed

        def _do():
            refreshed = fetch_feed(feed.feed_url, existing=feed)
            if self._store:
                self._store.cache_feed_artwork(refreshed)
            return refreshed

        worker = Worker(_do)
        worker.signals.result.connect(self._on_single_feed_refreshed)
        worker.signals.error.connect(self._on_refresh_error)
        ThreadPoolSingleton.get_instance().start(worker)

    def _on_single_feed_refreshed(self, feed) -> None:
        store = self._store
        if store is None:
            return
        if not self._persist_subscription_change(
            "save the refreshed podcast",
            lambda: store.update_feed(feed),
        ):
            return
        self._mark_feed_refreshed(feed.feed_url)
        self._set_status(f"Refreshed {feed.title}")
        self._refresh_feed_list()

        # Re-display episodes for the current view — _refresh_feed_list
        # restores the selection but setCurrentRow won't emit if the row
        # index didn't change, so the episode table wouldn't update.
        self._refresh_current_view()

    # ── Episode selection ────────────────────────────────────────────────

    def _get_selected_episode_refs(self):
        """Return list of (row, episode, feed) for selected episode rows."""
        if self._view_mode == _VIEW_SHOW and not self._selected_feed:
            return []

        result = []
        for row in self._episode_list.selected_rows():
            ref = self._episode_ref_at_row(row)
            if ref is not None:
                result.append(ref)
        return result

    def _episode_ref_at_row(self, row: int):
        if not (0 <= row < len(self._episode_dicts)):
            return None
        row_data = self._episode_dicts[row]
        key = str(row_data.get("_ep_key") or row_data.get("_ep_guid") or "")
        if not key:
            return None
        ep = self._episode_by_guid.get(key)
        feed = self._episode_feed_by_key.get(key) or self._selected_feed
        if ep is None or feed is None:
            return None
        return row, ep, feed

    def _get_selected_episodes(self):
        """Return list of (row, episode) for compatibility with callers/tests."""
        return [(row, ep) for row, ep, _feed in self._get_selected_episode_refs()]

    # ── Listened state ──────────────────────────────────────────────────

    def _set_listened_refs(self, episode_refs: list, listened: bool) -> None:
        changed_feeds: dict[str, PodcastFeed] = {}
        changed_count = 0

        for _row, ep, feed in episode_refs:
            if ep is None:
                continue
            if _episode_is_listened(ep) == listened:
                continue
            _set_episode_listened(ep, listened)
            changed_count += 1
            # Orphan podcasts have no subscription to write back to; letting
            # their placeholder feed through would invent one on the iPod.
            if feed is not None and not _is_synthetic_feed(feed):
                changed_feeds[getattr(feed, "feed_url", str(id(feed)))] = cast(
                    "PodcastFeed",
                    feed,
                )

        if changed_count <= 0:
            self._set_action_status(
                "Selected episodes are already marked"
                if listened
                else "Selected episodes are already unmarked"
            )
            return

        store = self._store
        if store is not None and changed_feeds:
            if not self._persist_subscription_change(
                "save listened status",
                lambda: store.update_feeds(list(changed_feeds.values())),
            ):
                return

        self._refresh_current_view()
        self._refresh_feed_list()

        state = "listened" if listened else "unlistened"
        self._set_action_status(
            f"Marked {changed_count} episode{'s' if changed_count != 1 else ''} as {state}"
        )

    # ── Add to iPod (download + sync in one step) ──────────────────

    def _on_add_to_ipod(self) -> None:
        """Sync selected episodes to iPod.

        Builds a sync plan that includes both downloaded and pending
        episodes. Pending episodes will be downloaded during sync execution.

        Single-action flow:
        1. Filters out episodes already on iPod
        2. Builds a sync plan (includes pending episodes)
        3. Emits plan for sync review
        """
        selected = self._get_selected_episode_refs()
        if not selected:
            self._set_action_status("Select episodes first")
            return
        self._add_to_ipod_refs(selected)

    def _refresh_episode_selection_bar(self) -> None:
        """Show the batch bar, and the actions the selection actually allows.

        Each button carries its own count so a mixed selection is honest about
        how it splits before anything is clicked.
        """
        bar = getattr(self, "_selection_bar", None)
        if bar is None:
            return  # Called during construction, before the bar exists.

        rows = self._episode_list.selected_rows()
        count = len(rows)
        if not count:
            bar.hide()
            return

        addable = 0
        removable = 0
        for row in rows:
            if not (0 <= row < len(self._episode_dicts)):
                continue
            row_data = self._episode_dicts[row]
            if row_data.get("_can_add_to_ipod"):
                addable += 1
            if row_data.get("_can_remove_from_ipod"):
                removable += 1

        noun = "episode" if count == 1 else "episodes"
        self._selection_count_label.setText(f"{count} {noun} selected")

        # Hide rather than disable: a greyed button with no explanation reads
        # as broken, and the bar only exists while something is selected.
        self._selection_apply_btn.setVisible(addable > 0)
        self._selection_apply_btn.setText(f"Add {addable} to iPod")
        self._selection_remove_btn.setVisible(removable > 0)
        self._selection_remove_btn.setText(f"Remove {removable} from iPod")

        self._update_selection_master(count)
        bar.show()

    def _update_selection_master(self, selected_count: int) -> None:
        """Drive the tri-state master to match the visible selection."""
        total = self._episode_list.row_count()
        if selected_count and selected_count >= total:
            state = Qt.CheckState.Checked
        elif selected_count:
            state = Qt.CheckState.PartiallyChecked
        else:
            state = Qt.CheckState.Unchecked
        self._selection_master.blockSignals(True)
        self._selection_master.setCheckState(state)
        self._selection_master.blockSignals(False)

    def _on_selection_master_clicked(self) -> None:
        """Any click on the master resolves to all-or-nothing."""
        rows = self._episode_list.selected_rows()
        if len(rows) >= self._episode_list.row_count():
            self._episode_list.clear_selection()
        else:
            self._episode_list.select_all()

    def _select_all_visible(self) -> None:
        """Select every listed episode, honouring an active search filter."""
        self._episode_list.select_all()

    def _on_apply_episode_selection(self) -> None:
        refs = self._get_selected_episode_refs()
        if not refs:
            return
        self._add_to_ipod_refs(refs)

    def _on_remove_episode_selection(self) -> None:
        """Send every removable episode in the selection to Sync Review."""
        from iopenpod.podcasts.models import STATUS_ON_IPOD

        removable = [
            (row, ep, feed)
            for row, ep, feed in self._get_selected_episode_refs()
            if getattr(ep, "status", "") == STATUS_ON_IPOD
            and getattr(ep, "ipod_db_track_id", 0)
        ]
        if not removable:
            self._set_action_status("No selected episode is on the iPod")
            return
        self._remove_from_ipod_refs(removable)

    def _add_to_ipod_refs(self, episode_refs: list) -> None:
        caps = self._device_sessions.current_session().capabilities
        if caps is not None and not caps.supports_podcast:
            self._set_action_status("This iPod does not support podcasts")
            return
        if not self._ipod_path:
            self._set_action_status("No iPod connected")
            return

        from iopenpod.podcasts.models import STATUS_DOWNLOADING, STATUS_ON_IPOD

        # Filter out episodes already on iPod
        actionable = [
            (row, ep, feed) for row, ep, feed in episode_refs
            if ep.status not in (STATUS_ON_IPOD, STATUS_DOWNLOADING)
        ]
        if not actionable:
            if all(ep.status == STATUS_ON_IPOD for _row, ep, _feed in episode_refs):
                self._set_action_status("Selected episodes are already on iPod")
            else:
                self._set_action_status("Selected episodes cannot be added yet")
            return

        # Build sync plan directly (pending episodes will download during sync)
        self._build_and_emit_refs(actionable)

    def _build_and_emit_plan(self, actionable_episodes, feed) -> None:
        """Build a SyncPlan from actionable episodes and emit to main app.

        Accepts both downloaded and pending episodes. Pending episodes will
        be downloaded during sync execution.

        Args:
            actionable_episodes: List of PodcastEpisodes (not yet on iPod)
            feed: Parent PodcastFeed
        """
        self._build_and_emit_refs(
            [(0, ep, feed) for ep in actionable_episodes]
        )

    def _build_and_emit_refs(self, actionable_refs) -> None:
        """Build a SyncPlan from ``(row, episode, feed)`` references."""
        episodes_for_plan = [
            (ep, feed)
            for _row, ep, feed in actionable_refs
            if ep is not None and feed is not None
        ]

        if not episodes_for_plan:
            self._set_action_status("No episodes to sync")
            return

        # Get current iPod tracks for dedup
        ipod_tracks = self._current_ipod_tracks() or []

        from iopenpod.podcasts.podcast_sync import build_podcast_sync_plan
        plan = build_podcast_sync_plan(episodes_for_plan, ipod_tracks, self._store)

        if not plan.to_add:
            self._set_action_status("All selected episodes are already on iPod")
            return

        n = len(plan.to_add)
        self._set_action_status(
            f"Sending {n} episode{'s' if n != 1 else ''} to sync…")

        self.podcast_sync_requested.emit(plan)

    def _on_add_error(self, error_tuple) -> None:
        self._progress_bar.hide()
        _, value, _ = error_tuple
        self._set_action_status(f"Failed: {value}")

    # ── Remove download / Remove from iPod ───────────────────────────────

    def _remove_downloads(self, episodes: list) -> None:
        """Delete downloaded files from the selected feed."""
        if not self._selected_feed:
            return
        self._remove_download_refs(
            [(0, ep, self._selected_feed) for ep in episodes]
        )

    def _remove_download_refs(self, episode_refs: list) -> None:
        """Delete downloaded files and reset episode status."""

        from iopenpod.podcasts.models import STATUS_NOT_DOWNLOADED

        removed = 0
        failures: list[tuple[object, Exception]] = []
        changed_feeds: dict[str, PodcastFeed] = {}
        store = self._store
        for _row, ep, feed in episode_refs:
            downloaded_path = ep.downloaded_path
            if downloaded_path:
                try:
                    if store is None:
                        raise RuntimeError("Podcast download storage is unavailable")
                    store.remove_episode_download(downloaded_path)
                except Exception as exc:
                    log.warning(
                        "Could not safely remove podcast download %r: %s",
                        downloaded_path,
                        exc,
                    )
                    failures.append((downloaded_path, exc))
                    continue
            ep.downloaded_path = ""
            ep.status = STATUS_NOT_DOWNLOADED
            if feed is not None and not _is_synthetic_feed(feed):
                changed_feeds[getattr(feed, "feed_url", str(id(feed)))] = cast(
                    "PodcastFeed",
                    feed,
                )
            removed += 1

        if store is not None and changed_feeds:
            if not self._persist_subscription_change(
                "save downloaded episode status",
                lambda: store.update_feeds(list(changed_feeds.values())),
            ):
                return

        self._refresh_current_view()
        self._refresh_feed_list()
        if failures:
            failed_count = len(failures)
            title = "Download Not Removed" if failed_count == 1 else "Downloads Not Removed"
            first_error = failures[0][1]
            QMessageBox.warning(
                self,
                title,
                "iOpenPod left "
                f"{failed_count} episode download"
                f"{'s' if failed_count != 1 else ''} and their saved state "
                "unchanged because the stored path was unsafe or the file "
                f"could not be removed.\n\n{first_error}",
            )
            if removed:
                self._set_action_status(
                    f"Removed {removed}; {failed_count} not removed"
                )
            else:
                verb = "was" if failed_count == 1 else "were"
                self._set_action_status(
                    f"{failed_count} download{'s' if failed_count != 1 else ''} "
                    f"{verb} not removed"
                )
            return
        self._set_action_status(
            f"Removed {removed} download{'s' if removed != 1 else ''}"
        )

    def _remove_from_ipod(self, episodes: list) -> None:
        """Build a sync plan to remove episodes from the iPod."""
        if not self._selected_feed:
            return
        self._remove_from_ipod_refs(
            [(0, ep, self._selected_feed) for ep in episodes]
        )

    def _remove_from_ipod_refs(self, episode_refs: list) -> None:
        """Build a sync plan to remove episode/feed refs from the iPod."""
        if not self._ipod_path:
            self._set_action_status("No iPod connected")
            return

        from iopenpod.application.sync_plan_builder import build_podcast_removal_sync_plan
        from iopenpod.sync.contracts import StorageSummary, SyncPlan

        ipod_tracks = self._current_ipod_tracks() or []
        episodes_by_feed: dict[str, tuple[object, list]] = {}
        for _row, ep, feed in episode_refs:
            if feed is None:
                continue
            key = getattr(feed, "feed_url", "") or str(id(feed))
            if key not in episodes_by_feed:
                episodes_by_feed[key] = (feed, [])
            episodes_by_feed[key][1].append(ep)

        plan = SyncPlan()
        plan.storage = StorageSummary()
        plan.removals_pre_checked = True
        for feed, episodes in episodes_by_feed.values():
            partial = build_podcast_removal_sync_plan(
                episodes,
                ipod_tracks,
                getattr(feed, "title", "") or "Podcast",
            )
            if partial is None:
                continue
            plan.to_remove.extend(partial.to_remove)
            plan.storage.bytes_to_remove += partial.storage.bytes_to_remove

        if not plan.to_remove:
            self._set_action_status("Episodes not found on iPod")
            return

        # Sync Review is the confirmation step for this action, so name it
        # rather than adding a second dialog on top of it.
        n = len(plan.to_remove)
        self._set_action_status(
            f"{n} removal{'s' if n != 1 else ''} sent to Sync Review"
        )
        self.podcast_sync_requested.emit(plan)

    def refresh_episodes(self) -> None:
        """Public: refresh the episode table and feed list from store.

        Called after sync completes so status changes (e.g. 'on_ipod')
        are reflected in the UI.
        """
        self._refresh_feed_list()
        self._refresh_current_view()

    # ── Artwork loading ──────────────────────────────────────────────────

    # ── Per-feed settings ───────────────────────────────────────────────

    def _load_feed_settings(self, feed) -> None:
        """Populate the per-feed setting controls from a PodcastFeed."""
        # Block signals while loading to avoid triggering saves
        for w in (self._feed_episode_slots, self._feed_fill_mode,
                  self._feed_clear_listened, self._feed_clear_older,
                  self._feed_clear_method):
            w.blockSignals(True)

        enabled = feed is not None
        self._feed_episode_slots.setEnabled(enabled)
        self._feed_fill_mode.setEnabled(enabled)
        self._feed_clear_listened.setEnabled(enabled)
        self._feed_clear_older.setEnabled(enabled)
        self._feed_clear_method.setEnabled(enabled)

        if feed is not None:
            self._feed_episode_slots.setValue(feed.episode_slots)

            _fill_display = {"newest": "Newest Episode", "next": "Next Episode"}
            idx = self._feed_fill_mode.findText(
                _fill_display.get(feed.fill_mode, "Newest Episode"),
            )
            if idx >= 0:
                self._feed_fill_mode.setCurrentIndex(idx)

            _cl_display = {True: "Yes", False: "No"}
            idx = self._feed_clear_listened.findText(
                _cl_display.get(feed.clear_when_listened, "Yes"),
            )
            if idx >= 0:
                self._feed_clear_listened.setCurrentIndex(idx)

            _older_display = {
                "immediate": "Immediately",
                "1_day": "1 Day", "3_days": "3 Days",
                "1_week": "1 Week", "2_weeks": "2 Weeks",
                "1_month": "1 Month", "2_months": "2 Months",
                "3_months": "3 Months", "never": "Never",
            }
            idx = self._feed_clear_older.findText(
                _older_display.get(feed.clear_older_than, "Never"),
            )
            if idx >= 0:
                self._feed_clear_older.setCurrentIndex(idx)

            _method_display = {
                "remove": "Remove Immediately",
                "replace": "Mark for Replacement",
            }
            idx = self._feed_clear_method.findText(
                _method_display.get(feed.clear_method, "Remove Immediately"),
            )
            if idx >= 0:
                self._feed_clear_method.setCurrentIndex(idx)
        else:
            self._feed_episode_slots.setValue(3)
            self._feed_fill_mode.setCurrentIndex(0)
            self._feed_clear_listened.setCurrentIndex(0)
            self._feed_clear_older.setCurrentIndex(
                self._feed_clear_older.count() - 1,  # "Never"
            )
            self._feed_clear_method.setCurrentIndex(0)

        for w in (self._feed_episode_slots, self._feed_fill_mode,
                  self._feed_clear_listened, self._feed_clear_older,
                  self._feed_clear_method):
            w.blockSignals(False)

    def _on_feed_setting_changed(self, *_args) -> None:
        """Write current setting controls back to the selected feed."""
        store = self._store
        if store is None or not self._selected_feed:
            return

        feed = self._selected_feed

        _fill_keys = {"Newest Episode": "newest", "Next Episode": "next"}
        _cl_keys = {"Yes": True, "No": False}
        _older_keys = {
            "Immediately": "immediate",
            "1 Day": "1_day", "3 Days": "3_days",
            "1 Week": "1_week", "2 Weeks": "2_weeks",
            "1 Month": "1_month", "2 Months": "2_months",
            "3 Months": "3_months", "Never": "never",
        }
        _method_keys = {
            "Remove Immediately": "remove",
            "Mark for Replacement": "replace",
        }

        feed.episode_slots = self._feed_episode_slots.value()
        feed.fill_mode = _fill_keys.get(
            self._feed_fill_mode.currentText(), "newest",
        )
        feed.clear_when_listened = _cl_keys.get(
            self._feed_clear_listened.currentText(), True,
        )
        feed.clear_older_than = _older_keys.get(
            self._feed_clear_older.currentText(), "never",
        )
        feed.clear_method = _method_keys.get(
            self._feed_clear_method.currentText(), "remove",
        )

        self._persist_subscription_change(
            "save podcast sync settings",
            lambda: store.update_feed(feed),
        )

    def _set_feed_art_placeholder(self) -> None:
        """Set a crisp HiDPI-safe placeholder icon in the feed artwork slot."""
        placeholder = self._artwork_placeholder_pixmap(52)
        if placeholder:
            self._feed_art.setPixmap(placeholder)
            self._feed_art.setText("")
        else:
            self._feed_art.setText("◎")
        self._reset_feed_hero_color()

    def _artwork_placeholder_pixmap(
        self,
        size: int,
        *,
        glyph: str = "broadcast",
    ) -> QPixmap | None:
        """Create the gray square tile used when artwork is missing.

        The library rows reuse this tile with their own glyph so they read as
        siblings of the real feed thumbnails beneath them.
        """
        glyph_px = glyph_pixmap(
            glyph,
            max(16, int(size * 0.52)),
            paint_css("text.tertiary"),
        )
        if glyph_px is None:
            return None

        px = QPixmap(size, size)
        px.fill(QColor(paint_css("surface.inset")))
        painter = QPainter(px)
        try:
            x = (size - glyph_px.width()) // 2
            y = (size - glyph_px.height()) // 2
            painter.drawPixmap(x, y, glyph_px)
        finally:
            painter.end()
        return px

    def _apply_hero_color_for_source(self, source: str, pixmap: QPixmap) -> None:
        cached = _artwork_color_cache.get(source)
        if cached is not None:
            self._apply_feed_hero_color(*cached)
            return
        color = dominant_artwork_color_from_pixmap(pixmap)
        if color is None:
            return
        _artwork_color_cache[source] = color
        self._apply_feed_hero_color(*color)

    def _apply_feed_hero_color(self, r: int, g: int, b: int) -> None:
        """Tint the hero header with the artwork's dominant color."""
        hero_paints = render_content_hero_paints(current_theme(), (r, g, b))

        self._feed_header.setStyleSheet(f"""
            QFrame#heroHeader {{
                background: qlineargradient(
                    x1:0, y1:0, x2:0, y2:1,
                    stop:0 {hero_paints.header_tint.css},
                    stop:1 {paint_css('canvas.default')}
                );
                border-bottom: 1px solid {hero_paints.header_border.css};
            }}
        """)
        self._feed_art.setStyleSheet(f"""
            background: {hero_paints.art_fill.css};
            border-radius: {Metrics.BORDER_RADIUS}px;
            border: 1px solid {hero_paints.art_border.css};
        """)
        self._feed_title_label.setStyleSheet(
            "color: " + paint_css("text.primary") + "; background: transparent;")
        self._feed_author_label.setStyleSheet(
            "color: " + paint_css("text.secondary") + "; background: transparent;")
        self._feed_description_label.setStyleSheet(
            "color: " + paint_css("text.secondary") + "; background: transparent;")
        self._feed_detail_label.setStyleSheet(
            "color: " + paint_css("text.tertiary") + "; background: transparent;")

        _glass_css = btn_css(
            bg=hero_paints.action_fill.css,
            bg_hover=hero_paints.action_hover.css,
            bg_press=hero_paints.action_pressed.css,
            fg=paint_css("text.primary"),
            border=f"1px solid {hero_paints.action_border.css}",
            padding="5px 12px",
            radius=Metrics.BORDER_RADIUS_SM,
        )
        for btn in self._hero_btns:
            btn.setStyleSheet(_glass_css)

    def _reset_feed_hero_color(self) -> None:
        """Reset the hero to the default (no artwork tint) style."""
        self._feed_header.setStyleSheet(f"""
            QFrame#heroHeader {{
                background: {paint_css('canvas.default')};
                border-bottom: 1px solid {paint_css('border.subtle')};
            }}
        """)
        self._feed_art.setStyleSheet(f"""
            background: {paint_css('surface.default')};
            border-radius: {Metrics.BORDER_RADIUS}px;
            border: 1px solid {paint_css('border.subtle')};
        """)
        # Labels and buttons may not exist yet during initial construction
        if not hasattr(self, '_feed_title_label'):
            return
        self._feed_title_label.setStyleSheet(
            "color: " + paint_css("text.primary") + "; background: transparent;")
        self._feed_author_label.setStyleSheet(
            "color: " + paint_css("text.secondary") + "; background: transparent;")
        self._feed_description_label.setStyleSheet(
            "color: " + paint_css("text.secondary") + "; background: transparent;")
        self._feed_detail_label.setStyleSheet(
            "color: " + paint_css("text.tertiary") + "; background: transparent;")
        _default_css = btn_css(padding="5px 12px", radius=Metrics.BORDER_RADIUS_SM)
        for btn in self._hero_btns:
            btn.setStyleSheet(_default_css)

    def _feed_artwork_source(self, feed) -> str:
        from iopenpod.podcasts.artwork import resolve_feed_artwork_source

        podcast_dir = self._store.podcast_dir if self._store else ""
        return resolve_feed_artwork_source(feed, podcast_dir)

    def _request_artwork(
        self,
        source: str,
        on_ready: Callable[[str, QPixmap], None],
    ) -> None:
        if not source:
            return

        cached = _artwork_cache.get(source)
        if cached is not None:
            on_ready(source, cached)
            return

        waiters = self._artwork_inflight.get(source)
        if waiters is not None:
            waiters.append(on_ready)
            return

        self._artwork_inflight[source] = [on_ready]

        from iopenpod.application.runtime import ThreadPoolSingleton, Worker

        def _fetch() -> tuple[str, bytes | None]:
            return source, _load_artwork_bytes(source)

        worker = Worker(_fetch)
        worker.signals.result.connect(self._on_artwork_request_finished)
        worker.signals.error.connect(
            lambda _, s=source: self._on_artwork_request_failed(s)
        )
        ThreadPoolSingleton.get_instance().start(worker)

    def _on_artwork_request_finished(self, result: tuple[str, bytes | None]) -> None:
        source, data = result
        callbacks = self._artwork_inflight.pop(source, [])
        if not data:
            return

        img = QImage()
        if not img.loadFromData(data):
            return

        full_pm = QPixmap.fromImage(img)
        _artwork_cache[source] = full_pm

        for callback in callbacks:
            callback(source, full_pm)

    def _on_artwork_request_failed(self, source: str) -> None:
        self._artwork_inflight.pop(source, None)
        log.debug("Failed to load artwork: %s", source)

    def _apply_feed_artwork_pixmap(self, source: str, full_pm: QPixmap) -> None:
        art_w = max(1, self._feed_art.width())
        art_h = max(1, self._feed_art.height())
        pm = scale_pixmap_for_display(
            full_pm,
            art_w,
            art_h,
            widget=self._feed_art,
            aspect_mode=Qt.AspectRatioMode.KeepAspectRatio,
            transform_mode=Qt.TransformationMode.SmoothTransformation,
        )
        self._feed_art.setPixmap(pm)
        self._feed_art.setText("")
        self._apply_hero_color_for_source(source, full_pm)

    def _load_feed_artwork(self, source: str) -> None:
        """Load feed artwork for the header panel in background."""
        def _apply_if_selected(loaded_source: str, full_pm: QPixmap) -> None:
            if (
                self._selected_feed
                and self._feed_artwork_source(self._selected_feed) == loaded_source
            ):
                self._apply_feed_artwork_pixmap(loaded_source, full_pm)
            self._update_feed_list_icon(loaded_source, full_pm)

        self._request_artwork(source, _apply_if_selected)

    def _load_feed_list_artwork(self, source: str) -> None:
        """Load a feed's artwork for its list item thumbnail."""
        self._request_artwork(
            source,
            lambda loaded_source, full_pm: self._update_feed_list_icon(
                loaded_source,
                full_pm,
            ),
        )

    def _update_feed_list_icon(self, url: str, full_pm: QPixmap) -> None:
        """Set the icon for all feed list items whose artwork URL matches."""
        if not self._store:
            return
        icon_pm = scale_pixmap_for_display(
            full_pm,
            36,
            36,
            widget=self._feed_list,
            aspect_mode=Qt.AspectRatioMode.KeepAspectRatio,
            transform_mode=Qt.TransformationMode.SmoothTransformation,
        )
        icon = QIcon(icon_pm)
        for feed in self._store.get_feeds():
            if self._feed_artwork_source(feed) != url:
                continue
            item = self._feed_list.item(self._row_for_key(feed.feed_url))
            if item:
                item.setIcon(icon)

    # ── Status helpers ───────────────────────────────────────────────────

    def _set_status(self, text: str, timeout_ms: int = 5000) -> None:
        """Set toolbar status text with auto-clear."""
        self._status_label.setText(text)
        self._status_clear_timer.stop()
        self._status_clear_text = text
        if timeout_ms > 0 and text:
            self._status_clear_timer.start(timeout_ms)

    def _clear_status_timeout(self) -> None:
        self._clear_status_if(self._status_clear_text)

    def _clear_status_if(self, expected: str) -> None:
        """Clear status only if it still shows the expected message."""
        if self._status_label.text() == expected:
            self._status_label.setText("")

    def _set_action_status(self, text: str, timeout_ms: int = 5000) -> None:
        """Show the status toast with *text*, auto-hiding after *timeout_ms*."""
        self._action_status.setText(text)
        self._action_status_clear_timer.stop()
        self._action_status_clear_text = text
        if text:
            self._status_toast.show()
        else:
            self._status_toast.hide()
        if timeout_ms > 0 and text:
            self._action_status_clear_timer.start(timeout_ms)

    def _clear_action_timeout(self) -> None:
        self._clear_action_if(self._action_status_clear_text)

    def _clear_action_if(self, expected: str) -> None:
        if self._action_status.text() == expected:
            self._action_status.setText("")
            self._status_toast.hide()
