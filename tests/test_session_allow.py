"""Tests for the in-memory 'allow for this session' layer."""

from __future__ import annotations

from novacode_cli.security.rule_synthesis import synthesize_rule
from novacode_cli.security.session_allow import (
    SessionAllowList,
    get_session_allow,
    reset_session_allow,
)


def test_empty_list_matches_nothing():
    s = SessionAllowList()
    assert s.matches("shell", {"command": "npm run build"}) is False


def test_shell_rule_matches_same_program():
    s = SessionAllowList()
    s.add(synthesize_rule("shell", {"command": "npm run build"}))
    assert s.matches("shell", {"command": "npm run build"}) is True
    assert s.matches("shell", {"command": "npm run build --prod"}) is True
    assert s.matches("shell", {"command": "npm test"}) is False


def test_path_rule_matches_within_directory():
    s = SessionAllowList()
    s.add(synthesize_rule("edit_file", {"file_path": "/src/app.py"}))
    assert s.matches("edit_file", {"file_path": "/src/util.py"}) is True
    assert s.matches("edit_file", {"file_path": "/tests/x.py"}) is False


def test_tool_rule_matches_any_call_of_that_tool():
    s = SessionAllowList()
    s.add(synthesize_rule("write_memory", {"content": "x"}))
    assert s.matches("write_memory", {"content": "anything"}) is True


def test_session_allow_cannot_override_builtin_deny():
    s = SessionAllowList()
    s.add(synthesize_rule("shell", {"command": "sudo apt update"}))
    assert s.matches("shell", {"command": "sudo apt update"}) is False


def test_singleton_get_and_reset():
    reset_session_allow()
    a = get_session_allow()
    a.add(synthesize_rule("shell", {"command": "ls"}))
    assert get_session_allow() is a
    assert get_session_allow().matches("shell", {"command": "ls -la"}) is True
    reset_session_allow()
    assert get_session_allow().matches("shell", {"command": "ls -la"}) is False


def test_domain_grant_and_clear():
    rules = SessionAllowList()
    rules.add(synthesize_rule("fetch_url", {"url": "https://example.com/a"}))
    assert rules.matches("fetch_url", {"url": "https://example.com/b"})
    assert not rules.matches("fetch_url", {"url": "https://other.invalid/"})
    rules.clear()
    assert not rules.matches("fetch_url", {"url": "https://example.com/b"})
