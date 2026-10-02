"""Background work must not leave the normal-running status ledger stale."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from gateway.run import GatewayRunner
from gateway.run_shutdown import GatewayShutdownMixin


@pytest.mark.asyncio
@pytest.mark.parametrize("live_count", [0, 2])
@pytest.mark.parametrize("draining", [False, True])
async def test_watcher_refreshes_work_without_a_messaging_turn(monkeypatch, live_count, draining):
    from gateway import drain_control
    import gateway.run as gateway_run
    import gateway.run_shutdown as shutdown

    ledger = {"active_agents": 1, "gateway_state": "draining" if draining else "running"}
    runner = SimpleNamespace(
        _running=True,
        _active_work_count=lambda: live_count,
        _enter_external_drain=Mock(),
        _exit_external_drain=Mock(),
    )
    runner._persist_active_agents = lambda: GatewayRunner._persist_active_agents(runner)
    monkeypatch.setattr(drain_control, "drain_requested", lambda: draining)
    monkeypatch.setattr(gateway_run, "_write_runtime_status_quiet", lambda **fields: ledger.update(fields))

    async def finish_tick(_interval):
        runner._running = False

    monkeypatch.setattr(shutdown.asyncio, "sleep", finish_tick)
    await GatewayShutdownMixin._drain_control_watcher(runner, interval=0)

    assert ledger["active_agents"] == live_count
    assert ledger["gateway_state"] == ("draining" if draining else "running")
    assert runner._enter_external_drain.call_count == int(draining)
    assert runner._exit_external_drain.call_count == int(not draining)
