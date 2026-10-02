"""Tests for pinned warm session runtimes (gateway/pinned_sessions.py) and
their API-server integration: hydrate-at-start, warm reuse on session chat,
turn serialization, crash invalidation, compression-rotation follow, model-lock
staleness rebuild, bounded auth probing, and the /api/pinned-sessions
readiness surface."""

import asyncio
import threading
import time
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.pinned_sessions import (
    PinnedSessionRegistry,
    parse_pinned_session_titles,
)
from gateway.platforms.api_server import APIServerAdapter
from hermes_state import SessionDB


class FakeAgent:
    session_prompt_tokens = 0
    session_completion_tokens = 0
    session_total_tokens = 0

    def __init__(self, session_id="pinned-session-id"):
        self.session_id = session_id
        self.tools = [{"name": "terminal"}]
        self.provider = "openai-codex"
        self.model = "gpt-5.6-sol-900k"
        self._hermes_api_runtime = {
            "provider": "openai-codex",
            "model": "gpt-5.6-sol-900k",
            "route_source": "session_model_lock",
        }
        self.stream_delta_callback = None
        self.tool_progress_callback = None
        self.tool_start_callback = None
        self.tool_complete_callback = None
        self._api_call_count = 99
        self._last_flushed_db_idx = 7

    def run_conversation(self, user_message, conversation_history, task_id):
        return {"final_response": "ok", "session_id": self.session_id}


def _ok_prober(provider, model):
    return {
        "ok": True,
        "provider": provider,
        "model": model,
        "latency_ms": 5,
        "checked_at": time.time(),
    }


def _make_registry(
    row=None,
    *,
    builder=None,
    prober=_ok_prober,
    memory_required=lambda: False,
    titles=("Warm Session",),
):
    calls = {"lookup": 0, "build": 0}

    def lookup(title):
        calls["lookup"] += 1
        return row

    def default_builder(session_row):
        calls["build"] += 1
        return FakeAgent(session_row["id"])

    registry = PinnedSessionRegistry(
        list(titles),
        session_lookup=lookup,
        agent_builder=builder or default_builder,
        auth_prober=prober,
        memory_required=memory_required,
    )
    return registry, calls


def _row(session_id="pinned-session-id", cwd=None, confirmed_lock=True):
    import json

    row = {"id": session_id, "title": "Warm Session", "cwd": cwd}
    if confirmed_lock:
        row["model_config"] = json.dumps(
            {
                "browser_model_lock": {
                    "provider": "openai-codex",
                    "model": "gpt-5.6-sol-900k",
                    "confirmed": True,
                }
            }
        )
    return row


# ---------------------------------------------------------------------------
# Config parsing
# ---------------------------------------------------------------------------


def test_parse_pinned_session_titles_accepts_strings_and_dicts():
    parsed = parse_pinned_session_titles(
        ["Clyde Remote", {"title": "Second"}, "", 42, {"title": ""}, "Clyde Remote"]
    )
    assert parsed == ["Clyde Remote", "Second"]
    assert parse_pinned_session_titles("Solo") == ["Solo"]
    assert parse_pinned_session_titles(None) == []
    assert parse_pinned_session_titles(123) == []


# ---------------------------------------------------------------------------
# Registry: hydration + facts
# ---------------------------------------------------------------------------


