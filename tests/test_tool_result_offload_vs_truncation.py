"""Truncation caps must respect which tools deepagents actually offloads.

deepagents' ``FilesystemMiddleware`` offloads an oversized tool result to the
backend (>20k tokens by default) and replaces it with a recovery pointer.
Truncation destroys the tail outright.

But offloading is NOT universal: deepagents ships

    TOOLS_EXCLUDED_FROM_EVICTION = ("ls", "glob", "grep", "read_file",
                                    "edit_file", "write_file", "delete")

so ``read_file`` never gets a pointer — Nova's char cap is the only bound on it.
Two opposite requirements follow:

* evictable tools (``execute``, ``shell``, ``fetch_url``) — cap ABOVE the offload
  threshold, so the result survives to the recoverable path;
* never-evicted tools (``read_file``) — keep a TIGHT cap, because raising it
  would let an unbounded result into context with nothing to catch it.
"""

from __future__ import annotations

from novacode_cli.tracking.file_tracker import (
    OFFLOAD_THRESHOLD_CHARS,
    RESULT_LIMITS,
    TOOLS_EXCLUDED_FROM_EVICTION,
    _has_offload_pointer,
    truncate_tool_result,
)

# Evictable: deepagents will offload these, so let them reach the threshold.
_EVICTABLE_BIG_TOOLS = ("shell", "execute", "fetch_url", "default")


def test_evictable_tools_are_capped_above_the_offload_threshold() -> None:
    for tool in _EVICTABLE_BIG_TOOLS:
        assert tool not in TOOLS_EXCLUDED_FROM_EVICTION, f"{tool} should be evictable"
        assert RESULT_LIMITS[tool] > OFFLOAD_THRESHOLD_CHARS, (
            f"{tool} truncates at {RESULT_LIMITS[tool]} chars, below the "
            f"{OFFLOAD_THRESHOLD_CHARS}-char offload threshold — the recoverable "
            "offload would never fire for it"
        )


def test_read_file_cap_stays_tight_because_it_is_never_offloaded() -> None:
    """The regression this prevents.

    ``read_file`` is in deepagents' exclusion list, so raising its cap to reach
    the offload threshold would let a huge result into context with NO safety
    net at all — strictly worse than truncating it.
    """
    assert "read_file" in TOOLS_EXCLUDED_FROM_EVICTION, (
        "if deepagents' exclusion list changed, this cap needs revisiting"
    )
    assert RESULT_LIMITS["read_file"] <= OFFLOAD_THRESHOLD_CHARS, (
        "read_file is never offloaded, so its cap must bound the result itself"
    )


def test_offload_threshold_matches_deepagents_default() -> None:
    """20k tokens at the 4-chars/token heuristic deepagents uses."""
    assert OFFLOAD_THRESHOLD_CHARS == 20_000 * 4


def test_self_truncating_tools_keep_tight_caps() -> None:
    for tool in ("grep", "glob", "ls"):
        assert RESULT_LIMITS[tool] <= OFFLOAD_THRESHOLD_CHARS


def test_exclusion_set_matches_deepagents() -> None:
    """Pinned deliberately: a drift here silently changes which caps are safe."""
    assert TOOLS_EXCLUDED_FROM_EVICTION == frozenset(
        {"ls", "glob", "grep", "read_file", "edit_file", "write_file", "delete"}
    )


def test_recognises_a_deepagents_offload_notice() -> None:
    notice = (
        "Tool result too large, the result of this tool call abc123 was saved in "
        "the filesystem at this path: /large_tool_results/abc123\n\n"
        "You can read the result from the filesystem by using the read_file tool.\n"
    )
    assert _has_offload_pointer(notice)


def test_raw_result_is_not_mistaken_for_a_pointer() -> None:
    assert not _has_offload_pointer("line 1\nline 2\n" * 5_000)


def test_pointer_mentioning_a_path_without_the_notice_is_not_a_pointer() -> None:
    """A log that merely mentions the directory must still be truncatable."""
    assert not _has_offload_pointer(
        "grep results:\n/large_tool_results/old.txt:12: some line\n" * 500
    )


def test_truncation_still_works_for_ordinary_large_results() -> None:
    big = "x" * 200_000
    out, was_truncated = truncate_tool_result("grep", big)
    assert was_truncated
    assert len(out) < len(big)


def test_read_file_result_is_still_bounded() -> None:
    """read_file has no offload net, so truncation must still bite."""
    big = "y" * 100_000
    out, was_truncated = truncate_tool_result("read_file", big)
    assert was_truncated, "read_file must be bounded — nothing else will bound it"
    assert len(out) < len(big)


def test_execute_result_reaches_the_offload_threshold() -> None:
    """A 100k-char execute result must NOT be cut here — it is evictable."""
    _out, was_truncated = truncate_tool_result("execute", "z" * 100_000)
    assert not was_truncated, "execute was truncated before it could be offloaded"
