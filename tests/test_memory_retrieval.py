"""Per-turn memory retrieval: relevant learned lesson BODIES (not just INDEX
pointers) must be injected for the current request, so accumulated learning
actually reaches the model instead of sitting inert on disk."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from novacode_cli.memory.agent_memory import AgentMemoryMiddleware


def _mw(agent_dir: Path) -> AgentMemoryMiddleware:
    """Build a middleware bound only to agent_dir (retrieval touches nothing else)."""
    mw = object.__new__(AgentMemoryMiddleware)
    mw.agent_dir = agent_dir
    mw._corpus_cache = None
    mw._corpus_sig = None
    mw._retrieval_cache = None
    return mw


def _req(text: str) -> SimpleNamespace:
    return SimpleNamespace(messages=[{"role": "user", "content": text}])


def _setup(tmp_path: Path) -> Path:
    mem = tmp_path / "memories"
    mem.mkdir(parents=True)
    (mem / "INDEX.md").write_text("# Memory Index\n- pointers only\n", encoding="utf-8")
    (mem / "websocket-token-auth.md").write_text(
        "- The cowork WebSocket takes the token as a query param because browsers "
        "cannot set headers on a WebSocket handshake.\n",
        encoding="utf-8",
    )
    (mem / "docker-image-sizing.md").write_text(
        "- Multi-stage builds keep the final Docker image under 200MB.\n",
        encoding="utf-8",
    )
    return tmp_path


def test_relevant_lesson_body_is_injected(tmp_path: Path):
    mw = _mw(_setup(tmp_path))
    out = mw._relevant_memories(_req("how do I pass a token to authenticate the websocket handshake?"))
    assert "websocket-token-auth" in out
    assert "query param because browsers" in out  # the BODY, not just a pointer
    assert "docker-image-sizing" not in out  # irrelevant topic excluded
    assert "INDEX" not in out  # the index file is never treated as a lesson


def test_unrelated_query_injects_nothing(tmp_path: Path):
    mw = _mw(_setup(tmp_path))
    assert mw._relevant_memories(_req("what is the capital of France?")) == ""


def test_retrieval_is_cached_per_query(tmp_path: Path):
    mw = _mw(_setup(tmp_path))
    q = "websocket token handshake authenticate"
    first = mw._relevant_memories(_req(q))
    assert first  # something matched
    # Corrupt the corpus cache; a cached query must NOT re-scan (returns same).
    mw._corpus_cache = {}
    assert mw._relevant_memories(_req(q)) == first


def test_empty_when_no_user_message(tmp_path: Path):
    mw = _mw(_setup(tmp_path))
    assert mw._relevant_memories(SimpleNamespace(messages=[])) == ""


# ── what the agent is facing, and bullet-level selection ───────────────────
# Found on Terminal-Bench: the lesson that would have saved a third of a task's
# budget ("`apt-get install` fails until `apt-get update` has run") could never
# be retrieved, because retrieval only compared lessons with the user's request
# and the request was about tensor parallelism. The error it describes only
# ever appears in a tool result.

_APT = "- In a bare container `apt-get install` fails with `Unable to locate package` until `apt-get update` has run first."


def _tooling(tmp_path: Path) -> Path:
    mem = tmp_path / "memories"
    mem.mkdir(parents=True)
    (mem / "tooling.md").write_text(
        "# Tooling\n\n"
        + _APT
        + "\n- `pip3 install` needs `--break-system-packages` under PEP 668 on Debian images.\n"
        "- PyTorch CPU wheels come from the download.pytorch.org cpu index url.\n",
        encoding="utf-8",
    )
    return tmp_path


def _facing(request: str, tool_output: str) -> SimpleNamespace:
    return SimpleNamespace(
        messages=[
            {"role": "user", "content": request},
            {"role": "assistant", "content": ""},
            {"role": "tool", "content": tool_output},
        ]
    )


def test_a_lesson_surfaces_when_the_agent_hits_the_situation_it_is_about(tmp_path: Path):
    mw = _mw(_tooling(tmp_path))
    request = "Implement tensor parallel linear layers and make the tests pass."
    assert mw._relevant_memories(_req(request)) == "", "nothing in the request points to it"

    error = "Reading package lists...\nE: Unable to locate package python3\napt-get install exited 100"
    out = mw._relevant_memories(_facing(request, error))
    assert "apt-get update" in out


def test_only_the_bullets_that_bear_on_the_moment_are_injected(tmp_path: Path):
    """The head of the topic file used to be injected whole, relevant or not."""
    mw = _mw(_tooling(tmp_path))
    out = mw._relevant_memories(
        _facing("Build the project.", "E: Unable to locate package python3 (apt-get install)")
    )
    assert "apt-get update" in out
    assert "PEP 668" not in out and "pytorch" not in out.lower()


def test_ordinary_tool_output_does_not_drag_lessons_in(tmp_path: Path):
    """Tool output is wordy; one or two shared words must not be enough."""
    mw = _mw(_tooling(tmp_path))
    listing = "total 12\n-rw-r--r-- 1 root root 2048 main.py\n-rw-r--r-- 1 root root 512 install.sh"
    assert mw._relevant_memories(_facing("Build the project.", listing)) == ""


def test_the_cache_follows_the_situation_not_just_the_request(tmp_path: Path):
    mw = _mw(_tooling(tmp_path))
    request = "Build the project."
    assert mw._relevant_memories(_facing(request, "compiled ok")) == ""
    out = mw._relevant_memories(_facing(request, "E: Unable to locate package python3 apt-get install"))
    assert "apt-get update" in out, "a new tool result must be looked at afresh"
