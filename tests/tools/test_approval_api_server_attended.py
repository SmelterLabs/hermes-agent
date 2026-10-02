"""``approvals.api_server_attended_mode`` — auto-approve for a LIVE lease-bound API run (the Clyde PWA).

Default deny keeps the interactive gate (the phone is asked). ``approve`` trusts that surface: flagged
actions run without a prompt. The mode is consulted only after the lease-lost checks, so a revoked lease
still fails closed exactly as before.
"""

import pytest

import tools.approval as approval_module
import tools.approval_context as approval_context


def _bind_live_lease(monkeypatch, session_key):
    from gateway.session_context import reset_session_vars
    from tools.approval import register_gateway_notify
    from tools.approval_context import (
        set_current_api_server_run_lease,
        set_current_session_key,
    )

    monkeypatch.setattr(approval_module, "_YOLO_MODE_FROZEN", False)
    monkeypatch.setattr(approval_context, "_get_approval_mode", lambda: "manual")
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)
    monkeypatch.delenv("HERMES_EXEC_ASK", raising=False)
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "api_server")
    reset_session_vars()
    lease = approval_module.issue_api_run_approval_lease(session_key, owner=object())
    assert lease is not None
    calls = []
    assert register_gateway_notify(session_key, lambda data: calls.append(data), capability=lease)
    tokens = (set_current_session_key(session_key), set_current_api_server_run_lease(lease))
    return lease, calls, tokens


def _unbind(tokens):
    from gateway.session_context import reset_session_vars
    from tools.approval_context import (
        reset_current_api_server_run_lease,
        reset_current_session_key,
    )

    session_token, lease_token = tokens
    reset_current_api_server_run_lease(lease_token)
    reset_current_session_key(session_token)
    reset_session_vars()


def test_mode_reader_defaults_to_deny(monkeypatch):
    monkeypatch.setattr(approval_context, "_binary_approval_mode", lambda key: "deny")
    assert approval_context._get_api_server_attended_approval_mode() == "deny"


def test_helper_is_inert_without_a_live_lease(monkeypatch):
    monkeypatch.setattr(approval_context, "_get_api_server_attended_approval_mode", lambda: "approve")
    assert approval_module._api_run_attended_auto_approve(pattern_key="k", description="d") is None


def test_helper_is_inert_in_deny_mode_with_a_live_lease(monkeypatch):
    lease, calls, tokens = _bind_live_lease(monkeypatch, "attended-deny")
    monkeypatch.setattr(approval_context, "_get_api_server_attended_approval_mode", lambda: "deny")
    try:
        assert approval_module._api_run_approval_is_live()
        assert approval_module._api_run_attended_auto_approve(pattern_key="k", description="d") is None
    finally:
        approval_module.revoke_api_run_approval_lease(lease)
        _unbind(tokens)
    assert calls == []


@pytest.mark.parametrize("gate", ["terminal", "execute_code"])
def test_live_lease_auto_approves_when_opted_in(monkeypatch, gate):
    """A live phone run with the mode set to approve runs the flagged action without asking."""
    from tools.approval import check_all_command_guards, check_execute_code_guard

    lease, calls, tokens = _bind_live_lease(monkeypatch, f"attended-approve-{gate}")
    monkeypatch.setattr(approval_context, "_get_api_server_attended_approval_mode", lambda: "approve")
    try:
        result = (
            check_all_command_guards("rm -rf /tmp/attended-approve", "local")
            if gate == "terminal"
            else check_execute_code_guard("import os", "local")
        )
    finally:
        approval_module.revoke_api_run_approval_lease(lease)
        _unbind(tokens)
    assert result["approved"] is True
    assert calls == []  # nothing was sent to the phone


@pytest.mark.parametrize("gate", ["terminal", "execute_code"])
def test_revoked_lease_cannot_inherit_attended_approve(monkeypatch, gate):
    """The lease-lost check runs first: a revoked lease fails closed even in approve mode."""
    from tools.approval import check_all_command_guards, check_execute_code_guard

    lease, calls, tokens = _bind_live_lease(monkeypatch, f"attended-revoked-{gate}")
    monkeypatch.setattr(approval_context, "_get_api_server_attended_approval_mode", lambda: "approve")
    monkeypatch.setattr(approval_context, "_get_unattended_approval_mode", lambda: "approve")
    try:
        assert approval_module.revoke_api_run_approval_lease(lease)
        result = (
            check_all_command_guards("rm -rf /tmp/attended-revoked", "local")
            if gate == "terminal"
            else check_execute_code_guard("import os", "local")
        )
    finally:
        _unbind(tokens)
    assert result["approved"] is False
    assert calls == []
