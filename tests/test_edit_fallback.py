"""An old_string that is right except for indentation still lands (Codex-style)."""

from __future__ import annotations

import novacode_cli.agents.core_agent  # noqa: F401 — installs the patched replacement
from deepagents.backends import utils

FILE = (
    "def process(input_path, output_path):\n"
    "    totals = {}\n"
    "    with open(input_path) as f:\n"
    "        lines = f.readlines()\n"
    "    for line in lines[1:-1]:  # skip header\n"
    "        parts = line.strip().split(',')\n"
)


def test_the_eval_failure_now_lands_at_the_real_indentation() -> None:
    # Verbatim shape from the fix-data-processing-bug trace: the model folded
    # the read_file gutter's two spaces into every line after the first.
    head = "with open(input_path) as f:\n          lines = f.readlines()\n"
    old = head + "      for line in lines[1:-1]:  # skip header"
    new = head + "      for line in lines[1:]:  # skip header"
    content, count = utils.perform_string_replacement(FILE, old, new)
    assert count == 1
    assert "    for line in lines[1:]:  # skip header\n" in content
    assert "        lines = f.readlines()\n" in content
    assert content.count("\n") == FILE.count("\n")


def test_a_whole_block_shifted_by_the_gutter() -> None:
    old = "      totals = {}\n      with open(input_path) as f:"
    new = "      totals: dict = {}\n      with open(input_path) as f:"
    content, _ = utils.perform_string_replacement(FILE, old, new)
    assert "    totals: dict = {}\n    with open(input_path) as f:\n" in content


def test_ambiguous_or_inconsistent_matches_still_fail() -> None:
    twice = FILE + "    totals = {}\n"
    assert isinstance(utils.perform_string_replacement(twice, "  totals = {}", "x"), str)
    # Lines disagree on the shift: not a gutter slip, so do not guess.
    old = "    totals = {}\n            with open(input_path) as f:\n    lines = f.readlines()"
    assert isinstance(utils.perform_string_replacement(FILE, old, "x\ny\nz"), str)


def test_exact_matches_and_crlf_files_are_untouched_by_the_fallback() -> None:
    content, _ = utils.perform_string_replacement(FILE, "totals = {}", "totals = dict()")
    assert "    totals = dict()\n" in content
    crlf = FILE.replace("\n", "\r\n")
    content, _ = utils.perform_string_replacement(crlf, "      totals = {}", "      totals = []")
    assert "    totals = []\r\n" in content and "\n" not in content.replace("\r\n", "")
