"""Shared computer_use test fixtures."""

import pytest


@pytest.fixture(autouse=True)
def _not_windows_session0(monkeypatch):
    """Keep tests hermetic when the suite itself runs inside a Windows service (Session 0): without this the real
    Session 0 detection would probe the live cua-driver daemon from tests that mock the spawn. Tests that exercise
    Session 0 patch ``_windows_session_id`` themselves."""
    from tools.computer_use import cua_backend_driver as drv
    monkeypatch.setattr(drv, "_windows_session_id", lambda: None)
