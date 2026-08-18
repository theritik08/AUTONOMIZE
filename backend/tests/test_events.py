"""Tests for the real-time activity bus (events.py).

The security properties here are the ones worth pinning: a ticket that can
be forged is a way to read another student's activity, and a publish path
that fans out by anything other than the authenticated user id is a leak
that no amount of frontend care can fix.
"""
import asyncio
import json
import time

import pytest

import events


@pytest.fixture(autouse=True)
def clean_bus():
    events.reset()
    yield
    events.reset()


# ---------------------------------------------------------------------------
# Tickets
# ---------------------------------------------------------------------------

def test_a_ticket_round_trips_to_the_user_it_names():
    ticket = events.issue_ticket("user-abc")
    assert events.verify_ticket(ticket) == "user-abc"


def test_a_ticket_for_one_user_cannot_be_rewritten_into_another():
    # The whole point of signing it. Swapping the user id invalidates the
    # signature, so a ticket is not a bearer pass to arbitrary streams.
    ticket = events.issue_ticket("victim")
    forged = ticket.replace("victim", "attacker", 1)
    assert events.verify_ticket(forged) is None


def test_an_expired_ticket_is_refused(monkeypatch):
    ticket = events.issue_ticket("user-abc")
    # Travel past the TTL rather than sleeping through it. The real clock is
    # captured first — reading time.time() inside the replacement would call
    # the replacement.
    future = time.time() + events.TICKET_TTL_SECONDS + 5
    monkeypatch.setattr(events.time, "time", lambda: future)
    assert events.verify_ticket(ticket) is None


def test_a_ticket_with_a_tampered_expiry_is_refused():
    ticket = events.issue_ticket("user-abc")
    user, expires, signature = ticket.split(":")
    extended = f"{user}:{int(expires) + 86400}:{signature}"
    assert events.verify_ticket(extended) is None


@pytest.mark.parametrize("bad", ["", "garbage", "a:b", "a:b:c:d", "user:notanumber:sig"])
def test_malformed_tickets_are_refused_without_raising(bad):
    # This runs on an unauthenticated endpoint, so a crash here is a DoS.
    assert events.verify_ticket(bad) is None


# ---------------------------------------------------------------------------
# Isolation — the property that matters most
# ---------------------------------------------------------------------------

def test_an_event_reaches_only_the_user_it_belongs_to():
    alice = events.subscribe("alice")
    bob = events.subscribe("bob")

    events.publish("alice", "activity", {"domain": "docs.google.com", "typed_chars": 10})

    assert alice.queue.qsize() == 1
    assert bob.queue.qsize() == 0, "an event leaked into another account's stream"


def test_publishing_to_a_user_with_no_listeners_is_a_no_op():
    events.publish("nobody", "activity", {"typed_chars": 1})
    assert events.connection_count() == 0


def test_every_connection_for_one_user_receives_the_event():
    # Two browsers, one account.
    first = events.subscribe("alice")
    second = events.subscribe("alice")
    events.publish("alice", "activity", {"typed_chars": 5})
    assert first.queue.qsize() == 1
    assert second.queue.qsize() == 1


def test_subscribers_are_capped_per_user():
    for _ in range(events.MAX_SUBSCRIBERS_PER_USER):
        assert events.subscribe("alice") is not None
    assert events.subscribe("alice") is None, "an account could open unbounded streams"


def test_unsubscribe_releases_the_user_entry():
    subscriber = events.subscribe("alice")
    events.unsubscribe("alice", subscriber)
    assert events.connection_count("alice") == 0


# ---------------------------------------------------------------------------
# Privacy — the same contract the upload path obeys
# ---------------------------------------------------------------------------

