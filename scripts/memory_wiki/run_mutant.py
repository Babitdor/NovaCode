"""Load one throwaway wiki mutant without changing the shared source tree."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import novacode_cli.memory  # noqa: E402 - resolve repository imports first

spec = importlib.util.spec_from_file_location("novacode_cli.memory.wiki", sys.argv[1])
if spec is None or spec.loader is None:
    message = "Cannot load the supplied mutation"
    raise RuntimeError(message)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
novacode_cli.memory.wiki = module

import pytest  # noqa: E402 - the mutant must be installed before collecting tests

sys.exit(pytest.main([sys.argv[2], "-q", "--timeout=90", "-p", "no:cacheprovider"]))
