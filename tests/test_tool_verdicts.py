"""The verdict scorer: token estimate, state fitting, batching, the endpoint.

Ported from ``fast-jev-compaction``'s vitest suite, which is where these
behaviours were pinned against the original library. Two of its cases have no
counterpart here (dropping tool *calls*, and rewriting a whole transcript),
because Nova clears results only -- see the module docstring.
"""

from __future__ import annotations

import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from novacode_cli.agents.tool_verdicts import (
    MAX_QUESTIONS_PER_REQUEST,
    MAX_VERDICT_WINDOW,
    FakeDecisionClient,
    SystemOneClient,
    ToolVerdictCache,
    VerdictScorer,
    batch_candidates,
    build_verdict_state,
    collect_candidates,
    estimate_tokens,
    goal_from_messages,
    parse_answers,
    questions_for,
    result_key,
    stale_from_noul,
    truncate,
)


def _turn(n: int, size: int = 100, tool: str = "read_file") -> list:
    """One tool turn: ask -> call -> result."""
    return [
        HumanMessage(content=f"ask {n}", id=f"h{n}"),
        AIMessage(
            content="",
            id=f"a{n}",
            tool_calls=[
                {"id": f"c{n}", "name": tool, "args": {"file_path": f"/src/f{n}.py"}}
            ],
        ),
        ToolMessage(
            content="x" * size, tool_call_id=f"c{n}", name=tool, id=f"t{n}"
        ),
    ]


def _history(turns: int = 3, size: int = 100) -> list:
    messages: list = []
    for n in range(1, turns + 1):
        messages.extend(_turn(n, size))
    return messages


# ── token estimate ─────────────────────────────────────────────────────────


def test_token_estimate_charges_words_digits_and_symbols_separately() -> None:
    """The exact cases pinned against the library's estimator."""
    assert estimate_tokens("") == 0
    assert estimate_tokens("hello world") == 2
    assert estimate_tokens("internationalization") == 4
    assert estimate_tokens("12345678") == 4


def test_truncate_keeps_short_text_and_marks_long_text() -> None:
    assert truncate("abc", 10) == "abc"
    assert truncate("abcdef", 4) == "abc\u2026"
    assert truncate("abc", 0) == "\u2026"


# ── candidates ─────────────────────────────────────────────────────────────


def test_candidates_pair_each_result_with_its_call() -> None:
    candidates = collect_candidates(_history(3), preserve_recent_results=5)
    assert [candidate.label for candidate in candidates] == ["t1", "t2", "t3"]
    assert [candidate.call_id for candidate in candidates] == ["c1", "c2", "c3"]
    assert all(candidate.tool == "read_file" for candidate in candidates)
    assert all(candidate.content_chars == 100 for candidate in candidates)


def test_the_newest_results_are_pinned() -> None:
    """A pinned result is still described in the state, but never a target."""
    candidates = collect_candidates(_history(3), preserve_recent_results=1)
    assert [candidate.pinned for candidate in candidates] == [False, False, True]


def test_a_result_without_a_call_is_not_a_candidate() -> None:
    orphan = [ToolMessage(content="x", tool_call_id="nope", name="bash")]
    assert collect_candidates(orphan) == []


def test_one_noul_question_per_result() -> None:
    """Results only, so the call question the library also asks is dropped."""
    candidate = collect_candidates(_history(1))[0]
    questions = questions_for(candidate)
    assert list(questions) == ["result_t1"]
    assert questions["result_t1"]["type"] == "noul"
    assert "read_file" in questions["result_t1"]["instructions"]


# ── batching ───────────────────────────────────────────────────────────────


def test_everything_fits_in_one_request_when_the_state_is_small() -> None:
    candidates = collect_candidates(_history(10))
    assert len(batch_candidates(candidates, 100, max_request_tokens=30_000)) == 1


