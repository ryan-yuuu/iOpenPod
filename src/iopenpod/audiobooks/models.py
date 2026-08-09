"""Audiobook metadata data contracts."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AudiobookCandidate:
    """One search hit, enough to let a user tell editions apart."""

    asin: str
    title: str
    authors: tuple[str, ...] = ()
    narrators: tuple[str, ...] = ()
    runtime_min: int = 0
    cover_url: str = ""

    @property
    def author_text(self) -> str:
        return ", ".join(self.authors)

    @property
    def narrator_text(self) -> str:
        return ", ".join(self.narrators)

    @property
    def runtime_text(self) -> str:
        """Human runtime, e.g. ``19h 08m``. Empty when unknown."""
        if self.runtime_min <= 0:
            return ""
        return f"{self.runtime_min // 60}h {self.runtime_min % 60:02d}m"


@dataclass(frozen=True)
class AudiobookMetadata:
    """A resolved audiobook record ready to be written into a file."""

    asin: str
    title: str
    subtitle: str = ""
    authors: tuple[str, ...] = ()
    narrators: tuple[str, ...] = ()
    publisher: str = ""
    release_year: int | None = None
    isbn: str = ""
    summary: str = ""
    genres: tuple[str, ...] = ()
    cover_url: str = ""
    runtime_min: int = 0

    @property
    def author_text(self) -> str:
        return ", ".join(self.authors)

    @property
    def narrator_text(self) -> str:
        return ", ".join(self.narrators)
