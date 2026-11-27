"""The main agent's /memories/ backend enforces the project-memory layout.

A page written flat as ``memories/projects/<topic>.md`` is invisible to the
memory wiki, /dream and memory_search, which all read
``projects/<project-key>/<topic>.md`` — so a flat write strands outside the
wiki and the wiki stops accumulating. The guard refuses the write and names
the scoped target instead. Paths here are post-route (the composite strips
``/memories/``), matching what the backend actually receives.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from novacode_cli.backends.filesystem import AgentMemoryBackend

if TYPE_CHECKING:
    from pathlib import Path


def test_flat_project_page_write_is_refused_and_read_still_works(tmp_path: Path) -> None:
    legacy = tmp_path / "memories" / "projects" / "legacy.md"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("# Legacy\nKeep this.\n", encoding="utf-8")
    backend = AgentMemoryBackend(str(tmp_path))

    write = backend.write("/memories/projects/new-page.md", "NEW")
    edit = backend.edit("/memories/projects/legacy.md", "Keep", "Drop")

    assert write.error
    assert "/memories/memories/projects/" in write.error
    assert edit.error
    assert legacy.read_text(encoding="utf-8") == "# Legacy\nKeep this.\n"
    assert not (tmp_path / "memories" / "projects" / "new-page.md").exists()
    # Reads and deletes of flat legacy pages stay open: the dreamer must see
    # them and the user may clean them up.
    read = backend.read("/memories/projects/legacy.md")
    assert read.file_data is not None
    assert "Keep this." in read.file_data["content"]


def test_scoped_project_pages_and_shared_topics_stay_writable(tmp_path: Path) -> None:
    backend = AgentMemoryBackend(str(tmp_path))

    scoped = backend.write("/memories/projects/key-a/topic.md", "# Topic\n")
    shared = backend.write("/memories/topic.md", "# Topic\n")

    assert scoped.error is None
    assert shared.error is None
    assert (tmp_path / "memories" / "projects" / "key-a" / "topic.md").is_file()
    assert (tmp_path / "memories" / "topic.md").is_file()