def test_questions_split_when_the_state_leaves_little_room() -> None:
    candidates = collect_candidates(_history(10))
    batches = batch_candidates(candidates, 29_600, max_request_tokens=30_000)
    assert len(batches) > 1
    assert [c.call_id for batch in batches for c in batch] == [
        c.call_id for c in candidates
    ]


def test_a_single_question_that_cannot_fit_raises() -> None:
    candidates = collect_candidates(_history(10))
    with pytest.raises(ValueError, match="no room"):
        batch_candidates(candidates, 29_990, max_request_tokens=30_000)


def test_a_request_carries_at_most_the_endpoint_question_limit() -> None:
    """Tev1 answers 64 questions per request, so 100 results need two batches.

    ``window=None`` because the batching rule is independent of the verdict
    window; with the default bound there would be fewer than 64 candidates.
    """
    candidates = collect_candidates(_history(100), window=None, preserve_recent_results=0)
    batches = batch_candidates(candidates, 0, max_request_tokens=1_000_000)
    assert len(batches) == 2
    for batch in batches:
        assert len(batch) <= MAX_QUESTIONS_PER_REQUEST
    assert sum(len(batch) for batch in batches) == 100


# ── state ──────────────────────────────────────────────────────────────────


def test_a_small_history_fits_without_shrinking() -> None:
    messages = _history(3)
    fitted = build_verdict_state(messages, collect_candidates(messages))
    assert fitted.stage == "full"
    assert fitted.tokens <= 1_000
    entries = fitted.state["history"]
    assert [entry["role"] for entry in entries] == ["user", "assistant"] * 3


def test_a_large_history_shrinks_and_still_fits() -> None:
    """The stage is diagnostic; the invariant is that it fits or raises."""
    messages = [HumanMessage(content="y" * 40_000)] + _history(4, size=2_000)
    fitted = build_verdict_state(messages, collect_candidates(messages))
    assert fitted.stage != "full"
    assert fitted.tokens <= 1_000


def test_a_history_that_cannot_fit_raises_rather_than_guessing() -> None:
    messages = _history(3)
    with pytest.raises(ValueError, match="too large"):
        build_verdict_state(messages, collect_candidates(messages), max_state_tokens=10)


def test_the_state_omits_tool_output_but_notes_its_size() -> None:
    """The model judges from the skeleton: it must not receive the payload."""
    messages = _history(1, size=4_000)
    fitted = build_verdict_state(messages, collect_candidates(messages))
    encoded = json.dumps(fitted.state)
    assert "x" * 100 not in encoded
    assert "4000 chars (omitted)" in encoded


def test_goal_defaults_to_the_last_three_user_prompts() -> None:
    messages = _history(4)
    assert goal_from_messages(messages).splitlines() == ["ask 2", "ask 3", "ask 4"]


def test_abridging_runs_oldest_first_so_the_newest_text_survives() -> None:
    """The pinned tail must lose its text last, not first."""
    messages = [
        HumanMessage(content="old " * 500, id="h0"),
        HumanMessage(content="new " * 50, id="h1"),
    ]
    # Calibrate against the estimator instead of guessing a budget: one token
    # under what the unabridged form needs, so exactly one abridgement fits.
    full = build_verdict_state(messages, [], max_state_tokens=10**9)
    assert full.stage == "full"

    fitted = build_verdict_state(
        messages, [], preserve_recent_messages=1, max_state_tokens=full.tokens - 1
    )
    history = fitted.state["history"]
    assert fitted.stage == "texts abridged"
    assert "chars omitted" in history[0]["text"]
    assert history[1]["text"].startswith("new new")


# ── verdicts ───────────────────────────────────────────────────────────────


def test_stale_is_below_the_threshold() -> None:
    assert stale_from_noul(0.1) is True
    assert stale_from_noul(0.9) is False
    assert stale_from_noul(0.5) is False, "at the threshold the result survives"
    assert stale_from_noul(0.5, keep_threshold=0.6) is True


