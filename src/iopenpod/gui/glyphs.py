"""SVG glyph loader for iOpenPod.

Loads 24x24 stroke-based SVG icons from assets/glyphs/, colorizes them by
replacing ``currentColor`` with the requested CSS color, and renders to
QIcon / QPixmap at DPI- sizes via QSvgRenderer.
"""

from __future__ import annotations

import atexit
import hashlib
import logging
import re
import shutil
import tempfile
from pathlib import Path

from PyQt6.QtCore import QByteArray, Qt
from PyQt6.QtGui import QIcon, QImage, QPainter, QPixmap

from iopenpod.resources import resource_path

from .hidpi import effective_device_pixel_ratio, logical_to_physical

log = logging.getLogger(__name__)

try:
    from PyQt6.QtSvg import QSvgRenderer
    _HAS_SVG = True
except ImportError:
    QSvgRenderer = None
    _HAS_SVG = False

_GLYPH_DIR = resource_path("assets", "glyphs")

_svg_cache: dict[str, bytes] = {}

# Rasterized glyphs referenced by stylesheets, keyed by (name, size, color).
# The directory is created on first use and removed when the process ends.
_stylesheet_glyph_cache: dict[tuple[str, int, str], str] = {}
_stylesheet_glyph_root: Path | None = None

_RE_RGBA = re.compile(
    r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*(?:,\s*(\d+(?:\.\d+)?)\s*)?\)"
)


def _parse_color(color: str) -> tuple[str, float]:
    """Convert a CSS color string to ``(hex_rgb, opacity_0_to_1)``."""
    if color.startswith("#"):
        return color, 1.0
    m = _RE_RGBA.match(color)
    if m:
        r, g, b = int(m.group(1)), int(m.group(2)), int(m.group(3))
        a = float(m.group(4)) if m.group(4) else 255.0
        if a > 1.0:
            a /= 255.0
        return f"#{r:02x}{g:02x}{b:02x}", a
    return color, 1.0


def _load_svg(name: str) -> bytes | None:
    if name in _svg_cache:
        return _svg_cache[name]
    path = _GLYPH_DIR / f"{name}.svg"
    if not path.is_file():
        return None
    with path.open("rb") as f:
        data = f.read()
    _svg_cache[name] = data
    return data


def _colorized_svg(name: str, color: str) -> bytes | None:
    """Return the named glyph's SVG source painted in *color*."""
    raw = _load_svg(name)
    if raw is None:
        return None
    hex_color, opacity = _parse_color(color)
    colored = raw.replace(b"currentColor", hex_color.encode("ascii"))
    if opacity < 0.99:
        colored = colored.replace(
            b"<svg ", f'<svg opacity="{opacity:.3f}" '.encode("ascii"), 1
        )
    return colored


def _render_glyph(colored_svg: bytes, pixel_size: int) -> QImage | None:
    """Rasterize prepared SVG source at an exact pixel size.

    Renders to a ``QImage`` rather than a ``QPixmap`` so that callers which
    only want bytes on disk — stylesheet marks — do not drag in a running
    QGuiApplication, which QPixmap requires and aborts without.
    """
    if QSvgRenderer is None:
        return None
    renderer = QSvgRenderer(QByteArray(colored_svg))
    if not renderer.isValid():
        return None
    image = QImage(pixel_size, pixel_size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    renderer.render(painter)
    painter.end()
    return image


def glyph_pixmap(name: str, size: int, color: str = "#ffffff") -> QPixmap | None:
    """Render a named SVG glyph to a *size x size* ``QPixmap``.

    *color* may be any CSS color (``#hex``, ``rgb()``, ``rgba()``).
    Returns ``None`` when SVG support is unavailable or the file is missing.
    """
    if not _HAS_SVG:
        return None
    colored = _colorized_svg(name, color)
    if colored is None:
        return None
    dpr = effective_device_pixel_ratio()
    image = _render_glyph(colored, logical_to_physical(size, dpr))
    if image is None:
        return None
    px = QPixmap.fromImage(image)
    px.setDevicePixelRatio(dpr)
    return px


def glyph_stylesheet_url(name: str, size: int, color: str) -> str:
    """Return a ``url(...)`` for a colorized glyph, for use in a stylesheet.

    Qt stylesheets can only reach an image through the filesystem, so the glyph
    is rasterized to a file the first time a size and color are asked for and
    reused afterwards. A ``@2x`` companion is written beside it so the mark
    stays crisp on a high-density display.

    Returns an empty string when the glyph cannot be rendered, which leaves the
    caller's rule without an image rather than pointing it at nothing.
    """
    if not _HAS_SVG:
        return ""

    key = (name, size, color)
    cached = _stylesheet_glyph_cache.get(key)
    if cached is not None:
        return cached

    colored = _colorized_svg(name, color)
    if colored is None:
        return ""

    directory = _stylesheet_glyph_dir()
    if directory is None:
        return ""

    # The color is part of the filename, so switching themes writes a new mark
    # instead of leaving the previous theme's color on screen.
    digest = hashlib.sha256(f"{name}|{size}|{color}".encode()).hexdigest()[:12]
    path = directory / f"{name}-{size}-{digest}.png"
    if not path.exists():
        standard = _render_glyph(colored, size)
        retina = _render_glyph(colored, size * 2)
        if standard is None or retina is None:
            return ""
        # Qt's @2x convention: it loads the denser file when the screen has the
        # pixels for it, and the plain one otherwise.
        if not standard.save(str(path)) or not retina.save(
            str(path.with_name(f"{path.stem}@2x.png"))
        ):
            return ""

    url = f"url({path.as_posix()})"
    _stylesheet_glyph_cache[key] = url
    return url


def _stylesheet_glyph_dir() -> Path | None:
    """The per-run directory holding rasterized stylesheet glyphs."""
    global _stylesheet_glyph_root
    if _stylesheet_glyph_root is None:
        try:
            _stylesheet_glyph_root = Path(
                tempfile.mkdtemp(prefix="iopenpod-glyphs-")
            )
        except OSError:
            log.warning("Could not create a glyph cache; marks will be omitted")
            return None
        atexit.register(
            shutil.rmtree, _stylesheet_glyph_root, ignore_errors=True
        )
    return _stylesheet_glyph_root


def glyph_icon(name: str, size: int, color: str = "#ffffff") -> QIcon | None:
    """Render a named SVG glyph to a ``QIcon``.

    Returns ``None`` when SVG support is unavailable or the file is missing.
    """
    px = glyph_pixmap(name, size, color)
    if px is None:
        return None
    return QIcon(px)
