"""The version-only startup optimization must preserve install detection."""

from __future__ import annotations

from importlib.metadata import distributions
from types import SimpleNamespace
from typing import TYPE_CHECKING

import deepagents._version as version

from novacode_cli.utils.startup import apply_deepagents_version_scan_patch

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    import pytest


def test_scan_requests_only_deepagents_and_remains_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []
    records = [object(), object()]

    def discover(**kwargs: str) -> Iterator[object]:
        calls.append(kwargs)
        return iter(records)

    monkeypatch.setattr(version, "distributions", discover)
    apply_deepagents_version_scan_patch()
    patched = version.distributions
    apply_deepagents_version_scan_patch()
    assert version.distributions is patched
    assert list(version.distributions()) == records
    assert calls == [{"name": "deepagents"}]


def test_editable_detection_checks_all_matching_records(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    wheel = SimpleNamespace(name="deepagents")
    editable = SimpleNamespace(name="deepagents")
    package = tmp_path / "checkout" / "deepagents"
    monkeypatch.setattr(version, "_running_package_root", lambda: package)
    monkeypatch.setattr(version, "distributions", lambda **_kwargs: iter([wheel, editable]))
    monkeypatch.setattr(
        version,
        "_editable_source_root",
        lambda dist: package.parent if dist is editable else None,
    )
    apply_deepagents_version_scan_patch()
    assert version._is_editable_install() is True


def test_unrelated_editable_checkout_does_not_mark_wheel_editable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    record = SimpleNamespace(name="deepagents")
    monkeypatch.setattr(version, "_running_package_root", lambda: tmp_path / "wheel" / "deepagents")
    monkeypatch.setattr(version, "distributions", lambda **_kwargs: iter([record]))
    monkeypatch.setattr(version, "_editable_source_root", lambda _dist: tmp_path / "other")
    apply_deepagents_version_scan_patch()
    assert version._is_editable_install() is False


def test_missing_upstream_scan_is_left_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delattr(version, "distributions")
    apply_deepagents_version_scan_patch()
    assert not hasattr(version, "distributions")


def test_filtered_discovery_preserves_duplicate_installs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    roots = [tmp_path / "wheel", tmp_path / "editable"]
    for root in roots:
        metadata = root / "deepagents-0.7.10.dist-info"
        metadata.mkdir(parents=True)
        (metadata / "METADATA").write_text("Name: deepagents\nVersion: 0.7.10\n")
        unrelated = root / "other-1.0.dist-info"
        unrelated.mkdir()
        # Unrelated metadata must never be read or decoded.
        (unrelated / "METADATA").write_bytes(b"\xff")
    monkeypatch.setattr(version, "distributions", distributions)
    apply_deepagents_version_scan_patch()
    records = list(version.distributions(path=[str(root) for root in roots]))
    assert len(records) == 2
    assert all(record.name == "deepagents" for record in records)
