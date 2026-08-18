"""Device registry, and the bridge from an anonymous install to an account.

WHAT A DEVICE ID IS HERE, AND WHAT IT IS NOT
---------------------------------------------

It is a random UUID that the EXTENSION generates once at install and
keeps in `chrome.storage.local`. It is not derived from anything about
the machine: no MAC address, no CPU identifier, no disk serial, no
canvas/WebGL fingerprint, no screen geometry, no font list.

That is a deliberate refusal, and the reasons are not only ethical:

  1. A hardware fingerprint cannot be revoked. The entire point of the
     device list is that a student can press "sign out this device" and
     have it mean something. You cannot revoke someone's CPU.

  2. A hardware fingerprint follows a person ACROSS accounts. Two
     students sharing a lab machine would be silently correlated, and a
     student who deleted their account and started again would be
     re-identified. Both are surveillance properties, and neither is
     needed to know "which installs are signed into this account".

  3. It is stable through uninstall. "Remove the extension" should mean
     something, and with a fingerprint it does not.

  4. It is the wrong tool anyway. A random id is a perfect identifier for
     an install; a fingerprint is a mediocre identifier for a machine.

So: `crypto.randomUUID()`, stored locally, sent as a header. If the user
uninstalls and reinstalls they get a new device row, which is correct —
it IS a new install.

THE LINKING FLOW
----------------

A fresh install has an anonymous device account (see
`accounts.create_device_user`) and starts collecting immediately — no
signup, which is the property that makes the project demonstrable. Later
the student wants that history on their real account.

    1. The extension, holding its device session, asks for a link code.
       Six characters from an unambiguous alphabet, valid for 10 minutes,
       single use, attempt-capped.

    2. The student opens the dashboard and SIGNS IN. This is the step
       that matters: the code is consumed by an AUTHENTICATED request, so
       possessing a code is not by itself enough to attach a device to
       anybody. An attacker who somehow read the code off the popup still
       has to be signed into an account, and the only account they can
       attach it to is their own — which gives them their own device's
       data, not the victim's.

    3. The server re-points the device's rows at the real account and
       revokes the anonymous account's credentials.

No user_id is ever typed. No device_id is ever typed. The code is the
only thing that crosses between the two surfaces and it is short-lived,
single-use, and useless without a session.

WHY THE MERGE DIRECTION IS ONE-WAY
-----------------------------------

Device rows move INTO the real account; the anonymous account is then
marked deleted and its sessions revoked. The reverse — attaching a real
account's history to an anonymous device — is never offered, because it
would be a way to pull data out of an account you cannot log into.
"""
import hashlib
import hmac
import secrets
import time
import uuid

import db

# No 0/O, no 1/I/L. A student reads this off a popup and types it into a
# dashboard; ambiguity here becomes support tickets, and "the code didn't
# work" is indistinguishable from an attack in the logs.
ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 6
CODE_TTL_SECONDS = 10 * 60
MAX_CODE_ATTEMPTS = 5

# Rows that carry a user's telemetry. A device merge re-points every one
# of them. Kept as an explicit list rather than "every table with a
# user_id" so that adding a table is a decision about whether it should
# move, not an accident.
#
# Split by primary key, because the two groups need different handling:
#
#   MERGE_TABLES_BY_ROW    keyed by session_id / event_id / check_id, all
#                          globally unique. A plain UPDATE can never
#                          collide.
#
#   MERGE_TABLES_BY_KEY    keyed by user_id (+ a discriminator). The
#                          target account may ALREADY have a row for the
#                          same key — its own baseline, its own settings,
#                          its own bandit state. A plain UPDATE there
#                          raises a PK violation, and on Postgres that
#                          aborts the whole transaction, taking the code
#                          consumption and every other table's move with
#                          it. So conflicting source rows are dropped and
#                          the target account's own data wins.
#
# Getting this wrong is not a crash — it is a merge that half-applies and
# leaves a student's history split across two ids with no way back.
#
MERGE_TABLES_BY_ROW = (
    "sessions", "nudge_events", "session_labels",
    "retrieval_checks", "session_ground_truth",
)

MERGE_TABLES_BY_KEY = {
    "user_baseline": ("user_id", "category"),
    "bandit_state": ("user_id", "arm"),
    "user_settings": ("user_id",),
}


