"""Pinned warm session runtimes for the API server.

A *pinned session* is a durable, title-addressed Hermes session (for example a
telephony/webhook bridge's canonical conversation) whose agent runtime must be
**warm**: hydrated when the gateway starts, kept in memory between turns, and
rehydrated automatically after a restart — so an inbound request never pays
agent-construction cost (system prompt build, context-file discovery, tool
schema, memory provider init) on the critical path.

The registry also owns the per-pin readiness facts consumed by external
monitoring and by bridge plugins deciding whether to accept traffic:

* ``session_persisted`` — the title resolves to a session row.
* ``cwd_bound``        — the row carries a working directory that exists.
* ``context_hydrated`` — the agent runtime is built and cached in memory.
* ``tools_ready``      — the cached agent exposes a non-empty tool surface.
* ``memory_ready``     — the configured memory provider initialized (or no
                          provider is configured).
* ``model_lock_confirmed`` — the session row carries a confirmed model lock.
* ``provider_auth_valid``  — a **real bounded request** through the pinned
  provider/model succeeded recently. The probe goes through the normal
  credential-resolution chain (which performs ordinary token refresh), so it
  validates live authentication rather than parsing token expiry. Only
  status, latency, provider/model identity, and error class are recorded —
  never token material or response content.

The facts are deliberately separate; they are never collapsed into one
boolean, so a monitoring consumer can distinguish "session row missing" from
"provider auth expired".

Concurrency contract: one pinned runtime serves one conversation. Turns on the
same pinned agent are serialized by ``turn_lock`` (an ``AIAgent`` is not safe
for concurrent ``run_conversation`` calls); distinct pins run concurrently.

Failure contract: a turn that crashes on a warm agent invalidates the cached
runtime; the next turn (or the next readiness poll) rebuilds it once. There is
no restart loop and no automatic credential mutation — an auth failure is
reported through ``provider_auth_valid`` and left for a human/monitor.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# Minimum seconds between two real provider-auth probes for one pin, even when
# callers force a refresh. Keeps a hot readiness poller from turning the
# "bounded real request" into metered request spam.
_AUTH_PROBE_MIN_INTERVAL_SECS = 30.0
# Default seconds a successful/failed probe result stays fresh before a
# readiness read triggers a new probe.
_AUTH_PROBE_TTL_SECS = 900.0


def default_auth_prober(provider: str, model: str, *, timeout: float = 45.0) -> Dict[str, Any]:
    """Execute one bounded real request through ``provider``/``model``.

    Uses the auxiliary LLM client, which resolves credentials through the
    normal provider chain (including ordinary OAuth refresh) and speaks the
    provider's native API mode. The request is content-free ("ping"), tightly
    token-bounded, and its response content is discarded — only status,
    latency, identity, and error class are recorded.
    """
    checked_at = time.time()
    started = time.perf_counter()
    try:
        from agent.auxiliary_client import call_llm

        call_llm(
            provider=provider or None,
            model=model or None,
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=16,
            timeout=timeout,
        )
        return {
            "ok": True,
            "provider": provider or "",
            "model": model or "",
            "latency_ms": int((time.perf_counter() - started) * 1000),
            "checked_at": checked_at,
        }
    except Exception as exc:
        return {
            "ok": False,
            "provider": provider or "",
            "model": model or "",
            "latency_ms": int((time.perf_counter() - started) * 1000),
            "checked_at": checked_at,
            "error_class": type(exc).__name__,
        }


class PinnedSessionRuntime:
    """One pinned session's cached agent runtime plus its readiness facts."""

    def __init__(self, title: str):
        self.title = title
        self.session_id: Optional[str] = None
        self.cwd: str = ""
        self.provider: str = ""
        self.model: str = ""
        self.model_lock_confirmed: bool = False
        self.agent: Any = None
        self.construction_signature: str = ""
        # Serializes turns on the shared agent instance. Held for the whole
        # executor-side turn; a second concurrent turn on the same pin waits.
        self.turn_lock = threading.Lock()
        self.hydrated_at: Optional[float] = None
        self.hydrate_error: Optional[str] = None
        self.last_turn_at: Optional[float] = None
        self.last_turn_ok: Optional[bool] = None
        self.memory_ready: bool = False
        self.memory_detail: str = ""
        self.auth_probe: Dict[str, Any] = {}

    def facts(self) -> Dict[str, Any]:
        """Per-pin readiness facts. Never collapsed to one boolean."""
        agent = self.agent
        payload: Dict[str, Any] = {
            "title": self.title,
            "session_id": self.session_id,
            "cwd": self.cwd or None,
            "provider": self.provider or None,
            "model": self.model or None,
            "facts": {
                "session_persisted": bool(self.session_id),
                "cwd_bound": bool(self.cwd) and _dir_exists(self.cwd),
                "context_hydrated": agent is not None,
                "tools_ready": bool(getattr(agent, "tools", None)),
                "memory_ready": bool(self.memory_ready),
                "model_lock_confirmed": bool(self.model_lock_confirmed),
                "provider_auth_valid": bool(self.auth_probe.get("ok")),
            },
            "hydrated_at": self.hydrated_at,
            "last_turn_at": self.last_turn_at,
            "last_turn_ok": self.last_turn_ok,
            "auth_probe": dict(self.auth_probe),
        }
        if self.hydrate_error:
            payload["hydrate_error"] = self.hydrate_error
        if self.memory_detail:
            payload["memory_detail"] = self.memory_detail
        return payload


