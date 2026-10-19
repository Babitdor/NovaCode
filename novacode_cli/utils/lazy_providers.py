"""Keep deepagents from loading provider SDKs a session may not use.

``deepagents.middleware.filesystem`` imports ``langchain_openai`` and
``langchain_google_genai`` (about 2 s and 2.5 s at startup) only to recognise
models that accept ``.docx``/``.pptx`` file blocks, and it already handles
either being missing. While deepagents loads, those two imports fail; its
check is then replaced by one that recognises the same classes by module
name, so a model built from either package later still qualifies. A session
on OpenAI or Gemini simply imports its package when it builds its model.
"""

from __future__ import annotations

import contextlib
import sys
from collections.abc import Iterator
from importlib.abc import MetaPathFinder
from typing import Any

#: Provider packages deepagents imports only for the file-block check.
DEFERRED = ("langchain_openai", "langchain_google_genai")


class _Defer(MetaPathFinder):
    def find_spec(self, name: str, path: Any = None, target: Any = None) -> None:  # noqa: ANN401
        if name.split(".", 1)[0] in DEFERRED:
            message = f"{name} is loaded later, when a model needs it"
            raise ImportError(message)


def _tolerates_non_pdf_files(model: Any) -> bool:  # noqa: ANN401
    """deepagents' check, by class module rather than imported class: true for
    ``ChatOpenAI``/``AzureChatOpenAI``/``ChatGoogleGenerativeAI`` and their subclasses."""
    return any(cls.__module__.startswith(DEFERRED) for cls in type(model).__mro__)


@contextlib.contextmanager
def deferred_provider_imports() -> Iterator[None]:
    """Import deepagents inside this block without loading the deferred SDKs."""
    if "deepagents" in sys.modules or any(name in sys.modules for name in DEFERRED):
        yield  # too late to save anything; leave deepagents as it is
        return
    finder = _Defer()
    sys.meta_path.insert(0, finder)
    try:
        yield
    finally:
        sys.meta_path.remove(finder)
        filesystem = sys.modules.get("deepagents.middleware.filesystem")
        if filesystem is not None and hasattr(filesystem, "_model_tolerates_non_pdf_files"):
            filesystem._model_tolerates_non_pdf_files = _tolerates_non_pdf_files
