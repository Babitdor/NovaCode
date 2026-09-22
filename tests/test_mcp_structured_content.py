"""An MCP result's structuredContent must reach the model, not just the artifact.

cua-driver returns the element tokens its ``click`` requires ONLY in
structuredContent; langchain-mcp-adapters files that as an artifact the model
never sees, so Nova could not click by element and guessed pixels instead.
"""

from __future__ import annotations

from novacode_cli.mcp.middleware import _render_structured, _with_structured

TREE = '- Window "Chrome"\n      - [30] Button "Reject all"\n'
STRUCTURED = {
    "_note": "deprecated fields live here",
    "elements": [
        {"element_index": 30, "element_token": "s7:30", "label": "Reject all", "role": "Button",
         "frame": {"x": 700, "y": 600, "w": 120, "h": 36}},
    ],
    "snapshot_id": "s7",
    "tree_markdown": TREE,
}


def test_tokens_reach_the_model_and_duplicates_do_not() -> None:
    content, artifact = _with_structured(([{"type": "text", "text": TREE}], {"structured_content": STRUCTURED}))
    shown = content[-1]["text"]
    assert "element_token=s7:30" in shown and "snapshot_id: s7" in shown
    assert '"x":700' in shown, "frames let a pixel click be computed, not guessed"
    assert "tree_markdown" not in shown, "already in the text part"
    assert "deprecated" not in shown, "private _fields are dropped"
    assert artifact == {"structured_content": STRUCTURED}


def test_string_content_and_plain_results() -> None:
    content, _ = _with_structured(("ok", {"structured_content": {"count": 3}}))
    assert content == "ok\n\nstructured result:\ncount: 3"
    assert _with_structured(("ok", None)) == ("ok", None)
    assert _with_structured("bare string") == "bare string"


def test_huge_structured_results_are_capped() -> None:
    big = {"items": [{"i": i, "v": "x" * 50} for i in range(2000)]}
    assert len(_render_structured(big, "")) < 13_000
