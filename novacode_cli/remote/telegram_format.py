"""Markdown -> Telegram HTML, split into messages that each parse on their own.

Models write GitHub-flavoured markdown (``**bold**``, ``###``, tables, fenced
code, ``snake_case``). Telegram's legacy ``Markdown`` parse mode rejects most of
that (an underscore in an identifier opens an unclosed italic) and the whole
message is refused. Its HTML mode needs only ``& < >`` escaped, so we parse the
markdown properly (markdown-it, already installed via rich) and emit the tags
Telegram supports: ``b i s code pre a blockquote``. What it cannot show becomes
the nearest thing it can: headings -> bold, lists -> bullets, tables ->
monospace, long quotes -> collapsed (``<blockquote expandable>``).

Chunks are cut between blocks, never inside a tag, so every message is valid.
"""

from __future__ import annotations

import html
import re

from markdown_it import MarkdownIt
from markdown_it.tree import SyntaxTreeNode

#: Telegram's cap is 4096 chars of *text*; tags only make HTML longer, so
#: measuring the HTML is a safe over-estimate.
MAX_MESSAGE = 4000
#: Quotes longer than this many lines start collapsed.
COLLAPSE_AFTER_LINES = 3

_md = MarkdownIt("commonmark", {"html": False}).enable(["table", "strikethrough"])
_SAFE_LINK = re.compile(r"^(https?|tg|mailto):", re.I)


def esc(text: str) -> str:
    return html.escape(text, quote=False)


def _inline(node: SyntaxTreeNode) -> str:
    out: list[str] = []
    for child in node.children:
        t = child.type
        if t == "text":
            out.append(esc(child.content))
        elif t in ("softbreak", "hardbreak"):
            out.append("\n")
        elif t == "code_inline":
            out.append(f"<code>{esc(child.content)}</code>")
        elif t == "strong":
            out.append(f"<b>{_inline(child)}</b>")
        elif t == "em":
            out.append(f"<i>{_inline(child)}</i>")
        elif t == "s":
            out.append(f"<s>{_inline(child)}</s>")
        elif t == "link":
            href = str(child.attrs.get("href", ""))
            label = _inline(child)
            out.append(
                f'<a href="{html.escape(href)}">{label}</a>' if _SAFE_LINK.match(href) else label
            )
        elif t == "image":
            src = str(child.attrs.get("src", ""))
            alt = _inline(child) or "image"
            out.append(f'<a href="{html.escape(src)}">{alt}</a>' if _SAFE_LINK.match(src) else alt)
        else:  # html_inline and anything unknown: show it, safely
            out.append(esc(child.content or ""))
    return "".join(out)


def _pre(code: str, lang: str = "") -> str:
    body = esc(code.rstrip("\n"))
    lang = lang.split()[0] if lang.strip() else ""
    return (
        f'<pre><code class="language-{esc(lang)}">{body}</code></pre>'
        if lang
        else f"<pre>{body}</pre>"
    )


def _table(node: SyntaxTreeNode) -> str:
    rows: list[list[str]] = []
    for section in node.children:  # thead / tbody
        for tr in section.children:
            rows.append(
                [
                    html.unescape(_inline(cell.children[0])) if cell.children else ""
                    for cell in tr.children
                ]
            )
    if not rows:
        return ""
    widths = [max(len(r[i]) if i < len(r) else 0 for r in rows) for i in range(max(map(len, rows)))]
    lines = [" | ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip() for r in rows]
    lines.insert(1, "-+-".join("-" * w for w in widths))
    return _pre("\n".join(lines))


def _block(node: SyntaxTreeNode, depth: int = 0, *, in_quote: bool = False) -> str:
    t = node.type
    if t == "paragraph":
        return _inline(node.children[0]) if node.children else ""
    if t == "heading":
        return f"<b>{_inline(node.children[0])}</b>" if node.children else ""
    if t in ("fence", "code_block"):
        return _pre(node.content, node.info if t == "fence" else "")
    if t in ("bullet_list", "ordered_list"):
        start = int(node.attrs.get("start", 1) or 1)
        items = []
        for n, item in enumerate(node.children):
            mark = f"{start + n}." if t == "ordered_list" else "•"
            parts = [_block(c, depth + 1, in_quote=in_quote) for c in item.children]
            body = "\n".join(p for p in parts if p)
            items.append("  " * depth + f"{mark} {body}")
        return "\n".join(items)
    if t == "blockquote":
        inner = "\n".join(p for p in (_block(c, depth, in_quote=True) for c in node.children) if p)
        if in_quote:  # Telegram does not nest quotes
            return inner
        tag = "blockquote expandable" if inner.count("\n") >= COLLAPSE_AFTER_LINES else "blockquote"
        return f"<{tag}>{inner}</blockquote>"
    if t == "hr":
        return "──────────"
    if t == "table":
        return _table(node)
    return esc(node.content or "")  # html_block, anything unknown


def _split_oversized(block: str) -> list[str]:
    """A single block over the limit: code splits into several ``<pre>``; the
    rest loses its formatting rather than risk cutting through a tag."""
    m = re.fullmatch(r'(<pre>(?:<code class="[^"]*">)?)(.*?)((?:</code>)?</pre>)', block, re.S)
    if m:
        head, body, tail = m.groups()
        room = MAX_MESSAGE - len(head) - len(tail)
        return [head + part + tail for part in _pack(body.split("\n"), room)]
    plain = esc(html.unescape(re.sub(r"<[^>]+>", "", block)))
    return _pack(plain.split("\n"), MAX_MESSAGE)


def _pack(lines: list[str], limit: int) -> list[str]:
    out, cur = [], ""
    for line in lines:
        while len(line) > limit:  # a single monster line
            if cur:
                out.append(cur)
                cur = ""
            cut = line.rfind(" ", 0, limit)
            cut = cut if cut > 0 else limit
            # never cut inside an HTML entity such as &amp;
            amp = line.rfind("&", 0, cut)
            if amp != -1 and ";" not in line[amp:cut]:
                cut = amp or limit
            out.append(line[:cut])
            line = line[cut:]
        cur = f"{cur}\n{line}" if cur else line
        if len(cur) > limit:
            out.append(cur[: -len(line) - 1])
            cur = line
    if cur:
        out.append(cur)
    return out


def render(markdown: str) -> list[str]:
    """``markdown`` as Telegram-HTML messages, each at most :data:`MAX_MESSAGE`."""
    tree = SyntaxTreeNode(_md.parse(markdown or ""))
    blocks = [b for b in (_block(child) for child in tree.children) if b.strip()]
    messages: list[str] = []
    cur = ""
    for block in blocks:
        pieces = [block] if len(block) <= MAX_MESSAGE else _split_oversized(block)
        for piece in pieces:
            if cur and len(cur) + 2 + len(piece) > MAX_MESSAGE:
                messages.append(cur)
                cur = piece
            else:
                cur = f"{cur}\n\n{piece}" if cur else piece
    if cur:
        messages.append(cur)
    return messages or [esc(markdown.strip()) or "…"]


def to_plain(html_text: str) -> str:
    """Fallback when Telegram still refuses the HTML: the text, no markup."""
    return html.unescape(re.sub(r"<[^>]+>", "", html_text))
