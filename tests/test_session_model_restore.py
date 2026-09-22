"""Resuming a session restores the model that session was using.

The session's model was always *saved* (``SessionMeta.model_name``) but never
read back: ``create_model()`` takes no arguments and ran before the session was
loaded, so a resume silently adopted whatever the global config said. The
picker even displayed the session's model, which made the label a lie.

These tests pin the restore path and, just as importantly, the two fallbacks:
a legacy session (no recorded provider) must fall back *silently*, and an
unavailable model must fall back *with a warning*.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from novacode_cli.config.model_create import create_model_for_session
from novacode_cli.session.session_persistence import SessionManager, SessionMeta
from novacode_cli.session.session_restore import format_session_summary

if TYPE_CHECKING:
    from pathlib import Path


def _write_meta(root: Path, session_id: str, **overrides: Any) -> Path:
    """Write a minimal meta.json, as save_session would."""
    d = root / session_id
    d.mkdir(parents=True, exist_ok=True)
    meta = {
        "session_id": session_id,
        "thread_id": "t",
        "created_at": "2026-01-01T00:00:00+00:00",
        "last_active": "2026-01-01T00:00:00+00:00",
        "project_root": None,
        "repo_hash": None,
        "Nova_md_checksum": None,
        "model_name": "some-model",
        "assistant_id": "a",
    }
    meta.update(overrides)
    (d / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return d


# ── the restore itself ────────────────────────────────────────────────────────


def test_session_model_is_rebuilt_from_the_recorded_provider(tmp_path: Path) -> None:
    """The core fix: a session's own model is rebuilt, not the global default."""
    _write_meta(
        tmp_path,
        "s1",
        model_provider="ollama",
        model_name="qwen3-coder:480b-cloud",
    )
    meta = SessionManager(sessions_dir=tmp_path).load_session_meta("s1")
    assert meta is not None

    model, warning = create_model_for_session(meta.model_provider, meta.model_name)

    assert model is not None, "the session's model should have been rebuilt"
    assert warning is None, "a successful restore must not warn"
    # The rebuilt model must be the SESSION's model, not some other default.
    assert getattr(model, "model", None) == "qwen3-coder:480b-cloud"


def test_unavailable_model_warns_and_falls_back() -> None:
    """An unrestorable model must warn, naming what could not be restored."""
    model, warning = create_model_for_session("bogus-provider", "some-model")

    assert model is None, "an unavailable provider must not yield a model"
    assert warning is not None, "the user must be told why they are not on their model"
    assert "bogus-provider" in warning
    assert "some-model" in warning


def test_legacy_session_falls_back_silently(tmp_path: Path) -> None:
    """A session saved before the provider was recorded must not warn.

    This is the common case for every session already on disk, so a warning
    here would be a warning storm on first resume after upgrading.
    """
    _write_meta(tmp_path, "legacy", model_name="gpt-4o")  # no model_provider
    meta = SessionManager(sessions_dir=tmp_path).load_session_meta("legacy")
    assert meta is not None
    assert meta.model_provider is None

    model, warning = create_model_for_session(meta.model_provider, meta.model_name)

    assert model is None
    assert warning is None, "a legacy session must fall back without warning"


def test_missing_model_name_falls_back_silently() -> None:
    """No recorded model at all is not an error."""
    assert create_model_for_session("ollama", None) == (None, None)
    assert create_model_for_session(None, "gpt-4o") == (None, None)


# ── persistence / backward compatibility ──────────────────────────────────────


def test_meta_without_provider_loads_as_none() -> None:
    """Old meta.json files must load unchanged, not raise."""
    old = {
        "session_id": "s",
        "thread_id": "t",
        "created_at": "c",
        "last_active": "l",
        "project_root": None,
        "repo_hash": None,
        "Nova_md_checksum": None,
        "model_name": "gpt-4o",
        "assistant_id": "a",
    }
    meta = SessionMeta.from_dict(old)

    assert meta.model_provider is None
    assert meta.model_name == "gpt-4o"
    # The field must not have displaced an existing one.
    assert meta.message_count == 0


def test_provider_round_trips_through_save_and_load(tmp_path: Path) -> None:
    """save_session must persist the provider, and load_session_meta read it."""
    sm = SessionManager(sessions_dir=tmp_path)
    sm.save_session(
        session_id="s2",
        thread_id="t",
        messages=[],
        assistant_id="a",
        model_name="claude-sonnet-4-5-20250929",
        model_provider="anthropic",
    )

    meta = sm.load_session_meta("s2")

    assert meta is not None
    assert meta.model_provider == "anthropic"
    assert meta.model_name == "claude-sonnet-4-5-20250929"


def test_load_session_meta_missing_session_returns_none(tmp_path: Path) -> None:
    assert SessionManager(sessions_dir=tmp_path).load_session_meta("nope") is None


# ── the label must not promise a restore it cannot perform ────────────────────


def test_label_marks_legacy_sessions_as_not_restored() -> None:
    legacy = SessionMeta.from_dict(
        {
            "session_id": "abcdef12",
            "thread_id": "t",
            "created_at": "c",
            "last_active": "2026-01-01T00:00:00+00:00",
            "project_root": None,
            "repo_hash": None,
            "Nova_md_checksum": None,
            "model_name": "gpt-4o",
            "assistant_id": "a",
        }
    )
    assert "not restored" in format_session_summary(legacy)


def test_label_does_not_mark_restorable_sessions() -> None:
    restorable = SessionMeta.from_dict(
        {
            "session_id": "abcdef12",
            "thread_id": "t",
            "created_at": "c",
            "last_active": "2026-01-01T00:00:00+00:00",
            "project_root": None,
            "repo_hash": None,
            "Nova_md_checksum": None,
            "model_name": "gpt-4o",
            "model_provider": "openai",
            "assistant_id": "a",
        }
    )
    summary = format_session_summary(restorable)

    assert "gpt-4o" in summary
    assert "not restored" not in summary
