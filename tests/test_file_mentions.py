"""``@file`` mentions must actually put the file's contents in the prompt.

A mention used to be parsed and then thrown away (``parse_file_mentions``
returned the text unchanged and its caller ignored the file list), so
``@foo.py`` reached the model as the literal string. These tests pin the
behaviour: resolve the path, inline the contents, and stay bounded.
"""

from __future__ import annotations

import pytest

from novacode_cli.core import input_preparation as ip
from novacode_cli.input_utils import (
    MAX_MENTION_DIR_ENTRIES,
    MAX_MENTION_FILE_CHARS,
    parse_file_mentions,
)


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    """A temp project dir used as both the cwd and the workspace root."""
    monkeypatch.setattr("novacode_cli.input_utils._mention_roots", lambda: [tmp_path])
    monkeypatch.chdir(tmp_path)
    return tmp_path


# ── parsing ──────────────────────────────────────────────────────────────────


def test_parses_a_file_and_returns_its_path(workspace):
    (workspace / "app.py").write_text("x = 1\n", encoding="utf-8")
    _, files = parse_file_mentions("look at @app.py")
    assert [p.name for p in files] == ["app.py"]


def test_parses_quoted_path_with_spaces(workspace):
    (workspace / "my file.py").write_text("x = 1\n", encoding="utf-8")
    _, files = parse_file_mentions('see @"my file.py"')
    assert [p.name for p in files] == ["my file.py"]


def test_parses_escaped_path_with_spaces(workspace):
    (workspace / "my file.py").write_text("x = 1\n", encoding="utf-8")
    _, files = parse_file_mentions(r"see @my\ file.py")
    assert [p.name for p in files] == ["my file.py"]


def test_parses_a_directory(workspace):
    (workspace / "pkg").mkdir()
    _, files = parse_file_mentions("inspect @pkg")
    assert [p.name for p in files] == ["pkg"]
    assert files[0].is_dir()


def test_parses_extensionless_file(workspace):
    (workspace / "Makefile").write_text("all:\n", encoding="utf-8")
    _, files = parse_file_mentions("@Makefile")
    assert [p.name for p in files] == ["Makefile"]


def test_duplicate_mentions_are_deduped(workspace):
    (workspace / "app.py").write_text("x = 1\n", encoding="utf-8")
    _, files = parse_file_mentions("@app.py then @app.py again")
    assert len(files) == 1


def test_false_positives_are_ignored(workspace):
    """Decorators, CSS at-rules and emails must not become file mentions."""
    _, files = parse_file_mentions(
        "@dataclass @keyframes @property @user@domain.com @nosuchfile.py"
    )
    assert files == []


def test_mention_with_trailing_punctuation_resolves(workspace):
    """Prose punctuation glued to a mention must not break resolution.

    "what does @app.py?" captured the "?" into the path, so it was dropped as
    an email-like token and the file never reached the model.
    """
    (workspace / "app.py").write_text("x = 1\n", encoding="utf-8")
    _, files = parse_file_mentions("what does @app.py?")
    assert [p.name for p in files] == ["app.py"]


