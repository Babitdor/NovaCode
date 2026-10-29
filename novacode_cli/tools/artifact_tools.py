"""Agent tools for creating and updating artifacts.

An artifact is a useful session output (a walkthrough, analysis, dashboard, …)
turned into a live, shareable web page. Create one when a result benefits from
visualization or when the user asks. The content is rendered in a sandboxed
browser context — it is never executed on the host.
"""

from __future__ import annotations

from langchain.tools import tool

_TYPES = "html, markdown, or dashboard"


def _host_artifact(art, registry) -> tuple[str, str | None]:
    """Host through Cloudflare, retaining local access when hosting is unavailable."""
    from novacode_cli.artifacts.server import artifact_url

    local_url = artifact_url(art.id)
    try:
        from novacode_cli.commands.share_handler import ensure_artifact_share

        return ensure_artifact_share(art.id, registry.session_id), None
    except Exception as exc:  # noqa: BLE001 — artifact creation must still succeed offline
        return local_url, f"Temporary public hosting unavailable: {exc}"


@tool(response_format="content_and_artifact")
def create_artifact(
    title: str,
    type: str = "markdown",  # noqa: A002 - Preserve the public tool argument named type.
    content: str = "",
) -> tuple[str, dict]:
    """Create a live, shareable web page from a session output.

    For results clearer as a visual page (a walkthrough, an analysis, a
    dashboard) or when the user asks for an artifact. DESIGN FIRST: for
    html/dashboard read the `frontend-design` skill, then write a designed
    page, not default-styled tags.
    Write one standalone, mobile-responsive document with everything inline
    (it renders in a srcdoc iframe; CDN web fonts load) and an explicit `body`
    background. Include the viewport meta tag, flexible widths, and a narrow
    screen layout; Nova adds overflow guards but cannot redesign fixed layouts.

    Args:
        title: Name shown in the TUI list and the page header.
        type: html, dashboard (full HTML page, CSS/JS allowed) or markdown
            (prose, cannot be styled). Use html when presentation matters.
        content: The full HTML document, or the Markdown text.

    Returns:
        The artifact id and URL; refine it with update_artifact, not a new one.
    """
    from novacode_cli.artifacts.registry import get_registry
    registry = get_registry()
    art = registry.create(title, type, content)
    url, share_note = _host_artifact(art, registry)
    availability = f"\n{share_note}" if share_note else ""
    text = (
        f"Artifact created: '{art.title}' ({art.type}).\n"
        f"id: {art.id}\nURL: {url}{availability}\n"
        f"Update it in place with update_artifact(id='{art.id}', content='...') "
        f"instead of creating a duplicate.\n"
        "Loaded tools: update_artifact"
    )
    return text, {
        "nova_tool_search": {
            "version": 1,
            "capabilities": [{"kind": "tool", "name": "update_artifact"}],
        },
    }


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
    document, including its `<style>`, or the page loses its design. Keep the
    page mobile-responsive; the same design bar as `create_artifact` applies.

    Args:
        id: The artifact id returned by create_artifact / list_artifacts.
        title: New title (optional).
        type: New type — one of html, markdown, dashboard (optional).
        content: New body — the full document, not a fragment (optional).
    """
    from novacode_cli.artifacts.registry import get_registry
    registry = get_registry()
    art = registry.update(id, title=title, type=type, content=content)
    if art is None:
        return f"No artifact with id '{id}'. Use list_artifacts() to see current ids."
    url, share_note = _host_artifact(art, registry)
    availability = f"\n{share_note}" if share_note else ""
    return (
        f"Artifact '{art.title}' updated to v{art.version}.\n"
        f"URL: {url} (the open page refreshes automatically).{availability}"
    )


@tool
def list_artifacts() -> str:
    """List the artifacts created in this session with their ids, types, and URLs."""
    from novacode_cli.artifacts.registry import get_registry
    registry = get_registry()
    arts = registry.list()
    if not arts:
        return "No artifacts yet. Create one with create_artifact(title, type, content)."
    try:
        from novacode_cli.commands.share_handler import ensure_artifact_share

        public_origin = ensure_artifact_share(session_id=registry.session_id).rstrip("/")
    except Exception as exc:  # noqa: BLE001
        public_origin = None
        share_note = f"\nTemporary public hosting unavailable: {exc}"
    else:
        share_note = ""
    lines = [f"{len(arts)} artifact(s):"]
    for a in arts:
        url = f"{public_origin}/artifacts/{a.id}" if public_origin else None
        if url is None:
            from novacode_cli.artifacts.server import artifact_url

            url = artifact_url(a.id)
        lines.append(f"  {a.id}  [{a.type}] v{a.version} {a.status} - {a.title}  ->  {url}")
    return "\n".join(lines) + share_note


__all__ = ["create_artifact", "list_artifacts", "update_artifact"]