def test_hydrate_all_builds_agent_and_reports_facts(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    registry, calls = _make_registry(_row(cwd=str(ws)))

    registry.hydrate_all()

    payload = registry.readiness()
    assert payload["status"] == "ok"
    (pin,) = payload["pins"]
    assert pin["session_id"] == "pinned-session-id"
    assert pin["facts"] == {
        "session_persisted": True,
        "cwd_bound": True,
        "context_hydrated": True,
        "tools_ready": True,
        "memory_ready": True,
        "model_lock_confirmed": True,
        "provider_auth_valid": True,
    }
    assert calls["build"] == 1
    assert pin["auth_probe"]["ok"] is True


def test_hydrate_missing_session_reports_unpersisted():
    registry, _ = _make_registry(None)
    registry.hydrate_all(probe_auth=False)

    payload = registry.readiness()
    assert payload["status"] == "degraded"
    (pin,) = payload["pins"]
    assert pin["facts"]["session_persisted"] is False
    assert pin["facts"]["context_hydrated"] is False
    assert pin["hydrate_error"] == "session_not_found"


def test_facts_are_separate_not_one_flag(tmp_path):
    """A failed auth probe must not mask the other facts (and vice versa)."""

    def bad_prober(provider, model):
        return {"ok": False, "error_class": "AuthError", "checked_at": time.time()}

    ws = tmp_path / "ws"
    ws.mkdir()
    registry, _ = _make_registry(_row(cwd=str(ws)), prober=bad_prober)
    registry.hydrate_all()

    payload = registry.readiness()
    (pin,) = payload["pins"]
    assert payload["status"] == "degraded"
    assert pin["facts"]["provider_auth_valid"] is False
    assert pin["facts"]["context_hydrated"] is True
    assert pin["facts"]["session_persisted"] is True
    assert pin["auth_probe"]["error_class"] == "AuthError"


def test_memory_fact_requires_manager_when_provider_configured(tmp_path):
    class AgentWithMemory(FakeAgent):
        def __init__(self, session_id):
            super().__init__(session_id)
            self._memory_manager = object()

    ws = tmp_path / "ws"
    ws.mkdir()
    row = _row(cwd=str(ws))

    registry_without, _ = _make_registry(row, memory_required=lambda: True)
    registry_without.hydrate_all(probe_auth=False)
    assert registry_without.readiness()["pins"][0]["facts"]["memory_ready"] is False

    registry_with, _ = _make_registry(
        row,
        builder=lambda r: AgentWithMemory(r["id"]),
        memory_required=lambda: True,
    )
    registry_with.hydrate_all(probe_auth=False)
    assert registry_with.readiness()["pins"][0]["facts"]["memory_ready"] is True


# ---------------------------------------------------------------------------
# Registry: warm reuse, staleness, rotation, invalidation, probe limits
# ---------------------------------------------------------------------------


def test_ensure_agent_reuses_and_rebinds_callbacks():
    registry, calls = _make_registry(_row())
    registry.hydrate_all(probe_auth=False)
    runtime = registry.runtime_for_session("pinned-session-id")
    assert runtime is not None

    delta = object()
    with runtime.turn_lock:
        agent = registry.ensure_agent(runtime, stream_delta_callback=delta)
    assert agent is runtime.agent
    assert agent.stream_delta_callback is delta
    assert agent._api_call_count == 0
    assert agent._last_flushed_db_idx == 0
    # No rebuild happened for a healthy, lock-stable runtime.
    assert calls["build"] == 1


def test_ensure_agent_rebuilds_on_model_lock_change():
    registry, calls = _make_registry(_row())
    registry.hydrate_all(probe_auth=False)
    runtime = registry.runtime_for_session("pinned-session-id")

    with runtime.turn_lock:
        registry.ensure_agent(
            runtime,
            expected_provider="openai-codex",
            expected_model="gpt-5.6-sol-900k",
        )
    assert calls["build"] == 1  # matching lock: no rebuild

    with runtime.turn_lock:
        registry.ensure_agent(
            runtime,
            expected_provider="nous",
            expected_model="x-ai/grok-4.5",
        )
    assert calls["build"] == 2  # changed lock: rebuilt


def test_failed_rehydrate_clears_the_previous_agent():
    state = {"fail": False}

    def builder(row):
        if state["fail"]:
            raise RuntimeError("rehydration failed")
        return FakeAgent(row["id"])

    registry, _ = _make_registry(_row(), builder=builder)
    registry.hydrate_all(probe_auth=False)
    runtime = registry.runtime_for_session("pinned-session-id")
    assert runtime.agent is not None

    state["fail"] = True
    registry.hydrate_all(probe_auth=False)

    assert runtime.agent is None
    assert runtime.hydrate_error == "RuntimeError"
    assert runtime.facts()["facts"]["context_hydrated"] is False


def test_rehydrate_invalidates_auth_for_a_changed_runtime_identity():
    identity = {"provider": "provider-one", "model": "model-one"}

    def builder(row):
        agent = FakeAgent(row["id"])
        agent.provider = identity["provider"]
        agent.model = identity["model"]
        agent._hermes_api_runtime = dict(identity)
        return agent

    registry, _ = _make_registry(_row(), builder=builder)
    registry.hydrate_all(probe_auth=False)
    runtime = registry.runtime_for_session("pinned-session-id")
    runtime.auth_probe = {"ok": True, "provider": "provider-one", "checked_at": time.time()}

    identity.update(provider="provider-two", model="model-two")
    registry.hydrate_all(probe_auth=False)

    assert runtime.provider == "provider-two"
    assert runtime.model == "model-two"
    assert runtime.auth_probe == {}


def test_note_turn_result_follows_rotation_and_invalidates_on_crash():
    registry, _ = _make_registry(_row())
    registry.hydrate_all(probe_auth=False)
    runtime = registry.runtime_for_session("pinned-session-id")

    rotated = FakeAgent("rotated-session-id")
    registry.note_turn_result(runtime, rotated, ok=True)
    assert runtime.session_id == "rotated-session-id"
    assert registry.runtime_for_session("rotated-session-id") is runtime
    assert runtime.agent is not None

    registry.note_turn_result(runtime, runtime.agent, ok=False)
    assert runtime.agent is None  # invalidated; next turn rebuilds once


def test_auth_probe_rate_limited():
    probes = {"count": 0}

    def counting_prober(provider, model):
        probes["count"] += 1
        return {"ok": True, "checked_at": time.time()}

    registry, _ = _make_registry(_row(), prober=counting_prober)
    registry.hydrate_all(probe_auth=False)
    runtime = registry.runtime_for_session("pinned-session-id")

    registry.probe_auth(runtime, force=True)
    registry.probe_auth(runtime, force=True)  # inside min interval: cached
    registry.probe_auth(runtime)  # inside TTL: cached
    assert probes["count"] == 1


# ---------------------------------------------------------------------------
# Adapter integration
# ---------------------------------------------------------------------------


@pytest.fixture
def session_db(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    try:
        yield db
    finally:
        db.close()


@pytest.fixture
def adapter(session_db):
    adapter = APIServerAdapter(
        PlatformConfig(enabled=True, extra={"pinned_sessions": ["Warm Session"]})
    )
    adapter._session_db = session_db
    return adapter


def _install_registry(adapter, row):
    """Give the adapter a hydrated registry backed by fakes."""
    registry, calls = _make_registry(row)
    registry.hydrate_all(probe_auth=False)
    adapter._pinned_sessions = registry
    runtime = registry.runtime_for_session(row["id"])
    signature = adapter._pinned_agent_signature(
        ephemeral_system_prompt=None,
        session_id=row["id"],
        gateway_session_key=row["id"],
        requested_model=None,
        requested_provider=None,
        model_options=None,
        route=None,
        session_model=None,
        session_cwd=row.get("cwd") or "",
        confirmed_runtime_lock=False,
    )
    runtime.construction_signature = signature
    runtime.agent._hermes_pinned_signature = signature
    return registry, calls


@pytest.mark.asyncio
async def test_run_agent_reuses_warm_runtime_without_create_agent(adapter):
    registry, calls = _install_registry(adapter, _row())
    runtime = registry.runtime_for_session("pinned-session-id")
    warm_agent = runtime.agent

    with patch.object(adapter, "_create_agent") as mock_create:
        result, usage = await adapter._run_agent(
            user_message="hello",
            conversation_history=[],
            session_id="pinned-session-id",
            warm_runtime=runtime,
        )

    mock_create.assert_not_called()
    assert result["session_id"] == "pinned-session-id"
    assert runtime.agent is warm_agent  # still cached after the turn
    assert runtime.last_turn_ok is True
    assert not runtime.turn_lock.locked()


@pytest.mark.asyncio
async def test_run_agent_rebuilds_warm_runtime_for_request_specific_inputs(adapter):
    registry, _ = _install_registry(adapter, _row())
    runtime = registry.runtime_for_session("pinned-session-id")
    replacement = FakeAgent("pinned-session-id")
    request = {
        "ephemeral_system_prompt": "Do not disclose private data.",
        "gateway_session_key": "external:caller:42",
        "requested_model": "request-model",
        "requested_provider": "request-provider",
        "model_options": {"temperature": 0.2},
    }

    with patch.object(adapter, "_create_agent", return_value=replacement) as mock_create:
        await adapter._run_agent(
            user_message="hello",
            conversation_history=[],
            session_id="pinned-session-id",
            warm_runtime=runtime,
            **request,
        )
        await adapter._run_agent(
            user_message="again",
            conversation_history=[],
            session_id="pinned-session-id",
            warm_runtime=runtime,
            **request,
        )

    mock_create.assert_called_once()
    kwargs = mock_create.call_args.kwargs
    assert kwargs["ephemeral_system_prompt"] == "Do not disclose private data."
    assert kwargs["gateway_session_key"] == "external:caller:42"
    assert kwargs["requested_model"] == "request-model"
    assert kwargs["requested_provider"] == "request-provider"
    assert kwargs["model_options"] == {"temperature": 0.2}
    assert runtime.agent is replacement


@pytest.mark.asyncio
async def test_run_agent_without_warm_runtime_still_creates_fresh_agent(adapter):
    with patch.object(adapter, "_create_agent", return_value=FakeAgent("plain")) as mock_create:
        result, _ = await adapter._run_agent(
            user_message="hello",
            conversation_history=[],
            session_id="plain",
        )
    mock_create.assert_called_once()
    assert result["session_id"] == "plain"


@pytest.mark.asyncio
async def test_run_agent_crash_invalidates_warm_runtime(adapter):
    registry, calls = _install_registry(adapter, _row())
    runtime = registry.runtime_for_session("pinned-session-id")

    class CrashingAgent(FakeAgent):
        def run_conversation(self, user_message, conversation_history, task_id):
            raise ValueError("boom")

    runtime.agent = CrashingAgent("pinned-session-id")

    with pytest.raises(ValueError):
        await adapter._run_agent(
            user_message="hello",
            conversation_history=[],
            session_id="pinned-session-id",
            warm_runtime=runtime,
        )

    assert runtime.agent is None  # invalidated for a one-shot rebuild
    assert runtime.last_turn_ok is False
    assert not runtime.turn_lock.locked()

    # Next turn self-repairs through the request-aware builder exactly once.
    repaired = FakeAgent("pinned-session-id")
    with patch.object(adapter, "_create_agent", return_value=repaired) as mock_create:
        result, _ = await adapter._run_agent(
            user_message="hello again",
            conversation_history=[],
            session_id="pinned-session-id",
            warm_runtime=runtime,
        )
    mock_create.assert_called_once()
    assert result["session_id"] == "pinned-session-id"
    assert runtime.agent is not None
    assert runtime.last_turn_ok is True


@pytest.mark.asyncio
async def test_concurrent_turns_on_one_pin_serialize(adapter):
    registry, _ = _install_registry(adapter, _row())
    runtime = registry.runtime_for_session("pinned-session-id")

    active = {"count": 0, "max": 0}
    lock = threading.Lock()

    class SlowAgent(FakeAgent):
        def run_conversation(self, user_message, conversation_history, task_id):
            with lock:
                active["count"] += 1
                active["max"] = max(active["max"], active["count"])
            time.sleep(0.15)
            with lock:
                active["count"] -= 1
            return {"final_response": "ok", "session_id": self.session_id}

    runtime.agent = SlowAgent("pinned-session-id")

    await asyncio.gather(
        adapter._run_agent(
            user_message="a",
            conversation_history=[],
            session_id="pinned-session-id",
            warm_runtime=runtime,
        ),
        adapter._run_agent(
            user_message="b",
            conversation_history=[],
            session_id="pinned-session-id",
            warm_runtime=runtime,
        ),
    )

    assert active["max"] == 1  # the pin's turn lock serialized the turns
    assert not runtime.turn_lock.locked()


@pytest.mark.asyncio
async def test_session_chat_passes_warm_runtime(adapter, session_db):
    session_id = session_db.create_session("warm-chat-session", "api_server")
    registry, _ = _install_registry(adapter, _row(session_id=session_id))
    runtime = registry.runtime_for_session(session_id)
    assert runtime is not None

    mock_run = AsyncMock(
        return_value=({"final_response": "ok", "session_id": session_id}, {"total_tokens": 1})
    )
    app = web.Application()
    app.router.add_post("/api/sessions/{session_id}/chat", adapter._handle_session_chat)
    with patch.object(adapter, "_run_agent", mock_run):
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(
                f"/api/sessions/{session_id}/chat", json={"message": "hi"}
            )
            assert resp.status == 200, await resp.text()

    _, kwargs = mock_run.call_args
    assert kwargs["warm_runtime"] is runtime


@pytest.mark.asyncio
async def test_pinned_sessions_endpoint_reports_facts(adapter):
    registry, _ = _install_registry(adapter, _row())
    # Give the pin a cached probe result so the endpoint read stays offline.
    runtime = registry.runtime_for_session("pinned-session-id")
    runtime.auth_probe = {"ok": True, "checked_at": time.time()}

    app = web.Application()
    app.router.add_get("/api/pinned-sessions", adapter._handle_pinned_sessions)
    async with TestClient(TestServer(app)) as cli:
        resp = await cli.get("/api/pinned-sessions")
        assert resp.status == 200
        payload = await resp.json()

    assert payload["object"] == "hermes.pinned_sessions"
    (pin,) = payload["pins"]
    assert pin["title"] == "Warm Session"
    assert set(pin["facts"]) == {
        "session_persisted",
        "cwd_bound",
        "context_hydrated",
        "tools_ready",
        "memory_ready",
        "model_lock_confirmed",
        "provider_auth_valid",
    }


@pytest.mark.asyncio
async def test_pinned_sessions_endpoint_empty_without_config(session_db):
    plain = APIServerAdapter(PlatformConfig(enabled=True))
    plain._session_db = session_db
    app = web.Application()
    app.router.add_get("/api/pinned-sessions", plain._handle_pinned_sessions)
    async with TestClient(TestServer(app)) as cli:
        resp = await cli.get("/api/pinned-sessions")
        assert resp.status == 200
        payload = await resp.json()
    assert payload == {"object": "hermes.pinned_sessions", "status": "ok", "pins": []}


def test_adapter_parses_pinned_sessions_config(adapter):
    assert adapter._pinned_sessions is not None
    assert adapter._pinned_sessions.titles == ["Warm Session"]

    plain = APIServerAdapter(PlatformConfig(enabled=True))
    assert plain._pinned_sessions is None