def test_answers_are_read_by_name_and_must_be_numeric() -> None:
    assert parse_answers({"answers": {"q": {"noul": 0.4}}}) == {"q": 0.4}
    assert parse_answers({"answers": {}}) == {}, "no answers means no verdicts"
    for broken in (
        {},
        {"answers": {"q": {"noul": "high"}}},
        {"answers": {"q": {"noul": None}}},
        {"answers": {"q": 0.4}},
        {"answers": {"q": {"noul": float("inf")}}},
    ):
        with pytest.raises(ValueError):
            parse_answers(broken)


# ── the endpoint ───────────────────────────────────────────────────────────


class _Response:
    def __init__(self, status_code: int = 200, payload: object = None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self) -> object:
        return self._payload


class _HTTP:
    """Stands in for ``httpx.Client``, recording what was posted."""

    def __init__(self, response: _Response) -> None:
        self.response = response
        self.calls: list[dict] = []

    def post(self, url, json=None, headers=None):  # noqa: A002 - httpx's kwarg name
        self.calls.append({"url": url, "json": json, "headers": headers})
        return self.response


def test_the_client_posts_the_state_and_questions_it_was_given() -> None:
    http = _HTTP(_Response(payload={"answers": {"result_t1": {"noul": 0.25}}}))
    client = SystemOneClient(endpoint="http://x/v1/systemone", model="tev1", client=http)

    answers = client.ask({"context": "c", "history": []}, questions_for(
        collect_candidates(_history(1))[0]
    ))

    assert answers == {"result_t1": 0.25}
    sent = http.calls[0]
    assert sent["url"] == "http://x/v1/systemone"
    assert sent["json"]["model"] == "tev1"
    assert sent["json"]["state"] == {"context": "c", "history": []}
    assert "Bearer" in sent["headers"]["authorization"]


def test_the_client_raises_on_a_failed_request() -> None:
    http = _HTTP(_Response(status_code=404, text="404 page not found"))
    client = SystemOneClient(client=http)
    with pytest.raises(ValueError, match="404"):
        client.ask({}, {})


def test_the_fake_client_answers_every_question() -> None:
    fake = FakeDecisionClient(0.3)
    answers = fake.ask({}, {"a": {}, "b": {}})
    assert answers == {"a": 0.3, "b": 0.3}
    assert len(fake.calls) == 1


# ── the cache ──────────────────────────────────────────────────────────────


def _result(call_id: str = "c1", content: str = "payload") -> ToolMessage:
    return ToolMessage(content=content, tool_call_id=call_id, name="read_file")


def test_cache_round_trips_a_verdict(tmp_path) -> None:
    cache = ToolVerdictCache(path=tmp_path / "tool_verdicts.json")
    message = _result()
    assert cache.get(message) is None, "unscored means no verdict, not zero"
    cache.put(message, 0.2)

    assert cache.get(message) == 0.2
    assert cache.stale(message, keep_threshold=0.5) is True
    assert ToolVerdictCache(path=tmp_path / "tool_verdicts.json").get(message) == 0.2


def test_a_reused_id_with_new_bytes_is_not_scored_by_the_old_verdict(tmp_path) -> None:
    """The key is the content, not the id: ids are only unique per session."""
    cache = ToolVerdictCache(path=tmp_path / "tool_verdicts.json")
    cache.put(_result(content="first"), 0.9)
    assert cache.get(_result(content="second")) is None


def test_an_unreadable_cache_degrades_to_no_verdicts(tmp_path) -> None:
    path = tmp_path / "tool_verdicts.json"
    (tmp_path / "tool_verdicts-1.json").write_text("{not json\n", encoding="utf-8")
    cache = ToolVerdictCache(path=path)
    assert cache.get(_result()) is None
    cache.begin()
    cache.put(_result(), 0.1)
    assert cache.get(_result()) == 0.1


