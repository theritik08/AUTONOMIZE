r"""Settings that outlive one browser profile.

WHY THIS EXISTS
---------------

Settings used to live in `chrome.storage.local` and nowhere else, and for a
while that was the right call: the only two readers were the background
worker and a dashboard rendered *inside* the extension, both of which have
`chrome.storage` available, and keeping one copy meant nothing could drift.

That arrangement breaks the moment a dashboard is served as an ordinary web
page. A page on `localhost:5173` or a deployed URL cannot read
`chrome.storage` at all — the API simply is not there. Any settings screen
it draws is therefore either inert, or a *second* source of truth that
disagrees with the extension from the first click. Both outcomes are worse
than not offering the screen.

So settings move to the server, and `chrome.storage` becomes a cache rather
than the record. That also buys two things the old scheme could not offer
at any price: settings survive reinstalling the extension, and they follow
the user to a second machine.

THE SHAPE IS OWNED BY THE EXTENSION
-----------------------------------

`DEFAULTS` below mirrors `extension/background.js`'s `DEFAULT_SETTINGS`
exactly, and `tests/test_settings_store.py` asserts the two agree by
parsing the JavaScript — a comment asking future editors to keep two files
in sync is a comment that will be wrong within a month.

The extension owns the shape because the extension is what *acts* on it.
The backend validates and stores; it does not get an opinion about what a
tracking category means.

WHY LAST-WRITE-WINS, AND WHY THAT IS ENOUGH
-------------------------------------------

Two clients can write: the dashboard and the extension. Resolving that
properly would need vector clocks or per-field merge, and neither is
justified by the actual conflict rate — this is one user toggling their own
checkboxes on devices they are looking at.

What matters is that the clock is the SERVER's, not the client's. A device
with a wrong system time would otherwise be able to pin its stale settings
permanently by claiming a timestamp in 2039. `updated_at` is stamped here,
and the sync protocol in `background.js` compares against it rather than
against anything it computed locally.
"""
import json
import time

import db

# Mirrors extension/background.js DEFAULT_SETTINGS. Asserted equal by
# tests/test_settings_store.py rather than maintained by good intentions.
DEFAULTS = {
    "backendUrl": "http://localhost:8787",
    # Where the extension's "Open full dashboard" goes. Server-side so a
    # deployment configures it once and every browser the student signs in
    # on picks it up, rather than each install needing to be told.
    "dashboardUrl": "http://localhost:5599/index.html",
    "tracking": {"ai_assistant": True, "writing": True, "assessment": True},
    "excludedDomains": [],
}

# The only categories that exist. An unknown key in `tracking` is dropped
# rather than stored: the extension would ignore it anyway, and persisting
# it would let a typo look like a working feature for as long as nobody
# checked.
TRACKING_KEYS = ("ai_assistant", "writing", "assessment")

# A domain list is user-supplied text that the content script matches
# against every page load. Bounded so one paste of a blocklist cannot make
# every navigation slow, and so the row stays small.
MAX_EXCLUDED_DOMAINS = 200
MAX_DOMAIN_LENGTH = 253  # the DNS limit; anything longer is not a hostname


class SettingsError(ValueError):
    """Rejected input, with a message safe to show a user."""


def _clean_domain(raw):
    """Normalises one excluded-domain entry, or returns None to drop it.

    Deliberately forgiving about what a user types — `https://Example.com/`
    and `example.com` are the same intent — and deliberately strict about
    what is stored, because the stored form is what the content script
    compares against.
    """
    if not isinstance(raw, str):
        return None
    value = raw.strip().lower()
    if not value:
        return None
    for prefix in ("https://", "http://", "//"):
        if value.startswith(prefix):
            value = value[len(prefix):]
    value = value.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    # Strip a port: the extension matches on hostname.
    if ":" in value:
        value = value.split(":", 1)[0]
    if value.startswith("www."):
        value = value[4:]
    if not value or len(value) > MAX_DOMAIN_LENGTH:
        return None
    # A hostname, not a sentence. Rejecting here keeps a stray paste out of
    # the match loop entirely.
    if any(c.isspace() for c in value) or "." not in value:
        return None
    return value


