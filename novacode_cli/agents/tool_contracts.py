"""Verified Code Mode return descriptions; never coerce tool values."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from langchain_core.tools import BaseTool

_FILESYSTEM = {
    "read_file": "Text pages return an exact source string preserving newlines. Media requires direct read_file. Oversized or transformed pages raise an error; request fewer lines.",
    "write_file": "Success returns a confirmation string. Filesystem failures raise an error.",
    "edit_file": "Success returns a confirmation string. Missing or ambiguous matches raise an error.",
    "ls": "Returns a string representation of a Python list of virtual paths, or 'No files found'. No stdout property. Tool error status raises an error.",
    "glob": "Returns a string representation of a Python list of matching virtual paths, or 'No files found', possibly with a truncation notice. No stdout property. Tool error status raises an error.",
    "grep": "Returns matching-file or matching-content text according to output_mode as a string, possibly with a truncation notice. No stdout property. Tool error status raises an error.",
    "execute": "Returns combined command output and exit-status text, possibly an offload notice for large output. Valid JSON text is decoded; other text remains a string. No stdout property. Tool error status raises an error.",
}


_FILE_OUTLINE = "Returns '@@ outline: N headings · M lines @@' plus one 'line: heading' row per heading, or, with `section`, '@@ lines X-Y of M @@' plus the section text. No match returns the outline with guidance; path and read failures start with 'Error:'."


def native_returns(target: BaseTool) -> dict:
    """Only describe implementations whose result boundary has been verified."""
    modules = {
        getattr(fn, "__module__", "")
        for fn in (getattr(target, "func", None), getattr(target, "coroutine", None))
        if fn
    }
    description = None
    if modules == {"deepagents.middleware.filesystem"}:
        description = _FILESYSTEM.get(target.name)
    elif modules == {"novacode_cli.tools.outline_tools"} and target.name == "file_outline":
        description = _FILE_OUTLINE
    elif modules == {"novacode_cli.shell.middleware"} and target.name in {"shell", "bash"}:
        description = "Returns command output/status text, including background or interactive notices. Valid JSON text is decoded; other text remains a string. No stdout property. Tool error status raises an error."
    return (
        {"status": "verified", "description": description}
        if description
        else {
            "status": "unknown",
            "description": "Return contract is unknown. Inspect the actual value; do not assume object fields such as stdout.",
        }
    )


def mcp_returns(target: BaseTool) -> dict:
    """Retain the server's declared structured-output schema verbatim when supplied."""
    schema = (target.metadata or {}).get("nova_mcp_output_schema")
    result = {
        "returns": {
            "status": "unknown",
            "description": "No output schema supplied. Result may be structured content, decoded JSON, text or content blocks; inspect the value.",
        }
    }
    if schema is not None:
        result = {
            "outputSchema": schema,
            "returns": {
                "status": "declared",
                "description": "Server-declared schema for structured content. If absent, the adapter may return text or content blocks.",
            },
        }
    return result
