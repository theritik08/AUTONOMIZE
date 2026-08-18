"""Near-real-time activity delivery.

WHY SERVER-SENT EVENTS RATHER THAN WEBSOCKETS
----------------------------------------------
The traffic here is strictly one-directional: the server tells a dashboard
that new activity landed. Nothing the dashboard says needs to travel back
over the same channel — it already has a REST API for that, with the
authentication, rate limiting and CSRF story already built.

SSE is plain HTTP. It needs no protocol upgrade, survives proxies that
mangle WebSocket handshakes, and `EventSource` reconnects on its own with
`Last-Event-ID` replay semantics defined by the spec. A WebSocket would
buy bidirectionality this feature does not use, and cost a second
authentication path and a hand-written reconnect loop.

WHAT TRAVELS OVER IT
--------------------
Only what the privacy model already permits to leave the device: aggregate
counters, a domain, a category, a timestamp. No typed text, no clipboard
contents, no ordered keystrokes — the same contract the upload path obeys,
enforced here again by `_public_event` allow-listing fields rather than
forwarding whatever the caller passed.

DURABILITY IS NOT THIS FILE'S JOB
----------------------------------
The database write is the durable record and happens first; publishing is
best-effort notification on top of it. A dropped event costs a few seconds
of freshness, never data — the dashboard reconciles by fetching from the
API when an event arrives, so the event is a *hint that something changed*,
not the thing that changed. That separation is deliberate: it means a slow
or disconnected listener can never apply backpressure to a write.

SCOPE LIMIT — READ THIS BEFORE DEPLOYING BEHIND MULTIPLE WORKERS
-----------------------------------------------------------------
The bus is in-process. With one uvicorn worker (the default, and what
`docker-compose.yml` runs) every publisher and every subscriber share it
and this is correct.

Run two or more workers and a dashboard connected to worker A will not see
events published on worker B. It does not break — the dashboard still
reconciles on its polling fallback — but it stops being real-time for some
users, silently. Fixing that properly means an out-of-process broker
(Redis pub/sub, Postgres LISTEN/NOTIFY); `publish()` is deliberately the
single seam where that would be swapped in. `describe()` reports the
limitation so a deployment cannot discover it by accident.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
from typing import Any, Dict, Optional, Set

logger = logging.getLogger("autonomize.events")

# How long a stream ticket is valid. Short, because it appears in a URL.
TICKET_TTL_SECONDS = 60

# Heartbeat cadence. Two jobs: it keeps intermediaries from reaping an idle
# connection, and it is how the client distinguishes "connected and quiet"
# from "connection silently died" — without it a dead socket looks exactly
# like a user who has not typed for a while.
HEARTBEAT_SECONDS = 20

# Per-subscriber backlog. A dashboard that stops reading (backgrounded tab,
# frozen laptop) must not grow an unbounded queue in the server. Overflow
# drops the OLDEST event and sets a flag telling the client to do a full
# reconcile, which is always correct because events are hints rather than
# state.
MAX_QUEUE = 64

# Bounds total memory if something goes wrong with connection cleanup.
MAX_SUBSCRIBERS_PER_USER = 8


class _Subscriber:
    __slots__ = ("queue", "dropped")

    def __init__(self) -> None:
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=MAX_QUEUE)
        self.dropped = False


# user_id -> set of subscribers. Keyed by user and never by anything the
# client supplies, which is what makes cross-account leakage structurally
# impossible rather than a check someone has to remember to write.
_subscribers: Dict[str, Set[_Subscriber]] = {}

# Monotonic per-user event id, so a reconnecting client can say what it
# last saw and the server can tell whether it missed anything.
_next_id: Dict[str, int] = {}


def describe() -> str:
    workers = os.environ.get("WEB_CONCURRENCY") or os.environ.get("UVICORN_WORKERS")
    note = "in-process bus (single worker)"
    if workers and workers.strip() not in ("", "1"):
        note = (f"in-process bus with {workers} workers — events published on one "
                "worker will NOT reach dashboards connected to another. Real-time "
                "delivery is degraded to the client's polling fallback for those "
                "users. Use one worker, or add a shared broker (see events.py).")
    return note


def _secret() -> bytes:
    """Reuses the application's auth secret.

    A separate key would be a second thing to rotate and a second thing to
    forget, for no gain: a stream ticket is strictly weaker than a session
    token — shorter-lived, read-only, and useless for anything but opening
    a stream for the user it names.
    """
    # Imported lazily rather than at module scope: accounts imports a good
    # deal of the app, and events is imported early by main.
    import accounts
    secret = accounts.SECRET
    return secret if isinstance(secret, bytes) else str(secret).encode()


def issue_ticket(user_id: str) -> str:
    """A short-lived credential for opening a stream.

    `EventSource` cannot send an Authorization header — the API simply has
    no place to put one. The alternatives are a cookie (which drags in CORS
    credentials and a CSRF story for a read-only stream) or a token in the
    query string.

    A query string is the pragmatic choice, but the SESSION token must
    never go there: URLs end up in access logs, proxy logs, and Referer
    headers, and a leaked session token is a full account compromise. So
    this is a distinct, single-purpose credential — it expires in a minute,
    grants nothing but "subscribe to this user's activity events", and
    cannot be exchanged for anything else.
    """
    expires = int(time.time()) + TICKET_TTL_SECONDS
    payload = f"{user_id}:{expires}"
    signature = hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{payload}:{signature}"


def verify_ticket(ticket: str) -> Optional[str]:
    """Returns the user_id a ticket names, or None. Never raises."""
    if not ticket or ticket.count(":") != 2:
        return None
    user_id, expires_raw, signature = ticket.split(":")
    try:
        expires = int(expires_raw)
    except ValueError:
        return None
    if expires < time.time():
        return None
    expected = hmac.new(
        _secret(), f"{user_id}:{expires}".encode(), hashlib.sha256
    ).hexdigest()[:32]
    # compare_digest, not ==: a plain comparison leaks the position of the
    # first differing byte through timing, which is enough to forge a
    # signature one byte at a time.
    if not hmac.compare_digest(signature, expected):
        return None
    return user_id


def _public_event(kind: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Allow-lists what may travel over the wire.

    Deliberately an allow-list rather than a redaction pass: a field added
    to the upload payload later is then invisible here by default, instead
    of leaking until someone remembers to exclude it. This is the same
    reasoning as accounts.public_user.
    """
    allowed = {
        "session_id", "category", "domain", "active_ms", "typed_chars",
        "pasted_chars", "backspace_count", "revision_count", "prompt_count",
        "likely_ai_pastes", "tab_switch_count", "capability", "detector",
        "is_final", "score",
    }
    return {
        "kind": kind,
        "at": int(time.time() * 1000),
        "data": {k: v for k, v in payload.items() if k in allowed},
    }


