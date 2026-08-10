"""Opting out of the launch update check, from the popup and from Settings."""

from __future__ import annotations

from types import SimpleNamespace

from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtWidgets import QDialog, QPushButton

from iopenpod.application.controllers import StartupUpdateController
from iopenpod.application.services import SettingsSnapshot
from iopenpod.gui.auto_updater import InstallMethod, UpdateResult
from iopenpod.gui.widgets import updateDialog
from iopenpod.gui.widgets.settingsPage import SettingsPage, ToggleRow
from iopenpod.infrastructure import settings_persistence
from iopenpod.infrastructure.settings_persistence import (
    load_app_settings,
    save_app_settings,
)
from iopenpod.infrastructure.settings_schema import AppSettings

OPT_OUT_TEXT = "Don't Remind Me Again"


def _result() -> UpdateResult:
    return UpdateResult(
        update_available=True,
        current_version="1.0.64",
        latest_version="1.0.65",
    )


def _source_checkout_method() -> InstallMethod:
    return InstallMethod(
        "source_checkout",
        "Source checkout",
        "Pull the latest source and sync the development environment.",
    )


def _opt_out_button(dialog: updateDialog.UpdateAvailableDialog) -> QPushButton | None:
    for button in dialog.findChildren(QPushButton):
        if button.text() == OPT_OUT_TEXT:
            return button
    return None


class _FakeChecker(QThread):
    """Stand-in for UpdateChecker with the same signal surface."""

    result_ready = pyqtSignal(object)

    def run(self) -> None:
        self.result_ready.emit(_result())


# ── The setting itself ──────────────────────────────────────────────────────


def test_launch_check_defaults_to_enabled() -> None:
    assert AppSettings().check_updates_on_launch is True


def test_launch_check_survives_a_save_and_load(tmp_path, monkeypatch) -> None:
    # Redirect the module's own path helpers, and leave ``settings_dir`` empty.
    # A non-empty ``settings_dir`` makes save_app_settings write a redirect file
    # into the real user settings directory, which a test must never touch.
    settings_dir = tmp_path / "settings"
    monkeypatch.setattr(
        settings_persistence, "default_settings_dir", lambda: str(settings_dir)
    )
    monkeypatch.setattr(
        settings_persistence,
        "get_settings_path",
        lambda: str(settings_dir / "settings.json"),
    )

    settings = AppSettings()
    settings.check_updates_on_launch = False
    save_app_settings(settings)

    assert load_app_settings().check_updates_on_launch is False


def test_snapshot_exposes_the_launch_check() -> None:
    settings = AppSettings()
    settings.check_updates_on_launch = False

    assert SettingsSnapshot.from_settings(settings).check_updates_on_launch is False


# ── The startup controller honours the setting ──────────────────────────────


def _controller(enabled) -> tuple[StartupUpdateController, list]:
    """Build a controller whose checker factory just records that it ran."""
    made: list = []

    def factory(owner):
        made.append(1)
        return _FakeChecker(owner)

    return StartupUpdateController(factory, None, is_enabled=enabled), made


def test_startup_check_runs_when_enabled(qtbot) -> None:
    controller, made = _controller(lambda: True)

    controller.start()
    qtbot.wait(250)

    assert made == [1]


def test_startup_check_is_skipped_when_disabled(qtbot) -> None:
    controller, made = _controller(lambda: False)

    controller.start()
    qtbot.wait(250)

    assert made == []


def test_startup_check_reads_the_setting_when_it_fires(qtbot) -> None:
    # Scheduled while enabled, disabled before the timer fires: the check must
    # not run.  This is why the gate lives in start(), not in start_later().
    enabled = True
    controller, made = _controller(lambda: enabled)

    controller.start_later(120)
    enabled = False
    qtbot.wait(400)

    assert made == []


def test_no_predicate_means_always_enabled(qtbot) -> None:
    controller, made = _controller(None)

    controller.start()
    qtbot.wait(250)

    assert made == [1]


# ── The popup button ────────────────────────────────────────────────────────


def _dialog(qtbot, *, allow_opt_out: bool) -> updateDialog.UpdateAvailableDialog:
    dialog = updateDialog.UpdateAvailableDialog(
        _result(),
        method=_source_checkout_method(),
        platform="linux",
        allow_opt_out=allow_opt_out,
    )
    qtbot.addWidget(dialog)
    return dialog


def test_startup_popup_offers_the_opt_out(qtbot) -> None:
    assert _opt_out_button(_dialog(qtbot, allow_opt_out=True)) is not None


def test_manual_check_popup_hides_the_opt_out(qtbot) -> None:
    # A manual check is a deliberate request — offering "stop reminding me"
    # there would be incoherent.
    assert _opt_out_button(_dialog(qtbot, allow_opt_out=False)) is None


def test_opt_out_defaults_to_dismiss_until_clicked(qtbot) -> None:
    assert _dialog(qtbot, allow_opt_out=True).selected_action == "dismiss"


def test_clicking_opt_out_records_the_choice_and_accepts(qtbot) -> None:
    dialog = _dialog(qtbot, allow_opt_out=True)
    button = _opt_out_button(dialog)
    assert button is not None

    button.click()

    assert dialog.selected_action == "never"
    assert dialog.result() == QDialog.DialogCode.Accepted