class DeviceError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def _now_ms() -> int:
    return int(time.time() * 1000)


def _hash_code(code: str) -> str:
    from accounts import SECRET
    return hmac.new(SECRET.encode(), f"link:{code.upper()}".encode(), hashlib.sha256).hexdigest()


def _hash_claim(secret: str) -> str:
    """Separate domain prefix from _hash_code on purpose.

    Both hashes live in the same row and the same column family. Without
    distinct prefixes, a value that is valid in one position would be
    valid in the other, and the claim secret — which mints a session on
    its own — must never be interchangeable with the six-character code,
    which deliberately cannot.
    """
    from accounts import SECRET
    return hmac.new(SECRET.encode(), f"claim:{secret}".encode(), hashlib.sha256).hexdigest()


def generate_code() -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(CODE_LENGTH))


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def register(conn, *, user_id: str, device_id: str | None = None,
             label: str | None = None, platform: str | None = None,
             client: str | None = None, ip_hash: str | None = None) -> dict:
    """Records (or refreshes) one install against one account.

    `device_id` is accepted from the client because the client is the only
    thing that knows which install it is — but note what that CANNOT do:
    the row's `user_id` comes from the authenticated session, never from
    the request. Claiming someone else's device_id therefore re-points
    nothing; at worst you overwrite the label on a row you already own.
    """
    device_id = (device_id or "").strip() or str(uuid.uuid4())
    if len(device_id) > 64:
        raise DeviceError("That device id is not valid.")

    now = _now_ms()
    existing = conn.execute(
        db.q("SELECT * FROM devices WHERE device_id = ?"), (device_id,)
    ).fetchone()

    if existing is not None:
        existing = dict(existing)
        if existing["user_id"] != user_id:
            # Two accounts cannot share one install row. Rather than
            # reject — which would strand an extension whose id collided
            # or which was re-linked — mint a fresh id for this account.
            return register(conn, user_id=user_id, device_id=str(uuid.uuid4()),
                            label=label, platform=platform, client=client, ip_hash=ip_hash)
        conn.execute(
            db.q("""UPDATE devices SET last_seen_at = ?, label = COALESCE(?, label),
                                       platform = COALESCE(?, platform),
                                       client = COALESCE(?, client), revoked_at = NULL
                    WHERE device_id = ?"""),
            (now, label, platform, client, device_id),
        )
        return get(conn, device_id)

    conn.execute(
        db.q("""INSERT INTO devices
                (device_id, user_id, label, platform, client, created_at, last_seen_at, ip_hash)
                VALUES (?,?,?,?,?,?,?,?)"""),
        (device_id, user_id, (label or "")[:80] or None, (platform or "")[:40] or None,
         (client or "")[:40] or None, now, now, ip_hash),
    )
    return get(conn, device_id)


def get(conn, device_id: str):
    row = conn.execute(db.q("SELECT * FROM devices WHERE device_id = ?"), (device_id,)).fetchone()
    return dict(row) if row else None


def touch(conn, device_id: str, user_id: str) -> None:
    """Updates last-seen. Scoped by user_id so a request cannot refresh a
    row it does not own."""
    conn.execute(
        db.q("UPDATE devices SET last_seen_at = ? WHERE device_id = ? AND user_id = ?"),
        (_now_ms(), device_id, user_id),
    )


def list_for_user(conn, user_id: str) -> list:
    rows = conn.execute(
        db.q("""SELECT device_id, label, platform, client, created_at, last_seen_at, revoked_at
                FROM devices WHERE user_id = ? ORDER BY last_seen_at DESC"""),
        (user_id,),
    ).fetchall()
    return [public_device(dict(r)) for r in rows]


def public_device(row: dict) -> dict:
    """Allow-listed, like accounts.public_user — a new column is invisible
    by default rather than leaking until someone remembers to redact it.
    `ip_hash` in particular must never reach a client."""
    return {
        "device_id": row["device_id"],
        "label": row.get("label"),
        "platform": row.get("platform"),
        "client": row.get("client"),
        "created_at": row.get("created_at"),
        "last_seen_at": row.get("last_seen_at"),
        "revoked": row.get("revoked_at") is not None,
    }