def test_a_cache_that_cannot_be_written_does_not_raise(tmp_path) -> None:
    """A read-only home must slow the feature, never break a turn."""
    blocked = tmp_path / "file-in-the-way"
    blocked.write_text("not a directory", encoding="utf-8")
    cache = ToolVerdictCache(path=blocked / "tool_verdicts.json")
    cache.put(_result(), 0.1)
    assert cache.get(_result()) == 0.1, "the in-memory map still answers"


def test_a_new_cycle_is_read_newest_first(tmp_path) -> None:
    """A rotation must not hide what the previous cycle already answered."""
    path = tmp_path / "tool_verdicts.json"
    cache = ToolVerdictCache(path=path)
    cache.begin()
    cache.put(_result("c1", "one"), 0.9)
    cache.begin()
    cache.put(_result("c2", "two"), 0.1)

    fresh = ToolVerdictCache(path=path)
    assert fresh.get(_result("c1", "one")) == 0.9, "the older cycle is still read"
    assert fresh.get(_result("c2", "two")) == 0.1
    assert [cycle for cycle, _ in fresh.cycle_files()] == [2, 1]


def test_old_cycles_are_pruned_to_bound_the_store(tmp_path) -> None:
    """An agent directory outlives a session, so the store cannot grow forever.

    Cycles, not pages: a cycle holds at most ``window`` verdicts, so the budget
    converts to a number of cycles. The first version of this rule compared
    those two units and pruned nothing at all -- which this test caught, because
    it asserts cycles really disappear.
    """
    from novacode_cli.agents.tool_verdicts import MAX_CACHED_VERDICTS

    path = tmp_path / "tool_verdicts.json"
    cache = ToolVerdictCache(path=path, window=1)  # one verdict per cycle
    cycles = MAX_CACHED_VERDICTS + 20
    for cycle in range(1, cycles + 1):
        cache.cycle = cycle
        cache.put(_result(f"c{cycle}", f"payload {cycle}"), 0.5)

    remaining = [cycle for cycle, _ in cache.cycle_files()]
    assert max(remaining) == cycles, "the newest cycle always survives"
    assert len(remaining) <= MAX_CACHED_VERDICTS, "the store stays bounded"
    assert min(remaining) > 1, "the oldest cycles are gone"
    assert cache.get(_result(f"c{cycles}", f"payload {cycles}")) == 0.5


# ── the window ─────────────────────────────────────────────────────────────


def test_only_the_newest_window_is_scored() -> None:
    """The state for a whole session does not fit, so the window is bounded."""
    candidates = collect_candidates(_history(40), preserve_recent_results=5)
    assert len(candidates) == MAX_VERDICT_WINDOW
    assert candidates[-1].label == f"t{MAX_VERDICT_WINDOW}"


def test_an_explicit_window_overrides_the_default() -> None:
    candidates = collect_candidates(_history(40), window=4, preserve_recent_results=1)
    assert len(candidates) == 4
    assert [candidate.pinned for candidate in candidates] == [False, False, False, True]


def test_no_window_scores_everything() -> None:
    candidates = collect_candidates(_history(40), window=None, preserve_recent_results=0)
    assert len(candidates) == 40


def test_a_real_session_skeleton_is_too_large_without_a_window() -> None:
    """The measured reason the window exists: 300 results need ~135k tokens."""
    messages = _history(120, size=4_000)
    everything = collect_candidates(messages, window=None, preserve_recent_results=5)
    with pytest.raises(ValueError, match="too large"):
        build_verdict_state(messages, everything, max_state_tokens=1_000)

    windowed = collect_candidates(messages, window=MAX_VERDICT_WINDOW)
    fitted = build_verdict_state(messages, windowed, max_state_tokens=1_000)
    assert fitted.tokens <= 1_000


# ── the scorer ─────────────────────────────────────────────────────────────


