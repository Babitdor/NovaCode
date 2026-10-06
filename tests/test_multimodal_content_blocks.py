"""Multimodal content blocks survive the trip from ``read_file`` to the model.

Two defects, both of which made a file the model could have read arrive as a
notice saying it could not:

1. ``convert_file_content_block_to_text`` rewrote EVERY ``file`` block to
   "[Unsupported file type: …]", not just PDFs. deepagents emits a ``file``
   block for ``.ppt``/``.pptx`` too, and OpenAI/Google read those natively — so
   the block was destroyed for the very providers that support it.

2. deepagents gates each multimodal block on the bound model's ``ModelProfile``
   and replaces it with "[read_file: X was not attached …]" when the field is
   ``False``. Nova decided a model was multimodal but only ever wrote
   ``image_inputs``, so a profile that vetoed the block inside a ``ToolMessage``
   (``image_tool_message``) still won.

The contract under test: a block Nova can hand to the model is handed over
unchanged; a block it genuinely cannot use is converted to text.
"""

from __future__ import annotations

import base64

import pytest

# ── convert_file_content_block_to_text: only PDFs are ours ──────────────────


def _pdf_block() -> dict:
    """A minimal, real PDF so PyMuPDF has something to open."""
    import fitz

    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Quarterly revenue rose in Q3.")
    data = doc.tobytes()
    doc.close()
    return {
        "type": "file",
        "base64": base64.b64encode(data).decode(),
        "mime_type": "application/pdf",
    }


def test_pdf_block_is_converted_to_text():
    """The behaviour that must not regress: a PDF becomes readable text."""
    from novacode_cli.utils.pdf_extraction import convert_file_content_block_to_text

    out = convert_file_content_block_to_text([_pdf_block()])
    assert out is not None
    assert len(out) == 1
    assert out[0]["type"] == "text"
    assert "Quarterly revenue rose in Q3." in out[0]["text"]


def test_pptx_file_block_is_left_intact():
    """A .pptx is a block OpenAI/Google read natively — do not destroy it."""
    from novacode_cli.utils.pdf_extraction import convert_file_content_block_to_text

    block = {
        "type": "file",
        "base64": base64.b64encode(b"PK\x03\x04 fake pptx").decode(),
        "mime_type": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    }
    content = [block]
    out = convert_file_content_block_to_text(content)
    assert out is content, "a non-PDF file block must be returned by identity"
    assert out[0] is block


def test_provider_managed_file_reference_is_left_intact():
    """A file block with no base64 is a provider-managed reference, not ours."""
    from novacode_cli.utils.pdf_extraction import convert_file_content_block_to_text

    block = {"type": "file", "file_id": "file-abc123"}
    content = [block]
    assert convert_file_content_block_to_text(content) is content


@pytest.mark.parametrize("block_type", ["image", "audio", "video"])
def test_non_file_blocks_are_untouched(block_type):
    """image/audio/video blocks are not this function's business."""
    from novacode_cli.utils.pdf_extraction import convert_file_content_block_to_text

    block = {"type": block_type, "base64": "AAAA", "mime_type": f"{block_type}/x"}
    content = [block]
    assert convert_file_content_block_to_text(content) is content


def test_mixed_content_converts_only_the_pdf():
    """A PDF beside a pptx: one is converted, the other survives."""
    from novacode_cli.utils.pdf_extraction import convert_file_content_block_to_text

    pptx = {"type": "file", "base64": "UEsDBA==", "mime_type": "application/vnd.ms-powerpoint"}
    out = convert_file_content_block_to_text([_pdf_block(), pptx])
    assert out is not None
    assert len(out) == 2
    assert out[0]["type"] == "text"
    assert out[1] is pptx, "the pptx must survive a sibling PDF conversion"


def test_sanitize_leaves_a_pptx_only_message_alone():
    """The history safety net must not rewrite a message it has no business in."""
    from langchain_core.messages import ToolMessage

    from novacode_cli.utils.pdf_extraction import sanitize_messages_file_blocks

    msg = ToolMessage(
        content=[
            {
                "type": "file",
                "base64": "UEsDBA==",
                "mime_type": "application/vnd.ms-powerpoint",
            }
        ],
        tool_call_id="call_1",
        name="read_file",
    )
    messages = [msg]
    out = sanitize_messages_file_blocks(messages)
    assert out[0] is msg, "a pptx-only message must be returned unchanged"


