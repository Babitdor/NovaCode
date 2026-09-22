"""Markdown -> Telegram HTML: what models write must reach the chat intact."""

from __future__ import annotations

from novacode_cli.remote.telegram_format import MAX_MESSAGE, render, to_plain


def test_common_model_markdown() -> None:
    out = render(
        "### Fixed `process.py`\n\n"
        "Uses **snake_case_names** and <tags> & ampersands. [docs](https://x.io)\n\n"
        "- one\n- two\n  1. nested\n"
    )[0]
    assert "<b>Fixed <code>process.py</code></b>" in out
    assert "<b>snake_case_names</b>" in out, "underscores in identifiers are not italics"
    assert "&lt;tags&gt; &amp; ampersands" in out
    assert '<a href="https://x.io">docs</a>' in out
    assert "• one" in out and "  1. nested" in out


def test_code_fences_tables_and_quotes() -> None:
    out = render(
        "```python\nif a < b:\n    pass\n```\n\n"
        "| a | b |\n|---|---|\n| 1 | 2 |\n\n"
        "> q1\n> q2\n> q3\n> q4\n"
    )[0]
    assert '<pre><code class="language-python">if a &lt; b:\n    pass</code></pre>' in out
    assert "<pre>a | b\n--+--\n1 | 2</pre>" in out
    assert "<blockquote expandable>q1" in out


def test_unsafe_links_and_raw_html_are_neutralised() -> None:
    out = render("[x](javascript:alert(1)) <script>alert(1)</script>")[0]
    assert "<a" not in out and "<script>" not in out


def test_long_output_splits_into_valid_messages() -> None:
    code = "```\n" + "\n".join(f"line {i} <x>" for i in range(2000)) + "\n```"
    pages = render("intro\n\n" + code + "\n\noutro")
    assert len(pages) > 1
    for page in pages:
        assert len(page) <= MAX_MESSAGE
        assert page.count("<pre>") == page.count("</pre>")
    assert "".join(to_plain(p) for p in pages).count("line 1999 <x>") == 1


def test_unclosed_markdown_mid_stream_is_still_valid() -> None:
    out = render("working on **bold and\n```py\nx = 1")[0]
    assert out.count("<pre>") == out.count("</pre>")


async def test_a_rejected_html_message_is_resent_plain_not_lost() -> None:
    from novacode_cli.remote.bridge import BridgeConfig, RemotePlatform
    from novacode_cli.remote.telegram_bridge import TelegramBridge
    import asyncio

    bridge = TelegramBridge(
        BridgeConfig(platform=RemotePlatform.TELEGRAM, token="t", chat_id=1), asyncio.Queue()
    )
    calls: list[dict] = []

    async def api(method, payload, **_):
        calls.append(payload)
        return None if payload.get("parse_mode") == "HTML" else {"ok": True, "result": {}}

    bridge._api_call = api
    await bridge._send_message(1, "**hi** <there>")
    assert calls[0]["parse_mode"] == "HTML" and calls[0]["text"] == "<b>hi</b> &lt;there&gt;"
    assert calls[1]["text"] == "hi <there>" and "parse_mode" not in calls[1]
