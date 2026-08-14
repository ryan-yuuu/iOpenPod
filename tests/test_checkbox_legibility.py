"""The multi-select checkbox has to be visible in every theme.

An unticked box carries no fill worth seeing, so its outline *is* the control.
When that outline is borrowed from a decorative border role it disappears into
the card behind it, and the only way to find out something is selectable is to
click and watch what happens.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from iopenpod.gui import styles
from iopenpod.gui.glyphs import glyph_stylesheet_url
from iopenpod.gui.styles import (
    CHECKBOX_INDICATOR_SIZE,
    apply_theme,
    checkbox_css,
    current_theme,
    table_css,
)
from iopenpod.infrastructure.theme_catalog import (
    bundled_theme_directory,
    load_theme_catalog,
)
from iopenpod.infrastructure.theme_renderer import Color, render_theme

# WCAG 2.2 SC 1.4.11 for control boundaries, and SC 1.4.3 for the mark on its
# fill. The renderer holds itself to these; the tests state them independently.
CONTROL_FLOOR = 3.0
HIGH_CONTRAST_FLOOR = 4.5
MARK_FLOOR = 4.5

# The autouse settings-dir fixture empties the user catalog, and get() falls
# back to "dark" for anything missing — which would quietly test one theme
# eleven times. Read the bundled files directly.
def _catalog():
    return load_theme_catalog(bundled_theme_directory())


BUNDLED_THEMES = sorted(_catalog().themes)


def _rendered(theme_id: str, *, high_contrast: bool = False):
    theme = _catalog().get(theme_id)
    assert theme.id == theme_id, "the catalog fell back to another theme"
    return render_theme(theme, high_contrast=high_contrast)


# ── Every theme, every state ────────────────────────────────────────────────


@pytest.mark.parametrize("theme_id", BUNDLED_THEMES)
def test_an_unticked_box_is_visible_on_the_surfaces_it_sits_on(theme_id: str) -> None:
    resolved = _rendered(theme_id)
    border = resolved.paint("control.checkbox.border").color

    for backdrop_name in (
        "SURFACE",              # cards, panels, tables
        "podcast.episode.fill",
        "surface.raised",       # the batch selection bar
    ):
        backdrop = resolved.paint(backdrop_name).color
        ratio = border.contrast_ratio(backdrop)
        assert ratio >= CONTROL_FLOOR - 0.05, (
            f"{theme_id}: the empty box is {ratio:.2f}:1 on {backdrop_name}"
        )


@pytest.mark.parametrize("theme_id", BUNDLED_THEMES)
def test_a_ticked_box_and_its_mark_both_read(theme_id: str) -> None:
    resolved = _rendered(theme_id)
    surface = resolved.paint("SURFACE").color
    fill = resolved.paint("control.checkbox.checked_fill").color
    mark = resolved.paint("control.checkbox.mark").color

    assert fill.contrast_ratio(surface) >= CONTROL_FLOOR - 0.05
    assert mark.contrast_ratio(fill) >= MARK_FLOOR - 0.05


@pytest.mark.parametrize("theme_id", BUNDLED_THEMES)
def test_a_selected_row_still_shows_its_box(theme_id: str) -> None:
    """A selected episode card is tinted, and the box sits on that tint.

    Only the ticked and hovered states can land here — a podcast card ticks its
    own box when the row is selected — so those are what have to clear. If that
    coupling is ever undone, the empty box needs checking against this fill too.
    """

    resolved = _rendered(theme_id)
    selected_card = resolved.paint("podcast.episode.selected_fill").color

    for name in ("control.checkbox.checked_fill", "control.checkbox.hover_border"):
        ratio = resolved.paint(name).color.contrast_ratio(selected_card)
        assert ratio >= CONTROL_FLOOR - 0.05, f"{theme_id}: {name} is {ratio:.2f}:1"


@pytest.mark.parametrize("theme_id", BUNDLED_THEMES)
def test_the_accessibility_preference_raises_the_floor(theme_id: str) -> None:
    resolved = _rendered(theme_id, high_contrast=True)
    surface = resolved.paint("SURFACE").color

    ratio = resolved.paint("control.checkbox.border").color.contrast_ratio(surface)

    assert ratio >= HIGH_CONTRAST_FLOOR - 0.05, f"{theme_id}: {ratio:.2f}:1"


# ── The theme still gets to look like itself ────────────────────────────────


def test_a_lifted_border_keeps_the_theme_hue() -> None:
    resolved = _rendered("sea-glass")

    border = resolved.paint("control.checkbox.border").color

    # sea-glass is a teal theme; a grey box would read as someone else's.
    assert border.blue > border.red
    assert border.green > border.red


def test_a_border_that_already_reads_is_left_alone() -> None:
    resolved = _rendered("dark")
    surface = resolved.paint("SURFACE").color
    authored = resolved.paint("BORDER").color
    assert authored.contrast_ratio(surface) < CONTROL_FLOOR  # the case we fix

    legible = authored.legible_over(surface, CONTROL_FLOOR)

    assert legible != authored
    assert legible.legible_over(surface, CONTROL_FLOOR) == legible


def test_legibility_moves_a_color_as_little_as_it_can() -> None:
    backdrop = Color(0x21, 0x21, 0x35)
    far = Color(0xFF, 0xFF, 0xFF)
    near = Color(0x35, 0x35, 0x46)

    # Already past the floor: untouched, however far past it is.
    assert far.legible_over(backdrop, CONTROL_FLOOR) is far
    lifted = near.legible_over(backdrop, CONTROL_FLOOR)
    assert lifted.contrast_ratio(backdrop) >= CONTROL_FLOOR
    # Just past the floor rather than all the way to white.
    assert lifted.contrast_ratio(backdrop) < CONTROL_FLOOR + 0.5


def test_the_renderer_records_why_a_border_moved() -> None:
    paint = _rendered("dark").paint("control.checkbox.border")

    assert paint.kind == "opaque"
    assert paint.backdrop_role == "SURFACE"
    assert "3.0:1 over SURFACE" in paint.recipe


# ── One recipe, used everywhere a box is drawn ──────────────────────────────


def _with_dark_theme(build):
    snapshot = current_theme()
    apply_theme("dark", "off", "blue")
    try:
        return build()
    finally:
        styles._THEME_RUNTIME.replace(snapshot)


def test_every_checkbox_surface_uses_the_shared_paints() -> None:
    sheets = _with_dark_theme(
        lambda: (checkbox_css(), table_css(), styles.app_stylesheet())
    )
    resolved = current_theme()

    for sheet in sheets:
        indicator_rules = "\n".join(
            line for line in sheet.splitlines() if "::indicator" in line
        )
        assert indicator_rules, "a stylesheet stopped drawing check indicators"
        assert resolved.paint("control.checkbox.border").css in sheet
        # The decorative border role is what made the box invisible; no
        # indicator may quietly go back to it.
        assert "::indicator" not in sheet.split(resolved.paint("border.default").css)[0][-200:]


def test_state_is_marked_and_not_only_coloured() -> None:
    sheet = _with_dark_theme(checkbox_css)

    checked = re.search(r"::indicator:checked \{[^}]*\}", sheet, re.S)
    partial = re.search(r"::indicator:indeterminate \{[^}]*\}", sheet, re.S)

    assert checked and "image: url(" in checked.group(0)
    assert partial and "image: url(" in partial.group(0)


def test_no_state_changes_the_size_of_the_box() -> None:
    sheet = _with_dark_theme(checkbox_css)

    # A state that redeclares width, height, or a thicker border would make the
    # box jump as the user ticks it — which is how the old partial state broke.
    for rule in re.findall(r"::indicator:[a-z:]+ \{[^}]*\}", sheet, re.S):
        assert "width:" not in rule
        assert "height:" not in rule
        assert re.search(r"border:\s", rule) is None


def test_widgets_are_told_how_much_room_the_box_needs() -> None:
    sheet = _with_dark_theme(checkbox_css)

    box = int(re.search(r"width: (\d+)px", sheet).group(1))

    # A widget that reserves only the drawn box clips the border off it.
    assert CHECKBOX_INDICATOR_SIZE == box + 2


# ── The mark itself ─────────────────────────────────────────────────────────


def test_a_mark_is_rasterized_once_and_reused() -> None:
    first = glyph_stylesheet_url("check", 12, "#ffffff")
    again = glyph_stylesheet_url("check", 12, "#ffffff")

    assert first == again
    assert first.startswith("url(") and first.endswith(".png)")


def test_each_colour_gets_its_own_mark() -> None:
    white = glyph_stylesheet_url("check", 12, "#ffffff")
    black = glyph_stylesheet_url("check", 12, "#000000")

    # Sharing one file would leave the previous theme's mark on screen.
    assert white != black


def test_a_dense_display_gets_a_denser_mark() -> None:
    url = glyph_stylesheet_url("minus", 12, "#ffffff")
    path = Path(url[len("url(") : -1])

    assert path.exists()
    assert path.with_name(f"{path.stem}@2x.png").exists()


def test_a_missing_glyph_leaves_the_rule_without_an_image() -> None:
    assert glyph_stylesheet_url("not-a-real-glyph", 12, "#ffffff") == ""
