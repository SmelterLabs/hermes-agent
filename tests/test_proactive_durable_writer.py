"""Tests for the Proactive Clyde durable primitives on SessionDB.

Covers the two additive methods used by Clyde's origin-side durable writer:
``ensure_proactive_session`` (deterministic canonical session) and
``append_assistant_message_idempotent`` (standalone assistant append that is
idempotent on ``dedupe_key``). All tests run against a private temp state.db.
"""

import math

import pytest

from hermes_state import CLYDE_PROACTIVE_SESSION_ID, SessionDB


@pytest.fixture()
def db(tmp_path):
    db_path = tmp_path / "proactive_state.db"
    session_db = SessionDB(db_path=db_path)
    yield session_db
    session_db.close()


def _messages(db, session_id):
    with db._read_ctx() as conn:
        return conn.execute(
            "SELECT id, role, content, platform_message_id, active, "
            "display_kind, display_metadata "
            "FROM messages WHERE session_id = ? ORDER BY id",
            (session_id,),
        ).fetchall()


def _session_rows(db, session_id):
    with db._read_ctx() as conn:
        return conn.execute(
            "SELECT id FROM sessions WHERE id = ?", (session_id,)
        ).fetchall()


def test_ensure_proactive_session_is_deterministic_and_single_row(db):
    first = db.ensure_proactive_session()
    second = db.ensure_proactive_session()
    third = db.ensure_proactive_session("proactive")

    assert first == CLYDE_PROACTIVE_SESSION_ID
    assert first == second == third
    # Exactly one session row despite repeated ensures.
    assert len(_session_rows(db, CLYDE_PROACTIVE_SESSION_ID)) == 1


def test_committed_entry_is_standalone_assistant_row(db):
    session_id = db.ensure_proactive_session()
    row_id = db.append_assistant_message_idempotent(
        session_id, "morning brief body", "dedupe-A"
    )

    assert isinstance(row_id, int) and row_id > 0
    rows = _messages(db, session_id)
    assert len(rows) == 1
    assert rows[0]["role"] == "assistant"
    assert rows[0]["content"] == "morning brief body"
    assert rows[0]["platform_message_id"] == "dedupe-A"
    assert rows[0]["active"] == 1
    assert rows[0]["display_kind"] is None
    assert rows[0]["display_metadata"] is None


def test_idempotent_replay_same_id_single_row(db):
    session_id = db.ensure_proactive_session()
    first = db.append_assistant_message_idempotent(
        session_id, "body one", "dedupe-B"
    )
    # Same dedupe_key, even with different content, must NOT create a new row.
    second = db.append_assistant_message_idempotent(
        session_id, "body one replayed", "dedupe-B"
    )

    assert first == second
    rows = _messages(db, session_id)
    assert len(rows) == 1
    # The original content is preserved; replay does not overwrite.
    assert rows[0]["content"] == "body one"


def test_distinct_dedupe_keys_create_distinct_rows(db):
    session_id = db.ensure_proactive_session()
    id_a = db.append_assistant_message_idempotent(session_id, "a", "key-a")
    id_b = db.append_assistant_message_idempotent(session_id, "b", "key-b")

    assert id_a != id_b
    assert len(_messages(db, session_id)) == 2


def test_explicit_timestamp_is_honoured(db):
    session_id = db.ensure_proactive_session()
    row_id = db.append_assistant_message_idempotent(
        session_id, "timed", "key-ts", timestamp=1_700_000_000.0
    )
    with db._read_ctx() as conn:
        ts = conn.execute(
            "SELECT timestamp FROM messages WHERE id = ?", (row_id,)
        ).fetchone()[0]
    assert ts == 1_700_000_000.0


def test_marked_event_round_trips_through_message_reader(db):
    session_id = db.ensure_proactive_session()
    metadata = {
        "source": "proactive-monitor",
        "severity": "warning",
        "target": "daily-brief",
    }

    row_id = db.append_assistant_message_idempotent(
        session_id,
        "monitor found a change",
        "event-round-trip",
        display_kind="event",
        display_metadata=metadata,
    )

    message = db.get_messages(session_id)[0]
    assert message["id"] == row_id
    assert message["display_kind"] == "event"
    assert message["display_metadata"] == metadata


def test_idempotent_replay_preserves_original_event_metadata(db):
    session_id = db.ensure_proactive_session()
    original = {"source": "monitor-a", "severity": "info"}
    replacement = {"source": "monitor-b", "severity": "error"}

    first = db.append_assistant_message_idempotent(
        session_id,
        "original body",
        "event-replay",
        display_kind="event",
        display_metadata=original,
    )
    second = db.append_assistant_message_idempotent(
        session_id,
        "replacement body",
        "event-replay",
        display_kind="event",
        display_metadata=replacement,
    )

    assert first == second
    messages = db.get_messages(session_id)
    assert len(messages) == 1
    assert messages[0]["content"] == "original body"
    assert messages[0]["display_kind"] == "event"
    assert messages[0]["display_metadata"] == original


@pytest.mark.parametrize(
    "metadata",
    [
        ["not", "an", "object"],
        {"source": 7, "severity": "info"},
        {"source": "monitor", "severity": []},
        {"source": "", "severity": "warning"},
        {"source": "monitor", "extra": {"not-json": object()}},
    ],
)
def test_malformed_event_metadata_is_ignored_without_losing_message(db, metadata):
    session_id = db.ensure_proactive_session()

    row_id = db.append_assistant_message_idempotent(
        session_id,
        "durable body",
        f"malformed-{id(metadata)}",
        display_kind="event",
        display_metadata=metadata,
    )

    message = db.get_messages(session_id)[0]
    assert message["id"] == row_id
    assert message["content"] == "durable body"
    assert message["display_kind"] == "event"
    assert message["display_metadata"] is None


def test_circular_event_metadata_is_dropped_without_losing_message(db):
    session_id = db.ensure_proactive_session()
    metadata = {"source": "monitor"}
    metadata["nested"] = metadata

    row_id = db.append_assistant_message_idempotent(
        session_id,
        "circular metadata must not defeat durability",
        "circular-input",
        display_kind="event",
        display_metadata=metadata,
    )

    row = _messages(db, session_id)[0]
    assert row["id"] == row_id
    assert row["content"] == "circular metadata must not defeat durability"
    assert row["display_kind"] == "event"
    assert row["display_metadata"] is None


@pytest.mark.parametrize("nonfinite", [math.nan, math.inf, -math.inf])
def test_nonfinite_event_metadata_is_dropped_without_losing_message(db, nonfinite):
    session_id = db.ensure_proactive_session()

    row_id = db.append_assistant_message_idempotent(
        session_id,
        "non-finite metadata must not escape",
        f"nonfinite-{nonfinite!r}",
        display_kind="event",
        display_metadata={"source": "monitor", "value": nonfinite},
    )

    row = _messages(db, session_id)[0]
    assert row["id"] == row_id
    assert row["content"] == "non-finite metadata must not escape"
    assert row["display_kind"] == "event"
    assert row["display_metadata"] is None
