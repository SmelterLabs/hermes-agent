"""Windows Session 0 (Hermes inside a service): cua-driver mcp/call must proxy to the interactive daemon's pipe."""

from unittest import mock

import pytest

from tools.computer_use import cua_backend_driver as drv


def test_interactive_session_adds_nothing():
    with mock.patch.object(drv, "_windows_session_id", return_value=1):
        assert drv.windows_session0_socket_args("cua-driver") == []


def test_non_windows_adds_nothing():
    with mock.patch.object(drv, "_windows_session_id", return_value=None):
        assert drv.windows_session0_socket_args("cua-driver") == []


def test_session0_with_listening_daemon_targets_shared_pipe():
    from tools.computer_use import cua_backend
    with mock.patch.object(drv, "_windows_session_id", return_value=0), \
         mock.patch.object(cua_backend, "cua_daemon_listening", return_value=True) as probe:
        assert drv.windows_session0_socket_args("cua-driver") == ["--socket", r"\\.\pipe\cua-driver"]
    probe.assert_called_once_with("cua-driver", r"\\.\pipe\cua-driver")


def test_session0_unknown_probe_still_tries_pipe():
    from tools.computer_use import cua_backend
    with mock.patch.object(drv, "_windows_session_id", return_value=0), \
         mock.patch.object(cua_backend, "cua_daemon_listening", return_value=None):
        assert drv.windows_session0_socket_args("cua-driver") == ["--socket", r"\\.\pipe\cua-driver"]


def test_session0_without_daemon_fails_with_actionable_error():
    from tools.computer_use import cua_backend
    with mock.patch.object(drv, "_windows_session_id", return_value=0), \
         mock.patch.object(cua_backend, "cua_daemon_listening", return_value=False):
        with pytest.raises(RuntimeError, match="Session 0.*autostart kick"):
            drv.windows_session0_socket_args("cua-driver")


def test_cli_fallback_appends_session0_socket():
    from tools.computer_use import cua_backend_session as sess
    s = sess._CuaDriverSession.__new__(sess._CuaDriverSession)
    s._embedded_daemon = None
    seen = {}

    def fake_run(cmd, env, name, timeout):
        seen["cmd"] = cmd
        return {"ok": True}

    from tools.computer_use import cua_backend
    with mock.patch.object(drv, "resolve_cua_driver_cmd", return_value="cua-driver"), \
         mock.patch.object(cua_backend, "cua_driver_child_env", return_value={}), \
         mock.patch.object(drv, "windows_session0_socket_args", return_value=["--socket", r"\\.\pipe\cua-driver"]), \
         mock.patch.object(sess, "_cli_run_json", side_effect=fake_run):
        s._call_tool_via_cli("list_windows", {}, 5.0)
    assert seen["cmd"][-2:] == ["--socket", r"\\.\pipe\cua-driver"]
