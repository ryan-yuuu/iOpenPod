"""Audiobook metadata provider: candidate search, record fetch, error copy."""

from __future__ import annotations

import pytest
import requests

from iopenpod.audiobooks.audible_search import search_audiobooks
from iopenpod.audiobooks.audnexus import fetch_audiobook, strip_summary_html
from iopenpod.audiobooks.models import AudiobookCandidate, AudiobookMetadata
from iopenpod.audiobooks.network_errors import (
    AudiobookNetworkError,
    describe_audiobook_error,
)

# Trimmed from a live api.audible.com response for "Kafka on the Shore".
AUDIBLE_PAYLOAD = {
    "products": [
        {
            "asin": "B00E83NFUC",
            "title": "Kafka on the Shore",
            "runtime_length_min": 1148,
            "authors": [
                {"name": "Haruki Murakami"},
                {"name": "Philip Gabriel - translator"},
            ],
            "narrators": [{"name": "Sean Barrett"}, {"name": "Oliver Le Sueur"}],
            "product_images": {"500": "https://example.invalid/cover500.jpg"},
        },
        {
            "asin": "B0C7PBPRXJ",
            "title": "1833. 87 Academic Words Reference",
            "runtime_length_min": 79,
            "authors": [],
            "narrators": [],
        },
    ]
}

# Trimmed from a live api.audnex.us response for the same ASIN.
AUDNEXUS_PAYLOAD = {
    "asin": "B00E83NFUC",
    "title": "Kafka on the Shore",
    "authors": [{"name": "Haruki Murakami"}],
    "narrators": [{"name": "Sean Barrett"}, {"name": "Oliver Le Sueur"}],
    "publisherName": "Random House Audio",
    "releaseDate": "2013-08-06T00:00:00.000Z",
    "isbn": "9780804166553",
    "runtimeLengthMin": 1148,
    "language": "english",
    "formatType": "unabridged",
    "image": "https://example.invalid/cover.jpg",
    "genres": [
        {"name": "Literature & Fiction", "type": "genre"},
        {"name": "Magical Realism", "type": "tag"},
    ],
    "summary": "<b>NATIONAL BESTSELLER</b> • A tale of<i>two</i>journeys.",
}


class _FakeResponse:
    def __init__(self, payload: object, status: int = 200) -> None:
        self._payload = payload
        self.status_code = status

    def json(self) -> object:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)  # type: ignore[arg-type]


# ── Candidate search ────────────────────────────────────────────────────────


def test_search_returns_candidates_in_response_order(monkeypatch) -> None:
    monkeypatch.setattr(
        "iopenpod.audiobooks.audible_search.requests.get",
        lambda *a, **k: _FakeResponse(AUDIBLE_PAYLOAD),
    )

    results = search_audiobooks("Kafka on the Shore")

    assert [r.asin for r in results] == ["B00E83NFUC", "B0C7PBPRXJ"]
    first = results[0]
    assert isinstance(first, AudiobookCandidate)
    assert first.title == "Kafka on the Shore"
    assert first.authors == ("Haruki Murakami", "Philip Gabriel - translator")
    assert first.narrators == ("Sean Barrett", "Oliver Le Sueur")
    assert first.runtime_min == 1148
    assert first.cover_url == "https://example.invalid/cover500.jpg"


def test_search_tolerates_missing_optional_fields(monkeypatch) -> None:
    monkeypatch.setattr(
        "iopenpod.audiobooks.audible_search.requests.get",
        lambda *a, **k: _FakeResponse(AUDIBLE_PAYLOAD),
    )

    sparse = search_audiobooks("anything")[1]

    assert sparse.authors == ()
    assert sparse.narrators == ()
    assert sparse.cover_url == ""


def test_search_rejects_blank_query() -> None:
    assert search_audiobooks("   ") == []


