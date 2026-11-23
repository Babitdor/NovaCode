"""`file_outline`: heading index and section reads for long documents.

The failure it fixes (seen in a real session): an agent paged a README, then
searched the page for `'## Run locally'` when the heading was `'## Run it
locally'` — 'section not found', two more eval round trips to recover. With
the outline, the wrong guess returns the real heading list in the same call.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from novacode_cli.agents.tool_contracts import native_returns
from novacode_cli.tools.outline_tools import build_file_outline_tool

README = """# Bot HQ

Local-first web app for AI agents.

```bash
# this comment is inside a fence, not a heading
npm ci
```

## Install on the computer

1. Clone the repo.
2. Run the installer.

### How this relates to Hermes Bot Mode

The app rides on the same gateway.

## Run it locally

You need Node.js 22.

## Run the tests

npm test

## Models and subscriptions

Pick a provider in the details panel.
"""


@pytest.fixture
def tool(tmp_path: Path):
    (tmp_path / "README.md").write_text(README, encoding="utf-8")
    return build_file_outline_tool(lambda: tmp_path.resolve())


def test_outline_lists_headings_and_skips_fenced_lines(tool) -> None:
    text = tool.invoke({"file_path": "/README.md"})
    assert "@@ outline: 6 headings · 29 lines @@" in text
    assert "1: # Bot HQ" in text
    assert "10: ## Install on the computer" in text
    assert "27: ## Models and subscriptions" in text
    assert "inside a fence" not in text, "fenced '#' lines are not headings"


def test_section_read_is_case_insensitive_and_partial(tool) -> None:
    text = tool.invoke({"file_path": "/README.md", "section": "run locally"})
    assert "@@ lines 19-22 of 29 @@" in text
    assert "## Run it locally" in text
    assert "You need Node.js 22." in text
    assert "Pick a provider" not in text, "the section ends at the next heading"


def test_section_read_includes_subsections(tool) -> None:
    text = tool.invoke({"file_path": "/README.md", "section": "Install on the computer"})
    assert "@@ lines 10-18 of 29 @@" in text
    assert "### How this relates to Hermes Bot Mode" in text
    assert "rides on the same gateway" in text
    assert "## Run it locally" not in text, "a ## section ends at the next ##"


def test_a_wrong_heading_guess_still_finds_the_section(tool) -> None:
    """The trace's failure: the model typed 'Run locally' for 'Run it locally'."""
    text = tool.invoke({"file_path": "/README.md", "section": "Run locally"})
    assert "@@ lines 19-22 of 29 @@" in text, "every query word matches: one call, no recovery loop"
    assert "## Run it locally" in text


def test_an_absent_section_returns_the_outline(tool) -> None:
    text = tool.invoke({"file_path": "/README.md", "section": "deploy to production"})
    assert "No section matching 'deploy to production'" in text
    assert "## Run it locally" in text, "the outline corrects the guess in one call"
    assert "Retry with section set to one of the headings above" in text


def test_ambiguous_matches_ask_for_the_exact_heading(tool) -> None:
    text = tool.invoke({"file_path": "/README.md", "section": "run"})
    assert "'run' matches 2 headings" in text
    assert "19: ## Run it locally" in text
    assert "23: ## Run the tests" in text
    assert "retry with the exact heading text" in text


def test_paths_outside_the_workspace_and_missing_files_are_errors(tmp_path: Path) -> None:
    tool = build_file_outline_tool(lambda: tmp_path.resolve())
    text = tool.invoke({"file_path": "/../escape.md"})
    assert text.startswith("Error:")
    text = tool.invoke({"file_path": "/nope.md"})
    assert text.startswith("Error: file not found")


def test_a_missing_root_is_an_error_not_a_crash(tmp_path: Path) -> None:
    tool = build_file_outline_tool(lambda: tmp_path / "missing")
    text = tool.invoke({"file_path": "/README.md"})
    assert text.startswith("Error:")


def test_the_return_contract_is_verified(tool) -> None:
    verdict = native_returns(tool)
    assert verdict["status"] == "verified"
    assert "outline" in verdict["description"]


async def test_code_mode_snippets_can_call_file_outline(tmp_path: Path) -> None:
    from deepagents.middleware.filesystem import FilesystemMiddleware

    from novacode_cli.agents.code_mode import CodeModeMiddleware
    from novacode_cli.backends.filesystem import OptimizedFilesystemBackend
    from novacode_cli.security.policy import get_policy
    from tests.test_unified_code_mode import invoke, scripted

    get_policy().tool_tiers["file_outline"] = "allow"

    (tmp_path / "README.md").write_text(README, encoding="utf-8")
    outline_tool = build_file_outline_tool(lambda: tmp_path.resolve())
    fs = FilesystemMiddleware(
        backend=OptimizedFilesystemBackend(root_dir=str(tmp_path), virtual_mode=True)
    )
    graph = scripted(
        [
            (
                "eval",
                "await tools.call('file_outline', "
                "{file_path: '/README.md', section: 'run locally'});",
            )
        ],
        middleware=[CodeModeMiddleware(), fs],
        tools=[outline_tool],
    )
    message = await invoke(graph)
    assert message.status == "success", message.content
    assert "@@ lines 19-22 of 29 @@" in message.content
    assert "## Run it locally" in message.content
