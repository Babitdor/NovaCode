"""Wider control sweep: is Tev1:4b's error systematic?

The four-question control showed the 4B assigning a higher probability to a
false statement (0.678) than to a true one (0.583). Two of four answers were
decisive and correct (0.953, 0.046), so this asks whether the misses are noise
on hard wording or a systematic reversal, by running more controls of both kinds
on one state.
"""

from __future__ import annotations

import json
import sys
import urllib.request

ENDPOINT = "http://127.0.0.1:11434/v1/systemone"

STATE = (
    "A coding assistant conversation. The user asked to fix a failing parser "
    "test. The assistant ran pytest and it FAILED with one assertion error in "
    "test_trailing_comma. The assistant edited src/parser.py, re-ran the test, "
    "and it PASSED. The user's final message was: 'Great. Next, add a changelog "
    "entry for this fix.'"
)

CONTROLS = [
    ("t_test_failed_before", "The test run described failed before the edit was made.", True),
    ("f_user_asked_delete", "The user asked the assistant to delete the parser module.", False),
    ("t_edit_was_made", "An edit to src/parser.py was made during this conversation.", True),
    ("f_asked_rust_tests", "The assistant was asked to write unit tests for a Rust codebase.", False),
    ("t_user_mentioned_changelog", "The user mentioned a changelog.", True),
    ("f_parser_rewritten_in_rust", "The parser was rewritten in Rust.", False),
    ("t_test_passed_after_edit", "The test passed after the edit.", True),
    ("f_user_asked_for_refund", "The user asked for a refund.", False),
    ("t_pytest_was_run", "pytest was run during this conversation.", True),
    ("f_database_was_deleted", "A database was deleted.", False),
    ("t_file_src_parser_edited", "The file src/parser.py was edited.", True),
    ("f_user_asked_about_kubernetes", "The user asked about Kubernetes.", False),
]


def ask(model: str) -> dict:
    questions = {
        name: {"type": "noul", "instructions": text} for name, text, _ in CONTROLS
    }
    payload = json.dumps({"model": model, "state": STATE, "questions": questions})
    request = urllib.request.Request(
        ENDPOINT,
        data=payload.encode("utf-8"),
        headers={"content-type": "application/json", "authorization": "Bearer ollama"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=300) as response:
        return json.loads(response.read().decode("utf-8"))


for model in ("tev1:4b", "tev1:0.8b"):
    try:
        body = ask(model)
    except Exception as error:  # noqa: BLE001 - a probe reports, it does not raise
        print(f"{model}: FAILED {type(error).__name__}: {error}")
        continue

    answers = body.get("answers", {})
    true_values: list[float] = []
    false_values: list[float] = []
    wrong: list[str] = []

    print(f"=== {body.get('model', model)} ===")
    print(f"{'name':<30}{'expect':>7}{'noul':>8}   ok")
    print("-" * 54)
    for name, _text, expected in CONTROLS:
        value = (answers.get(name) or {}).get("noul")
        if not isinstance(value, (int, float)):
            print(f"{name:<30}{'?':>7}   MISSING")
            continue
        value = float(value)
        (true_values if expected else false_values).append(value)
        correct = (value > 0.5) == expected
        if not correct:
            wrong.append(name)
        print(f"{name:<30}{str(expected):>7}{value:>8.3f}   {'ok' if correct else 'WRONG'}")

    print()
    print(f"true  : {min(true_values):.3f} .. {max(true_values)}  (n={len(true_values)})")
    print(f"false : {min(false_values):.3f} .. {max(false_values)}  (n={len(false_values)})")
    print(f"margin (min true - max false): {min(true_values) - max(false_values):+.3f}")
    print(f"wrong at a 0.5 cut: {len(wrong)}/{len(CONTROLS)} {wrong}")
    print()
