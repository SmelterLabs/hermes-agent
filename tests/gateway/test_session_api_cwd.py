"""Tests for persisted API-session working-directory (cwd) binding.

Covers the contract added for external session hosts (e.g. telephony/webhook
bridges) that drive one long-lived Hermes session through the API server:

* ``POST /api/sessions`` accepts, validates, persists, and returns ``cwd``.
* Invalid cwd values (relative, nonexistent, non-string, outside the optional
  ``session_cwd_roots`` allowlist) are rejected at creation.
* ``/chat`` and ``/chat/stream`` bind the stored cwd for the whole agent run
  through the session-context vars, and fail closed (503) when the stored
  directory has since disappeared.
* Concurrent runs keep distinct cwd bindings; a later run without a cwd does
  not inherit an earlier run's binding.
"""

import asyncio
import threading
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter
from hermes_state import SessionDB


@pytest.fixture
def session_db(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    try:
        yield db
    finally:
        close = getattr(db, "close", None)
        if callable(close):
            close()


@pytest.fixture
def adapter(session_db):
    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    adapter._session_db = session_db
    return adapter


def _create_session_app(adapter: APIServerAdapter) -> web.Application:
    app = web.Application()
    app.router.add_post("/api/sessions", adapter._handle_create_session)
    app.router.add_get("/api/sessions/{session_id}", adapter._handle_get_session)
    app.router.add_post("/api/sessions/{session_id}/fork", adapter._handle_fork_session)
    app.router.add_post("/api/sessions/{session_id}/chat", adapter._handle_session_chat)
    app.router.add_post("/api/sessions/{session_id}/chat/stream", adapter._handle_session_chat_stream)
    return app


@pytest.mark.asyncio
async def test_create_session_persists_and_returns_cwd(adapter, session_db, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    app = _create_session_app(adapter)
    async with TestClient(TestServer(app)) as cli:
        resp = await cli.post(
            "/api/sessions",
            json={"id": "cwd-session", "cwd": str(workspace)},
        )
        assert resp.status == 201, await resp.text()
        payload = await resp.json()

        get_resp = await cli.get("/api/sessions/cwd-session")
        assert get_resp.status == 200
        fetched = await get_resp.json()

    # Returned in both the create and get resources (non-secret identifier).
    assert payload["session"]["cwd"] == str(workspace.resolve())
    assert fetched["session"]["cwd"] == str(workspace.resolve())
    # Persisted in the sessions row itself.
    assert session_db.get_session("cwd-session")["cwd"] == str(workspace.resolve())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_cwd",
    [
        "relative/path",
        "",
        123,
        {"path": "/tmp"},
    ],
)
async def test_create_session_rejects_invalid_cwd(adapter, bad_cwd):
    app = _create_session_app(adapter)
    async with TestClient(TestServer(app)) as cli:
        resp = await cli.post("/api/sessions", json={"cwd": bad_cwd})
        assert resp.status == 400, await resp.text()
        body = await resp.json()
    assert body["error"]["code"] == "invalid_session_cwd"


@pytest.mark.asyncio
async def test_create_session_rejects_nonexistent_cwd(adapter, tmp_path):
    app = _create_session_app(adapter)
    async with TestClient(TestServer(app)) as cli:
        resp = await cli.post(
            "/api/sessions", json={"cwd": str(tmp_path / "does-not-exist")}
        )
        assert resp.status == 400, await resp.text()
        body = await resp.json()
    assert body["error"]["code"] == "invalid_session_cwd"


@pytest.mark.asyncio
async def test_create_session_cwd_allowlist(session_db, tmp_path):
    allowed_root = tmp_path / "allowed"
    allowed_root.mkdir()
    inside = allowed_root / "project"
    inside.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    adapter = APIServerAdapter(
        PlatformConfig(enabled=True, extra={"session_cwd_roots": [str(allowed_root)]})
    )
    adapter._session_db = session_db

    app = _create_session_app(adapter)
    async with TestClient(TestServer(app)) as cli:
        ok = await cli.post("/api/sessions", json={"cwd": str(inside)})
        assert ok.status == 201, await ok.text()

        denied = await cli.post("/api/sessions", json={"cwd": str(outside)})
        assert denied.status == 400, await denied.text()
        body = await denied.json()
    assert body["error"]["code"] == "invalid_session_cwd"


@pytest.mark.asyncio
async def test_configured_but_invalid_cwd_roots_deny_all_bindings(session_db, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    adapter = APIServerAdapter(
        PlatformConfig(
            enabled=True,
            extra={"session_cwd_roots": [str(tmp_path / "missing-root")]},
        )
    )
    adapter._session_db = session_db

    app = _create_session_app(adapter)
    async with TestClient(TestServer(app)) as cli:
        resp = await cli.post("/api/sessions", json={"cwd": str(workspace)})
        assert resp.status == 400, await resp.text()
        body = await resp.json()

    assert adapter._session_cwd_roots == ()
    assert body["error"]["code"] == "invalid_session_cwd"


@pytest.mark.asyncio
async def test_existing_session_is_rejected_after_cwd_allowlist_tightens(session_db, tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    session_id = session_db.create_session(
        "tightened-cwd",
        "api_server",
        cwd=str(outside),
    )
    adapter = APIServerAdapter(
        PlatformConfig(enabled=True, extra={"session_cwd_roots": [str(allowed)]})
    )
    adapter._session_db = session_db
    mock_run = AsyncMock()

    app = _create_session_app(adapter)
    with patch.object(adapter, "_run_agent", mock_run):
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(
                f"/api/sessions/{session_id}/chat",
                json={"message": "hi"},
            )
            assert resp.status == 503, await resp.text()
            body = await resp.json()

    assert body["error"]["code"] == "session_cwd_unavailable"
    mock_run.assert_not_called()


def test_pinned_hydration_rejects_cwd_outside_current_allowlist(session_db, tmp_path):
    allowed = tmp_path / "allowed-pin"
    allowed.mkdir()
    outside = tmp_path / "outside-pin"
    outside.mkdir()
    adapter = APIServerAdapter(
        PlatformConfig(enabled=True, extra={"session_cwd_roots": [str(allowed)]})
    )
    adapter._session_db = session_db

    with patch.object(adapter, "_create_agent") as create_agent:
        with pytest.raises(RuntimeError, match="outside session_cwd_roots"):
            adapter._build_pinned_agent(
                {"id": "pinned-outside", "title": "Pinned", "cwd": str(outside)}
            )

    create_agent.assert_not_called()


@pytest.mark.asyncio
async def test_fork_inherits_the_validated_source_cwd(adapter, session_db, tmp_path):
    workspace = tmp_path / "fork-workspace"
    workspace.mkdir()
    source_id = session_db.create_session(
        "fork-source",
        "api_server",
        cwd=str(workspace),
    )

    app = _create_session_app(adapter)
    async with TestClient(TestServer(app)) as cli:
        resp = await cli.post(
            f"/api/sessions/{source_id}/fork",
            json={"id": "fork-child"},
        )
        assert resp.status == 201, await resp.text()

    assert session_db.get_session("fork-child")["cwd"] == str(workspace.resolve())


@pytest.mark.asyncio
async def test_session_chat_threads_stored_cwd_to_run_agent(adapter, session_db, tmp_path):
    workspace = tmp_path / "bound-workspace"
    workspace.mkdir()
    session_id = session_db.create_session(
        "cwd-chat-session", "api_server", cwd=str(workspace)
    )

    mock_run = AsyncMock(
        return_value=({"final_response": "ok", "session_id": session_id}, {"total_tokens": 1})
    )
    app = _create_session_app(adapter)
    with patch.object(adapter, "_run_agent", mock_run):
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(
                f"/api/sessions/{session_id}/chat", json={"message": "hi"}
            )
            assert resp.status == 200, await resp.text()

    _, kwargs = mock_run.call_args
    assert kwargs["session_cwd"] == str(workspace)


@pytest.mark.asyncio
async def test_session_chat_stream_threads_stored_cwd_to_run_agent(adapter, session_db, tmp_path):
    workspace = tmp_path / "stream-workspace"
    workspace.mkdir()
    session_id = session_db.create_session(
        "cwd-stream-session", "api_server", cwd=str(workspace)
    )

    captured = {}

    async def fake_run(**kwargs):
        captured.update(kwargs)
        return {"final_response": "ok", "session_id": session_id}, {"total_tokens": 1}

    app = _create_session_app(adapter)
    with patch.object(adapter, "_run_agent", side_effect=fake_run):
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(
                f"/api/sessions/{session_id}/chat/stream", json={"message": "hi"}
            )
            assert resp.status == 200, await resp.text()
            # Drain the SSE body so the background task has invoked _run_agent.
            await resp.text()

    assert captured["session_cwd"] == str(workspace)


@pytest.mark.asyncio
async def test_run_agent_binds_session_cwd_for_agent_context(adapter, monkeypatch):
    """The bound cwd must be visible to the agent through resolve_agent_cwd()."""
    import pathlib

    observed = {}

    class FakeAgent:
        session_prompt_tokens = 0
        session_completion_tokens = 0
        session_total_tokens = 0

        def __init__(self, session_id: str):
            self.session_id = session_id

        def run_conversation(self, user_message, conversation_history, task_id):
            from agent.runtime_cwd import resolve_agent_cwd

            observed["agent_cwd"] = str(resolve_agent_cwd())
            return {"final_response": "ok"}

    monkeypatch.setattr(
        adapter, "_create_agent", lambda **kwargs: FakeAgent(kwargs["session_id"])
    )

    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        bound = str(pathlib.Path(tmp).resolve())
        result, _ = await adapter._run_agent(
            user_message="hello",
            conversation_history=[],
            session_id="cwd-bind-session",
            session_cwd=bound,
        )

        assert result["session_id"] == "cwd-bind-session"
        assert observed["agent_cwd"] == bound


@pytest.mark.asyncio
async def test_chat_fails_closed_when_stored_cwd_disappears(adapter, session_db, tmp_path):
    vanished = tmp_path / "vanished"
    vanished.mkdir()
    session_id = session_db.create_session(
        "cwd-gone-session", "api_server", cwd=str(vanished)
    )
    vanished.rmdir()

    mock_run = AsyncMock()
    app = _create_session_app(adapter)
    with patch.object(adapter, "_run_agent", mock_run):
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(
                f"/api/sessions/{session_id}/chat", json={"message": "hi"}
            )
            assert resp.status == 503, await resp.text()
            body = await resp.json()

            stream_resp = await cli.post(
                f"/api/sessions/{session_id}/chat/stream", json={"message": "hi"}
            )
            assert stream_resp.status == 503, await stream_resp.text()
            stream_body = await stream_resp.json()

    assert body["error"]["code"] == "session_cwd_unavailable"
    assert stream_body["error"]["code"] == "session_cwd_unavailable"
    mock_run.assert_not_called()


@pytest.mark.asyncio
async def test_concurrent_runs_keep_distinct_cwd_bindings(adapter, monkeypatch, tmp_path):
    """Two simultaneous API-session runs must not observe each other's cwd."""
    ws_a = tmp_path / "workspace-a"
    ws_b = tmp_path / "workspace-b"
    ws_a.mkdir()
    ws_b.mkdir()

    both_started = threading.Barrier(2, timeout=10)
    observed = {}

    class FakeAgent:
        session_prompt_tokens = 0
        session_completion_tokens = 0
        session_total_tokens = 0

        def __init__(self, session_id: str):
            self.session_id = session_id

        def run_conversation(self, user_message, conversation_history, task_id):
            from agent.runtime_cwd import resolve_agent_cwd

            # Hold until BOTH runs are in-flight so the bindings overlap in
            # time — a sequential pass would trivially (and wrongly) pass.
            both_started.wait()
            observed[task_id] = str(resolve_agent_cwd())
            return {"final_response": "ok"}

    monkeypatch.setattr(
        adapter, "_create_agent", lambda **kwargs: FakeAgent(kwargs["session_id"])
    )

    await asyncio.gather(
        adapter._run_agent(
            user_message="a",
            conversation_history=[],
            session_id="concurrent-a",
            session_cwd=str(ws_a.resolve()),
        ),
        adapter._run_agent(
            user_message="b",
            conversation_history=[],
            session_id="concurrent-b",
            session_cwd=str(ws_b.resolve()),
        ),
    )

    assert observed["concurrent-a"] == str(ws_a.resolve())
    assert observed["concurrent-b"] == str(ws_b.resolve())


@pytest.mark.asyncio
async def test_cwd_binding_is_cleared_after_run(adapter, monkeypatch, tmp_path):
    """A later run WITHOUT a session cwd must not inherit an earlier binding
    from a reused executor thread."""
    ws = tmp_path / "first-workspace"
    ws.mkdir()
    observed = {}

    class FakeAgent:
        session_prompt_tokens = 0
        session_completion_tokens = 0
        session_total_tokens = 0

        def __init__(self, session_id: str):
            self.session_id = session_id

        def run_conversation(self, user_message, conversation_history, task_id):
            from agent.runtime_cwd import resolve_agent_cwd

            observed[task_id] = str(resolve_agent_cwd())
            return {"final_response": "ok"}

    monkeypatch.setattr(
        adapter, "_create_agent", lambda **kwargs: FakeAgent(kwargs["session_id"])
    )

    await adapter._run_agent(
        user_message="first",
        conversation_history=[],
        session_id="bound-run",
        session_cwd=str(ws.resolve()),
    )
    await adapter._run_agent(
        user_message="second",
        conversation_history=[],
        session_id="unbound-run",
    )

    assert observed["bound-run"] == str(ws.resolve())
    assert observed["unbound-run"] != str(ws.resolve())


# ---------------------------------------------------------------------------
# Profile default cwd (local overlay, 2026-09-01): an API-created session with
# no ``cwd`` in the body binds to the active profile's ``terminal.cwd``.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_session_defaults_cwd_to_profile_terminal_cwd(adapter, session_db, tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    app = _create_session_app(adapter)
    with patch("hermes_cli.config.load_config_readonly", return_value={"terminal": {"cwd": str(workspace)}}):
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post("/api/sessions", json={"id": "profile-default-cwd"})
            assert resp.status == 201, await resp.text()
            payload = await resp.json()
    assert payload["session"]["cwd"] == str(workspace.resolve())
    assert session_db.get_session("profile-default-cwd")["cwd"] == str(workspace.resolve())


@pytest.mark.asyncio
async def test_create_session_explicit_cwd_beats_profile_default(adapter, session_db, tmp_path):
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()
    explicit = tmp_path / "explicit"
    explicit.mkdir()
    app = _create_session_app(adapter)
    with patch("hermes_cli.config.load_config_readonly", return_value={"terminal": {"cwd": str(profile_dir)}}):
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post("/api/sessions", json={"id": "explicit-wins", "cwd": str(explicit)})
            assert resp.status == 201, await resp.text()
    assert session_db.get_session("explicit-wins")["cwd"] == str(explicit.resolve())


@pytest.mark.asyncio
@pytest.mark.parametrize("configured", ["", "auto", ".", "cwd", "relative/path", "C:/definitely/missing/folder-9f3a"])
async def test_create_session_unusable_profile_cwd_leaves_session_unbound(adapter, session_db, configured):
    app = _create_session_app(adapter)
    with patch("hermes_cli.config.load_config_readonly", return_value={"terminal": {"cwd": configured}}):
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post("/api/sessions", json={"id": f"unbound-{abs(hash(configured))}"})
            assert resp.status == 201, await resp.text()
            payload = await resp.json()
    assert payload["session"].get("cwd") in (None, "")
