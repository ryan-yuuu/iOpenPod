"""Shared test isolation.

Tests must never read or write the real user settings directory.  Several
settings helpers resolve their own paths at call time, and ``load_app_settings``
rewrites the file it just read when theme normalisation changes a value, so even
a read-only-looking test can clobber a developer's settings.  Worse,
``save_app_settings`` drops a redirect file into the default directory whenever
``settings_dir`` points elsewhere, which silently repoints the real install at a
temporary directory.

Redirecting the path helpers for every test makes isolation the default rather
than something each test has to remember.  Tests that need their own layout can
still monkeypatch these names again; the later patch wins and both are undone.
"""

from __future__ import annotations

import pytest

from iopenpod.infrastructure import (
    settings_paths,
    settings_persistence,
    theme_catalog,
)


@pytest.fixture(autouse=True)
def isolate_settings_dir(tmp_path_factory, monkeypatch):
    """Point every settings path helper at a per-test temporary directory.

    Deliberately a sibling of the test's own ``tmp_path`` rather than a child:
    tests that use ``tmp_path`` as a fake device root assert it stays empty.
    """
    settings_dir = tmp_path_factory.mktemp("iopenpod-settings")
    settings_path = settings_dir / "settings.json"

    # ``settings_persistence`` and ``theme_catalog`` bound these names at import
    # time, so patching the source module alone would not cover them.
    for module, name, value in (
        (settings_paths, "default_settings_dir", str(settings_dir)),
        (settings_paths, "get_settings_dir", str(settings_dir)),
        (settings_paths, "get_settings_path", str(settings_path)),
        (settings_persistence, "default_settings_dir", str(settings_dir)),
        (settings_persistence, "get_settings_path", str(settings_path)),
        (theme_catalog, "get_settings_path", str(settings_path)),
    ):
        monkeypatch.setattr(module, name, lambda _value=value: _value)

    return settings_dir
