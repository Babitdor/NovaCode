"""Positive control for a decision model's `noul` probabilities.

A `noul` answer is only usable for gating if it *separates* a question whose
answer is certainly true from one whose answer is certainly false. Tev1:0.8b
returned 0.51-0.76 for both kinds -- an uncalibrated band in which no threshold
can mean anything. This runs the same paired test against a given model, so the
question is answered before any threshold is tuned.

Usage:
    python scripts/probe_noul_control.py --model tev1:4b
    python scripts/probe_noul_control.py --model tev1:0.8b
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

ENDPOINT = "http://127.0.0.1:11434/v1/systemone"

#: A state in which the yes/no answers are not in doubt.
STATE = (
    "A coding assistant conversation. The user asked to fix a failing parser "
    "test. The assistant ran `pytest tests/test_parser.py` and the run FAILED "
    "with one assertion error in test_trailing_comma. The assistant then edited "
    "src/parser.py, re-ran the same test, and it PASSED. The user's final "
    "message was: 'Great. Next, add a changelog entry for this fix.'"
)

#: (name, question, what the true answer is). Certainty is the whole point.
CONTROLS: list[tuple[str, str, bool]] = [
    (
        "clearly_true",
        "The test run described in the history failed before the edit was made.",
        True,
    ),
    (
        "clearly_false",
        "The user asked the assistant to delete the parser module.",
        False,
    ),
    (
        "clearly_true_2",
        "An edit to src/parser.py was made during this conversation.",
        True,
    ),
    (
        "clearly_false_2",
        "The assistant was asked to write unit tests for a Rust codebase.",
        False,
    ),
]

#: The band Tev1:0.8b produced for every question, true or false.
UNUSABLE_BAND = (0.51, 0.76)


def ask(model: str, questions: dict) -> dict:
    payload = json.dumps({"model": model, "state": STATE, "questions": questions})
    request = urllib.request.Request(
        ENDPOINT,
        data=payload.encode("utf-8"),
        headers={"content-type": "application/json", "authorization": "Bearer ollama"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=300) as response:
        return json.loads(response.read().decode("utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="tev1:4b")
    args = parser.parse_args(argv)

    questions = {
        name: {"type": "noul", "instructions": text}
        for name, text, _ in CONTROLS
    }

    try:
        body = ask(args.model, questions)
    except urllib.error.HTTPError as error:
        print(f"HTTP {error.code}: {error.read().decode('utf-8', 'replace')[:300]}")
        return 1
    except urllib.error.URLError as error:
        print(f"Could not reach {ENDPOINT}: {error}")
        return 1

    answers = body.get("answers", {})
    usage = body.get("usage", {})

    print(f"model: {body.get('model', args.model)}")
    print(f"usage: {usage}")
    print()
    print(f"{'question':<18}{'true answer':>12}{'noul':>8}   verdict")
    print("-" * 56)

    true_values: list[float] = []
    false_values: list[float] = []
    for name, _text, expected in CONTROLS:
        answer = answers.get(name) or {}
        value = answer.get("noul")
        if not isinstance(value, (int, float)):
            print(f"{name:<18}{str(expected):>12}{'MISSING':>8}   no answer returned")
            continue
        (true_values if expected else false_values).append(float(value))
        # A usable probability puts a true statement above the false ones.
        print(f"{name:<18}{str(expected):>12}{value:>8.3f}")

    print()
    if not true_values or not false_values:
        print("INCONCLUSIVE: not enough answers to compare")
        return 1

    highest_false = max(false_values)
    lowest_true = min(true_values)
    print(f"true statements : {min(true_values):.3f} .. {max(true_values):.3f}")
    print(f"false statements: {min(false_values):.3f} .. {max(false_values):.3f}")
    print(f"margin (lowest true - highest false): {lowest_true - highest_false:+.3f}")

    # The control is the whole test: does a threshold exist that separates them?
    if lowest_true > highest_false:
        threshold = (lowest_true + highest_false) / 2
        print()
        print(
            f"USABLE: a threshold in ({highest_false:.3f}, {lowest_true:.3f}) "
            f"separates every control, e.g. keep_threshold={threshold:.2f}"
        )
        return 0

    print()
    print(
        "UNUSABLE: the true and false bands overlap, so no threshold can "
        "separate them. Any cutting point would clear results the model judged "
        "both ways."
    )
    lo, hi = UNUSABLE_BAND
    if all(lo <= value <= hi for value in true_values + false_values):
        print(
            f"Every answer sits inside the {lo}-{hi} band the 0.8B produced for "
            "both kinds of question."
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
