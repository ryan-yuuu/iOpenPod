"""Audnexus client — resolves an ASIN to a full audiobook record.

Audnexus aggregates Audible catalog data behind an open API that needs no
authentication.  It has no search endpoint; pair it with
:mod:`iopenpod.audiobooks.audible_search` to get an ASIN first.
"""

from __future__ import annotations

import html
import logging
import re
from typing import Any

import requests

from .models import AudiobookMetadata
from .network_errors import audiobook_network_error

log = logging.getLogger(__name__)

_BASE_URL = "https://api.audnex.us"
_TIMEOUT = 20  # seconds
_ALLOWED_REGIONS = frozenset({"au", "ca", "de", "es", "fr", "in", "it", "jp", "uk", "us"})
DEFAULT_REGION = "us"

_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"\s+")
_SPACE_BEFORE_PUNCT_RE = re.compile(r"\s+([,.;:!?])")
_OPEN_PAREN_RE = re.compile(r"\(\s+")
_CLOSE_PAREN_RE = re.compile(r"\s+\)")


def strip_summary_html(raw: str) -> str:
    """Flatten the HTML summary Audnexus returns into plain text.

    Tags become spaces rather than being deleted; deleting them welds the
    surrounding words together (``old man.Here we meet``).
    """
    if not raw:
        return ""
    text = html.unescape(_TAG_RE.sub(" ", raw))
    text = _SPACE_RE.sub(" ", text).strip()
    text = _OPEN_PAREN_RE.sub("(", text)
    text = _CLOSE_PAREN_RE.sub(")", text)
    return _SPACE_BEFORE_PUNCT_RE.sub(r"\1", text)


def normalize_region(region: str) -> str:
    candidate = (region or "").strip().lower()
    return candidate if candidate in _ALLOWED_REGIONS else DEFAULT_REGION


def fetch_audiobook(
    asin: str,
    *,
    region: str = DEFAULT_REGION,
    raise_on_error: bool = False,
) -> AudiobookMetadata | None:
    """Fetch the full record for ``asin``.

    Returns ``None`` when the item is unknown or unavailable in the region —
    a routine outcome that callers handle by suggesting another region.
    """
    identifier = asin.strip()
    if not identifier:
        return None

    url = f"{_BASE_URL}/books/{identifier}"
    try:
        resp = requests.get(url, params={"region": normalize_region(region)}, timeout=_TIMEOUT)
        if resp.status_code == 404:
            log.info("Audnexus has no record for %s in region %s", identifier, region)
            return None
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:  # noqa: BLE001 - re-raised as a described error
        log.warning("Audnexus lookup failed for %s: %s", identifier, exc)
        if raise_on_error:
            raise audiobook_network_error(exc, action="load audiobook details") from exc
        return None

    if not isinstance(payload, dict) or payload.get("error"):
        return None
    return _metadata_from_payload(identifier, payload)


def _metadata_from_payload(asin: str, payload: dict[str, Any]) -> AudiobookMetadata:
    return AudiobookMetadata(
        asin=str(payload.get("asin") or asin).strip(),
        title=str(payload.get("title") or "").strip(),
        subtitle=str(payload.get("subtitle") or "").strip(),
        authors=_names(payload.get("authors")),
        narrators=_names(payload.get("narrators")),
        publisher=str(payload.get("publisherName") or "").strip(),
        release_year=_year(payload.get("releaseDate")),
        isbn=str(payload.get("isbn") or "").strip(),
        summary=strip_summary_html(str(payload.get("summary") or "")),
        genres=_names(payload.get("genres")),
        cover_url=str(payload.get("image") or "").strip(),
        runtime_min=_int(payload.get("runtimeLengthMin")),
    )


def fetch_cover_bytes(url: str, *, timeout: int = _TIMEOUT) -> bytes:
    """Download cover artwork. Audnexus returns a URL, not image data."""
    if not url:
        return b""
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp.content


def _names(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    names = []
    for entry in value:
        if isinstance(entry, dict):
            name = str(entry.get("name") or "").strip()
            if name:
                names.append(name)
    return tuple(names)


def _year(value: Any) -> int | None:
    text = str(value or "").strip()
    if len(text) < 4 or not text[:4].isdigit():
        return None
    return int(text[:4])


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
