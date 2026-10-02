"""A provider client that returns a coroutine whenever it sees a running event loop.

The Claude direct-SDK plugin's ``chat.completions.create`` checks ``asyncio.get_running_loop()``
and returns ``self._acreate(...)`` when one exists, because Hermes' auxiliary seam awaits it.
Relay runs its managed provider callbacks synchronously on a private event loop, so the client
answered with an un-awaited coroutine and the turn died with ``'coroutine' object is not
iterable`` (stream) or recorded a coroutine as the response (non-stream).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

pytest.importorskip("nemo_relay")

from agent import relay_llm, relay_runtime


@pytest.fixture()
def relay_turn(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    relay_runtime._reset_for_tests()
    lease = relay_runtime.SESSION_COORDINATOR.acquire_conversation(
        profile_key=relay_runtime.current_profile_key(),
        session_id="session-1",
        platform="cli",
    )
    turn = relay_runtime.SESSION_COORDINATOR.begin_turn(lease, turn_id="turn-1", task_id="task-1")
    lease.host.retain_managed_execution("test.relay_llm_loop_sensitive")
    try:
        yield lease.host.relay, turn
    finally:
        lease.host.release_managed_execution("test.relay_llm_loop_sensitive")
        relay_runtime.SESSION_COORDINATOR.end_turn(turn, outcome="success")
        relay_runtime.SESSION_COORDINATOR.release_conversation(lease)
        relay_runtime._reset_for_tests()


def _chunk(content, finish_reason=None):
    return SimpleNamespace(
        model="test-model",
        choices=[SimpleNamespace(delta=SimpleNamespace(content=content, tool_calls=None), finish_reason=finish_reason)],
        usage=None,
    )


class LoopSensitiveClient:
    """Mirrors the plugin: sync result without a running loop, a coroutine with one."""

    def __init__(self, result):
        self.result = result
        self.sync_calls = 0
        self.async_calls_started = 0

    def create(self, request):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            self.sync_calls += 1
            return self.result(request)
        return self._acreate(request)

    async def _acreate(self, request):
        self.async_calls_started += 1
        return self.result(request)


def test_managed_stream_reinvokes_a_loop_sensitive_factory_off_the_loop(relay_turn):
    client = LoopSensitiveClient(lambda _request: iter([_chunk("PONG"), _chunk(None, "stop")]))

    stream = relay_llm.stream(
        {"model": "test-model", "messages": []},
        client.create,
        session_id="session-1",
        name="test-provider",
        model_name="test-model",
        finalizer=lambda: {"model": "test-model", "choices": [{"message": {"role": "assistant", "content": "PONG"}, "finish_reason": "stop"}]},
        metadata={"api_mode": "custom", "api_request_id": "request-loop-stream", "call_role": "primary"},
    )
    chunks = list(stream)

    contents = [getattr(c.choices[0].delta, "content", None) if hasattr(c, "choices") else c["choices"][0]["delta"].get("content") for c in chunks]
    assert "PONG" in contents
    assert client.sync_calls == 1
    assert client.async_calls_started == 0  # the discarded coroutine never ran


def test_managed_non_stream_reinvokes_a_loop_sensitive_callback_off_the_loop(relay_turn):
    client = LoopSensitiveClient(lambda _request: {"content": "PONG"})

    result = relay_llm.execute(
        {"model": "test-model", "messages": []},
        client.create,
        session_id="session-1",
        name="test-provider",
        model_name="test-model",
        metadata={"api_mode": "custom", "api_request_id": "request-loop-execute"},
    )

    assert result == {"content": "PONG"}
    assert client.sync_calls == 1
    assert client.async_calls_started == 0
