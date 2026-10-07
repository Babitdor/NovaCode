"""Artifacts: registry semantics, sandboxed rendering, and the agent tools."""

from __future__ import annotations

import json
import re
import urllib.request

from novacode_cli.artifacts.registry import ArtifactRegistry
from novacode_cli.artifacts.server import _viewer_html, ensure_server
from novacode_cli.tools.artifact_tools import (
    create_artifact,
    list_artifacts,
    update_artifact,
)


def test_registry_create_update_observer() -> None:
    r = ArtifactRegistry()
    events: list[tuple[str, str, int]] = []
    r.add_observer(lambda ev, a: events.append((ev, a.id, a.version)))

    a = r.create("PR Walkthrough", "html", "<h1>hi</h1>")
    assert a.version == 1 and a.status == "ready"
    assert r.create("Notes", "nonsense", "x").type == "markdown"  # coerced
    assert r.count() == 2

    u = r.update(a.id, content="<h1>hi2</h1>")
    assert u is not None and u.version == 2 and u.status == "updated"
    assert r.update("missing") is None  # no event, no crash

    assert [e[0] for e in events] == ["created", "created", "updated"]


def test_registry_observer_can_be_removed() -> None:
    r = ArtifactRegistry()
    events: list[str] = []

    class ObserverOwner:
        def observe(self, event: str, _artifact: object) -> None:
            events.append(event)

    owner = ObserverOwner()
    r.add_observer(owner.observe)
    r.add_observer(owner.observe)
    r.create("first", "markdown", "one")
    r.remove_observer(owner.observe)
    r.create("second", "markdown", "two")

    assert events == ["created"]
    assert r._observers == []


def test_html_artifact_is_sandboxed_and_escaped() -> None:
    r = ArtifactRegistry()
    art = r.create("x", "html", "<h1>Hi</h1><script>steal()</script>")
    page = _viewer_html(art)
    # Rendered inside a sandboxed iframe with no same-origin access.
    assert '<iframe sandbox="allow-scripts' in page
    assert "allow-same-origin" not in page  # cannot reach the viewer origin
    # The raw HTML is escaped into srcdoc (not injected into the outer document).
    assert "&lt;script&gt;steal()" in page
    assert "<script>steal()</script>" not in page


def test_markdown_artifact_renders_without_executing_raw_html() -> None:
    r = ArtifactRegistry()
    art = r.create("md", "markdown", "# Title\n\n**bold** and `code`\n\n<script>alert(1)</script>")
    page = _viewer_html(art)
    assert "<strong>bold</strong>" in page
    assert "<code>code</code>" in page
    # Raw HTML in markdown must not become a live script tag.
    assert "<script>alert(1)</script>" not in page


def test_api_exposes_only_safe_metadata() -> None:
    r = ArtifactRegistry()
    art = r.create("x", "html", "SECRET-CONTENT")
    assert "content" not in art.public_dict()
    assert set(art.public_dict()) == {
        "id", "title", "type", "version", "status", "created_at", "updated_at"
    }


def test_tool_flow_serves_page_and_updates_in_place() -> None:
    out = create_artifact.invoke(
        {"title": "Arch Dashboard", "type": "dashboard", "content": "<h1>Arch</h1>"}
    )
    aid = re.search(r"id: (\w+)", out).group(1)
    url = re.search(r"URL: (\S+)", out).group(1)

    base = ensure_server()
    assert url.startswith(base)

    page = urllib.request.urlopen(url, timeout=5).read().decode()
    assert "<iframe sandbox=" in page and "Copy Link" in page

    update_artifact.invoke({"id": aid, "content": "<h1>Arch v2</h1>"})
    meta = json.loads(urllib.request.urlopen(f"{base}/api/artifacts/{aid}", timeout=5).read().decode())
    assert meta["version"] == 2 and meta["status"] == "updated"

    listing = list_artifacts.invoke({})
    assert aid in listing and "Arch Dashboard" in listing


# ── design guidance reaches the model ────────────────────────────────────────


def test_create_artifact_schema_directs_the_model_to_the_design_skill() -> None:
    """Artifacts were shipping with default browser styling.

    A `create-html-artifact` skill existed, but with ~220 skills installed the
    model rarely noticed it mid-task. The tool's own docstring becomes its
    schema description — the one thing the model always reads at call time — so
    the design instruction has to live there.
    """
    desc = create_artifact.description
    assert "DESIGN FIRST" in desc
    assert "frontend-design" in desc, "must name the skill to read"
    assert "srcdoc" in desc, "must warn that relative URLs do not resolve"
    assert "background" in desc, "must warn the iframe canvas is white"


def test_create_artifact_steers_away_from_unstylable_markdown() -> None:
    """markdown renders with raw HTML disabled, so it cannot be styled."""
    desc = create_artifact.description
    assert "cannot be styled" in desc


def test_update_artifact_warns_content_is_a_full_replacement() -> None:
    """A partial re-send silently drops the page's <style> block."""
    desc = update_artifact.description
    assert "REPLACES" in desc
    assert "full document" in desc


def test_styled_html_survives_into_the_served_page() -> None:
    """The design the model writes must actually reach the browser."""
    css = "body{background:#0d0d12;color:#e8e8f0}"
    out = create_artifact.invoke(
        {
            "title": "Styled",
            "type": "html",
            "content": f"<html><head><style>{css}</style></head><body><h1>x</h1></body></html>",
        }
    )
    url = re.search(r"URL: (\S+)", out).group(1)
    page = urllib.request.urlopen(url, timeout=5).read().decode()
    assert "background:#0d0d12" in page.replace("&#x27;", "'").replace("&quot;", '"')