def test_only_allow_listed_fields_travel():
    subscriber = events.subscribe("alice")
    events.publish("alice", "activity", {
        "domain": "docs.google.com",
        "typed_chars": 12,
        # None of these may ever cross the wire.
        "text": "the actual sentence they typed",
        "clipboard": "pasted content",
        "keystrokes": ["h", "e", "l", "l", "o"],
        "password": "hunter2",
        "email": "student@example.edu",
    })
    event = subscriber.queue.get_nowait()
    data = event["data"]
    assert data == {"domain": "docs.google.com", "typed_chars": 12}
    for forbidden in ("text", "clipboard", "keystrokes", "password", "email"):
        assert forbidden not in data


def test_the_serialised_frame_contains_no_free_text():
    subscriber = events.subscribe("alice")
    events.publish("alice", "activity", {
        "domain": "docs.google.com", "typed_chars": 3,
        "text": "SECRET-SENTENCE", "clipboard": "SECRET-PASTE",
    })
    frame = events.format_sse(subscriber.queue.get_nowait())
    assert "SECRET-SENTENCE" not in frame
    assert "SECRET-PASTE" not in frame


# ---------------------------------------------------------------------------
# Ordering, ids, and backlog behaviour
# ---------------------------------------------------------------------------

def test_event_ids_increase_per_user():
    subscriber = events.subscribe("alice")
    for _ in range(3):
        events.publish("alice", "activity", {"typed_chars": 1})
    ids = [subscriber.queue.get_nowait()["id"] for _ in range(3)]
    assert ids == sorted(ids) and len(set(ids)) == 3


def test_a_stalled_listener_drops_the_oldest_and_is_flagged_desynced():
    subscriber = events.subscribe("alice")
    # Never read; overflow the backlog.
    for _ in range(events.MAX_QUEUE + 10):
        events.publish("alice", "activity", {"typed_chars": 1})

    assert subscriber.queue.qsize() == events.MAX_QUEUE, "the backlog must stay bounded"
    assert subscriber.dropped is True, (
        "a listener that lost events must be told, so it can reconcile "
        "rather than silently showing stale data"
    )
    # The NEWEST event is the one worth keeping.
    newest = None
    while not subscriber.queue.empty():
        newest = subscriber.queue.get_nowait()
    assert newest["id"] == events.MAX_QUEUE + 10


def test_publish_never_raises_on_a_broken_subscriber():
    # A publish happens after a database commit, in a request handler. It
    # must not be able to turn a successful write into a 500.
    subscriber = events.subscribe("alice")
    subscriber.queue = None  # simulate something badly wrong
    events.publish("alice", "activity", {"typed_chars": 1})


# ---------------------------------------------------------------------------
# Frame format
# ---------------------------------------------------------------------------

def test_frames_are_valid_sse_with_an_id_line():
    frame = events.format_sse({"id": 7, "kind": "activity", "at": 1, "data": {}})
    assert frame.startswith("id: 7\n")
    assert "event: activity\n" in frame
    assert frame.endswith("\n\n"), "an SSE frame must end with a blank line"
    payload = [l for l in frame.split("\n") if l.startswith("data: ")][0][6:]
    assert json.loads(payload)["id"] == 7


def test_stream_opens_with_a_ready_frame_and_reports_missed_events():
    async def run():
        events.publish("alice", "activity", {"typed_chars": 1})  # no listeners; bumps id
        agen = events.stream("alice", last_event_id=0)
        first = await agen.__anext__()
        await agen.aclose()
        return first

    first = asyncio.run(run())
    assert "event: ready" in first
    payload = json.loads([l for l in first.split("\n") if l.startswith("data: ")][0][6:])
    # A reconnecting client that is behind must be told, so it reconciles
    # through the API rather than assuming it is up to date.
    assert payload["data"]["resumed"] is True


def test_stream_cleans_up_its_subscriber_on_close():
    async def run():
        agen = events.stream("alice", None)
        await agen.__anext__()
        assert events.connection_count("alice") == 1
        await agen.aclose()

    asyncio.run(run())
    assert events.connection_count("alice") == 0, "a closed stream must release its slot"
