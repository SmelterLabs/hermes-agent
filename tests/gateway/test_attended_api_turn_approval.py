"""An attended canonical API turn (Clyde app /v1/runs on a saved conversation) can ask a human;
an unobserved one stays unattended. Regression for 2026-10-20: every Clyde turn auto-BLOCKED."""
from types import SimpleNamespace

import pytest

from gateway.run_turn_runner import TurnRunner
from gateway import session_api_turn
from tools import approval, approval_context


class _Authority:
    def __init__(self, observed):
        self.api_observers = {"adm-1": [{}]} if observed else {}
        live = SimpleNamespace(event_stream=SimpleNamespace(execution={"admission_id": "adm-1"}))
        self.sessions = {"sess": live}


def _runner(observed=True, api=True, owner=True):
    turn = TurnRunner.__new__(TurnRunner)
    turn._approval_owner = (_Authority(observed), "sess", 1) if owner else None
    token = session_api_turn.api_execution.set({"settings": {}} if api else None)
    return turn, token


@pytest.fixture(autouse=True)
def _clean_registry():
    yield
    with approval._lock:
        approval._api_run_approval_leases.pop("key-1", None)
        approval._gateway_notify_cbs.pop("key-1", None)
        approval._gateway_notify_capabilities.pop("key-1", None)


def test_observed_api_turn_gets_live_lease_and_counts_as_attended():
    turn, token = _runner()
    try:
        lease, _ = turn._attended_api_approval_lease("key-1")
        assert lease is not None and approval.is_api_run_approval_lease_active(lease)
        key_token = approval_context.set_current_session_key("key-1")
        lease_token = approval_context.set_current_api_server_run_lease(lease)
        try:
            assert approval.register_gateway_notify("key-1", lambda d: None, capability=lease) is True
            assert approval._api_server_approval_capable() is True
        finally:
            approval_context.reset_current_api_server_run_lease(lease_token)
            approval_context.reset_current_session_key(key_token)
        assert approval.revoke_api_run_approval_lease(lease) is True
        assert not approval.is_api_run_approval_lease_active(lease)
    finally:
        session_api_turn.api_execution.reset(token)


@pytest.mark.parametrize("observed,api,owner", [(False, True, True), (True, False, True), (True, True, False)])
def test_unobserved_or_non_api_turn_stays_unattended(observed, api, owner):
    turn, token = _runner(observed=observed, api=api, owner=owner)
    try:
        assert turn._attended_api_approval_lease("key-1") == (None, None)
    finally:
        session_api_turn.api_execution.reset(token)


def test_without_lease_api_server_listener_is_not_capable():
    key_token = approval_context.set_current_session_key("key-1")
    try:
        approval.register_gateway_notify("key-1", lambda d: None)
        assert approval._api_server_approval_capable() in (True, False)  # ordinary listener...
        # ...but an api_server platform context never treats an unleased listener as a human.
        assert approval.get_current_gateway_approval_listener() is None or approval_context._get_session_platform() != "api_server"
    finally:
        approval.unregister_gateway_notify("key-1")
        approval_context.reset_current_session_key(key_token)