def test_search_uses_regional_marketplace_host(monkeypatch) -> None:
    seen: dict[str, str] = {}

    def _capture(url: str, **kwargs: object) -> _FakeResponse:
        seen["url"] = url
        return _FakeResponse({"products": []})

    monkeypatch.setattr("iopenpod.audiobooks.audible_search.requests.get", _capture)

    search_audiobooks("x", region="uk")

    assert "audible.co.uk" in seen["url"]


def test_search_wraps_network_failure_when_asked(monkeypatch) -> None:
    def _fail(*a: object, **k: object) -> None:
        raise requests.ConnectionError("offline")

    monkeypatch.setattr("iopenpod.audiobooks.audible_search.requests.get", _fail)

    assert search_audiobooks("x") == []
    with pytest.raises(AudiobookNetworkError):
        search_audiobooks("x", raise_on_error=True)


# ── Full record fetch ───────────────────────────────────────────────────────


def test_fetch_audiobook_maps_every_field(monkeypatch) -> None:
    monkeypatch.setattr(
        "iopenpod.audiobooks.audnexus.requests.get",
        lambda *a, **k: _FakeResponse(AUDNEXUS_PAYLOAD),
    )

    meta = fetch_audiobook("B00E83NFUC")

    assert isinstance(meta, AudiobookMetadata)
    assert meta.asin == "B00E83NFUC"
    assert meta.title == "Kafka on the Shore"
    assert meta.authors == ("Haruki Murakami",)
    assert meta.narrators == ("Sean Barrett", "Oliver Le Sueur")
    assert meta.publisher == "Random House Audio"
    assert meta.release_year == 2013
    assert meta.isbn == "9780804166553"
    assert meta.runtime_min == 1148
    assert meta.cover_url == "https://example.invalid/cover.jpg"
    assert meta.genres == ("Literature & Fiction", "Magical Realism")


def test_fetch_audiobook_returns_none_when_region_lacks_item(monkeypatch) -> None:
    payload = {"error": {"code": "REGION_UNAVAILABLE", "message": "nope"}}
    monkeypatch.setattr(
        "iopenpod.audiobooks.audnexus.requests.get",
        lambda *a, **k: _FakeResponse(payload, status=404),
    )

    assert fetch_audiobook("B00E83NFUC") is None


def test_fetch_audiobook_rejects_blank_asin() -> None:
    assert fetch_audiobook("") is None


# ── Summary cleaning ────────────────────────────────────────────────────────


def test_strip_summary_html_replaces_tags_with_spaces() -> None:
    # Stripping to empty string welds words together; the fix is a space.
    assert strip_summary_html("<b>A tale of</b><i>two</i>journeys.") == "A tale of two journeys."


def test_strip_summary_html_unescapes_entities_and_collapses_space() -> None:
    assert strip_summary_html("Cats &amp;   dogs\n\nagree") == "Cats & dogs agree"


def test_strip_summary_html_tidies_space_before_punctuation() -> None:
    assert strip_summary_html("<p>The New Yorker</p> , and more") == "The New Yorker, and more"


def test_fetch_audiobook_cleans_summary(monkeypatch) -> None:
    monkeypatch.setattr(
        "iopenpod.audiobooks.audnexus.requests.get",
        lambda *a, **k: _FakeResponse(AUDNEXUS_PAYLOAD),
    )

    meta = fetch_audiobook("B00E83NFUC")

    assert meta is not None
    assert "<b>" not in meta.summary
    assert "of two journeys" in meta.summary


# ── User-facing error copy ──────────────────────────────────────────────────


def test_describe_audiobook_error_covers_offline_and_timeout() -> None:
    offline = describe_audiobook_error(requests.ConnectionError("x"))
    timed_out = describe_audiobook_error(requests.Timeout("x"))

    assert offline.title and offline.message
    assert timed_out.title and timed_out.message
    assert offline.title != timed_out.title


def test_describe_audiobook_error_surfaces_rate_limit_code() -> None:
    response = requests.Response()
    response.status_code = 429
    info = describe_audiobook_error(requests.HTTPError("slow down", response=response))

    assert info.code == "HTTP 429"
