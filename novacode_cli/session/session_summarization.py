"""RETIRED — session summarization is superseded; kept only as a marker.

This module previously produced a declarative ``memory.md`` from conversation
history (``summarize_messages_to_memory``) and gated it behind
``should_trigger_summarization``. Neither had any caller: the live paths are

* :mod:`novacode_cli.compaction` — ``compact_conversation`` / ``summarize_conversation``
  (the ``/compact`` and auto-compact path), and
* deepagents' built-in ``SummarizationMiddleware`` (the mid-turn backstop),

so the dead trigger and its summarizer were removed.

The whole file, and ``novacode_cli/prompts/session_summarization.jinja``
(referenced only by the deleted function), can be deleted outright — this stub
exists only because the file could not be removed in-place.
"""

from __future__ import annotations

__all__: list[str] = []
