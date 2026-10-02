"""API session workspace binding and pinned warm-agent lifecycle."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import threading
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from aiohttp import web

logger = logging.getLogger(__name__)


class SessionRuntimeMixin:
    def _init_session_runtime(self, extra: Dict[str, Any]) -> None:
        from gateway.pinned_sessions import PinnedSessionRegistry, parse_pinned_session_titles

        self._session_cwd_roots = self._parse_session_cwd_roots(extra.get("session_cwd_roots"))
        titles = parse_pinned_session_titles(extra.get("pinned_sessions"))
        self._pinned_sessions = PinnedSessionRegistry(
            titles, session_lookup=self._pinned_session_lookup,
            agent_builder=self._build_pinned_agent,
            memory_required=self._memory_provider_configured,
        ) if titles else None


    def _pinned_session_lookup(self, title: str) -> Optional[Dict[str, Any]]:
        """Resolve a pinned session row by exact title (canonical addressing)."""
        db = self._ensure_session_db()
        if db is None:
            return None
        try:
            return db.get_session_by_title(title)
        except Exception:
            logger.debug("Pinned session lookup failed for %r", title, exc_info=True)
            return None

    def _profile_default_session_cwd(self) -> Optional[str]:
        """The active profile's ``terminal.cwd`` as a validated session cwd.

        Read through ``load_config_readonly()`` so the multiplexer's per-profile
        home override selects the right ``config.yaml``. Returns ``None`` when
        the profile sets no folder, a placeholder (``.``/``auto``/``cwd``), or a
        folder that fails ``_validate_session_cwd`` (missing, relative, or
        outside ``session_cwd_roots``).
        """
        try:
            from hermes_cli.config import load_config_readonly
            from gateway.cwd_placeholder import CWD_PLACEHOLDERS

            cfg = load_config_readonly() or {}
            raw = str((cfg.get("terminal") or {}).get("cwd") or "").strip()
            if not raw or raw in CWD_PLACEHOLDERS:
                return None
            resolved, error = self._validate_session_cwd(raw)
            if error is not None:
                logger.warning("profile terminal.cwd not usable as session cwd (%s): %s", error, raw)
                return None
            return resolved
        except Exception as exc:
            logger.debug("profile default session cwd unavailable: %s", exc)
            return None

    @staticmethod
    def _memory_provider_configured() -> bool:
        """Whether an external memory provider is configured for this profile."""
        try:
            from hermes_cli.config import load_config_readonly

            cfg = load_config_readonly() or {}
            return bool(str((cfg.get("memory") or {}).get("provider") or "").strip())
        except Exception:
            return False

    @staticmethod
    def _pinned_agent_signature(**inputs: Any) -> str:
        """Hash every construction input so warm agents never ignore overrides."""
        payload = json.dumps(
            inputs,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def _build_pinned_agent(self, session: Dict[str, Any]) -> Any:
        """Build a fully hydrated agent for a pinned session row.

        Applies the same runtime precedence as a live chat turn (confirmed
        model lock → session-persisted model → global defaults) and binds the
        row's persisted working directory through the session-context vars
        for the duration of construction, so context-file discovery and the
        system prompt hydrate against the session's own workspace.
        """
        from gateway.session_context import clear_session_vars, set_session_vars

        session_id = session.get("id") or ""
        runtime_request = self._effective_session_runtime_request(
            session=session,
            body={},
        )
        lock_active = bool(runtime_request.get("require_model_lock"))
        agent_overrides: Dict[str, Any] = {}
        if lock_active:
            route = runtime_request.get("route")
            session_model = None
            requested = runtime_request.get("requested") or {}
            if requested.get("model"):
                agent_overrides["requested_model"] = requested["model"]
            if requested.get("provider"):
                agent_overrides["requested_provider"] = requested["provider"]
            if runtime_request.get("model_options"):
                agent_overrides["model_options"] = runtime_request["model_options"]
        else:
            stored_model = self._stored_session_model(session)
            stored_route = self._resolve_route(stored_model)
            route = stored_route
            session_model = (
                stored_model if (stored_model and stored_route is None) else None
            )
        stored_cwd = session.get("cwd")
        cwd = stored_cwd.strip() if isinstance(stored_cwd, str) else ""
        if cwd:
            cwd, cwd_error = self._validate_session_cwd(cwd)
            if cwd_error:
                raise RuntimeError(
                    f"Pinned session {session_id!r} cwd is unavailable or outside session_cwd_roots"
                )
        tokens = set_session_vars(
            platform="api_server",
            chat_id=session_id,
            session_key=session_id,
            session_id=session_id,
            cwd=cwd,
            async_delivery=False,
            cron_session="",
        )
        try:
            agent = self._create_agent(
                session_id=session_id,
                gateway_session_key=session_id,
                route=route,
                session_model=session_model,
                confirmed_runtime_lock=lock_active,
                **agent_overrides,
            )
            agent._hermes_pinned_signature = self._pinned_agent_signature(
                ephemeral_system_prompt=None,
                session_id=session_id,
                gateway_session_key=session_id,
                requested_model=agent_overrides.get("requested_model"),
                requested_provider=agent_overrides.get("requested_provider"),
                model_options=agent_overrides.get("model_options"),
                route=route,
                session_model=session_model,
                session_cwd=cwd,
                confirmed_runtime_lock=lock_active,
            )
            return agent
        finally:
            clear_session_vars(tokens)

    def _start_pinned_session_hydration(self) -> None:
        """Hydrate pinned runtimes off the event loop at gateway (re)start."""
        registry = self._pinned_sessions
        if not registry:
            return

        def _hydrate() -> None:
            try:
                registry.hydrate_all()
                logger.info(
                    "[%s] pinned session hydration complete: %s",
                    self.name,
                    ", ".join(registry.titles),
                )
            except Exception:
                logger.warning(
                    "[%s] pinned session hydration failed",
                    self.name,
                    exc_info=True,
                )

        threading.Thread(
            target=_hydrate,
            daemon=True,
            name="pinned-session-hydration",
        ).start()

    async def _handle_pinned_sessions(self, request: "web.Request") -> "web.Response":
        """GET /api/pinned-sessions — per-pin readiness facts.

        ``?probe_auth=1`` forces a fresh bounded provider-auth probe (still
        rate-limited by the registry's minimum probe interval).
        """
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        registry = self._pinned_sessions
        if not registry:
            return web.json_response(
                {"object": "hermes.pinned_sessions", "status": "ok", "pins": []}
            )
        from gateway.platforms.api_server import _coerce_request_bool

        probe = _coerce_request_bool(request.query.get("probe_auth"), default=False)
        payload = await asyncio.to_thread(registry.readiness, probe_auth=probe)
        return web.json_response(payload)

    @staticmethod
    def _parse_session_cwd_roots(raw: Any) -> Optional[tuple[Path, ...]]:
        """Parse the optional ``session_cwd_roots`` allowlist config.

        Accepts a list of absolute directory paths (or a single string).
        Entries that are not absolute existing directories are dropped with a
        warning: a typo'd root must not widen the allowlist to "anything".
        """
        if raw is None:
            return None
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, (list, tuple)):
            logger.warning("session_cwd_roots must be a list of absolute paths; ignoring %r", type(raw).__name__)
            return ()
        roots = []
        for entry in raw:
            if not isinstance(entry, str) or not entry.strip():
                logger.warning("session_cwd_roots entry ignored (not a path string): %r", entry)
                continue
            p = Path(entry.strip()).expanduser()
            if not p.is_absolute() or not p.is_dir():
                logger.warning("session_cwd_roots entry ignored (not an existing absolute directory): %s", entry)
                continue
            try:
                roots.append(p.resolve())
            except OSError:
                logger.warning("session_cwd_roots entry ignored (unresolvable): %s", entry)
        return tuple(roots)

    def _validate_session_cwd(self, raw: Any) -> tuple:
        """Validate a client-supplied persisted session working directory.

        Returns ``(normalized_cwd, error_message)`` — exactly one is None.
        Fail-closed contract: only an existing absolute directory (inside the
        configured ``session_cwd_roots`` allowlist, when one is set) is
        accepted; everything else is rejected at creation time so a chat turn
        can never silently run in a wrong or missing workspace.
        """
        if not isinstance(raw, str) or not raw.strip():
            return None, "cwd must be a non-empty string path"
        candidate = Path(raw.strip()).expanduser()
        if not candidate.is_absolute():
            return None, "cwd must be an absolute path"
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError):
            return None, "cwd does not exist or is not accessible"
        if not resolved.is_dir():
            return None, "cwd must be an existing directory"
        if self._session_cwd_roots is not None:
            allowed = any(
                resolved == root or root in resolved.parents
                for root in self._session_cwd_roots
            )
            if not allowed:
                return None, "cwd is outside the allowed session_cwd_roots"
        return str(resolved), None

    def _session_cwd_for_run(self, session: Dict[str, Any]):
        """Resolve a persisted session's cwd for an agent run, failing closed.

        Returns ``(cwd, error_response)``. A session with no stored cwd runs
        with ``""`` (no binding — the pre-cwd behavior). A stored cwd whose
        directory has since disappeared returns a 503 error response instead
        of letting the turn run in whatever directory the process happens to
        be in (wrong-workspace fail-closed contract).
        """
        from gateway.platforms.api_server import _openai_error

        stored = session.get("cwd")
        if not isinstance(stored, str) or not stored.strip():
            return "", None
        resolved, error = self._validate_session_cwd(stored)
        if error:
            return None, web.json_response(
                _openai_error(
                    "The session's working directory is unavailable or no longer allowed: "
                    f"{stored}",
                    code="session_cwd_unavailable",
                ),
                status=503,
            )
        return resolved, None
