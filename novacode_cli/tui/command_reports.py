"""Native views for cache controls and structured command diagnostics."""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.filesize import decimal
from rich.table import Table
from rich.text import Text
from textual import work
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Static, Tree

from novacode_cli.tui.reference_screens import ReferenceScreen

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from textual.app import ComposeResult
    from textual.widgets.tree import TreeNode


class CacheScreen(ReferenceScreen):
    """Read and change only the invoking workspace's knowledge cache."""

    DEFAULT_CSS = """
    CacheScreen #cache-body { height: 1fr; min-height: 3; margin-top: 1; }
    CacheScreen #cache-status { height: auto; color: $success; text-style: bold; }
    CacheScreen #cache-status.disabled { color: $text-muted; }
    CacheScreen #cache-error { height: auto; color: $error; }
    CacheScreen #cache-activity { height: auto; color: $text-muted; margin-top: 1; }
    CacheScreen #cache-metrics { height: auto; margin-top: 1; }
    CacheScreen #cache-actions { height: 3; margin-top: 1; }
    CacheScreen #cache-actions Button { min-width: 10; margin-right: 1; }
    CacheScreen.compact #cache-actions {
        layout: grid; grid-size: 2; grid-columns: 1fr 1fr; grid-rows: 3 3;
        height: 6; margin: 0;
    }
    CacheScreen.compact #cache-actions Button { width: 1fr; margin: 0; }
    """

    def __init__(
        self, control: Callable[[str], Awaitable[dict]], *, initial_action: str = "status"
    ) -> None:
        """Use the invoking session's shared cache controller."""
        super().__init__()
        self.control = control
        self.initial_action = initial_action
        self.enabled = True
        self.busy = False

    def compose(self) -> ComposeResult:
        """Keep metrics scrollable and actions reachable in a small terminal."""
        with Vertical(id="reference-shell"):
            yield Static("Knowledge cache", classes="reference-title")
            yield Static(
                "Workspace knowledge overviews. Provider prompt-cache usage: /cost.",
                classes="reference-subtitle",
            )
            with VerticalScroll(id="cache-body"):
                yield Static("Loading…", id="cache-status", markup=False)
                yield Static("", id="cache-error", markup=False)
                yield Static("", id="cache-activity", markup=False)
                yield Static("", id="cache-metrics")
            with Horizontal(id="cache-actions"):
                yield Button("Disable", id="cache-toggle")
                yield Button("Clear cache", id="cache-clear", variant="warning")
                yield Button("Refresh", id="cache-refresh")
                yield Button("Close", id="cache-close")

    def on_mount(self) -> None:
        """Load current statistics without blocking the UI."""
        self.load(self.initial_action)

    @work
    async def load(self, action: str) -> None:
        """Serialize mutations and display only the controller's returned state."""
        if self.busy:
            return
        self.busy = True
        buttons = [b for b in self.query(Button) if b.id != "cache-close"]
        for button in buttons:
            button.disabled = True
        try:
            data = await self.control(action)
            if data.get("error"):
                self.query_one("#cache-error", Static).update(str(data["error"]))
                self.query_one("#cache-status", Static).update("Unavailable")
                return
            self.query_one("#cache-error", Static).update("")
            self.enabled = bool(data["semantic_cache_enabled"])
            status = self.query_one("#cache-status", Static)
            status.set_class(not self.enabled, "disabled")
            status.update("Enabled" if self.enabled else "Disabled")
            self.query_one("#cache-activity", Static).update(
                "No knowledge lookups yet. Only knowledge_lookup populates this cache, "
                "not file reads or shell commands. First-time overviews use a model call."
                if not data.get("lookups", 0)
                else "Only knowledge_lookup activity is counted here. Token savings are estimates."
            )
            table = Table.grid(padding=(0, 2))
            table.add_column(style="dim")
            table.add_column(justify="right")
            fields = (
                ("Stored data", decimal(data.get("retained_bytes", 0))),
                ("Exact hits", f"{data.get('exact_hit', 0):,}"),
                ("Semantic hits", f"{data.get('semantic_hit', 0):,}"),
                ("Misses", f"{data.get('misses', 0):,}"),
                ("Lookups", f"{data.get('lookups', 0):,}"),
                ("Total lookup time", f"{data.get('lookup_ms', 0):,.1f} ms"),
                ("Generation calls", f"{data.get('generation_calls', 0):,}"),
                ("Tokens avoided (est.)", f"{data.get('avoided_tokens_estimate', 0):,}"),
                ("Invalidations", f"{data.get('invalidations', 0):,}"),
                # `epoch` counts clear/off fences, not generations — label it
                # as what it is, or a cleared-twice cache reads as "active".
                ("Clears (epoch)", f"{data.get('epoch', 0):,}"),
            )
            for label, value in fields:
                table.add_row(label, value)
            self.query_one("#cache-metrics", Static).update(table)
            self.query_one("#cache-toggle", Button).label = "Disable" if self.enabled else "Enable"
            if action != "status":
                self.app.notify(
                    {"clear": "Cache cleared.", "on": "Cache enabled.", "off": "Cache disabled."}[
                        action
                    ]
                )
        except Exception as error:  # noqa: BLE001 - a failed cache cannot crash the UI.
            self.query_one("#cache-error", Static).update(f"Cache unavailable: {error}")
        finally:
            self.busy = False
            for button in buttons:
                button.disabled = False

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Route explicit user actions through the shared controller."""
        if event.button.id == "cache-close":
            self.action_close()
            return
        action = {
            "cache-toggle": "off" if self.enabled else "on",
            "cache-clear": "clear",
            "cache-refresh": "status",
        }.get(event.button.id)
        if action:
            self.load(action)


class StructuredReportScreen(ReferenceScreen):
    """Browse nested diagnostics without dumping a JSON document into chat."""

    def __init__(self, title: str, data: object) -> None:
        """Retain full diagnostic values for keyboard selection."""
        super().__init__()
        self.report_title = title
        self.data = data

    def compose(self) -> ComposeResult:
        """Separate the report hierarchy from its full selected value."""
        with Vertical(id="reference-shell"):
            yield Static(self.report_title, classes="reference-title", markup=False)
            yield Static(
                "Arrow keys to browse · Enter to select · Full values below",
                classes="reference-subtitle",
            )
            yield Tree(self.report_title, data=self.data, id="reference-list")
            with VerticalScroll(classes="reference-details"):
                yield Static(
                    "Select a field to inspect its full value.", id="reference-detail", markup=False
                )
            with Horizontal(id="reference-actions"):
                yield Button("Close", id="reference-close")

    def on_mount(self) -> None:
        """Build literal tree labels, retaining unabridged values on each node."""
        tree = self.query_one(Tree)

        def add(parent: TreeNode, key: str, value: object) -> None:
            label = key.replace("_", " ").capitalize()
            if isinstance(value, (dict, list)):
                node = parent.add(Text(f"{label} ({len(value)})"), data=value)
                entries = value.items() if isinstance(value, dict) else enumerate(value, 1)
                for child_key, child_value in entries:
                    add(node, str(child_key), child_value)
            else:
                preview = " ".join(str(value).split())
                parent.add_leaf(Text(f"{label}: {preview[:100]}"), data=value)

        if isinstance(self.data, dict):
            for key, value in self.data.items():
                add(tree.root, str(key), value)
        else:
            add(tree.root, "Report", self.data)
        tree.root.expand()
        tree.focus()

    def on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        """Reveal full strings without interpreting tool-provided markup."""
        value = event.node.data
        detail = (
            f"{len(value)} entries. Expand this field to browse its contents."
            if isinstance(value, (dict, list))
            else str(value)
        )
        self.query_one("#reference-detail", Static).update(detail)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Dismiss the report without changing its source data."""
        if event.button.id == "reference-close":
            self.action_close()