def _dir_exists(path: str) -> bool:
    try:
        from pathlib import Path

        return Path(path).is_dir()
    except Exception:
        return False


class PinnedSessionRegistry:
    """Owns every configured pinned runtime for one API-server adapter.

    Dependency-injected so it stays testable and adapter-agnostic:

    * ``session_lookup(title)`` returns the session row dict (or None).
    * ``agent_builder(session_row)`` builds a fully hydrated agent for the
      row — the adapter implements this with its normal runtime/model-lock
      resolution and session-context (cwd) binding.
    * ``auth_prober(provider, model)`` executes the bounded real auth probe.
    * ``memory_required()`` reports whether a memory provider is configured.
    """

    def __init__(
        self,
        titles: List[str],
        *,
        session_lookup: Callable[[str], Optional[Dict[str, Any]]],
        agent_builder: Callable[[Dict[str, Any]], Any],
        auth_prober: Optional[Callable[..., Dict[str, Any]]] = None,
        memory_required: Optional[Callable[[], bool]] = None,
        auth_probe_ttl_secs: float = _AUTH_PROBE_TTL_SECS,
    ):
        self._runtimes: Dict[str, PinnedSessionRuntime] = {
            title: PinnedSessionRuntime(title) for title in titles
        }
        self._session_lookup = session_lookup
        self._agent_builder = agent_builder
        self._auth_prober = auth_prober or default_auth_prober
        self._memory_required = memory_required or (lambda: False)
        self._auth_probe_ttl_secs = float(auth_probe_ttl_secs)
        self._probe_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------

    def __bool__(self) -> bool:
        return bool(self._runtimes)

    @property
    def titles(self) -> List[str]:
        return list(self._runtimes)

    def runtime_for_session(self, session_id: str) -> Optional[PinnedSessionRuntime]:
        """Match a chat request to a pinned runtime by its effective session id."""
        if not session_id:
            return None
        for runtime in self._runtimes.values():
            if runtime.session_id and runtime.session_id == session_id:
                return runtime
        return None

    # ------------------------------------------------------------------
    # Hydration
    # ------------------------------------------------------------------

    def hydrate_all(self, *, probe_auth: bool = True) -> None:
        """Resolve and build every configured pin. Called at gateway start
        (and re-called after restart) so pinned runtimes are warm before any
        bridge reports itself ready."""
        for runtime in self._runtimes.values():
            try:
                with runtime.turn_lock:
                    self._hydrate_locked(runtime)
            except Exception as exc:
                runtime.hydrate_error = type(exc).__name__
                logger.warning(
                    "Pinned session %r hydration failed: %s",
                    runtime.title,
                    type(exc).__name__,
                )
            if probe_auth:
                self.probe_auth(runtime)

    def _install_agent_locked(self, runtime: PinnedSessionRuntime, agent: Any) -> None:
        """Publish a fully built agent and refresh all dependent readiness state."""
        previous_identity = (runtime.provider, runtime.model)
        api_runtime = getattr(agent, "_hermes_api_runtime", None) or {}
        provider = str(api_runtime.get("provider") or getattr(agent, "provider", "") or "")
        model = str(api_runtime.get("model") or getattr(agent, "model", "") or "")

        runtime.provider = provider
        runtime.model = model
        if previous_identity != (provider, model):
            runtime.auth_probe = {}

        required = False
        try:
            required = bool(self._memory_required())
        except Exception:
            required = False
        if not required:
            runtime.memory_ready = True
            runtime.memory_detail = "no memory provider configured"
        else:
            runtime.memory_ready = getattr(agent, "_memory_manager", None) is not None
            runtime.memory_detail = ""

        runtime.agent = agent
        runtime.construction_signature = str(
            getattr(agent, "_hermes_pinned_signature", "") or ""
        )
        runtime.hydrated_at = time.time()
        runtime.hydrate_error = None

    def _hydrate_locked(self, runtime: PinnedSessionRuntime) -> None:
        """Hydrate one pin. Caller holds ``runtime.turn_lock``."""
        # Fail closed before any lookup/build: an exception must never leave the
        # previous agent or its auth result advertised as the current runtime.
        runtime.agent = None
        runtime.construction_signature = ""
        runtime.hydrated_at = None
        runtime.memory_ready = False
        runtime.memory_detail = ""
        runtime.auth_probe = {}

        row = self._session_lookup(runtime.title)
        if not row:
            runtime.session_id = None
            runtime.cwd = ""
            runtime.provider = ""
            runtime.model = ""
            runtime.model_lock_confirmed = False
            runtime.hydrate_error = "session_not_found"
            return
        runtime.session_id = row.get("id")
        cwd = row.get("cwd")
        runtime.cwd = cwd.strip() if isinstance(cwd, str) else ""
        lock = _confirmed_model_lock(row)
        runtime.model_lock_confirmed = lock is not None
        agent = self._agent_builder(row)
        self._install_agent_locked(runtime, agent)

    def ensure_agent(
        self,
        runtime: PinnedSessionRuntime,
        *,
        expected_provider: str = "",
        expected_model: str = "",
        expected_signature: str = "",
        agent_builder: Optional[Callable[[], Any]] = None,
        stream_delta_callback: Any = None,
        tool_progress_callback: Any = None,
        tool_start_callback: Any = None,
        tool_complete_callback: Any = None,
        interim_assistant_callback: Any = None,
        reasoning_callback: Any = None,
        status_callback: Any = None,
    ) -> Any:
        """Return the warm agent for a turn, rebuilding only when required.

        Caller MUST hold ``runtime.turn_lock``. Rebuild triggers: no cached
        agent (first turn, post-crash invalidation, restart before hydration
        finished) or a persisted model-lock change (``expected_*`` no longer
        matches the cached runtime identity). Callbacks are rebound and the
        per-turn state reset mirrors the gateway agent-cache re-init
        (``GatewayRunner._init_cached_agent_for_turn``).
        """
        stale_lock = bool(
            (expected_provider and runtime.provider and expected_provider != runtime.provider)
            or (expected_model and runtime.model and expected_model != runtime.model)
        )
        stale_signature = bool(
            expected_signature
            and runtime.construction_signature != expected_signature
        )
        if runtime.agent is None or stale_lock or stale_signature:
            if stale_lock:
                logger.info(
                    "Pinned session %r model lock changed (%s/%s -> %s/%s); rebuilding warm runtime",
                    runtime.title,
                    runtime.provider,
                    runtime.model,
                    expected_provider or runtime.provider,
                    expected_model or runtime.model,
                )
            if agent_builder is not None and expected_signature:
                runtime.agent = None
                runtime.auth_probe = {}
                agent = agent_builder()
                setattr(agent, "_hermes_pinned_signature", expected_signature)
                self._install_agent_locked(runtime, agent)
            else:
                self._hydrate_locked(runtime)
            if runtime.agent is None:
                raise RuntimeError(
                    f"pinned session {runtime.title!r} could not be hydrated"
                )
        agent = runtime.agent
        agent.stream_delta_callback = stream_delta_callback
        agent.tool_progress_callback = tool_progress_callback
        agent.tool_start_callback = tool_start_callback
        agent.tool_complete_callback = tool_complete_callback
        # Upstream's per-turn streams (gateway/run_turn_runner.py rebinds the same
        # three): a reused agent must not keep the previous turn's closures.
        agent.interim_assistant_callback = interim_assistant_callback
        agent.reasoning_callback = reasoning_callback
        agent.status_callback = status_callback
        # Per-turn reset, mirroring GatewayRunner._init_cached_agent_for_turn
        # (fresh external turn, depth 0): activity triple + flush cursor +
        # API-call counter.
        try:
            from agent.session_activity import ActivityProvenance

            agent._last_activity_ts = time.time()
            agent._last_activity_desc = "starting new turn (pinned)"
            agent._last_activity_provenance = ActivityProvenance.UNKNOWN
        except Exception:
            pass
        if hasattr(agent, "_last_flushed_db_idx"):
            agent._last_flushed_db_idx = 0
        if hasattr(agent, "_api_call_count"):
            agent._api_call_count = 0
        return agent

    # ------------------------------------------------------------------
    # Turn accounting
    # ------------------------------------------------------------------

    def note_turn_result(self, runtime: PinnedSessionRuntime, agent: Any, *, ok: bool) -> None:
        """Record a finished turn; follow compression rotation; invalidate on crash."""
        runtime.last_turn_at = time.time()
        runtime.last_turn_ok = bool(ok)
        if not ok:
            # Bounded self-repair: drop the cached agent so the next turn (or
            # readiness poll) rebuilds it once. Never loop.
            runtime.agent = None
            return
        effective = getattr(agent, "session_id", None)
        if isinstance(effective, str) and effective and effective != runtime.session_id:
            logger.info(
                "Pinned session %r rotated %s -> %s (compression); updating mapping",
                runtime.title,
                runtime.session_id,
                effective,
            )
            runtime.session_id = effective

    # ------------------------------------------------------------------
    # Auth probe + readiness
    # ------------------------------------------------------------------

    def probe_auth(self, runtime: PinnedSessionRuntime, *, force: bool = False) -> Dict[str, Any]:
        """Refresh ``provider_auth_valid`` with a bounded real request.

        Rate-limited: a fresh result inside the TTL is reused unless forced,
        and even forced probes respect a minimum interval.
        """
        with self._probe_lock:
            last = runtime.auth_probe.get("checked_at") or 0.0
            age = time.time() - last
            if runtime.auth_probe and not force and age < self._auth_probe_ttl_secs:
                return dict(runtime.auth_probe)
            if runtime.auth_probe and age < _AUTH_PROBE_MIN_INTERVAL_SECS:
                return dict(runtime.auth_probe)
            result = self._auth_prober(runtime.provider, runtime.model)
            runtime.auth_probe = dict(result)
            return dict(result)

    def readiness(self, *, probe_auth: bool = False) -> Dict[str, Any]:
        """All pins' facts plus a rolled-up status (facts stay separate)."""
        pins = []
        for runtime in self._runtimes.values():
            if probe_auth and runtime.provider:
                self.probe_auth(runtime, force=True)
            elif runtime.provider and not runtime.auth_probe:
                self.probe_auth(runtime)
            pins.append(runtime.facts())
        all_ready = all(
            all(pin["facts"].values()) for pin in pins
        ) if pins else True
        return {
            "object": "hermes.pinned_sessions",
            "status": "ok" if all_ready else "degraded",
            "pins": pins,
        }


