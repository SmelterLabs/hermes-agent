"""Behind-count recovery via the GitHub compare API (source_check.py).

The class of bug: any code path that knows two tip SHAs but has no local
history to count across (shallow installer clones, ls-remote-only probes)
used to fabricate a count of ``1`` — the UI then rendered "+1" / "1 commit
behind" forever while the real distance grew (#84591: 61 commits behind,
indicator said 1). The fix has two halves:

1. Honesty: never fabricate a number. Uncountable = UPDATE_AVAILABLE_NO_COUNT
   sentinel (CLI) / null (desktop), rendered as a generic "update available".
2. Accuracy: recover the exact count via GitHub's compare API, which knows
   the full graph regardless of local clone depth.
"""

import json
from unittest.mock import patch

import pytest

from hermes_cli import source_check

SHA_A = "a" * 40
SHA_B = "b" * 40


class _FakeResponse:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self, limit=None):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _patch_urlopen(payload):
    return patch(
        "urllib.request.urlopen",
        return_value=_FakeResponse(json.dumps(payload).encode()),
    )


# ---------------------------------------------------------------------------
# _github_compare_behind
# ---------------------------------------------------------------------------


def test_compare_behind_returns_ahead_by():
    with _patch_urlopen({"ahead_by": 61, "status": "ahead"}):
        assert source_check._github_compare_behind(SHA_A, SHA_B) == 61


def test_compare_behind_zero_means_local_ahead():
    with _patch_urlopen({"ahead_by": 0, "status": "behind"}):
        assert source_check._github_compare_behind(SHA_A, SHA_B) == 0


def test_compare_behind_rejects_short_shas_without_network():
    with patch("urllib.request.urlopen") as mock_open:
        assert source_check._github_compare_behind("abc123", SHA_B) is None
        assert source_check._github_compare_behind(SHA_A, "") is None
        assert source_check._github_compare_behind(None, SHA_B) is None
    mock_open.assert_not_called()


def test_compare_behind_network_failure_returns_none():
    with patch("urllib.request.urlopen", side_effect=OSError("offline")):
        assert source_check._github_compare_behind(SHA_A, SHA_B) is None


@pytest.mark.parametrize(
    "payload",
    [
        {"status": "diverged"},  # no ahead_by
        {"ahead_by": -3},  # negative
        {"ahead_by": "12"},  # wrong type
        {"ahead_by": True},  # bool masquerading as int
        [],  # wrong shape
    ],
)
def test_compare_behind_rejects_malformed_payloads(payload):
    with _patch_urlopen(payload):
        assert source_check._github_compare_behind(SHA_A, SHA_B) is None


# ---------------------------------------------------------------------------
# _behind_count: local-only HEAD recovers its count from the upstream base
#
# An overlay branch rebased onto upstream has a HEAD that exists nowhere on
# GitHub, so compare 404s for it forever -- and because every update re-creates
# that local-only HEAD, the no-count sentinel would become permanent (the badge
# reads a bare "update" instead of "+N"). The merge-base with origin/<branch> is
# an upstream commit, so base...target is the honest distance.
# ---------------------------------------------------------------------------

SHA_BASE = "c" * 40


def _checkout(tmp_path, head=SHA_A):
    return source_check._Checkout(
        root=tmp_path,
        git="git",
        embedded=None,
        head=head,
        current_branch="local/overlay",
        origin="https://github.com/nousresearch/hermes-agent.git",
        repository="nousresearch/hermes-agent",
        dirty=False,
    )


def _no_local_ancestry():
    """HEAD is not a descendant of target, so the early up-to-date arm cannot fire."""
    return patch.object(source_check, "_git_ok", return_value=False)


def test_behind_count_local_only_head_counts_from_upstream_base(tmp_path):
    def compare(current, target, repository):
        # GitHub has never seen the local-only HEAD; it answers only for the base.
        return {"ahead_by": 2305} if current == SHA_BASE else None

    with _no_local_ancestry(), \
            patch.object(source_check, "_github_compare", side_effect=compare), \
            patch.object(source_check, "_git_stdout", return_value=SHA_BASE):
        behind, _ = source_check._behind_count(_checkout(tmp_path), SHA_B, branch="main")

    assert behind == 2305


def test_behind_count_local_base_on_upstream_tip_is_up_to_date(tmp_path):
    with _no_local_ancestry(), \
            patch.object(source_check, "_github_compare", return_value=None), \
            patch.object(source_check, "_git_stdout", return_value=SHA_B):
        behind, commits = source_check._behind_count(_checkout(tmp_path), SHA_B, branch="main")

    assert (behind, commits) == (0, [])


def test_behind_count_keeps_sentinel_when_the_base_compare_also_fails(tmp_path):
    with _no_local_ancestry(), \
            patch.object(source_check, "_github_compare", return_value=None), \
            patch.object(source_check, "_git_stdout", return_value=SHA_BASE):
        behind, _ = source_check._behind_count(_checkout(tmp_path), SHA_B, branch="main")

    assert behind == source_check.UPDATE_AVAILABLE_NO_COUNT


def test_behind_count_keeps_sentinel_when_no_merge_base_resolves(tmp_path):
    with _no_local_ancestry(), \
            patch.object(source_check, "_github_compare", return_value=None), \
            patch.object(source_check, "_git_stdout", return_value=None):
        behind, _ = source_check._behind_count(_checkout(tmp_path), SHA_B, branch="main")

    assert behind == source_check.UPDATE_AVAILABLE_NO_COUNT


def test_behind_count_base_lookup_uses_the_selected_branch(tmp_path):
    seen = {}

    def git_stdout(args, **kwargs):
        seen["args"] = args
        return SHA_BASE

    def compare(current, target, repository):
        return {"ahead_by": 7} if current == SHA_BASE else None

    with _no_local_ancestry(), \
            patch.object(source_check, "_github_compare", side_effect=compare), \
            patch.object(source_check, "_git_stdout", side_effect=git_stdout):
        behind, _ = source_check._behind_count(_checkout(tmp_path), SHA_B, branch="release")

    assert behind == 7
    assert seen["args"] == ["merge-base", "HEAD", "origin/release"]