def revoke(conn, *, device_id: str, user_id: str) -> bool:
    """Signs one device out. Scoped by user_id — that scoping IS the
    authorization check, and dropping it would make any device revocable
    by anyone who could name it."""
    cursor = conn.execute(
        db.q("UPDATE devices SET revoked_at = ? WHERE device_id = ? AND user_id = ? AND revoked_at IS NULL"),
        (_now_ms(), device_id, user_id),
    )
    return cursor.rowcount > 0


def rename(conn, *, device_id: str, user_id: str, label: str) -> dict:
    label = (label or "").strip()[:80]
    if not label:
        raise DeviceError("Give the device a name.")
    cursor = conn.execute(
        db.q("UPDATE devices SET label = ? WHERE device_id = ? AND user_id = ?"),
        (label, device_id, user_id),
    )
    if cursor.rowcount == 0:
        raise DeviceError("No such device on this account.", status=404)
    return public_device(get(conn, device_id))


# ---------------------------------------------------------------------------
# Linking an anonymous install to a real account
# ---------------------------------------------------------------------------

def start_link(conn, *, device_user_id: str, device_id: str | None) -> dict:
    """Called by the extension with its DEVICE session. Returns a code to
    show the user. The raw code is never stored — only its HMAC."""
    now = _now_ms()

    # One live code per device. A second request replaces the first rather
    # than adding to it, so a popup left open for an hour cannot leave a
    # trail of valid codes behind it.
    conn.execute(
        db.q("""UPDATE device_link_codes SET consumed_at = ?
                WHERE device_user_id = ? AND consumed_at IS NULL"""),
        (now, device_user_id),
    )

    code = generate_code()
    # The claim secret goes back to the extension and nowhere else. The
    # user never sees it, never types it, and it never reaches the
    # dashboard — see _hash_claim and the module docstring's step 4.
    claim_secret = secrets.token_urlsafe(32)
    conn.execute(
        db.q("""INSERT INTO device_link_codes
                (code_hash, device_id, device_user_id, created_at, expires_at,
                 attempts, claim_secret_hash)
                VALUES (?,?,?,?,?,0,?)"""),
        (_hash_code(code), device_id or "", device_user_id, now,
         now + CODE_TTL_SECONDS * 1000, _hash_claim(claim_secret)),
    )
    return {"code": code, "claim_secret": claim_secret,
            "expires_at": now + CODE_TTL_SECONDS * 1000,
            "expires_in_seconds": CODE_TTL_SECONDS}


def complete_link(conn, *, code: str, account_user_id: str) -> dict:
    """Called by the DASHBOARD with a real signed-in session.

    `account_user_id` comes from that session and is never read from the
    request body. That is what makes a leaked code low-value: the holder
    can only attach the device to an account they can already log into.
    """
    code = (code or "").strip().upper()
    now = _now_ms()
    generic = DeviceError("That code is wrong or has expired. Generate a new one.", status=400)

    row = conn.execute(
        db.q("SELECT * FROM device_link_codes WHERE code_hash = ?"), (_hash_code(code),)
    ).fetchone()
    if row is None:
        raise generic
    row = dict(row)

    if row["consumed_at"] is not None or row["expires_at"] < now:
        raise generic
    if int(row["attempts"]) >= MAX_CODE_ATTEMPTS:
        raise generic

    device_user_id = row["device_user_id"]
    if device_user_id == account_user_id:
        raise DeviceError("That device is already on this account.", status=409)

    # Single-use, claimed by rowcount so two racing requests cannot both
    # merge the same device.
    cursor = conn.execute(
        db.q("UPDATE device_link_codes SET consumed_at = ? WHERE code_hash = ? AND consumed_at IS NULL"),
        (now, row["code_hash"]),
    )
    if cursor.rowcount != 1:
        raise generic

    moved = merge_user_data(conn, from_user_id=device_user_id, to_user_id=account_user_id)

    device_id = row["device_id"] or str(uuid.uuid4())
    register(conn, user_id=account_user_id, device_id=device_id, client="extension")

    # Record WHICH account the device now belongs to, so the extension's
    # claim knows whose session to mint. Without this the caller revokes
    # the device's only credential and leaves it no way to obtain another
    # — which is precisely the bug migration 13 exists to close.
    conn.execute(
        db.q("UPDATE device_link_codes SET linked_user_id = ? WHERE code_hash = ?"),
        (account_user_id, row["code_hash"]),
    )

    return {"device_id": device_id, "rows_moved": moved, "from_user_id": device_user_id}