def test_sanitize_converts_a_pdf_message():
    """The history safety net still converts a restored PDF read."""
    from langchain_core.messages import ToolMessage

    from novacode_cli.utils.pdf_extraction import sanitize_messages_file_blocks

    msg = ToolMessage(content=[_pdf_block()], tool_call_id="call_1", name="read_file")
    out = sanitize_messages_file_blocks([msg])
    assert out[0] is not msg
    assert isinstance(out[0].content, list)
    assert out[0].content[0]["type"] == "text"


# ── declare_multimodal_profile: Nova's answer reaches deepagents ────────────


def _chat_model(profile: dict | None = None):
    """A real ``BaseChatModel`` with a controlled ``profile``.

    A real instance rather than a duck-typed stub: ``declare_multimodal_profile``
    guards on the type, so a bare object would let the test pass while the
    production path silently no-ops. ``FakeListChatModel`` is langchain's own
    minimal concrete chat model — no network, no API key.
    """
    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    model = FakeListChatModel(responses=["ok"])
    if profile is not None:
        model.profile = profile
    return model


def _declare(model, *, images=True):
    from novacode_cli.config.model_capabilities import declare_multimodal_profile

    declare_multimodal_profile(model, images=images)


def test_declares_image_support_on_an_empty_profile():
    model = _chat_model({})
    _declare(model)
    assert model.profile["image_inputs"] is True


def test_clears_a_tool_scoped_veto():
    """The reported shape: the plain field says yes, the tool field says no.

    ``read_file`` results are always ToolMessages, so deepagents consults
    ``image_tool_message`` as well — a ``False`` there stripped the image even
    though ``image_inputs`` was already ``True``.
    """
    model = _chat_model({"image_inputs": True, "image_tool_message": False})
    _declare(model)
    assert model.profile["image_tool_message"] is True


def test_clears_a_pdf_tool_scoped_veto():
    model = _chat_model({"pdf_tool_message": False})
    _declare(model)
    assert model.profile["pdf_tool_message"] is True


def test_preserves_unrelated_profile_keys():
    """Seeding must not clobber the window or the provider's own answers."""
    model = _chat_model({"max_input_tokens": 128_000, "audio_inputs": False})
    _declare(model)
    assert model.profile["max_input_tokens"] == 128_000
    assert model.profile["audio_inputs"] is False, "Nova has no opinion on audio"


def test_does_not_claim_image_support_when_nova_says_no():
    """A text-only model must keep its profile — the caption path depends on it."""
    model = _chat_model({"image_inputs": False})
    _declare(model, images=False)
    assert model.profile["image_inputs"] is False


def test_never_raises_on_a_non_model():
    """A profile hint must not be able to block an agent build."""
    _declare(object())
    _declare(None)


def test_is_idempotent():
    model = _chat_model({})
    _declare(model)
    first = dict(model.profile)
    _declare(model)
    assert model.profile == first


# ── the two halves agree: a declared model keeps its image block ────────────


def test_declared_profile_survives_deepagents_scrub():
    """End-to-end: after declaring, deepagents no longer replaces the block.

    This is the assertion that would have caught the original bug — the profile
    Nova writes is the one deepagents actually reads.
    """
    from deepagents.middleware.filesystem import _scrub_unsupported_multimodal_content
    from langchain_core.messages import ToolMessage

    model = _chat_model({"image_inputs": False, "image_tool_message": False})
    msg = ToolMessage(
        content_blocks=[{"type": "image", "base64": "AAAA", "mime_type": "image/png"}],
        tool_call_id="call_1",
        name="read_file",
    )

    before = _scrub_unsupported_multimodal_content([msg], model)
    assert before[0].content_blocks[0]["type"] == "text", (
        "precondition: an undeclared profile must strip the image"
    )

    _declare(model)
    after = _scrub_unsupported_multimodal_content([msg], model)
    assert after[0].content_blocks[0]["type"] == "image", (
        "after declaring image support the block must reach the model"
    )