# ── Persisting the choice ───────────────────────────────────────────────────


class _FakeSettingsService:
    def __init__(self) -> None:
        self.settings = AppSettings()
        self.saved: list[bool] = []

    def get_global_settings(self) -> AppSettings:
        return self.settings

    def save_global_settings(self, settings: AppSettings):
        self.saved.append(settings.check_updates_on_launch)
        return None


def _page_stub(service: _FakeSettingsService, qtbot) -> SimpleNamespace:
    """Stand-in for the page, carrying a *real* ToggleRow.

    The real row emits ``changed`` from its ``value`` setter, and the page wires
    that to ``_save``.  A plain attribute stub cannot emit, so it would hide the
    re-entrant save this method exists to suppress.
    """
    row = ToggleRow("Check for Updates on Launch", checked=True)
    qtbot.addWidget(row)

    page = SimpleNamespace(
        _loading_settings=False,
        check_updates_on_launch=row,
        _settings_service=service,
        save_guard_seen=[],
    )
    # Stands in for the page's own ``changed -> _save`` connection, recording
    # whether a save triggered this way would have been suppressed.
    row.changed.connect(
        lambda _checked: page.save_guard_seen.append(page._loading_settings)
    )
    return page


def test_opting_out_writes_global_settings(qtbot) -> None:
    service = _FakeSettingsService()
    page = _page_stub(service, qtbot)

    SettingsPage.set_check_updates_on_launch(page, False)

    assert service.settings.check_updates_on_launch is False
    assert service.saved == [False]


def test_opting_out_updates_the_toggle_row(qtbot) -> None:
    service = _FakeSettingsService()
    page = _page_stub(service, qtbot)

    SettingsPage.set_check_updates_on_launch(page, False)

    assert page.check_updates_on_launch.value is False


def test_opting_out_suppresses_the_toggles_own_save(qtbot) -> None:
    # Setting the row emits changed -> _save.  Without the suppression that save
    # would run before Settings was ever opened, persisting constructor-default
    # control values over the user's real global settings.
    service = _FakeSettingsService()
    page = _page_stub(service, qtbot)

    SettingsPage.set_check_updates_on_launch(page, False)

    assert page.save_guard_seen == [True], "re-entrant save was not suppressed"


def test_opting_out_does_not_leave_the_page_in_loading_state(qtbot) -> None:
    # The flag must be restored afterwards or every later edit would silently
    # stop persisting.
    service = _FakeSettingsService()
    page = _page_stub(service, qtbot)

    SettingsPage.set_check_updates_on_launch(page, False)

    assert page._loading_settings is False


def test_re_enabling_writes_global_settings_too(qtbot) -> None:
    service = _FakeSettingsService()
    service.settings.check_updates_on_launch = False
    page = _page_stub(service, qtbot)

    SettingsPage.set_check_updates_on_launch(page, True)

    assert service.settings.check_updates_on_launch is True
    assert service.saved == [True]


# ── Routing the popup's choice ──────────────────────────────────────────────


def _accepted_modal(_self) -> int:
    """Stand-in for QDialog's modal call: always returns Accepted."""
    return QDialog.DialogCode.Accepted


def _fake_dialog_class(seen: list, action: str):
    """Build a stand-in dialog that records how it was constructed."""

    class _FakeDialog:
        def __init__(self, result, parent=None, *, allow_opt_out=False, **kwargs):
            seen.append(allow_opt_out)
            self.selected_action = action

    # Assigned rather than declared so the name matches QDialog's API.
    _FakeDialog.exec = _accepted_modal
    return _FakeDialog


def _routing_page(persisted: list) -> SimpleNamespace:
    return SimpleNamespace(set_check_updates_on_launch=persisted.append)


def test_startup_popup_opt_out_is_persisted(monkeypatch, qtbot) -> None:
    seen: list = []
    persisted: list = []
    monkeypatch.setattr(
        updateDialog, "UpdateAvailableDialog", _fake_dialog_class(seen, "never")
    )

    SettingsPage._handle_update_result(
        _routing_page(persisted), _result(), from_startup=True
    )

    assert seen == [True], "startup popup should offer the opt-out"
    assert persisted == [False], "opting out must disable the launch check"


def test_manual_check_never_offers_the_opt_out(monkeypatch, qtbot) -> None:
    seen: list = []
    persisted: list = []
    monkeypatch.setattr(
        updateDialog, "UpdateAvailableDialog", _fake_dialog_class(seen, "dismiss")
    )

    SettingsPage._handle_update_result(_routing_page(persisted), _result())

    assert seen == [False]
    assert persisted == []


def test_dismissing_the_startup_popup_leaves_the_setting_alone(
    monkeypatch, qtbot
) -> None:
    seen: list = []
    persisted: list = []
    monkeypatch.setattr(
        updateDialog, "UpdateAvailableDialog", _fake_dialog_class(seen, "dismiss")
    )

    SettingsPage._handle_update_result(
        _routing_page(persisted), _result(), from_startup=True
    )

    assert persisted == []
