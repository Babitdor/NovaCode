"""TextRepetitionGuard: detect degenerate self-repetition in streamed prose.

The model sometimes stops making tool calls and just re-emits the same
paragraphs verbatim, forever ("Let me also check the X element. … Good." on a
cycle). No tool-loop guard can see it — no tool ever runs — and the stream has
no natural end, so the turn only stops when something dies.

Detection is on *repeated blocks*, not repeated lines: a sliding window of
``window`` consecutive substantial lines is hashed, and the same window
recurring ``threshold`` times means the model is cycling rather than writing.
Requiring a multi-line block keeps legitimately repetitive output (boilerplate
code, table rows, a wrapped list) from tripping it.

Feed it every streamed text/reasoning delta; it buffers partial lines.
"""

from __future__ import annotations

from collections import deque


class TextRepetitionGuard:
    """Detect degenerate self-repetition in a model's streamed prose."""

    def __init__(self, *, threshold: int = 4, window: int = 3, min_line_chars: int = 40) -> None:
        self.threshold = threshold
        self.window = window
        self.min_line_chars = min_line_chars
        self.tripped = False
        self._partial = ""
        self._recent: deque[int] = deque(maxlen=window)
        self._seen: dict[tuple[int, ...], int] = {}

    def note_progress(self) -> None:
        """Record that the turn did real work (a tool call), clearing the history.

        Repetition only means "stuck" when nothing else is happening. A tool
        call is proof the turn is advancing, so blocks seen before it must not
        count toward blocks seen after it. Without this, a long productive turn
        whose narration repeats around each step ("Now I will apply the
        change… / I will read the surrounding context…") accumulated repeats
        across unrelated steps and was killed mid-flight, reporting that
        nothing had been committed.
        """
        self._recent.clear()
        self._seen.clear()
        self._partial = ""

    def feed(self, text: str) -> bool:
        """Add streamed text. Returns True once the output is a repetition loop."""
        if self.tripped or not text:
            return self.tripped
        self._partial += text
        *lines, self._partial = self._partial.split("\n")
        for line in lines:
            normalized = " ".join(line.split())
            if len(normalized) < self.min_line_chars:
                continue  # short lines ("Good.", "```") are noise, not a cycle
            self._recent.append(hash(normalized))
            if len(self._recent) < self.window:
                continue
            key = tuple(self._recent)
            count = self._seen.get(key, 0) + 1
            if count >= self.threshold:
                self.tripped = True
                return True
            # ponytail: plain dict, cleared wholesale. A response long enough to
            # blow this cap is itself the pathology we're watching for.
            if len(self._seen) > 50_000:
                self._seen.clear()
            self._seen[key] = count
        return False
