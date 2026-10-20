"""Generated inbox histories preserve exactly the unread, identity-bound messages."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

from hypothesis import given, settings
from hypothesis import strategies as st

from novacode_cli.teams.models import Identity
from novacode_cli.teams.store import TeamStore


@settings(max_examples=30, deadline=None)
@given(
    st.lists(
        st.tuples(
            st.text(alphabet="abc XYZ", min_size=1, max_size=20).filter(str.strip), st.booleans()
        ),
        min_size=1,
        max_size=30,
    )
)
def test_message_sequences_preserve_exact_pending_order(history: list[tuple[str, bool]]):
    with TemporaryDirectory(prefix="nova-team-property-") as directory:
        store = TeamStore(Path(directory) / "teams.sqlite3")
        lead = Identity("project", "root")
        team = store.create_team(lead, "generated")
        record = store.add_agent(lead, team["id"], "peer", "default", directory)
        peer = Identity(lead.project, lead.root, record["id"], team["id"])
        expected = []
        for index, (text, acknowledge) in enumerate(history):
            message = store.send(lead, team["id"], peer.agent, text, str(index))
            # Retrying acceptance does not duplicate delivery.
            assert store.send(lead, team["id"], peer.agent, text, str(index))["id"] == message["id"]
            if acknowledge:
                store.ack(peer, message["id"])
            else:
                expected.append(text)
        assert [row["text"] for row in store.inbox(peer, team["id"])] == expected
        assert store.inbox(lead, team["id"]) == []
        store.close()
