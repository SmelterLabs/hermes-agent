"""Live compression: config hot-reload onto a running agent, pending model switch apply, /compress
(CompressionLockHeld when a turn holds the lock), session-key sync after compress. Bodies are rebound
onto server.py's globals (method_ctx.bind_module) and reference them bare."""

from __future__ import annotations

import contextlib

from utils import is_truthy_value

from .method_ctx import bind_module


def _tui_compression_config_signature(cfg: dict | None) -> tuple:
    """Stable snapshot of compression/context keys that must apply next turn: the messaging-gateway
    cache-busting extract plus ``idle_compact_after_seconds``/``tail_mode`` (live-TUI-only keys)."""
    from gateway.run import GatewayRunner
    keys = GatewayRunner._extract_cache_busting_config(cfg)
    picked = {k: v for k, v in keys.items() if k.startswith("compression.") or k == "model.context_length"}
    compression = cfg.get("compression") if isinstance(cfg, dict) and isinstance(cfg.get("compression"), dict) else {}
    picked.update({f"compression.{k}": compression.get(k) for k in ("idle_compact_after_seconds", "tail_mode")})
    return tuple(sorted(picked.items()))


def _compressor_ctor_default(name: str, fallback: Any) -> Any:
    """Default read off ContextCompressor.__init__'s REAL signature, so unset-key restoration uses the
    construction path's derivation instead of a hardcoded copy that could drift.

    Unset restoration must go through the same derivation the construction path uses (#94724 review finding
    on #95980) — pulling the default off ``ContextCompressor.__init__`` itself instead of hardcoding copies
    keeps the two from drifting.
    """
    try:
        import inspect
        from agent.context_compressor import ContextCompressor
        default = inspect.signature(ContextCompressor.__init__).parameters[name].default
        return fallback if default is inspect.Parameter.empty else default
    except Exception:
        return fallback


def _default_threshold_tokens_cap():
    """The cap a fresh agent build installs when the key is absent: DEFAULT_CONFIG's
    ``compression.threshold_tokens``. agent_init reads the MERGED config, so "no key in
    config.yaml" installs that default at construction; key removal here must restore the
    same value, or a live session would diverge from a rebuilt one after the first turn
    (#117093). An explicit ``threshold_tokens: null`` stays ratio-only — the key is present,
    so ``.get`` returns it untouched."""
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    return (DEFAULT_CONFIG.get("compression") or {}).get("threshold_tokens")


def _derived_default_threshold_percent(agent: Any, compression: dict) -> float:
    """Default compaction threshold when ``compression.threshold`` is unset. Mirrors agent_init: ctor
    global default, then per-model resolution (Codex autoraise etc.) via the SAME
    ``_resolve_compression_threshold`` — removing the key restores the model-derived value."""
    try:
        pct = float(_compressor_ctor_default("threshold_percent", 0.50))
    except (TypeError, ValueError):
        pct = 0.50
    try:
        from agent.agent_init import _resolve_compression_threshold
        from agent.auxiliary_client import _compression_threshold_for_model, _is_codex_gpt54_or_gpt55, _is_codex_spark
        model, provider = getattr(agent, "model", "") or "", getattr(agent, "provider", "") or ""
        autoraise_enabled = str(compression.get("codex_gpt55_autoraise", True)).lower() in {"true", "1", "yes"}
        pct, _notice = _resolve_compression_threshold(
            pct, _compression_threshold_for_model(model, provider, allow_codex_gpt55_autoraise=autoraise_enabled),
            model=model, is_codex_autoraise=_is_codex_gpt54_or_gpt55(model, provider) or _is_codex_spark(model, provider),
        )
    except Exception:
        pass
    return pct


# (config key == compressor attr, ctor-default fallback, min_value)
_COMPRESSION_INT_KEYS = (
    ("proactive_prune_tokens", 0, 0),
    ("proactive_prune_min_result_chars", 8000, 0),
    ("proactive_prune_min_reclaim_tokens", 4096, 0),
    ("protect_last_n", 20, 0),
    ("min_tail_user_messages", 1, 1),
)


