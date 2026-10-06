"""NOVA CLI - Interactive AI coding assistant."""

import os as _os

# Must run before numpy is imported anywhere in the process. numpy's OpenBLAS
# pre-allocates a buffer per CPU thread at import: on a 12-thread machine that
# is ~380 MB of private memory for nothing — Nova's numeric work is small
# vector searches (skill and code retrieval over a few thousand 256-d rows),
# where one thread is measurably FASTER (91 us vs 180 us for a 1000-row
# search) because thread hand-off costs more than the arithmetic. One thread:
# ~9 MB. It applies to every Nova process — the TUI and each spawned session.
# setdefault, so an explicit setting in the environment still wins.
_os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
# Other numerical backends also default to machine-wide thread pools. Multiple
# Nova processes should not each occupy every core; explicit user values win.
_os.environ.setdefault("OMP_NUM_THREADS", "1")
_os.environ.setdefault("MKL_NUM_THREADS", "1")
_os.environ.setdefault("NUMEXPR_NUM_THREADS", "1")

# Must run before langchain_core is imported: it pulls in `transformers` (and
# therefore torch/numpy/PIL) at module scope for a fallback tokenizer Nova never
# uses, costing ~160 MB RSS and ~10 s of startup. See novacode_cli._lazy_heavy.
from novacode_cli import _lazy_heavy as _lazy_heavy

_lazy_heavy.install()

# from novacode_cli.main import cli_main

__all__ = []