def _wait(scorer: VerdictScorer, timeout: float = 10.0) -> None:
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        thread = scorer._thread
        if thread is None or not thread.is_alive():
            return
        time.sleep(0.01)


def test_the_scorer_fills_a_cache_without_blocking(tmp_path) -> None:
    messages = _history(20)
    cache = ToolVerdictCache(path=tmp_path / "tool_verdicts.json")
    scorer = VerdictScorer(
        FakeDecisionClient(0.1), cache, min_interval_seconds=0.0
    )

    assert scorer.maybe_score(messages) is True
    _wait(scorer)

    assert scorer.last_error is None, scorer.last_error
    assert len(cache) > 0


def test_the_scorer_skips_when_nothing_is_unscored(tmp_path) -> None:
    messages = _history(20)
    cache = ToolVerdictCache(path=tmp_path / "tool_verdicts.json")
    scorer = VerdictScorer(FakeDecisionClient(0.1), cache, min_interval_seconds=0.0)

    assert scorer.maybe_score(messages) is True
    _wait(scorer)
    assert scorer.maybe_score(messages) is False, "nothing new to ask about"


def test_a_scored_verdict_is_findable_by_the_real_message(tmp_path) -> None:
    """The regression that made the whole feature a silent no-op.

    The scorer stores a verdict against whatever the candidate carries. It used
    to carry only a *character count*, so the store was keyed on a placeholder
    and no real ``ToolMessage`` could ever match it -- every lookup missed, the
    edit failed open, and nothing cleared at any threshold. Measured on real
    transcripts: 0 of 149 results could be answered for.

    The payloads here are deliberately **irregular**. A fixture of ``"x" * 100``
    cannot see this defect at all, because ``"x" * content_chars`` reproduces it
    exactly -- which is how an earlier version of this test passed with the
    defect re-injected. Mutation-checked: swapping the stored content back to a
    placeholder must make this fail.
    """
    messages = _history_with_irregular_payloads()
    results = [message for message in messages if isinstance(message, ToolMessage)]
    cache = ToolVerdictCache(path=None)
    scorer = VerdictScorer(FakeDecisionClient(0.1), cache, min_interval_seconds=0.0)
    scorer.maybe_score(messages)
    _wait(scorer)

    assert len(cache._memory) > 0, "nothing was scored"
    stored = set(cache._memory)
    expected = {result_key(message) for message in results}
    matched = stored & expected
    assert len(matched) == len(stored), (
        f"{len(stored) - len(matched)} of {len(stored)} verdicts are stored under "
        "a key no real message produces, so they can never be looked up"
    )


def _history_with_irregular_payloads(turns: int = 20) -> list:
    """A transcript whose results cannot be reconstructed from their length."""
    messages: list = []
    for n in range(1, turns + 1):
        messages.append(HumanMessage(content=f"ask {n}", id=f"h{n}"))
        messages.append(
            AIMessage(
                content="",
                id=f"a{n}",
                tool_calls=[
                    {
                        "id": f"c{n}",
                        "name": "read_file",
                        "args": {"file_path": f"/src/module_{n}.py"},
                    }
                ],
            )
        )
        messages.append(
            ToolMessage(
                f"def handler_{n}(value):\n    return value * {n}  # payload {n}\n"
                * 3,
                tool_call_id=f"c{n}",
                name="read_file",
                id=f"t{n}",
            )
        )
    return messages


def test_a_failing_endpoint_leaves_the_cache_empty_and_says_so(tmp_path) -> None:
    class Broken:
        def ask(self, _state, _questions):  # noqa: ANN001
            raise OSError("no endpoint")

    messages = _history(20)
    cache = ToolVerdictCache(path=tmp_path / "tool_verdicts.json")
    scorer = VerdictScorer(Broken(), cache, min_interval_seconds=0.0)

    scorer.maybe_score(messages)
    _wait(scorer)

    assert len(cache) == 0
    assert scorer.last_error and "no endpoint" in scorer.last_error
