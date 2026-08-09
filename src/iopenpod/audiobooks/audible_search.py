"""Audible catalog search — turns a title into candidate ASINs.

The Audnexus metadata API is ASIN-only and has no search endpoint, so a
separate search step is required before any record can be fetched.  The
public Audible catalog API needs no authentication.
"""

from __future__ import annotations

import logging
from typing import Any

import requests

from .models import AudiobookCandidate
from .network_errors import audiobook_network_error

log = logging.getLogger(__name__)

# Audible runs one marketplace host per region; Audnexus region codes map
# onto these directly.
_MARKETPLACE_HOSTS: dict[str, str] = {
    "us": "api.audible.com",
    "uk": "api.audible.co.uk",
    "ca": "api.audible.ca",
    "au": "api.audible.com.au",
    "de": "api.audible.de",
    "fr": "api.audible.fr",
    "it": "api.audible.it",
    "es": "api.audible.es",
    "in": "api.audible.in",
    "jp": "api.audible.co.jp",
}
DEFAULT_REGION = "us"
_RESPONSE_GROUPS = "product_desc,contributors,product_attrs,media"
_TIMEOUT = 15  # seconds


def marketplace_host(region: str) -> str:
    """Return the Audible API host for ``region``, falling back to US."""
    return _MARKETPLACE_HOSTS.get(region.strip().lower(), _MARKETPLACE_HOSTS[DEFAULT_REGION])


def search_audiobooks(
    query: str,
    *,
    limit: int = 10,
    region: str = DEFAULT_REGION,
    raise_on_error: bool = False,
) -> list[AudiobookCandidate]:
    """Search the Audible catalog by title.

    Returns candidates in the service's relevance order.  On network failure
    this returns an empty list, or raises :class:`AudiobookNetworkError` when
    ``raise_on_error`` is set, matching the podcast search client.
    """
    term = query.strip()
    if not term:
        return []

    url = f"https://{marketplace_host(region)}/1.0/catalog/products"
    params = {
        "title": term,
        "num_results": max(1, min(int(limit), 50)),
        "products_sort_by": "Relevance",
        "response_groups": _RESPONSE_GROUPS,
    }

    try:
        resp = requests.get(url, params=params, timeout=_TIMEOUT)
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:  # noqa: BLE001 - re-raised as a described error
        log.warning("Audible search failed for %r: %s", term, exc)
        if raise_on_error:
            raise audiobook_network_error(exc, action="search for audiobooks") from exc
        return []

    products = payload.get("products") if isinstance(payload, dict) else None
    if not isinstance(products, list):
        return []

    candidates: list[AudiobookCandidate] = []
    for product in products:
        if isinstance(product, dict):
            candidate = _candidate_from_product(product)
            if candidate is not None:
                candidates.append(candidate)
    return candidates


def _candidate_from_product(product: dict[str, Any]) -> AudiobookCandidate | None:
    asin = str(product.get("asin") or "").strip()
    if not asin:
        return None
    return AudiobookCandidate(
        asin=asin,
        title=str(product.get("title") or "").strip(),
        authors=_names(product.get("authors")),
        narrators=_names(product.get("narrators")),
        runtime_min=_int(product.get("runtime_length_min")),
        cover_url=_largest_image(product.get("product_images")),
    )


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


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _largest_image(images: Any) -> str:
    """Pick the widest available cover; keys are pixel widths as strings."""
    if not isinstance(images, dict) or not images:
        return ""
    best_key = max(images, key=lambda key: _int(key))
    return str(images.get(best_key) or "").strip()
