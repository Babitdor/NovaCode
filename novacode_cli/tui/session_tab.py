"""Session activity labels repaint without laying out the entire transcript."""

from textual import events
from textual.content import Content, ContentText
from textual.widgets import Static, Tab


class SessionTab(Tab):
    """Keep the underline/layout stable when only a spinner or colour changes."""

    closeable = False
    close_requested = False

    class CloseRequested(Tab.TabMessage):
        """The cross on this session's tab was clicked."""

    def _on_click(self, event: events.Click) -> None:
        """Close from the trailing cross without activating a background tab."""
        if self.closeable and event.x >= self.size.width - 3:
            event.stop()
            event.prevent_default()
            if not self.close_requested:
                self.close_requested = True
                self.post_message(self.CloseRequested(self))
            return
        super()._on_click()
        event.prevent_default()

    @Tab.label.setter
    def label(self, label: ContentText) -> None:
        """Update the label without reflowing equal-width animation frames."""
        content = Content.from_text(label)
        previous = getattr(self, "_label", None)
        if previous is not None and content == previous:
            return
        if previous is not None and content.cell_length == previous.cell_length:
            # Tab.update emits Relabelled and requests layout on every update.
            # Neither is needed when the tab's terminal-cell width is unchanged.
            self._label = content
            Static.update(self, content, layout=False)
        else:
            Tab.label.fset(self, content)
