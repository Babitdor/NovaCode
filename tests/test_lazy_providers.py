"""deepagents loads without the OpenAI and Google SDKs; its file check still works."""

from __future__ import annotations

import subprocess
import sys

import pytest

SCRIPT = r"""
import sys
from novacode_cli.utils.lazy_providers import deferred_provider_imports

with deferred_provider_imports():
    import novacode_cli.integrations.sandbox_factory  # loads deepagents

deferred = [m for m in ("langchain_openai", "langchain_google_genai", "openai", "google.genai") if m in sys.modules]
print("loaded:", deferred)

from deepagents.middleware import filesystem as fs
from langchain_anthropic import ChatAnthropic
from langchain_openai import ChatOpenAI  # still importable afterwards

class Gateway(ChatOpenAI):  # Nova's own subclasses (OpenCode, ChatGPT plan)
    pass

openai_model = Gateway(model="gpt-5.5", api_key="x")
claude = ChatAnthropic(model_name="claude-sonnet-5-5", api_key="x")
print("openai file blocks:", fs._model_tolerates_non_pdf_files(openai_model))
print("claude file blocks:", fs._model_tolerates_non_pdf_files(claude))
print("none:", fs._model_tolerates_non_pdf_files(None))
"""


@pytest.mark.timeout(600)
def test_deepagents_loads_without_unused_provider_sdks():
    result = subprocess.run(
        [sys.executable, "-c", SCRIPT], capture_output=True, text=True, timeout=600, check=False
    )
    assert result.returncode == 0, result.stderr[-2000:]
    out = result.stdout
    assert "loaded: []" in out, "OpenAI and Google SDKs were not loaded with deepagents"
    assert "openai file blocks: True" in out, "a ChatOpenAI subclass still gets .docx/.pptx blocks"
    assert "claude file blocks: False" in out and "none: False" in out
