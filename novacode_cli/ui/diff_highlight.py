"""Per-line syntax highlighting for diff previews.

Both diff renderers (the Rich console path in ``ui_elements.py`` and the
Textual TUI path in ``tui/app.py``) colour whole lines by their diff marker
(``+`` / ``-`` / context). This module adds *syntax* colouring on top of that
without disturbing the marker signal:

- The diff marker colour stays the **base** style (it carries the add/remove
  signal, including the green/red background in the Rich path).
- Syntax token colours overlay **foreground only**, so a highlighted line is
  still visibly an addition or a deletion.

Tokenization is done **per line**, not per hunk: lexing a whole hunk would
re-flow the code and break alignment with the diff markers.

Pygments is imported lazily (first use) to keep startup fast, matching the
project's heavy-dependency convention.
"""

from __future__ import annotations

from functools import lru_cache
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pygments.lexer import Lexer

# Pygments token types whose colour we deliberately drop: they are structural
# (whitespace, newlines, end-of-line) and styling them adds noise without
# information. Everything else is mapped to a Rich style string.
_SKIP_TOKENS = frozenset(
    {
        "Token.Text.Whitespace",
        "Token.Text.Whitespace.Newline",
        "Token.Text.Whitespace.Indentation",
    }
)


def lexer_for_path(path: str | None) -> Lexer | None:
    """Return a Pygments lexer for ``path``'s extension, or ``None``.

    Unknown or missing extensions return ``None`` so callers fall back to
    plain diff colouring rather than guessing a language.

    Args:
        path: A filename or path (e.g. ``"foo.py"`` or ``"Diff foo.py"``).

    Returns:
        A Pygments ``Lexer`` instance, or ``None`` if none can be resolved.
    """
    if not path:
        return None
    try:
        from pygments.lexers import get_lexer_for_filename
        from pygments.util import ClassNotFound
    except ImportError:  # pragma: no cover - pygments ships with rich
        return None

    # The title passed to the renderers is often "Diff foo.py"; take the last
    # whitespace-separated token that looks like a filename.
    candidate = path.strip()
    if " " in candidate:
        candidate = candidate.rsplit(" ", 1)[-1]
    if not candidate or "." not in candidate:
        return None
    try:
        return get_lexer_for_filename(candidate, stripnl=False, ensurenl=False)
    except ClassNotFound:
        return None
    except Exception:  # noqa: BLE001 - never let lexer resolution break rendering
        return None


@lru_cache(maxsize=1)
def _token_style_map() -> dict[str, str]:
    """Build a ``{pygments_token_type: rich_style}`` map from the default style.

    Uses Pygments' own style definitions so token colours match the lexer's
    intended palette, then converts each ``#rrggbb`` colour to a Rich style
    string. Cached (the style is static for the process lifetime).
    """
    from pygments.styles import get_style_by_name

    style = get_style_by_name("monokai")
    mapping: dict[str, str] = {}
    for token_type, style_def in style.styles.items():
        # Key by the dotted token string (e.g. "Token.Keyword") so lookups in
        # highlight_line can walk the token hierarchy by str(token_type).
        # Pygments style values are strings like "#f8f8f2" or
        # "bold #ed007e bg:#1e0010"; convert to a Rich style string, keeping
        # only foreground attributes (backgrounds would fight the diff marker).
        name = str(token_type)
        # Keep only foreground attributes; "bg:#..." and "noinherit" are dropped.
        parts = [
            attr
            for attr in str(style_def).split()
            if attr in ("bold", "italic", "underline") or attr.startswith("#")
        ]
        if parts:
            mapping[name] = " ".join(parts)
    return mapping


def highlight_line(code: str, lexer: Lexer | None) -> list[tuple[str, str]]:
    """Tokenize a single line of code into ``(text, rich_style)`` spans.

    Args:
        code: The line content **without** its diff marker.
        lexer: A Pygments lexer, or ``None`` for no highlighting.

    Returns:
        A list of ``(text, style)`` pairs. When ``lexer`` is ``None`` or
        tokenization fails, returns a single ``(code, "")`` pair so the caller
        can apply its own base style.
    """
    if not code or lexer is None:
        return [(code, "")]

    try:
        from pygments.token import Token

        style_map = _token_style_map()
        spans: list[tuple[str, str]] = []
        for token_type, value in lexer.get_tokens(code):
            if not value:
                continue
            if str(token_type) in _SKIP_TOKENS:
                spans.append((value, ""))
                continue
            # Walk up the token hierarchy to find the nearest styled ancestor
            # (e.g. Token.Literal.String.Doc -> Token.Literal.String).
            style = ""
            tt = token_type
            while tt is not None and tt is not Token:
                name = str(tt)
                if name in style_map:
                    style = style_map[name]
                    break
                tt = tt.parent
            spans.append((value, style))
    except Exception:  # noqa: BLE001 - highlighting must never break rendering
        return [(code, "")]
    else:
        return spans or [(code, "")]


def has_highlighting(spans: list[tuple[str, str]]) -> bool:
    """Return True if any span carries a non-empty style (i.e. real colouring)."""
    return any(style for _, style in spans)


__all__: list[str] = [
    "has_highlighting",
    "highlight_line",
    "lexer_for_path",
]
