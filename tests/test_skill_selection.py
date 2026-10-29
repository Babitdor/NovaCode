"""Offline selection benchmark and source/usage regressions for skill discovery."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.store.memory import InMemoryStore

from novacode_cli.hermes.middleware import NovaLearningMiddleware
from novacode_cli.hermes.tracker import ToolUsageTracker
from novacode_cli.skills import retrieval
from novacode_cli.skills.refreshing_middleware import RefreshingSkillsMiddleware
from novacode_cli.skills.runtime import pinned_message
from tests.test_skill_retrieval import _BagOfWords, _skill


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(retrieval, "_model", _BagOfWords)
    monkeypatch.setattr("novacode_cli.skills.skills_prefs.effective_disabled", set)
    retrieval._index_cache.clear()
    yield
    retrieval._index_cache.clear()


def test_index_keeps_source_and_activation_metadata_current():
    first = _skill("review", "Review code safely.")
    second = {**first, "path": "/project-skills/review/SKILL.md"}
    retrieval.get_index([first])
    assert retrieval.get_index([second]).search("review")[0][0]["path"] == second["path"]
    third = {**second, "metadata": {"include_tools": "new_tool"}}
    assert retrieval.get_index([third]).search("review")[0][0]["metadata"] == third["metadata"]
    # Caller mutation must not silently change an existing cached index.
    index = retrieval.get_index([third])
    third["metadata"]["include_tools"] = "changed"
    assert index.search("review")[0][0]["metadata"]["include_tools"] == "new_tool"


async def test_usage_ranking_reads_owned_sessions_and_legacy_counts():
    store = InMemoryStore()
    await store.aput(("nova", "session-a", "skill_usage"), "textual", {"invocations": 7})
    await store.aput(("nova", "session-b", "skill_usage"), "textual", {"invocations": 10})
    await store.aput(("nova", "skill_usage"), "legacy", {"invocations": 2})
    await store.aput(("other", "session-a", "skill_usage"), "alien", {"invocations": 99})
    middleware = RefreshingSkillsMiddleware(backend=None, sources=[])
    await middleware._read_usage(SimpleNamespace(store=store))
    assert middleware._usage == {"textual": 17, "legacy": 2}


async def test_only_successful_loads_open_an_attribution_window():
    store = InMemoryStore()
    tracker = ToolUsageTracker(store, owner="session")
    assert (
        tracker.skill_name_from_tool_call({"name": "skills_load", "args": {"name": "textual"}})
        == "textual"
    )
    await tracker.record_tool_usage("skills_load", success=False, skill_invoked="textual")
    assert await tracker.get_skill_usage() == {}
    await tracker.record_tool_usage("skills_load", success=True, skill_invoked="textual")
    assert (await tracker.get_skill_usage())["textual"]["invocations"] == 1


@pytest.mark.parametrize("outcome", ["valid", "missing", "wrong_skill", "no_path", "error"])
async def test_learning_validates_load_result_before_credit(outcome: str):
    from unittest.mock import AsyncMock

    store = InMemoryStore()
    middleware = NovaLearningMiddleware(store=store, owner="session", enabled=True)
    snapshot = {"name": "textual", "path": "/skills/textual/SKILL.md"}
    if outcome == "wrong_skill":
        snapshot["name"] = "other"
    if outcome == "no_path":
        snapshot.pop("path")
    response = ToolMessage(
        content="Error: unknown or disabled skill." if outcome == "missing" else "Loaded",
        artifact={} if outcome == "missing" else {"skill": snapshot},
        status="error" if outcome == "error" else "success",
        tool_call_id="load",
        name="skills_load",
    )
    request = SimpleNamespace(
        tool_call={"name": "skills_load", "args": {"name": "textual"}, "id": "load"}
    )
    await middleware.awrap_tool_call(request, AsyncMock(return_value=response))
    stats = await middleware._tracker.get_skill_usage()
    assert bool(stats) == (outcome == "valid")


BENCHMARK = [
    ("Build keyboard navigation for this Textual terminal application", "textual"),
    ("Review my docker-compose configuration for security", "docker-reviewer"),
    ("Optimize ClickHouse database query performance", "clickhouse-io"),
    ("Create PowerPoint presentation slide decks", "pptx"),
    ("Use $textual", "textual"),
    ("thanks", None),
    ("continue", None),
    ("What is the weather tomorrow?", None),
    ("Do not use $textual", None),
]
LIBRARY = [
    _skill(
        "textual",
        "Build Textual terminal applications. Use for widgets, keyboard navigation "
        "and interaction tests. Not for websites.",
    ),
    _skill("docker-reviewer", "Review docker-compose configuration for security and correctness."),
    _skill("clickhouse-io", "Optimize ClickHouse database query performance."),
    _skill("pptx", "Create PowerPoint presentation slide decks."),
]


@pytest.mark.parametrize("dense", [False, True], ids=["keywords", "hybrid"])
@pytest.mark.parametrize("fillers", [0, 100], ids=["small", "large"])
@pytest.mark.parametrize(("query", "expected"), BENCHMARK)
def test_selection_benchmark(
    query: str,
    expected: str | None,
    fillers: int,
    monkeypatch: pytest.MonkeyPatch,
    *,
    dense: bool,
):
    if not dense:
        monkeypatch.setattr(retrieval, "_model", lambda: None)
    skills = [
        *LIBRARY,
        *[_skill(f"filler-{i}", "Unrelated gardening and compost advice.") for i in range(fillers)],
    ]
    names = [s["name"] for s in retrieval.get_index(skills).suggest(query)]
    assert len(names) <= 3
    if expected is None:
        assert names == []
    else:
        assert expected in names


def test_search_exact_names_win_and_results_keep_boundaries():
    middleware = RefreshingSkillsMiddleware(backend=None, sources=[])
    middleware._skills = LIBRARY
    result = middleware._skill_search("textual")
    assert result.startswith("- **textual**")
    assert "Not for websites." in result
    assert "Not for websites." in middleware._format_skills_list(LIBRARY)


def test_suggestions_can_reappear_until_loaded_and_after_compaction():
    middleware = RefreshingSkillsMiddleware(backend=None, sources=[])
    middleware._skills = LIBRARY
    query = HumanMessage("Build keyboard navigation for this Textual terminal application")
    note = middleware._suggestions({"messages": [query]}, LIBRARY)
    assert note is not None
    assert middleware._suggestions({"messages": [query, note, AIMessage("OK"), query]}, LIBRARY)
    loaded = pinned_message(LIBRARY[0], "Follow Textual instructions.")
    assert middleware._suggestions({"messages": [query, loaded, query]}, LIBRARY) is None
    assert middleware._suggestions({"messages": [query]}, LIBRARY)


def test_followup_uses_task_context_but_new_task_does_not():
    middleware = RefreshingSkillsMiddleware(backend=None, sources=[])
    first = HumanMessage("Build keyboard navigation for this Textual terminal application")
    note = middleware._suggestions(
        {"messages": [first, AIMessage("Plan"), HumanMessage("Proceed")]}, LIBRARY
    )
    assert note is not None
    assert "textual" in note.content
    assert (
        middleware._suggestions(
            {"messages": [first, AIMessage("Done"), HumanMessage("What is the weather tomorrow?")]},
            LIBRARY,
        )
        is None
    )


def test_long_request_keeps_trailing_requirements():
    middleware = RefreshingSkillsMiddleware(backend=None, sources=[])
    query = HumanMessage(
        "Background information. " * 150
        + "Build keyboard navigation in a Textual terminal application"
    )
    note = middleware._suggestions({"messages": [query]}, LIBRARY)
    assert note is not None
    assert "textual" in note.content
    assert len(note.content) <= 2000


@pytest.mark.parametrize("size", [1, 3, 10, 21, 100])
def test_semantic_only_match_is_independent_of_library_size(
    size: int,
    monkeypatch: pytest.MonkeyPatch,
):
    import numpy as np

    class SemanticModel:
        def encode(self, texts: list[str]) -> np.ndarray:
            return np.array(
                [
                    [1.0, 0.0]
                    if "PowerPoint" in text or "keynote narration" in text
                    else [0.0, 1.0]
                    for text in texts
                ]
            )

    monkeypatch.setattr(retrieval, "_model", SemanticModel)
    skills = [LIBRARY[-1], *[_skill(f"filler-{i}", "Gardening advice.") for i in range(size - 1)]]
    assert retrieval.get_index(skills).suggest("Polish my keynote narration")[0]["name"] == "pptx"


def test_popularity_cannot_displace_the_explicitly_requested_skill():
    skills = [
        _skill("textual", "Build terminal applications with keyboard navigation."),
        _skill("terminal", "Build terminal applications with keyboard navigation."),
    ]
    picks = retrieval.get_index(skills).suggest(
        "Use textual for terminal keyboard navigation",
        usage={"terminal": 10000},
    )
    assert picks[0]["name"] == "textual"


def test_long_description_preserves_exclusion_and_stays_bounded():
    description = (
        "Design websites. " + "Background and examples. " * 50 + "Do not use for code review."
    )
    result = retrieval.skill_summary(description, 200)
    assert len(result) <= 200
    assert "Design websites." in result
    assert "Do not use for code review." in result


async def test_usage_storage_failure_is_optional():
    from unittest.mock import AsyncMock

    store = SimpleNamespace(alist_namespaces=AsyncMock(side_effect=OSError("unavailable")))
    middleware = RefreshingSkillsMiddleware(backend=None, sources=[])
    await middleware._read_usage(SimpleNamespace(store=store))
    assert middleware._usage == {}
    assert middleware._format_skills_list(LIBRARY)


def test_real_graph_discloses_candidate_body_and_reference_in_order(tmp_path: Path):
    from langchain.agents import create_agent

    from novacode_cli.agents.tool_search import ToolSearchMiddleware
    from novacode_cli.backends import OptimizedFilesystemBackend
    from tests.test_skills_runtime import RecordingModel

    directory = tmp_path / "textual"
    directory.mkdir()
    (directory / "SKILL.md").write_text(
        "---\nname: textual\ndescription: Build Textual keyboard navigation.\n---\n"
        "INSTRUCTION_BODY. Read reference.md only for testing.",
        encoding="utf-8",
    )
    (directory / "reference.md").write_text("REFERENCE_BODY", encoding="utf-8")
    middleware = RefreshingSkillsMiddleware(
        backend=OptimizedFilesystemBackend(root_dir=tmp_path, virtual_mode=True),
        sources=["/"],
        watch_dirs=[tmp_path],
        listing_chars=0,
    )
    model = RecordingModel(
        responses=[
            AIMessage(
                "",
                tool_calls=[
                    {
                        "name": "skills_load",
                        "args": {"name": "textual"},
                        "id": "load",
                    }
                ],
            ),
            AIMessage(
                "",
                tool_calls=[
                    {
                        "name": "search_tools",
                        "args": {"query": "skills_read_resource"},
                        "id": "search",
                    }
                ],
            ),
            AIMessage(
                "",
                tool_calls=[
                    {
                        "name": "skills_read_resource",
                        "args": {"name": "textual", "path": "reference.md"},
                        "id": "reference",
                    }
                ],
            ),
            AIMessage("Done"),
        ]
    )
    graph = create_agent(model, middleware=[middleware, ToolSearchMiddleware()])
    result = graph.invoke({"messages": [HumanMessage("Build Textual keyboard navigation")]})
    notes = [m.content for m in result["messages"] if isinstance(m, HumanMessage)]
    assert any("skills that may apply" in note for note in notes)
    assert "INSTRUCTION_BODY" not in str(notes)
    assert "REFERENCE_BODY" not in str(notes)
    outputs = {m.tool_call_id: m.content for m in result["messages"] if isinstance(m, ToolMessage)}
    assert "INSTRUCTION_BODY" in outputs["load"]
    assert "REFERENCE_BODY" not in outputs["load"]
    assert "REFERENCE_BODY" in outputs["reference"]
    assert "skills_read_resource" not in model.bound[0]
    assert "skills_read_resource" in model.bound[2]
