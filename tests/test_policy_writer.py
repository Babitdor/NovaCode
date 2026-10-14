"""Tests for persisting a confirmed approval rule to approval-policy.json."""

from __future__ import annotations

import json

from novacode_cli.security import policy_writer
from novacode_cli.security.policy import load_policy, reset_policy_cache
from novacode_cli.security.rule_synthesis import synthesize_rule


def test_append_shell_rule_to_project_file(tmp_path):  # noqa: ANN001
    reset_policy_cache()
    rule = synthesize_rule("shell", {"command": "npm run build"})
    path = policy_writer.append_rule(rule, target="project", project_root=tmp_path)

    assert path == tmp_path / ".nova" / "approval-policy.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert rule.value in data["shell"]["allow"]

    policy = load_policy(project_root=tmp_path)
    assert policy.evaluate("shell", {"command": "npm run build --prod"}).tier == "ask"


def test_append_is_idempotent(tmp_path):  # noqa: ANN001
    reset_policy_cache()
    rule = synthesize_rule("edit_file", {"file_path": "/src/app.py"})
    policy_writer.append_rule(rule, target="project", project_root=tmp_path)
    policy_writer.append_rule(rule, target="project", project_root=tmp_path)
    data = json.loads((tmp_path / ".nova" / "approval-policy.json").read_text(encoding="utf-8"))
    assert data["paths"]["allow"].count(rule.value) == 1


def test_append_tool_rule_sets_tier(tmp_path):  # noqa: ANN001
    reset_policy_cache()
    rule = synthesize_rule("write_memory", {"content": "x"})
    policy_writer.append_rule(rule, target="project", project_root=tmp_path)
    data = json.loads((tmp_path / ".nova" / "approval-policy.json").read_text(encoding="utf-8"))
    assert data["tools"]["write_memory"] == "allow"


def test_append_global_uses_home_dir(tmp_path, monkeypatch):  # noqa: ANN001
    reset_policy_cache()
    monkeypatch.setattr(policy_writer, "HOME_DIR", tmp_path)
    rule = synthesize_rule("fetch_url", {"url": "https://docs.python.org/3/"})
    path = policy_writer.append_rule(rule, target="global")
    assert path == tmp_path / "approval-policy.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert "docs.python.org" in data["domains"]["allow"]


def test_append_preserves_existing_unrelated_keys(tmp_path):  # noqa: ANN001
    reset_policy_cache()
    cfg = tmp_path / ".nova" / "approval-policy.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(json.dumps({"tools": {"shell": "ask"}}), encoding="utf-8")
    rule = synthesize_rule("shell", {"command": "ls"})
    policy_writer.append_rule(rule, target="project", project_root=tmp_path)
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert data["tools"]["shell"] == "ask"
    assert rule.value in data["shell"]["allow"]
