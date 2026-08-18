"""A small in-process rate limiter for the write endpoints.

Why this exists: `POST /api/session/upsert` accepts an unauthenticated
`user_id` by default and writes a row for it. On localhost that's fine —
the only client is the user's own browser. The moment this service is
deployed to a public URL, it's an open, unauthenticated write endpoint,
and "someone can fill my database" is the first thing a reviewer asks
about it.

WHAT THIS IS NOT
----------------
This is a token bucket in the memory of one process. That means:

  - it does not coordinate across replicas — two instances behind a load
    balancer each enforce the limit separately, so the effective limit is
    N times the configured one;
  - it resets when the process restarts;
  - it is not a defence against a distributed flood. That belongs at the
    edge (Cloudflare, an ingress controller, your platform's own limiter),
    not in application code.

It is deliberately still worth having: it stops one buggy client or one
casual curl loop from filling a free-tier Postgres, it costs nothing, and
it needs no Redis. Anything stronger than that is a lie about what a
dict in one process can do, so the README says the same thing.

Off unless `AUTONOMIZE_RATE_LIMIT` is set, so the default local-dev
experience is unchanged.
"""
import os
import threading
import time
from collections import deque

# e.g. AUTONOMIZE_RATE_LIMIT="120/60"  -> 120 requests per 60 seconds
_RAW = os.environ.get("AUTONOMIZE_RATE_LIMIT", "").strip()

ENABLED = False
MAX_REQUESTS = 0
WINDOW_SECONDS = 60.0

if _RAW:
    try:
        count, _, window = _RAW.partition("/")
        MAX_REQUESTS = int(count)
        WINDOW_SECONDS = float(window or 60)
        ENABLED = MAX_REQUESTS > 0 and WINDOW_SECONDS > 0
    except ValueError:
        # A malformed value must not silently mean "no limit" — that's the
        # failure mode where someone thinks they're protected and isn't.
        raise ValueError(
            f'AUTONOMIZE_RATE_LIMIT="{_RAW}" is not valid. Use "<requests>/<seconds>", e.g. "120/60".'
        )

# Bounds the number of distinct keys held, so an attacker rotating user_ids
# can't turn the limiter itself into the memory leak.
MAX_TRACKED_KEYS = 10_000

_lock = threading.Lock()
_hits: dict = {}


def describe() -> str:
    if not ENABLED:
        return "off"
    return f"{MAX_REQUESTS} requests / {WINDOW_SECONDS:.0f}s per client (single-process)"


def reset() -> None:
    """Test hook — clears all recorded state."""
    with _lock:
        _hits.clear()


def check(key: str, now: float = None) -> bool:
    """True if this request is allowed. Records it as a hit when so."""
    if not ENABLED:
        return True

    now = now if now is not None else time.monotonic()
    cutoff = now - WINDOW_SECONDS

    with _lock:
        bucket = _hits.get(key)
        if bucket is None:
            if len(_hits) >= MAX_TRACKED_KEYS:
                _evict_stale(cutoff)
            bucket = _hits.setdefault(key, deque())

        while bucket and bucket[0] < cutoff:
            bucket.popleft()

        if len(bucket) >= MAX_REQUESTS:
            return False

        bucket.append(now)
        return True


def _evict_stale(cutoff: float) -> None:
    """Drops keys with no activity inside the window. Caller holds the lock."""
    for key in [k for k, v in _hits.items() if not v or v[-1] < cutoff]:
        del _hits[key]
    if len(_hits) >= MAX_TRACKED_KEYS:
        # Everything is live and we're still at the cap: drop the oldest
        # half rather than growing without bound. Under this much pressure
        # a shared limiter is the right answer anyway.
        for key in sorted(_hits, key=lambda k: _hits[k][-1])[: MAX_TRACKED_KEYS // 2]:
            del _hits[key]
