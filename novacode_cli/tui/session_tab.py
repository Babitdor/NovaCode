"""Session activity labels repaint without laying out the entire transcript."""

from textual.content import Content, ContentText
from textual.widgets import Static, Tab


class SessionTab(Tab):
    """Keep the underline/layout stable when only a spinner or colour changes."""

    @Tab.label.setter
    def label(self, label: ContentText) -> None:
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
