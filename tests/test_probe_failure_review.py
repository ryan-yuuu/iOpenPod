"""Probe failures must be surfaced for confirmation, never acted on silently."""

from __future__ import annotations

from types import SimpleNamespace

from iopenpod.application.sync_review_model import (
    has_probe_failure,
    items_with_probe_failure,
    probe_failed_items,
    probe_failure_display_name,
)


def _item(*, probe_failed: bool | None, path: str = "/music/song.m4a"):
    plan = None if probe_failed is None else SimpleNamespace(probe_failed=probe_failed)
    return SimpleNamespace(
        transcode_plan=plan,
        pc_track=SimpleNamespace(path=path),
        description="song.m4a",
    )


def test_item_without_a_transcode_plan_is_not_flagged() -> None:
    assert has_probe_failure(_item(probe_failed=None)) is False


def test_clean_probe_is_not_flagged() -> None:
    assert has_probe_failure(_item(probe_failed=False)) is False


def test_failed_probe_is_flagged() -> None:
    assert has_probe_failure(_item(probe_failed=True)) is True


def test_filter_selects_only_failed_items() -> None:
    items = [
        _item(probe_failed=False, path="/a.m4a"),
        _item(probe_failed=True, path="/b.m4a"),
        _item(probe_failed=None, path="/c.m4a"),
        _item(probe_failed=True, path="/d.m4a"),
    ]
    flagged = items_with_probe_failure(items)
    assert [probe_failure_display_name(i) for i in flagged] == ["/b.m4a", "/d.m4a"]


def test_plan_scan_covers_add_and_update_file_only() -> None:
    plan = SimpleNamespace(
        to_add=[_item(probe_failed=True, path="/add.m4a")],
        to_update_file=[_item(probe_failed=True, path="/upd.m4a")],
        # A metadata-only change rewrites tags, never the payload, so a probe
        # failure there cannot cause a re-encode and must not be reported.
        to_update_metadata=[_item(probe_failed=True, path="/meta.m4a")],
    )
    assert [probe_failure_display_name(i) for i in probe_failed_items(plan)] == [
        "/add.m4a",
        "/upd.m4a",
    ]


def test_empty_plan_reports_nothing() -> None:
    assert probe_failed_items(SimpleNamespace()) == []


def test_display_name_falls_back_to_description() -> None:
    item = SimpleNamespace(
        transcode_plan=SimpleNamespace(probe_failed=True),
        pc_track=None,
        description="mystery track",
    )
    assert probe_failure_display_name(item) == "mystery track"