def publish(user_id: str, kind: str, payload: Dict[str, Any]) -> None:
    """Notifies this user's connected dashboards. Never raises, never blocks.

    Called from a synchronous request handler after the database write has
    committed. Delivery failures are logged and dropped: the durable record
    already exists, and the client reconciles against the API when it sees
    an event, so the worst case of a lost notification is a few seconds of
    staleness rather than missing data.
    """
    listeners = _subscribers.get(user_id)
    if not listeners:
        return

    _next_id[user_id] = _next_id.get(user_id, 0) + 1
    event = _public_event(kind, payload)
    event["id"] = _next_id[user_id]

    for subscriber in list(listeners):
        try:
            subscriber.queue.put_nowait(event)
        except asyncio.QueueFull:
            # A listener that stopped reading. Drop the oldest rather than
            # this one — the newest event is the one worth having — and
            # flag it so the stream tells the client to do a full reload.
            try:
                subscriber.queue.get_nowait()
                subscriber.queue.put_nowait(event)
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                pass
            subscriber.dropped = True
        except Exception as error:            # pragma: no cover - defensive
            logger.warning("event publish failed: %s", error)


def subscribe(user_id: str) -> Optional[_Subscriber]:
    listeners = _subscribers.setdefault(user_id, set())
    if len(listeners) >= MAX_SUBSCRIBERS_PER_USER:
        return None
    subscriber = _Subscriber()
    listeners.add(subscriber)
    return subscriber


def unsubscribe(user_id: str, subscriber: _Subscriber) -> None:
    listeners = _subscribers.get(user_id)
    if not listeners:
        return
    listeners.discard(subscriber)
    if not listeners:
        _subscribers.pop(user_id, None)


def connection_count(user_id: Optional[str] = None) -> int:
    if user_id is not None:
        return len(_subscribers.get(user_id, ()))
    return sum(len(v) for v in _subscribers.values())


def reset() -> None:
    """Test hook — drops every subscriber and id counter."""
    _subscribers.clear()
    _next_id.clear()


def format_sse(event: Dict[str, Any]) -> str:
    """One SSE frame.

    The `id:` line is what makes reconnection safe: the browser echoes the
    last id it saw in `Last-Event-ID`, so the server can tell the client
    whether it missed anything rather than the client guessing.
    """
    return (f"id: {event.get('id', 0)}\n"
            f"event: {event.get('kind', 'message')}\n"
            f"data: {json.dumps(event, separators=(',', ':'))}\n\n")


async def stream(user_id: str, last_event_id: Optional[int] = None):
    """Yields SSE frames for one connection until the client goes away."""
    subscriber = subscribe(user_id)
    if subscriber is None:
        yield ("event: error\n"
               "data: {\"detail\":\"Too many open streams for this account.\"}\n\n")
        return

    try:
        # Tell the client where the server is, so it can decide whether it
        # missed events while disconnected. It reconciles by fetching from
        # the REST API rather than by replaying — there is no event log to
        # replay from, and inventing one would be a durability claim this
        # bus deliberately does not make.
        current = _next_id.get(user_id, 0)
        missed = last_event_id is not None and current > last_event_id
        yield format_sse({
            "id": current,
            "kind": "ready",
            "at": int(time.time() * 1000),
            "data": {"missed_events": bool(missed), "resumed": last_event_id is not None},
        })

        while True:
            try:
                event = await asyncio.wait_for(
                    subscriber.queue.get(), timeout=HEARTBEAT_SECONDS)
            except asyncio.TimeoutError:
                # A comment frame. Invisible to `onmessage`, but it keeps
                # the socket warm and lets the client's watchdog tell a
                # live-but-quiet connection from a dead one.
                yield ": keep-alive\n\n"
                continue

            if subscriber.dropped:
                subscriber.dropped = False
                yield format_sse({
                    "id": event.get("id", 0),
                    "kind": "desync",
                    "at": int(time.time() * 1000),
                    "data": {"reason": "backlog overflow"},
                })
            yield format_sse(event)
    except asyncio.CancelledError:
        # Normal disconnect: the client navigated away or closed the tab.
        raise
    finally:
        unsubscribe(user_id, subscriber)
