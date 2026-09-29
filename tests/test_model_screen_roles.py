"""The /model role rows: retargeting, and the role travelling with a model pick.

These drive `ModelScreen` directly, without mounting it: the role logic is plain
attributes and an OptionList, and mounting a modal would test Textual rather than
this change. `_repaint` and `dismiss` are the two things that need the screen's
parent, so both are substituted.
"""

from __future__ import annotations

import pytest

from novacode_cli.config.nova_config import ROLE_NAMES


class _FakeConfig:
    """A NovaConfig stand-in, so these tests never touch the real ~/.nova.

    The first version of this file patched `Settings.from_environment` instead, and
    its assertions still described the developer's own saved model, because the
    screen resolves its config by a path that patch did not cover. Worse, the test
    that set a main model wrote it to disk. Substituting the name `role_models`
    actually calls removes the whole class of problem and keeps I/O out of the test.
    """

    def __init__(self, saved: dict[str, dict[str, str]] | None = None) -> None:
        self.saved = dict(saved or {})

    def get_role_model(self, role: str) -> dict[str, str] | None:
        entry = self.saved.get(role)
        return dict(entry) if entry else None


@pytest.fixture(autouse=True)
def _fake_config(monkeypatch):
    """Every test here runs against an empty in-memory config, never the real one."""
    fake = _FakeConfig()
    monkeypatch.setattr("novacode_cli.config.role_models.NovaConfig", lambda: fake)
    return fake


def _screen(monkeypatch, **kw):
    from textual.widgets import OptionList

    from novacode_cli.tui.screens import ModelScreen

    screen = ModelScreen(kw.get("provider", "ollama"), kw.get("model", "gemma4:31b-cloud"))
    monkeypatch.setattr(screen, "_repaint", lambda: None)
    return screen, OptionList()


def test_the_picker_offers_a_row_per_role(monkeypatch):
    screen, options = _screen(monkeypatch)
    added = screen._add_role_section(options)

    assert added == len(ROLE_NAMES) + 1, "expected a header row plus one row per role"
    roles = [pick.name for pick in screen._targets.values() if pick.kind == "role"]
    assert roles == list(ROLE_NAMES)
    # Every role row says what that role would actually run, not just its name.
    labels = {pick.name: pick.label for pick in screen._targets.values() if pick.kind == "role"}
    for role, label in labels.items():
        if role == "async":
            # The remote server owns this one, so it never claims the main model.
            assert "server default" in label, (role, label)
        else:
            assert "gemma4:31b-cloud" in label, (role, label)


def test_main_is_the_default_target_so_plain_model_use_is_unchanged(monkeypatch):
    screen, _ = _screen(monkeypatch)
    assert screen._role_target == "main"


def test_choosing_a_role_retargets_instead_of_closing(monkeypatch):
    from novacode_cli.tui.screens import _Pick

    screen, _ = _screen(monkeypatch)
    dismissed: list = []
    monkeypatch.setattr(screen, "dismiss", lambda payload=None: dismissed.append(payload))

    screen._choose(_Pick("role", "", "subagent"))

    assert screen._role_target == "subagent", "the picker did not retarget"
    assert dismissed == [], "choosing a role closed the picker, losing the user's intent"


def test_a_model_pick_carries_the_targeted_role(monkeypatch):
    from novacode_cli.tui.screens import _Pick

    screen, _ = _screen(monkeypatch)
    captured: list = []
    monkeypatch.setattr(screen, "dismiss", lambda payload=None: captured.append(payload))

    screen._role_target = "async"
    screen._choose(_Pick("model", "openai", "gpt-4o-mini"))

    (payload,) = captured
    assert payload["role"] == "async"
    assert payload["kind"] == "model"
    assert payload["provider"] == "openai"
    assert payload["model"] == "gpt-4o-mini"


def test_a_main_pick_still_reports_main(monkeypatch):
    """The app branches on this, so an unset target must not become anything else."""
    from novacode_cli.tui.screens import _Pick

    screen, _ = _screen(monkeypatch)
    captured: list = []
    monkeypatch.setattr(screen, "dismiss", lambda payload=None: captured.append(payload))

    screen._choose(_Pick("model", "ollama", "llama3"))

    assert captured[0]["role"] == "main"


def test_the_main_row_shows_the_saved_model_and_the_rest_inherit_it(monkeypatch, _fake_config):
    """The row must describe what a delegation would really run."""
    _fake_config.saved["main"] = {"provider": "openai", "model": "gpt-5-mini"}
    screen, options = _screen(monkeypatch)
    screen._add_role_section(options)

    labels = {pick.name: pick.label for pick in screen._targets.values() if pick.kind == "role"}
    assert "openai:gpt-5-mini" in labels["main"]
    assert "openai:gpt-5-mini" in labels["subagent"], labels["subagent"]
    # The async row must not claim the main model: the server decides that one, and
    # the row still names what it covers after the model.
    assert "server default" in labels["async"], labels["async"]
