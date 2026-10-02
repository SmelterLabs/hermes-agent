"""Activity from before admission must not keep a later turn alive."""
import io
import json
from types import SimpleNamespace

import pytest
from tui_gateway.compute_host import ComputeHost

@pytest.mark.parametrize('generation,stamp,expected_fresh', [
    (7, 1000.0, False),  # prior activity at the identical clock tick
    (8, 1000.0, True),   # new activity at the identical clock tick
    (8, 999.0, False),   # an older snapshot is never current progress
    (8, None, False),   # missing activity is never current progress
    (None, 1000.0, False),  # legacy agent without the activity counter
    (None, 1001.0, True),
    (8, 1001.0, True),
])
def test_turn_activity_requires_new_progress(generation, stamp, expected_fresh):
    agent = SimpleNamespace(
        _turn_liveness_activity_generation=generation,
        get_activity_summary=lambda: {'last_activity_at': stamp, 'seconds_since_activity': 0.1},
    )
    session = {'agent': agent, '_compute_host_activity_generation_at_start': 7}
    output = io.StringIO()
    host = ComputeHost(stdout=output, heartbeat_secs=0)
    try:
        host._emit_turn_activity('s', session, 'new-turn', 1000.0)
        payload = json.loads(output.getvalue())['message']['params']
        assert (payload['activity_ns'] is not None) is expected_fresh
    finally:
        host.close()
