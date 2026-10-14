"""Replay full-suite failures in an untouched archived source tree."""
import argparse
import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", type=Path)
    parser.add_argument("results", type=Path)
    parser.add_argument("--timeout", default="20")
    args = parser.parse_args()
    targets = []
    for case in ET.parse(args.results).iter("testcase"):
        if case.find("failure") is None:
            continue
        parts = case.attrib["classname"].split(".")
        index = next(i for i, name in enumerate(parts) if name.startswith("test_"))
        filename = "/".join(parts[:index + 1]) + ".py"
        target = filename + "::" + "::".join([*parts[index + 1:], case.attrib["name"]])
        if (args.baseline / filename).is_file():
            targets.append(target)
    if not targets:
        raise RuntimeError("No baseline test targets found")
    output = Path("context-learning-baseline-tests.xml").resolve()
    command = [sys.executable, "-m", "pytest", *targets, "-q", "--tb=short", "--timeout=" + args.timeout, "--junitxml=" + str(output)]
    result = subprocess.run(command, cwd=args.baseline.resolve(), capture_output=True, encoding="utf-8", errors="replace")
    Path("context-learning-baseline.json").write_text(json.dumps({"command": command, "exit_code": result.returncode, "targets": targets, "output": result.stdout}, indent=2), encoding="utf-8")
    print(result.stdout)
    print(result.stderr, file=sys.stderr)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
