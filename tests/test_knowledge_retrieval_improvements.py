"""Recall uses substantive evidence, and wiki questions find relevant passages."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

from novacode_cli.hermes.ledger import LearningLedger
from novacode_cli.memory.search import MemorySearchService
from novacode_cli.memory.wiki import END, START
from novacode_cli.wiki.ask import WikiAskEngine
from novacode_cli.wiki.manager import WikiManager

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


async def test_short_technical_terms_are_recalled_without_navigation_noise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("NOVA_BUILTIN_LESSONS", "0")
    monkeypatch.setattr("novacode_cli.memory.semantic.similarities", lambda *_: [])
    memories = tmp_path / "memories"
    memories.mkdir()
    (memories / "sql-api.md").write_text(
        "# SQL API\nUse SQL parameters for API queries.\n", encoding="utf-8"
    )
    (memories / "unrelated.md").write_text(
        f"# Color palette\nUse blue.\n{START}\n[SQL API](sql-api.md)\n{END}\n",
        encoding="utf-8",
    )
    result = await MemorySearchService(tmp_path).search("How should SQL API queries work?")
    assert "Use SQL parameters" in result
    assert "unrelated" not in result
    assert "Wiki navigation" not in result


async def test_wiki_question_finds_body_evidence_beyond_page_intro(tmp_path: Path):
    wiki = WikiManager(tmp_path)
    wiki.ensure_structure()
    wiki.write_page(
        "patterns",
        "Transport.md",
        "# Transport\n"
        + "Introduction.\n" * 160
        + "\n## Authentication\nPass the JWT token during the WebSocket handshake.\n",
    )
    wiki.update_index("Transport", "patterns/Transport.md", "Network notes")
    result = await WikiAskEngine(wiki).build_prompt("How do I authenticate the WebSocket with JWT?")
    assert "JWT token during the WebSocket handshake" in result
    assert "patterns/Transport.md" in result
    assert len(result) < 8500
    assert (
        await WikiAskEngine(wiki).build_prompt("Explain ceramic glazing")
        == "Explain ceramic glazing"
    )
    assert wiki.search("") == []


def test_repeated_learning_proposals_are_idempotent_but_new_evidence_is_not(tmp_path: Path):
    ledger = LearningLedger(tmp_path / "learning.sqlite3")
    proposal = {"content": "Verify the SQL migration", "project": "a", "evidence": ("test passed",)}
    with ThreadPoolExecutor(max_workers=4) as pool:
        records = list(pool.map(lambda _: ledger.propose(**proposal), range(8)))
    assert len({record.reference for record in records}) == 1
    assert len(ledger.list()) == 1
    changed = ledger.propose(**{**proposal, "evidence": ("new test passed",)})
    assert changed.reference != records[0].reference
    other = ledger.propose(**{**proposal, "project": "b"})
    assert other.reference != records[0].reference
    ledger.transition(records[0].reference, "reject")
    assert ledger.propose(**proposal).reference != records[0].reference
