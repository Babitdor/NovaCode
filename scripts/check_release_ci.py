"""Fail closed unless the exact release commit passed the main-branch CI workflow."""

import json
import os
import subprocess
import urllib.parse
import urllib.request


def require_success(runs: list[dict], sha: str) -> dict:
    eligible = [
        run
        for run in runs
        if run.get("head_sha") == sha
        and run.get("head_branch") == "main"
        and run.get("event") == "push"
    ]
    if not eligible:
        raise ValueError("No main-branch CI run found for the release commit")
    latest = max(eligible, key=lambda run: (run["run_number"], run.get("run_attempt", 1)))
    if latest.get("status") != "completed" or latest.get("conclusion") != "success":
        raise ValueError(
            "Latest CI run must finish successfully before publishing: "
            + latest.get("html_url", "unknown run")
        )
    return latest


def main() -> None:
    # Resolve annotated tags to the actual checkout commit, not the tag object.
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    query = urllib.parse.urlencode(
        {"head_sha": sha, "branch": "main", "event": "push", "per_page": 100}
    )
    url = (
        f"{os.environ['GITHUB_API_URL']}/repos/{os.environ['GITHUB_REPOSITORY']}"
        f"/actions/workflows/ci.yml/runs?{query}"
    )
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {os.environ['GH_TOKEN']}",
            "Accept": "application/vnd.github+json",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        runs = json.load(response)["workflow_runs"]
    run = require_success(runs, sha)
    print(f"Verified CI for {sha}: {run['html_url']}")


if __name__ == "__main__":
    main()