def normalise(incoming, existing=None):
    """Validates a partial settings update and merges it over what exists.

    Partial by design: a dashboard that only renders tracking toggles must
    be able to save them without having to round-trip `backendUrl` it never
    displayed. Anything absent keeps its current value, and anything
    unrecognised is dropped rather than stored.
    """
    if not isinstance(incoming, dict):
        raise SettingsError("Settings must be an object.")

    base = dict(DEFAULTS)
    base["tracking"] = dict(DEFAULTS["tracking"])
    if existing:
        base.update({k: v for k, v in existing.items() if k in DEFAULTS})
        base["tracking"] = {**DEFAULTS["tracking"],
                            **(existing.get("tracking") or {})}

    if "backendUrl" in incoming:
        url = incoming["backendUrl"]
        if not isinstance(url, str) or not url.strip():
            raise SettingsError("Backend URL must be a non-empty string.")
        url = url.strip().rstrip("/")
        if not url.startswith(("http://", "https://")):
            raise SettingsError("Backend URL must start with http:// or https://.")
        base["backendUrl"] = url

    if "dashboardUrl" in incoming:
        url = incoming["dashboardUrl"]
        if not isinstance(url, str) or not url.strip():
            raise SettingsError("Dashboard URL must be a non-empty string.")
        url = url.strip()
        if not url.startswith(("http://", "https://")):
            raise SettingsError("Dashboard URL must start with http:// or https://.")
        # NOT rstrip("/")-ed like backendUrl: this is a page address the
        # extension opens directly, and it may legitimately end in a path
        # or carry a trailing slash that matters to the static host.
        base["dashboardUrl"] = url

    if "tracking" in incoming:
        tracking = incoming["tracking"]
        if not isinstance(tracking, dict):
            raise SettingsError("Tracking must be an object of category flags.")
        for key in TRACKING_KEYS:
            if key in tracking:
                base["tracking"][key] = bool(tracking[key])

    if "excludedDomains" in incoming:
        domains = incoming["excludedDomains"]
        if not isinstance(domains, list):
            raise SettingsError("Excluded domains must be a list.")
        cleaned = []
        for entry in domains:
            value = _clean_domain(entry)
            # Deduplicated while preserving order, so the list a user sees
            # back is recognisably the list they typed.
            if value and value not in cleaned:
                cleaned.append(value)
        if len(cleaned) > MAX_EXCLUDED_DOMAINS:
            raise SettingsError(
                f"At most {MAX_EXCLUDED_DOMAINS} excluded domains.")
        base["excludedDomains"] = cleaned

    return base


def load(conn, user_id):
    """This user's settings, falling back to the defaults.

    Never returns None. A user who has never opened the settings screen has
    the same settings as one who opened it and changed nothing, and making
    the caller handle a null here would put that distinction into every
    call site for no benefit.
    """
    row = conn.execute(
        db.q("SELECT settings, updated_at FROM user_settings WHERE user_id = ?"),
        (user_id,),
    ).fetchone()
    if not row:
        return dict(DEFAULTS, tracking=dict(DEFAULTS["tracking"])), None
    try:
        stored = json.loads(row["settings"])
    except (TypeError, ValueError):
        # A corrupt row must not take the settings screen down with it.
        # Defaults are a safe answer: tracking on, nothing excluded.
        return dict(DEFAULTS, tracking=dict(DEFAULTS["tracking"])), None
    return normalise(stored), row["updated_at"]


def save(conn, user_id, incoming):
    """Merges an update over the stored settings and returns the result.

    The timestamp is the server's — see the module docstring. A client that
    could supply it could pin stale settings forever by claiming a date in
    the future.
    """
    existing, _ = load(conn, user_id)
    merged = normalise(incoming, existing)
    now = int(time.time() * 1000)
    payload = json.dumps(merged, separators=(",", ":"))

    if db.USE_POSTGRES:
        conn.execute(
            db.q("""INSERT INTO user_settings (user_id, settings, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT (user_id) DO UPDATE
                      SET settings = EXCLUDED.settings,
                          updated_at = EXCLUDED.updated_at"""),
            (user_id, payload, now),
        )
    else:
        conn.execute(
            db.q("""INSERT INTO user_settings (user_id, settings, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(user_id) DO UPDATE
                      SET settings = excluded.settings,
                          updated_at = excluded.updated_at"""),
            (user_id, payload, now),
        )
    return merged, now


# There is deliberately no delete() here. `user_settings` is listed in
# db.USER_SCOPED_TABLES, so both `/api/me/export` and `/api/me/data` already
# cover it — a second deletion path would be one more thing to forget.
