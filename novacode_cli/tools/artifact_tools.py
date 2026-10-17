"""Agent tools for creating and updating artifacts.

An artifact is a useful session output (a walkthrough, analysis, dashboard, …)
turned into a live, shareable web page. Create one when a result benefits from
visualization or when the user asks. The content is rendered in a sandboxed
browser context — it is never executed on the host.
"""

from __future__ import annotations

from langchain.tools import tool

_TYPES = "html, markdown, or dashboard"


@tool
def create_artifact(title: str, type: str = "markdown", content: str = "") -> str:
    """Create a live, shareable web page from a session output.

    For results clearer as a visual page (a walkthrough, an analysis, a
    dashboard) or when the user asks for an artifact. DESIGN FIRST: for
    html/dashboard read the `frontend-design` skill, then write a designed
    page, not default-styled tags.
    Write one standalone document with everything inline (it renders in a
    srcdoc iframe; CDN web fonts load) and an explicit `body` background.

    Args:
        title: Name shown in the TUI list and the page header.
        type: html, dashboard (full HTML page, CSS/JS allowed) or markdown
            (prose, cannot be styled). Use html when presentation matters.
        content: The full HTML document, or the Markdown text.

    Returns:
        The artifact id and URL; refine it with update_artifact, not a new one.
    """
    from novacode_cli.artifacts.registry import get_registry
    from novacode_cli.artifacts.server import artifact_url

    art = get_registry().create(title, type, content)
    url = artifact_url(art.id)
    return (
        f"Artifact created: '{art.title}' ({art.type}).\n"
        f"id: {art.id}\nURL: {url}\n"
        f"Update it in place with update_artifact(id='{art.id}', content='...') "
        f"instead of creating a duplicate.\n"
        "Loaded tools: update_artifact"  # tool_search.LOADED_MARKER: bound from the next step
    )


@tool
def update_artifact(
    id: str,
    title: str | None = None,
    type: str | None = None,
    content: str | None = None,
) -> str:
    """Update an existing artifact in place (keeps its id/URL; bumps the version).

    Prefer this over creating a new artifact when refining the same output.

    `content` REPLACES the whole body — it is not a patch. Re-send the complete
    document, including its `<style>`, or the page loses its design. The same
    design bar as `create_artifact` applies to the replacement.

    Args:
        id: The artifact id returned by create_artifact / list_artifacts.
        title: New title (optional).
        type: New type — one of html, markdown, dashboard (optional).
        content: New body — the full document, not a fragment (optional).
    """
    from novacode_cli.artifacts.registry import get_registry
    from novacode_cli.artifacts.server import artifact_url

    art = get_registry().update(id, title=title, type=type, content=content)
    if art is None:
        return f"No artifact with id '{id}'. Use list_artifacts() to see current ids."
    return (
        f"Artifact '{art.title}' updated to v{art.version}.\n"
        f"URL: {artifact_url(art.id)} (the open page refreshes automatically)."
    )


@tool
def list_artifacts() -> str:
    """List the artifacts created in this session with their ids, types, and URLs."""
    from novacode_cli.artifacts.registry import get_registry
    from novacode_cli.artifacts.server import artifact_url

    arts = get_registry().list()
    if not arts:
        return "No artifacts yet. Create one with create_artifact(title, type, content)."
    lines = [f"{len(arts)} artifact(s):"]
    for a in arts:
        lines.append(f"  {a.id}  [{a.type}] v{a.version} {a.status} - {a.title}  ->  {artifact_url(a.id)}")
    return "\n".join(lines)


__all__ = ["create_artifact", "list_artifacts", "update_artifact"]
