"""Executable acceptance cases for cross-project session communication."""

import random

import pytest

from novacode_cli.sessions.messaging import MessageError, SessionBroker, SessionInfo


def broker(**limits):
    bus = SessionBroker(**limits)
    bus.update_sessions(
        [
            SessionInfo("root", "main", "/frontend"),
            SessionInfo("b", "backend", "/backend"),
            SessionInfo("c", "worker", "/worker"),
        ]
    )
    return bus


def test_explicit_and_shorthand_addresses_preserve_other_mentions():
    bus = broker()
    assert bus.resolve_mention("@backend What fields?", ()) == ("b", "What fields?")
    assert bus.resolve_mention("@tab:b /skills", ()) == ("b", "/skills")
    assert bus.resolve_mention("@tab:BACKEND hello", {"backend"}) == ("b", "hello")
    assert bus.resolve_mention("@backend hello", {"backend"}) is None
    assert bus.resolve_mention("@src/main.py review", ()) is None
    assert bus.resolve_mention("hello @backend", ()) is None
    with pytest.raises(MessageError, match="Unknown"):
        bus.resolve_mention("@tab:missing hello", ())
    with pytest.raises(MessageError, match="message"):
        bus.resolve_mention("@tab:b", ())


def test_duplicate_titles_require_stable_id_and_do_not_silently_pick_one():
    bus = broker()
    bus.update_sessions([SessionInfo("b", "api", "/one"), SessionInfo("c", "API", "/two")])
    with pytest.raises(MessageError, match="Ambiguous"):
        bus.resolve_mention("@api hello", ())
    assert bus.resolve_mention("@tab:c hello", ()) == ("c", "hello")


def test_requests_and_replies_are_bound_to_session_and_request_id():
    bus = broker()
    first = bus.send("root", "backend", "Explain login")
    second = bus.send("c", "b", "Explain logout")
    assert bus.next_message("b").id == first.id
    bus.start(first.id)
    with pytest.raises(MessageError, match="recipient"):
        bus.reply("c", first.id, "forged")
    bus.reply("b", first.id, "{ token, user }")
    assert bus.read("root")[0]["response"] == "{ token, user }"
    assert bus.read("root") == []
    assert bus.next_message("b").id == second.id
    assert bus.read("c") == []
    with pytest.raises(MessageError, match="Unknown"):
        bus.read("outsider")


def test_limits_fail_without_losing_pending_work_and_evict_completed_only():
    bus = broker(max_pending=1, max_messages=2)
    first = bus.send("root", "b", "one")
    with pytest.raises(MessageError, match="queue"):
        bus.send("c", "b", "two")
    bus.start(first.id)
    bus.complete(first.id, "result")
    second = bus.send("root", "c", "two")
    third = bus.send("root", "b", "three")
    assert first.id not in bus.messages
    assert second.id in bus.messages and third.id in bus.messages
    with pytest.raises(MessageError, match="capacity"):
        bus.send("b", "root", "four")
    with pytest.raises(MessageError, match="large"):
        broker().send("root", "b", "x" * 20000)
    with pytest.raises(MessageError, match="memory"):
        broker(max_bytes=3).send("root", "b", "four")


def test_closed_recipient_fails_request_and_replies_do_not_create_turns():
    bus = broker()
    bus.send("root", "b", "one")
    bus.update_sessions([SessionInfo("root", "main", "/frontend")])
    assert bus.read("root")[0]["status"] == "failed"
    assert bus.next_message("root") is None
    with pytest.raises(MessageError, match="Unknown"):
        bus.send("root", "b", "two")
    with pytest.raises(MessageError, match="self"):
        bus.send("root", "root", "loop")
    bus.close()
    assert not bus.messages and bus.retained_bytes == 0


def test_message_text_roundtrips_without_executing_or_rewriting_it():
    rng = random.Random(42)
    alphabet = "a /@\\\n\té日😀\u0000"
    for _ in range(100):
        text = "Q" + "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 128)))
        bus = broker()
        sent = bus.send("root", "b", text)
        assert bus.next_message("b").text == text
        bus.start(sent.id)
        bus.complete(sent.id, text)
        assert bus.read("root")[0]["response"] == text


def test_validation_idempotence_memory_eviction_and_chain_bounds():
    bus = broker(max_bytes=12)
    first = bus.send("root", "b", "Q")
    with pytest.raises(MessageError, match="running"):
        bus.reply("b", first.id, "early")
    bus.start(first.id)
    with pytest.raises(MessageError, match="already"):
        bus.start(first.id)
    with pytest.raises(MessageError, match="status"):
        bus.complete(first.id, "x", "queued")
    bus.complete(first.id, "answer")
    assert bus.complete(first.id, "duplicate").response == "answer"
    bus.send("b", "root", "new-message")
    assert first.id not in bus.messages and bus.retained_bytes == 11
    for text in ("", "  ", None, "\ud800"):
        with pytest.raises(MessageError):
            broker().send("root", "b", text)
    with pytest.raises(MessageError, match="Unknown"):
        broker().reply("b", "missing", "x")
    with pytest.raises(MessageError, match="chain"):
        broker().send("root", "b", "x", depth=9)
