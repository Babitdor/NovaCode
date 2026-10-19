"""``/tree``: browse every branch of the session and continue from any point."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from rich.text import Text
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Static, Tree

from novacode_cli.tui.animations import animate_modal_screen

if TYPE_CHECKING:
    from collections.abc import Callable

    from textual.app import ComposeResult
    from textual.binding import BindingType
    from textual.widgets.tree import TreeNode

    from novacode_cli.session.history_tree import HistoryTree, Node

#: Filter cycle for ``f``: name -> message kinds shown (branch forks always show).
FILTERS: dict[str, set[str] | None] = {
    "conversation": {"user", "assistant"},
    "all": None,
    "user": {"user"},
    "bookmarks": set(),
}
_STYLE = {"user": "bold", "assistant": "", "tool": "dim", "system": "dim italic"}


def node_line(node: Node, *, current: bool) -> Text:
    """One tree row: bookmark, who, and the first line of the message."""
    line = Text()
    if node.label:
        line.append(f"★ {node.label}  ", style="bold yellow")
    if node.msg.get("tool_calls"):
        preview = "⚙ " + ", ".join(c.get("name", "") for c in node.msg["tool_calls"])
    elif node.kind == "tool":
        preview = "⚙ " + (node.msg.get("name") or "result")
    else:
        first = next((ln for ln in node.text.splitlines() if ln.strip()), "(empty)")
        preview = ("you: " if node.kind == "user" else "nova: ") + first.strip()
    line.append(preview[:120], style=_STYLE.get(node.kind, ""))
    if current:
        line.append("  ◀ now", style="bold green")
    return line


class HistoryTreeScreen(ModalScreen[str | None]):
    """Pick a message to continue from; returns its node id, or None."""

    DEFAULT_CSS = """
    HistoryTreeScreen #modal-box { width: 94%; max-width: 140; height: 90%; }
    HistoryTreeScreen Tree { height: 1fr; background: $surface; }
    HistoryTreeScreen #tree-label { display: none; margin-top: 1; }
    HistoryTreeScreen #tree-label.editing { display: block; }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        ("escape", "cancel", "Cancel"),
        ("f", "cycle_filter", "Filter"),
        ("b", "bookmark", "Bookmark"),
    ]

    def __init__(self, tree: HistoryTree, on_label: Callable[[str, str], None]) -> None:  # noqa: D107
        super().__init__()
        self._tree = tree
        self._on_label = on_label
        self._filter = "conversation"
        self._on_path = {n.id for n in tree.path_to(tree.leaf)}

    def compose(self) -> ComposeResult:
        """Tree, hint line and the (hidden) bookmark box."""
        with Vertical(id="modal-box"):
            yield Static(Text("Session tree", style="bold"), id="modal-title")
            yield Static("", id="tree-hint")
            yield Tree("session", id="history")
            yield Input(placeholder="Bookmark name (empty removes it)", id="tree-label")

    def on_mount(self) -> None:
        """Fill the tree with the cursor on the current position."""
        widget = self.query_one("#history", Tree)
        widget.show_root = False
        self._rebuild()
        widget.focus()
        animate_modal_screen(self)

    def _visible(self, node: Node) -> bool:
        kinds = FILTERS[self._filter]
        if self._filter == "bookmarks":
            return bool(node.label)
        return kinds is None or node.kind in kinds

    def _rebuild(self) -> None:
        widget = self.query_one("#history", Tree)
        widget.clear()
        tree = self._tree
        cursor: TreeNode | None = None
        # Single-child chains stay at one level; each branch nests under its
        # fork. Forks and branch heads always show, so the shape survives a filter.
        heads = tree.roots if len(tree.roots) > 1 else []
        stack: list[tuple[TreeNode, str | None, bool]] = [
            (widget.root, r, True) for r in reversed(heads)
        ] or [(widget.root, tree.roots[0] if tree.roots else None, False)]
        while stack:
            container, nid, is_head = stack.pop()
            while nid is not None:
                node = tree.nodes[nid]
                fork = len(node.children) > 1
                if is_head or fork or self._visible(node):
                    line = node_line(node, current=nid == tree.leaf)
                    if is_head or fork:
                        item = container.add(line, data=nid, expand=nid in self._on_path)
                    else:
                        item = container.add_leaf(line, data=nid)
                    if nid == tree.leaf or (cursor is None and nid in self._on_path):
                        cursor = item
                    if is_head:
                        container = item
                is_head = False
                if fork:
                    stack.extend((item, child, True) for child in reversed(node.children))
                    break
                nid = node.children[0] if node.children else None
        if cursor is not None:
            # Line numbers exist only after the tree lays out its new nodes.
            self.call_after_refresh(widget.move_cursor, cursor)
        self.query_one("#tree-hint", Static).update(
            Text.assemble(
                (f"filter: {self._filter}", "bold"),
                (
                    "   ↑/↓ move · Enter continue from here · f filter · b bookmark · Esc close",
                    "dim",
                ),
            )
        )

    def on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        """Continue from the chosen message."""
        if event.node.data:
            self.dismiss(event.node.data)

    def action_cycle_filter(self) -> None:
        """Switch which message kinds are listed."""
        names = list(FILTERS)
        self._filter = names[(names.index(self._filter) + 1) % len(names)]
        self._rebuild()

    def action_bookmark(self) -> None:
        """Name (or rename) a bookmark on the highlighted message."""
        node = self.query_one("#history", Tree).cursor_node
        if node is None or not node.data:
            return
        box = self.query_one("#tree-label", Input)
        box.value = self._tree.nodes[node.data].label or ""
        box.add_class("editing")
        box.focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Save the bookmark and return to the tree."""
        widget = self.query_one("#history", Tree)
        node = widget.cursor_node
        event.input.remove_class("editing")
        if node is not None and node.data:
            self._on_label(node.data, event.value)
            node.set_label(
                node_line(self._tree.nodes[node.data], current=node.data == self._tree.leaf)
            )
        widget.focus()

    def action_cancel(self) -> None:
        """Close the bookmark box, or the screen."""
        box = self.query_one("#tree-label", Input)
        if box.has_class("editing"):
            box.remove_class("editing")
            self.query_one("#history", Tree).focus()
            return
        self.dismiss(None)
