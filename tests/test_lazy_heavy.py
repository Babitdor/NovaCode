"""`transformers`/`torch` must stay out of Nova's startup import graph.

langchain_core imports `transformers` at module scope for a *fallback* GPT-2
token counter Nova never uses; it drags in torch, numpy, huggingface_hub and
PIL. Measured on this project that is ~180 MB of resident memory and ~10 s of
startup — enough to exhaust the paging file on a constrained machine.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

from novacode_cli import _lazy_heavy


def _run(code: str, env_extra: dict[str, str] | None = None) -> str:
    """Run *code* in a FRESH interpreter and return its stdout.

    A subprocess is required: this is about what gets imported at startup, and
    the test process has already imported everything.
    """
    import os

    env = dict(os.environ)
    env.pop("NOVA_ALLOW_TRANSFORMERS", None)
    if env_extra:
        env.update(env_extra)
    out = subprocess.run(  # noqa: S603
        [sys.executable, "-c", textwrap.dedent(code)],
        capture_output=True,
        text=True,
        timeout=180,
        env=env,
        check=False,
    )
    assert out.returncode == 0, f"subprocess failed:\n{out.stdout}\n{out.stderr}"
    return out.stdout.strip()


def test_importing_nova_does_not_load_torch_or_transformers():
    """The regression: a plain `import novacode_cli.main` pulled in torch."""
    out = _run(
        """
        import sys, json
        import novacode_cli.main  # noqa: F401
        print(json.dumps({
            "torch": "torch" in sys.modules,
            "transformers": "transformers" in sys.modules,
        }))
        """
    )
    import json

    loaded = json.loads(out.splitlines()[-1])
    assert not loaded["torch"], "torch was imported at startup (~160 MB)"
    assert not loaded["transformers"], "transformers was imported at startup"


def test_langchain_degrades_gracefully_rather_than_crashing():
    """The guard raises ImportError — the branch langchain_core already handles."""
    out = _run(
        """
        import json
        import novacode_cli  # installs the guard
        import langchain_core.language_models.base as b
        print(json.dumps({"has_transformers": b._get_tokenizer_module() is not None}))
        """
    )
    import json

    assert json.loads(out.splitlines()[-1])["has_transformers"] is False


def test_env_var_opts_out():
    """A user who genuinely needs transformers can re-enable it."""
    out = _run(
        """
        import json
        import novacode_cli
        from novacode_cli import _lazy_heavy
        print(json.dumps({"active": _lazy_heavy.is_active()}))
        """,
        env_extra={"NOVA_ALLOW_TRANSFORMERS": "1"},
    )
    import json

    assert json.loads(out.splitlines()[-1])["active"] is False


def test_allow_transformers_removes_the_guard():
    """Programmatic escape hatch, so nothing is permanently locked out."""
    was_active = _lazy_heavy.is_active()
    try:
        _lazy_heavy.install()
        _lazy_heavy.allow_transformers()
        assert not _lazy_heavy.is_active()
    finally:
        if was_active:
            _lazy_heavy.install()


def test_guard_does_not_block_ordinary_imports():
    """Only the named packages are refused."""
    _lazy_heavy.install()
    import json as _json  # noqa: F401  — a normal import must still work

    import novacode_cli.compaction  # noqa: F401