# The window the extension has to collect its new credential, measured
# from the moment the link completes. Deliberately longer than the code's
# ten minutes: the code's clock is a human reading six characters off a
# popup, and this one is a background worker that may be asleep when the
# link lands. Chrome will not wake an alarm more often than once a
# minute, so a ten-minute claim window would be a handful of attempts.
CLAIM_TTL_SECONDS = 60 * 60


def claim_link(conn, *, claim_secret: str) -> dict:
    """Called by the EXTENSION, unauthenticated, after the link completes.

    Unauthenticated because it has to be: complete_link revoked the only
    token this caller had. What stands in for a bearer token is the 256-bit
    secret minted at start_link and returned solely to the extension that
    asked — never displayed, never typed, never sent to the dashboard.

    Three states, and the distinction matters to the caller:

      pending  — the code has not been entered yet. Keep polling.
      linked   — here is a session for the real account. Stop polling.
      expired  — the code lapsed, or the credential was already claimed.
                 Start over; do not retry.
    """
    now = _now_ms()
    row = conn.execute(
        db.q("SELECT * FROM device_link_codes WHERE claim_secret_hash = ?"),
        (_hash_claim(claim_secret or ""),),
    ).fetchone()
    if row is None:
        raise DeviceError("That link request is not recognised.", status=404)
    row = dict(row)

    if row["linked_user_id"] is None:
        # Not linked yet. The code itself can still expire underneath us,
        # and saying so is what stops a popup polling a dead code forever.
        if row["expires_at"] < now:
            raise DeviceError("That code expired before it was used.", status=410)
        return {"status": "pending"}

    # Single-use, claimed by rowcount for the same reason complete_link is:
    # two racing polls must not both mint a session.
    cursor = conn.execute(
        db.q("""UPDATE device_link_codes SET claimed_at = ?
                WHERE claim_secret_hash = ? AND claimed_at IS NULL"""),
        (now, row["claim_secret_hash"]),
    )
    if cursor.rowcount != 1:
        raise DeviceError("That credential was already claimed.", status=410)

    if row["consumed_at"] is not None and row["consumed_at"] + CLAIM_TTL_SECONDS * 1000 < now:
        raise DeviceError("Too long since the link completed. Link again.", status=410)

    return {"status": "linked", "user_id": row["linked_user_id"],
            "device_id": row["device_id"] or None}


def merge_user_data(conn, *, from_user_id: str, to_user_id: str) -> dict:
    """Re-points one user's telemetry at another.

    Only ever called with `from_user_id` being an anonymous device account
    that the caller has proven control of via a link code. It is not a
    general-purpose "merge two accounts" — that would need both sides to
    consent, and nothing in this system asks for that.

    Rows whose primary key would collide are dropped rather than
    overwritten — a collision means the target account already has its own
    baseline/settings/bandit state for that key, and the account's own
    data wins. The alternative is that linking a device silently rewrites
    history the student already had, which is the worse surprise.

    Returns a per-table count of rows moved, so the caller can report what
    actually happened instead of asserting success.
    """
    moved = {}

    for table in MERGE_TABLES_BY_ROW:
        cursor = conn.execute(
            db.q(f"UPDATE {table} SET user_id = ? WHERE user_id = ?"),
            (to_user_id, from_user_id),
        )
        moved[table] = cursor.rowcount

    for table, key in MERGE_TABLES_BY_KEY.items():
        discriminators = key[1:]
        # Drop the source rows the target already holds, matched on the
        # non-user part of the key. The correlated subquery aliases the
        # table so the outer row is still addressable by its bare name.
        match = "".join(f" AND t.{column} = {table}.{column}" for column in discriminators)
        conn.execute(
            db.q(f"""DELETE FROM {table} WHERE user_id = ? AND EXISTS (
                       SELECT 1 FROM {table} t WHERE t.user_id = ?{match}
                     )"""),
            (from_user_id, to_user_id),
        )
        cursor = conn.execute(
            db.q(f"UPDATE {table} SET user_id = ? WHERE user_id = ?"),
            (to_user_id, from_user_id),
        )
        moved[table] = cursor.rowcount

    return moved