def test_relative_mention_resolves_against_the_workspace_root(workspace, monkeypatch):
    """The TUI picker emits workspace-root-relative paths, so the parser must
    resolve them there even when the process cwd is a subdirectory."""
    (workspace / "src").mkdir()
    (workspace / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
    sub = workspace / "src"
    monkeypatch.chdir(sub)
    monkeypatch.setattr("novacode_cli.input_utils._mention_roots", lambda: [workspace])
    _, files = parse_file_mentions("@src/app.py")
    assert [p.name for p in files] == ["app.py"]


# ── inlining ─────────────────────────────────────────────────────────────────


async def test_file_contents_are_inlined(workspace):
    (workspace / "app.py").write_text("SECRET_MARKER = 1\n", encoding="utf-8")
    out = await ip.prepare_input_content("look at @app.py")
    assert isinstance(out, str)
    assert "## Referenced Files" in out
    assert "SECRET_MARKER = 1" in out
    assert str(workspace / "app.py") in out


async def test_quoted_path_with_space_is_inlined(workspace):
    (workspace / "my file.py").write_text("SPACED = 1\n", encoding="utf-8")
    out = await ip.prepare_input_content('see @"my file.py"')
    assert "SPACED = 1" in out


async def test_no_mentions_leaves_input_untouched(workspace):
    out = await ip.prepare_input_content("just a normal message")
    assert out == "just a normal message"
    assert "## Referenced Files" not in out


async def test_skip_file_mentions_disables_inlining(workspace):
    (workspace / "app.py").write_text("x = 1\n", encoding="utf-8")
    out = await ip.prepare_input_content("look at @app.py", skip_file_mentions=True)
    assert "## Referenced Files" not in out
    assert out == "look at @app.py"


async def test_large_file_is_truncated_with_a_visible_notice(workspace):
    (workspace / "big.py").write_text("y = 2\n" * 20_000, encoding="utf-8")
    out = await ip.prepare_input_content("read @big.py")
    assert "file truncated" in out
    # Bounded: the rendered block stays near the per-file cap.
    assert len(out) < MAX_MENTION_FILE_CHARS + 1_000


async def test_binary_file_is_not_inlined(workspace):
    (workspace / "blob.bin").write_bytes(b"\x00\x01\x02\x03binary")
    out = await ip.prepare_input_content("read @blob.bin")
    assert "binary file" in out
    assert "\x00" not in out


# ── image mentions attach the picture, not a binary notice ──────────────────


def _write_png(workspace, name: str = "shot.png"):
    from PIL import Image

    path = workspace / name
    Image.new("RGB", (8, 8), (10, 20, 30)).save(path, format="PNG")
    return path


async def test_image_mention_is_attached_as_an_image_block(workspace, monkeypatch):
    """``@screenshot.png`` must reach a multimodal model as an image.

    The old path fed a binary file through the text inliner, so the model got
    ``[binary file, N bytes — not inlined]`` and never the picture — even though
    ``/images`` documents ``@path/to/image.png`` as an attach route.
    """
    _write_png(workspace)
    monkeypatch.setattr(ip, "_main_model_can_see_images", lambda: True)

    out = await ip.prepare_input_content("what is @shot.png?")
    assert isinstance(out, list), "an image mention must produce image blocks"
    assert out[0]["type"] == "text"
    assert "@shot.png" in out[0]["text"]
    assert any(b.get("type") == "image_url" for b in out)
    assert "binary file" not in str(out)


async def test_image_mention_is_captioned_for_a_text_only_model(workspace, monkeypatch):
    """A text-only model gets a caption for an ``@image`` mention, not blocks."""
    import novacode_cli.bootstrap.vision_router as vr

    _write_png(workspace)
    monkeypatch.setattr(ip, "_main_model_can_see_images", lambda: False)

    async def fake_captions(urls, *args, **kwargs):  # noqa: ANN002, ANN003
        return "a dark blue square"

    monkeypatch.setattr(vr, "caption_images", fake_captions)

    out = await ip.prepare_input_content("what is @shot.png?")
    assert isinstance(out, str)
    assert "a dark blue square" in out
    assert "binary file" not in out


async def test_unloadable_image_mention_falls_back_to_the_binary_notice(workspace):
    """A corrupt ``.png`` must not vanish silently — the notice still explains it."""
    (workspace / "broken.png").write_bytes(b"\x00\x01notreallyanimage")

    out = await ip.prepare_input_content("read @broken.png")
    assert isinstance(out, str)
    assert "binary file" in out


async def test_directory_mention_lists_entries_without_recursing(workspace):
    (workspace / "pkg").mkdir()
    (workspace / "pkg" / "a.py").write_text("a = 1\n", encoding="utf-8")
    (workspace / "pkg" / "b.py").write_text("b = 2\n", encoding="utf-8")
    (workspace / "pkg" / "deep").mkdir()
    (workspace / "pkg" / "deep" / "hidden.py").write_text("h = 1\n", encoding="utf-8")

    out = await ip.prepare_input_content("inspect @pkg")
    assert "a.py" in out
    assert "b.py" in out
    # Non-recursive: the nested file's contents must not be dumped.
    assert "h = 1" not in out
    assert "deep/" in out


async def test_directory_listing_is_capped(workspace):
    (workspace / "many").mkdir()
    for i in range(MAX_MENTION_DIR_ENTRIES + 20):
        (workspace / "many" / f"f{i:03}.txt").write_text("x", encoding="utf-8")
    out = await ip.prepare_input_content("list @many")
    assert "and 20 more" in out


async def test_total_budget_skips_the_overflow(workspace, monkeypatch):
    """Past the total budget, mentions are listed by path but not inlined."""
    monkeypatch.setattr(ip, "MAX_MENTION_TOTAL_CHARS", 200)
    for name in ("a.py", "b.py", "c.py"):
        (workspace / name).write_text("x = 1\n" * 40, encoding="utf-8")

    out = await ip.prepare_input_content("read @a.py @b.py @c.py")
    assert "context budget reached" in out
    assert "a.py" in out  # first one inlined


async def test_missing_file_is_silently_skipped(workspace):
    out = await ip.prepare_input_content("read @nope.py")
    assert "## Referenced Files" not in out


# ── @agent delegation wiring ─────────────────────────────────────────────────


def test_agent_mention_import_path_is_correct():
    """The TUI must import these from ``input_utils``.

    It previously imported ``novacode_cli.input`` (a module that no longer
    exists) inside a broad ``except Exception``, so ``@agent`` delegation was
    silently dead rather than failing loudly.
    """
    import novacode_cli.input_utils as iu

    assert callable(iu.parse_agent_mentions)
    assert callable(iu.parse_agent_mentions_multi)


def test_tui_does_not_import_the_removed_input_module():
    import inspect

    from novacode_cli.tui import app as tui_app

    src = inspect.getsource(tui_app)
    assert "from novacode_cli.input import" not in src