def _apply_live_compression_config(agent: Any, cfg: dict | None) -> None:
    """Update a live session's compressor in place from config.yaml. Every adopted key has UNSET semantics:
    a removed key restores the normalized default (or model-derived value) through the construction
    path's own derivation — acting only on PRESENT keys would leave stale values active forever.

    Every adopted key has UNSET semantics (#94724 review finding on the merged #95980): removing a key from
    config.yaml restores the normalized default — or the model-derived value — on the next turn, through the
    same derivation the construction path uses (ContextCompressor ctor defaults read off its real signature,
    the Codex threshold autoraise via ``_resolve_compression_threshold``, context-length re-inference via
    the deferred ``get_model_context_length`` resolution).
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    compression = cfg.get("compression") if isinstance(cfg.get("compression"), dict) else {}
    from agent.agent_init import config_context_length_for_runtime, set_config_context_length
    enabled_raw = compression.get("enabled", True)
    agent.compression_enabled = enabled_raw if isinstance(enabled_raw, bool) else str(enabled_raw).lower() in {"true", "1", "yes"}
    agent.codex_responses_native_compaction = is_truthy_value(compression.get("codex_responses_native", False))
    native_threshold_raw = compression.get("codex_responses_compact_threshold", 200_000)
    try:
        if isinstance(native_threshold_raw, bool) or (native_threshold := int(native_threshold_raw)) <= 0:
            raise ValueError
    except (TypeError, ValueError):
        logger.warning("Invalid compression.codex_responses_compact_threshold=%r; using 200000.", native_threshold_raw)
        native_threshold = 200_000
    agent.codex_responses_compact_threshold = native_threshold
    # Absence restores the agent_init/config default (0 = disabled).
    with contextlib.suppress(TypeError, ValueError):
        agent.compression_idle_compact_after_seconds = max(0, int(compression.get("idle_compact_after_seconds", 0) or 0))
    cc = getattr(agent, "context_compressor", None)
    if cc is None:
        return
    # tail_mode: unknown/absent values land on the ctor default ("lean"), matching agent_init.
    default_tail = str(_compressor_ctor_default("tail_mode", "lean"))
    mode = str(compression.get("tail_mode", default_tail) or default_tail).strip().lower()
    cc.tail_mode = mode if mode in ("legacy", "lean") else default_tail
    for key, fallback, min_value in _COMPRESSION_INT_KEYS:
        default = int(_compressor_ctor_default(key, fallback))
        raw = compression.get(key, default)
        with contextlib.suppress(TypeError, ValueError):
            setattr(cc, key, max(min_value, default if raw is None else int(raw)))
    with contextlib.suppress(TypeError, ValueError):
        ratio_raw = compression.get("target_ratio", _compressor_ctor_default("summary_target_ratio", 0.20))
        cc.summary_target_ratio = max(0.10, min(float(ratio_raw), 0.80))
    # Absent or invalid shape (agent_init treats both as empty): stale overrides must stop steering.
    raw_thresholds = compression.get("model_thresholds")
    cc.model_thresholds = {
        str(k): float(v) for k, v in raw_thresholds.items() if isinstance(v, (int, float)) and not isinstance(v, bool)
    } if isinstance(raw_thresholds, dict) else {}
    # threshold: present value wins; absence derives via the agent_init resolution (default + autoraise).
    # resolve_model_threshold returns ``pct`` unchanged when model_thresholds is empty.
    from agent.context_compressor import resolve_model_threshold
    pct: float | None = None
    if "threshold" in compression:
        with contextlib.suppress(TypeError, ValueError):
            pct = float(compression["threshold"])
    if pct is None:
        pct = _derived_default_threshold_percent(agent, compression)
    cc._config_threshold_percent = cc._configured_threshold_percent = pct
    base = cc._base_threshold_percent = resolve_model_threshold(
        getattr(agent, "model", "") or "", cc.model_thresholds, pct, getattr(agent, "provider", "") or "",
    )
    try:
        cc.threshold_percent = cc._effective_threshold_percent(cc.context_length, base)
    except Exception:
        cc.threshold_percent = pct
    # Same scoping rule as construction and the switch path: the pin describes the configured default
    # route, so a session that /model-switched elsewhere must not have it re-applied on a config save
    # (None = absent, invalid, or scoped out).
    new_ctx = config_context_length_for_runtime(agent, cfg)
    if new_ctx is not None:
        # Both cached copies: the compressor's (its own re-resolution) and the agent's
        # (switch/fallback + every display surface). Writing one left the other stale, so the
        # session showed a pinned ceiling while compressing against a different window (#116467).
        set_config_context_length(agent, new_ctx)
        with contextlib.suppress(Exception):
            cc.context_length = new_ctx
    elif getattr(cc, "_config_context_length", None) is not None:
        # model.context_length removed: drop the override and force re-inference from model metadata on
        # next access (construction's deferred resolution); re-applies the small-context floor too.
        set_config_context_length(agent, None)
        cc._resolved_context_length = None
    cc.threshold_tokens_cap = cc._coerce_threshold_tokens_cap(
        compression.get("threshold_tokens", _default_threshold_tokens_cap())
    )
    # Invalidate the cached trigger so the next preflight re-derives from percent/window, then the cap.
    cc._threshold_tokens = cc._tail_token_budget = None


def _sync_agent_compression_with_config(sid: str, session: dict) -> None:
    """Adopt compression.* / model.context_length edits at turn start (messaging gateways rebuild the
    agent on these keys; Desktop/TUI keeps the live compressor, so it must be updated in place).

    Desktop/TUI only synced the model; the live compressor kept the threshold captured at agent creation
    (#95151).
    """
    agent = session.get("agent")
    if agent is None:
        return
    cfg = _load_cfg() or {}
    signature = _tui_compression_config_signature(cfg)
    seen = session.get("config_compression_seen")
    session["config_compression_seen"] = signature
    if signature == seen:
        return
    try:
        _apply_live_compression_config(agent, cfg)
    except Exception as e:
        logger.warning("Could not apply live compression config for %s: %s", sid, e)


def _apply_pending_model_switch(sid: str, session: dict) -> None:
    """Apply a model switch queued (``session["pending_model_switch"]``) while a turn was running. Runs on
    the TURN thread at turn start — nothing in flight — so the in-place swap (client rebuild) is safe. A
    failed switch keeps the current model and never blocks the turn.

    A dropped pick is a warning notice, not an ``error``: clients paint ``error`` as a failed turn
    (Desktop's red retry card under the user's message) while this turn runs normally on the old model.
    The session.info re-sync moves the pill off the pick the session never adopted."""
    pending = session.pop("pending_model_switch", None)
    if not pending or session.get("agent") is None:
        return
    model = pending.get("display_model") or pending["raw"]
    try:
        result = _apply_model_switch(sid, session, pending["raw"], confirm_expensive_model=bool(pending.get("confirm_expensive_model")))
        # Honour the expensive-model confirm: drop the switch rather than spend on a model the user
        # never confirmed.
        if not result.get("confirm_required"):
            return
        detail = result.get("confirm_message") or result.get("warning") or ""
        logger.warning("Queued model switch to %s dropped for session %s: %s", model, sid, detail.split("\n", 1)[0])
        current = getattr(session["agent"], "model", "") or "the current model"
        # One line: the TUI status bar truncates and the Desktop toast collapses newlines. The full
        # guard text comes back in the confirm prompt when the user picks it again.
        text = f"Stayed on {current}: switching to {model} needs confirmation. Pick it again to confirm."
    except Exception as e:
        logger.warning("Queued model switch to %s failed for session %s: %s", model, sid, e)
        text = f"Could not switch model: {e}"
    _emit("notification.show", sid, {
        "text": text.rstrip(), "level": "warn", "kind": "ttl", "ttl_ms": 12000,
        "key": "model_switch.dropped", "id": "model_switch.dropped"})
    _emit_session_info(sid, session)


class CompressionLockHeld(Exception):
    """Raised by _compress_session_history when a concurrent compression_locks row skipped compression."""

    def __init__(self, holder: str | None = None):
        self.holder = holder
        super().__init__(f"Compression lock held: {holder or 'unknown'}")


class CompressionBusy(Exception):
    """Raised by _manual_compress_turn when a turn already holds the session; callers map it to their busy reply."""


@contextlib.contextmanager
def _manual_compress_turn(sid: str, session: dict):
    """Hold the session busy (``running``) for a whole manual compaction: snapshot, LLM summary, commit
    and session-key re-anchor. A prompt.submit arriving meanwhile takes the busy path (demoted to queue
    by ``_session_compression_in_flight``) instead of snapshotting a history version the compaction is
    about to bump, which dropped the turn's reply (#133504). The queued prompt drains on release."""
    with session["history_lock"]:
        if session.get("running"):
            raise CompressionBusy(busy_message("compress", bool(session.get("_manual_compress_active"))))
        session["running"] = session["_manual_compress_active"] = True
    try:
        yield
    finally:
        with session["history_lock"]:
            session.pop("_manual_compress_active", None)
            session["running"] = False
        # The compaction emitted session.info with running=true; close that edge or Desktop latches busy.
        _emit_session_info_for_session(sid, session)
        _drain_queued_prompt("__compress__", sid, session)


def _compress_session_history(
    session: dict, focus_topic: str | None = None, approx_tokens: int | None = None,
    before_messages: list | None = None, history_version: int | None = None,
) -> tuple[int, dict]:
    """Single choke point for all manual-compress routes. ``focus_topic`` is the RAW argument string after
    ``/compress``; the shared core (``agent.conversation_compression_manual``) parses boundary forms
    (``here [N]``, ``up to here``, ``--keep N``) so a partial compress triggers on EVERY route instead of a
    FULL compress focused on the literal text. ``--preview`` returns without touching history."""
    from agent.conversation_compression import finalize_context_engine_compression_notification
    from agent.conversation_compression_manual import (
        AGGRESSIVE_UNSUPPORTED, MIN_MESSAGES, compress_now, parse_compress_args)
    agent = session["agent"]
    # Snapshot under the lock so the LLM-bound compression call does NOT hold history_lock for the
    # request — otherwise prompt.submit etc. block on the dispatcher loop while compaction runs.
    if before_messages is None or history_version is None:
        with session["history_lock"]:
            before_messages, history_version = list(session.get("history", [])), int(session.get("history_version", 0))
    if len(before_messages) < MIN_MESSAGES:
        return 0, _get_usage(agent)
    request = parse_compress_args(focus_topic or "")
    if request.aggressive:
        raise ValueError(AGGRESSIVE_UNSUPPORTED)
    # RPC thread: bind the session cwd, or the boundary prompt rebuild resolves the backend's cwd and
    # persists a prompt every other process then rejects as stale runtime (fresh build, no tools pin).
    tokens = _set_session_context(session.get("session_key") or "", cwd=_session_cwd(session))
    def snapshot_is_current():
        with session["history_lock"]:
            return int(session.get("history_version", 0)) == history_version

    try:
        result = compress_now(agent, before_messages, request, task_id=session.get("session_key") or "default",
                              snapshot_is_current=snapshot_is_current)
    finally:
        _clear_session_context(tokens)
    if result.status == "preview":
        return 0, _get_usage(agent)
    # Lock-skipped: raise so callers surface a clear message instead of "No changes from compression".
    if result.status == "lock_skipped":
        raise CompressionLockHeld(result.lock_holder)
    if result.status != "compressed":
        return 0, _get_usage(agent)
    with session["history_lock"]:
        if int(session.get("history_version", 0)) != history_version:
            # External mutation during compaction — drop the result so we don't clobber concurrent edits.
            finalize_context_engine_compression_notification(agent, committed=False)
            return 0, _get_usage(agent)
        session["history"] = result.after_messages
        session["history_version"] = history_version + 1
    return result.removed, _get_usage(agent)


# A failed post-turn compaction must not refire at every turn boundary —
# each attempt blocks the compression lock for minutes. One retry per
# cooldown window; the next turn's preflight remains the safety net.
_POST_TURN_COMPRESS_FAILURE_COOLDOWN_S = 300.0


def _maybe_post_turn_compress(sid: str, session: dict) -> None:
    """Start background compaction the moment a turn settles over-threshold.

    Previously an over-threshold session only compressed at the NEXT turn's
    preflight — from the user's chair the session "stopped", and
    'Summarizing thread' only began after they typed something. This hook
    runs the same automatic gate as preflight
    (ContextCompressor.should_compress: threshold + summary-failure cooldown
    + anti-thrash) as soon as the turn finishes, and compresses on a daemon
    thread while the session sits idle, so the next user message starts on a
    compacted transcript with no preflight stall.

    A user prompt that races the compaction wins: _compress_session_history
    drops its result on external history mutation, and the racing turn's
    preflight defers on the held compression lock (#69870) instead of
    double-compressing.
    """
    agent = session.get("agent")
    if agent is None:
        return
    if session.get("running") or session.get("queued_prompt"):
        return
    # Codex app-server sessions compact natively — mirror the preflight skip.
    if getattr(agent, "codex_app_server_auto_compaction", None):
        return
    if session.get("_post_turn_compress_running"):
        return
    failed_at = session.get("_post_turn_compress_failed_at")
    if failed_at and (
        time.time() - float(failed_at) < _POST_TURN_COMPRESS_FAILURE_COOLDOWN_S
    ):
        return
    comp = getattr(agent, "context_compressor", None)
    if comp is None or not getattr(comp, "threshold_tokens", 0):
        return
    from agent.model_metadata import estimate_request_tokens_rough

    with session["history_lock"]:
        history = list(session.get("history") or [])
    if not history:
        return
    try:
        tokens = estimate_request_tokens_rough(
            history,
            system_prompt=str(getattr(agent, "_cached_system_prompt", "") or ""),
            tools=getattr(agent, "tools", None) or None,
        )
    except Exception:
        return
    try:
        if not comp.should_compress(tokens):
            return
    except Exception:
        return
    session["_post_turn_compress_running"] = True

    def _worker() -> None:
        # A named-profile session's compaction must resolve config (thresholds,
        # auxiliary compression model) and secrets from ITS profile, not the
        # root default. The prompt path installs this scope per turn; a
        # background worker running off-turn has to install it itself.
        home_token = None
        secret_token = None
        profile_home = session.get("profile_home")
        try:
            if profile_home:
                home_token = set_hermes_home_override(profile_home)
                try:
                    from agent.secret_scope import (
                        build_profile_secret_scope,
                        set_secret_scope,
                    )

                    secret_token = set_secret_scope(
                        build_profile_secret_scope(Path(profile_home))
                    )
                except Exception:
                    secret_token = None
            with _sessions_lock:
                if _sessions.get(sid) is not session:
                    return
            if session.get("running") or session.get("queued_prompt"):
                return
            logger.info(
                "post-turn compression starting: sid=%s session=%s "
                "~%s tokens >= %s threshold",
                sid,
                getattr(agent, "session_id", "") or "",
                f"{tokens:,}",
                f"{int(getattr(comp, 'threshold_tokens', 0) or 0):,}",
            )
            try:
                _emit(
                    "status.update",
                    sid,
                    {
                        "kind": "compacting",
                        "text": (
                            "🗜️ Compacting context in the background — "
                            "summarizing earlier conversation…"
                        ),
                    },
                )
            except Exception:
                pass
            removed, _usage = _compress_session_history(session)
            _sync_session_key_after_compress(
                sid,
                session,
                clear_pending_title=False,
                restart_slash_worker=True,
            )
            session.pop("_post_turn_compress_failed_at", None)
            logger.info(
                "post-turn compression done: sid=%s removed=%d messages",
                sid,
                removed,
            )
            try:
                _emit_settled_session_info(sid, session, agent)
            except Exception:
                pass
        except CompressionLockHeld:
            logger.info(
                "post-turn compression skipped: lock held sid=%s", sid
            )
        except Exception:
            session["_post_turn_compress_failed_at"] = time.time()
            logger.warning(
                "post-turn compression failed sid=%s", sid, exc_info=True
            )
        finally:
            if secret_token is not None:
                try:
                    from agent.secret_scope import reset_secret_scope

                    reset_secret_scope(secret_token)
                except Exception:
                    pass
            if home_token is not None:
                reset_hermes_home_override(home_token)
            session.pop("_post_turn_compress_running", None)

    threading.Thread(
        target=_worker, daemon=True, name=f"post-turn-compress-{sid}"
    ).start()


def _sync_session_key_after_compress(
    sid: str, session: dict, *, clear_pending_title: bool = True, restart_slash_worker: bool = True
) -> None:
    """Re-anchor the gateway-side ``session_key`` when _compress_context rotates ``agent.session_id``;
    otherwise approval routing, slash worker, DB lookups and yolo state keep targeting the ended parent.
    ``clear_pending_title``: True for manual /compress (title belongs to the old session), False for
    post-turn auto-compression. ``restart_slash_worker``: False only when the caller manages the worker."""
    agent = session.get("agent")
    new_session_id = getattr(agent, "session_id", None) or ""
    old_key = session.get("session_key", "") or ""
    if not new_session_id or new_session_id == old_key:
        return
    if not _transfer_active_session_slot(sid, session, new_session_id=new_session_id):
        logger.warning(
            "Compression session lease did not re-anchor: sid=%s old_session_id=%s new_session_id=%s",
            sid, old_key, new_session_id,
        )
    # Even if the approval module fails to import, anchor session_key on the continuation id.
    session["session_key"] = new_session_id
    with contextlib.suppress(Exception):
        from tools import approval
        with contextlib.suppress(Exception):
            approval.unregister_gateway_notify(old_key)
        from tools.approval_yolo import transfer_session_yolo
        transfer_session_yolo(old_key, new_session_id)
        with contextlib.suppress(Exception):
            approval.register_gateway_notify(new_session_id, lambda data: _emit_approval_request(sid, data))
    # Invalidate any in-flight ``_drain_queued_prompt`` claim taken under the pre-rotation key: a raced
    # drain must not dispatch on the continuation (its envelope is restored to the queue).
    session["_queued_prompt_generation"] = int(session.get("_queued_prompt_generation", 0)) + 1
    if clear_pending_title:
        session["pending_title"] = None
    if restart_slash_worker:
        with contextlib.suppress(Exception):
            _restart_slash_worker(sid, session)


def register(server) -> None:
    """Publish this module's helpers + handlers onto ``server``, rebound to its globals."""
    bind_module(globals(), server, skip=("_",))
