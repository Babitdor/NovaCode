"""Home dashboard layout and local system telemetry regressions."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

from novacode_cli.tui.home import SystemSampler, _rate

if TYPE_CHECKING:
    import pytest


def test_system_sampler_uses_counter_deltas(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    from novacode_cli.tui import home

    readings = iter(
        (
            SimpleNamespace(bytes_recv=1000, bytes_sent=2000),
            SimpleNamespace(bytes_recv=3048, bytes_sent=3024),
        )
    )
    monkeypatch.setitem(
        sys.modules,
        "psutil",
        SimpleNamespace(
            cpu_percent=lambda **_kwargs: 25.0,
            virtual_memory=lambda: SimpleNamespace(percent=50.0),
            net_io_counters=lambda: next(readings),
        ),
    )
    monkeypatch.setattr(home.time, "monotonic", iter((10.0, 12.0)).__next__)
    sampler = SystemSampler()
    first = sampler.sample()
    second = sampler.sample()
    assert first is not None
    assert first.received_per_second is None
    assert second is not None
    assert second.cpu == 25.0
    assert second.memory == 50.0
    assert second.received_per_second == 1024.0
    assert second.sent_per_second == 512.0
    assert _rate(second.received_per_second) == "1.0 KB/s"
