"""Release notices use official, bounded changelog details with safe fallbacks."""

import base64
from unittest.mock import Mock, patch
from urllib.parse import urlparse

import pytest
from hypothesis import given
from hypothesis import strategies as st

from novacode_cli import updates as U


def test_git_update_gets_version_and_title_from_changelog(monkeypatch):
    notes = "# NovaCode 2.3.0 — Better shells\n\n- Background commands work.\n"
    request = Mock(
        side_effect=[
            [
                {"name": "CHANGELOG-v2.1.0.md"},
                {"name": "CHANGELOG-v2.3.0.md"},
                {"name": "ignore.txt"},
            ],
            {"content": base64.b64encode(notes.encode()).decode(), "encoding": "base64"},
        ]
    )
    monkeypatch.setattr(U, "_get_json", request)
    status = U.release_details(U.UpdateStatus(True, "a" * 40, "b" * 40))
    assert status.release_version == "2.3.0"
    assert status.release_title == "NovaCode 2.3.0 — Better shells"
    assert status.release_notes == notes
    assert (
        status.changelog_url
        == "https://github.com/Babitdor/NovaCode/blob/"
        + "b" * 40
        + "/changelog/CHANGELOG-v2.3.0.md"
    )
    assert "ref=" + "b" * 40 in request.call_args_list[0].args[0]


@pytest.mark.parametrize(
    "payload", [OSError("offline"), {}, [{"name": "../../evil"}], [{"name": "CHANGELOG-vbad.md"}]]
)
def test_missing_or_invalid_changelog_does_not_hide_available_update(monkeypatch, payload):
    request = (
        Mock(side_effect=payload) if isinstance(payload, Exception) else Mock(return_value=payload)
    )
    monkeypatch.setattr(U, "_get_json", request)
    status = U.release_details(U.UpdateStatus(True, "a" * 40, "b" * 40))
    assert status.available
    assert status.changelog_url == "https://github.com/Babitdor/NovaCode/tree/main/changelog"
    assert not status.release_notes


def test_package_changelog_preserves_exact_target_version_and_rejects_oversize_notes(monkeypatch):
    request = Mock(return_value={"content": "x" * 100000, "encoding": "base64"})
    monkeypatch.setattr(U, "_get_json", request)
    status = U.release_details(U.UpdateStatus(True, "1.0.0", "1.2.0"))
    assert status.release_version == "1.2.0"
    assert "CHANGELOG-v1.2.0.md" in status.changelog_url
    assert not status.release_notes


def test_unavailable_update_does_not_fetch_changelog(monkeypatch):
    request = Mock()
    monkeypatch.setattr(U, "_get_json", request)
    original = U.UpdateStatus(False, "current", "current")
    assert U.release_details(original) is original
    request.assert_not_called()


def test_valid_base64_notes_are_still_bounded_after_decode(monkeypatch):
    payload = base64.b64encode(b"x" * 65537).decode()
    monkeypatch.setattr(
        U, "_get_json", Mock(return_value={"content": payload, "encoding": "base64"})
    )
    status = U.release_details(U.UpdateStatus(True, "1.0.0", "1.2.0"))
    assert status.release_version == "1.2.0"
    assert not status.release_notes


@given(st.text(max_size=120))
def test_untrusted_revision_cannot_change_changelog_host_or_update_identity(revision):
    with patch.object(U, "_get_json", return_value={"bad": "metadata"}):
        status = U.release_details(U.UpdateStatus(True, "current", revision))
    assert status.available and status.latest == revision
    url = urlparse(status.changelog_url)
    assert url.scheme == "https" and url.netloc == "github.com"
    assert url.path.startswith("/Babitdor/NovaCode/")