def _confirmed_model_lock(session_row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Extract a confirmed browser_model_lock from a session row, if any."""
    import json

    raw = session_row.get("model_config")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return None
    if not isinstance(raw, dict):
        return None
    lock = raw.get("browser_model_lock")
    if isinstance(lock, dict) and lock.get("confirmed"):
        return lock
    return None


def parse_pinned_session_titles(raw: Any) -> List[str]:
    """Parse the ``pinned_sessions`` platform config value.

    Accepts a list of title strings or ``{"title": ...}`` dicts (or a single
    string). Invalid entries are dropped with a warning; duplicates collapse.
    """
    if raw is None:
        return []
    if isinstance(raw, (str, dict)):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        logger.warning(
            "pinned_sessions must be a list of titles; ignoring %r",
            type(raw).__name__,
        )
        return []
    titles: List[str] = []
    for entry in raw:
        if isinstance(entry, dict):
            entry = entry.get("title")
        if not isinstance(entry, str) or not entry.strip():
            logger.warning("pinned_sessions entry ignored (not a title): %r", entry)
            continue
        title = entry.strip()
        if title not in titles:
            titles.append(title)
    return titles


__all__ = [
    "PinnedSessionRegistry",
    "PinnedSessionRuntime",
    "default_auth_prober",
    "parse_pinned_session_titles",
]
