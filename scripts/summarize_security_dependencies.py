"""Summarize pip-audit output without calling duplicate entries new findings."""

from __future__ import annotations

import json
import argparse
import tomllib
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "docs" / "security-audit"


def main():
    global REPORTS
    parser = argparse.ArgumentParser()
    parser.add_argument("--reports", type=Path, default=REPORTS)
    REPORTS = parser.parse_args().reports
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    edges = defaultdict(set)
    for package in lock["package"]:
        edges[package["name"]].update(d["name"] for d in package.get("dependencies", []))
    runtime = set()
    pending = ["novacode-cli"]
    while pending:
        name = pending.pop()
        if name in runtime:
            continue
        runtime.add(name)
        pending.extend(edges[name] - runtime)
    packages = []
    audited, raw_count, unique_count = 0, 0, 0
    for filename in ("dependencies.json", "dependencies-alternate.json"):
        data = json.loads((REPORTS / filename).read_text(encoding="utf-8"))
        for package in data["dependencies"]:
            if "skip_reason" in package:
                raise RuntimeError(f"Unaudited package {package['name']}: {package['skip_reason']}")
            audited += 1
            vulnerabilities = package.get("vulns", [])
            if not vulnerabilities:
                continue
            unique = {v["id"]: v for v in vulnerabilities}
            raw_count += len(vulnerabilities)
            unique_count += len(unique)
            packages.append({
                "name": package["name"], "version": package["version"],
                "possibly_in_base_dependency_closure": package["name"] in runtime,
                "raw_advisory_entries": len(vulnerabilities),
                "unique_primary_advisory_ids": len(unique),
                "advisories": [{"id": v["id"], "aliases": v.get("aliases", []),
                                "fix_versions": v.get("fix_versions", [])} for v in unique.values()],
            })
    summary = {"registry_versions_audited": audited, "packages_with_advisories": len(packages),
               "raw_advisory_entries": raw_count, "unique_primary_advisory_ids": unique_count,
               "packages": packages,
               "limits": "IDs deduplicated within each version, not across aliases. Base dependency closure is conservative: platform markers are not evaluated. Advisory presence does not establish exploitation in Nova."}
    (REPORTS / "dependency-summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in summary.items() if key != "packages"}, indent=2))


if __name__ == "__main__":
    main()
