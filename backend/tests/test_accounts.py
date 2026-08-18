"""Tests for accounts, roles, sessions and the audit trail.

The properties worth protecting are: you cannot enumerate users, you
cannot brute-force forever, you cannot grant yourself a role, and a signed
-out token is genuinely dead rather than merely expiring eventually.
"""
import time

import pytest

import accounts
import db


@pytest.fixture()
def conn(sqlite_conn):
    return sqlite_conn


def make(conn, email="a@b.com", password="the quiet river runs north", role="student"):
    return accounts.create_user(conn, email=email, password=password, role=role)


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------

def test_create_and_fetch(conn):
    user = make(conn)
    assert user["email"] == "a@b.com"
    assert user["role"] == "student"
    assert accounts.get_user_by_email(conn, "A@B.COM")["user_id"] == user["user_id"]


def test_email_is_normalised(conn):
    make(conn, email="  MiXeD@Case.COM  ")
    assert accounts.get_user_by_email(conn, "mixed@case.com") is not None


def test_duplicate_email_is_rejected(conn):
    make(conn)
    with pytest.raises(accounts.AuthError) as e:
        make(conn)
    assert e.value.status == 409


def test_invalid_email_is_rejected(conn):
    with pytest.raises(accounts.AuthError):
        accounts.create_user(conn, email="not-an-email", password="the quiet river runs north")


def test_password_is_never_stored_in_the_clear(conn):
    make(conn, password="the quiet river runs north")
    row = conn.execute("SELECT password_hash FROM users").fetchone()
    assert "quiet river" not in (row["password_hash"] or "")


def test_public_user_never_leaks_the_hash(conn):
    user = make(conn)
    public = accounts.public_user(user)
    assert "password_hash" not in public
    assert "failed_logins" not in public
    # Allow-listed, so a future sensitive column is invisible by default.
    assert set(public) == {"user_id", "email", "role", "display_name", "provider", "email_verified",
                            "has_password", "is_device_account"}


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

def test_correct_credentials_issue_a_session(conn):
    make(conn)
    session = accounts.authenticate(conn, email="a@b.com", password="the quiet river runs north")
    assert session["access_token"]
    assert session["user"]["email"] == "a@b.com"


def test_wrong_password_is_rejected(conn):
    make(conn)
    with pytest.raises(accounts.AuthError) as e:
        accounts.authenticate(conn, email="a@b.com", password="wrong password here")
    assert e.value.status == 401


def test_unknown_and_wrong_password_give_an_identical_response(conn):
    make(conn)
    with pytest.raises(accounts.AuthError) as unknown:
        accounts.authenticate(conn, email="nobody@nowhere.com", password="whatever long pass")
    with pytest.raises(accounts.AuthError) as wrong:
        accounts.authenticate(conn, email="a@b.com", password="whatever long pass")
    # Identical message AND status: any difference is a user-enumeration
    # oracle that tells an attacker which addresses are worth attacking.
    assert unknown.value.message == wrong.value.message
    assert unknown.value.status == wrong.value.status


def test_repeated_failures_lock_the_account(conn):
    make(conn)
    for _ in range(accounts.MAX_FAILED_LOGINS):
        with pytest.raises(accounts.AuthError):
            accounts.authenticate(conn, email="a@b.com", password="wrong password here")

    with pytest.raises(accounts.AuthError) as e:
        accounts.authenticate(conn, email="a@b.com", password="the quiet river runs north")
    # Even the CORRECT password is refused while locked out.
    assert e.value.status == 429


def test_lockout_is_temporary_not_permanent(conn):
    user = make(conn)
    for _ in range(accounts.MAX_FAILED_LOGINS):
        with pytest.raises(accounts.AuthError):
            accounts.authenticate(conn, email="a@b.com", password="wrong password here")

    # Wind the clock past the lock. A permanent lock would turn an attack
    # on someone's account into a denial of service against its owner.
    conn.execute("UPDATE users SET locked_until = ? WHERE user_id = ?",
                 (int(time.time() * 1000) - 1000, user["user_id"]))
    session = accounts.authenticate(conn, email="a@b.com", password="the quiet river runs north")
    assert session["access_token"]


def test_successful_login_clears_the_failure_counter(conn):
    make(conn)
    with pytest.raises(accounts.AuthError):
        accounts.authenticate(conn, email="a@b.com", password="wrong password here")
    accounts.authenticate(conn, email="a@b.com", password="the quiet river runs north")
    assert accounts.get_user_by_email(conn, "a@b.com")["failed_logins"] == 0


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

def test_issued_token_verifies(conn):
    make(conn)
    session = accounts.authenticate(conn, email="a@b.com", password="the quiet river runs north")
    assert accounts.verify_session(conn, session["access_token"])["email"] == "a@b.com"


def test_logout_kills_the_token_immediately(conn):
    make(conn)
    session = accounts.authenticate(conn, email="a@b.com", password="the quiet river runs north")
    accounts.revoke_session(conn, session["access_token"])
    # The entire reason sessions are stored rather than purely stateless:
    # the JWT is still cryptographically valid and unexpired here.
    with pytest.raises(accounts.AuthError):
        accounts.verify_session(conn, session["access_token"])


def test_logout_everywhere_kills_every_session(conn):
    user = make(conn)
    a = accounts.authenticate(conn, email="a@b.com", password="the quiet river runs north")
    b = accounts.authenticate(conn, email="a@b.com", password="the quiet river runs north")
    assert accounts.revoke_all_sessions(conn, user["user_id"]) == 2
    for session in (a, b):
        with pytest.raises(accounts.AuthError):
            accounts.verify_session(conn, session["access_token"])


def test_a_forged_token_is_rejected(conn):
    import jwt as pyjwt
    make(conn)
    forged = pyjwt.encode(
        {"sub": "somebody", "role": "admin", "jti": "made-up", "iss": accounts.ISSUER,
         "exp": int(time.time()) + 3600},
        "not-the-real-signing-key-padded-to-32-bytes", algorithm="HS256",
    )
    with pytest.raises(accounts.AuthError):
        accounts.verify_session(conn, forged)


def test_a_validly_signed_token_with_no_session_row_is_rejected(conn):
    import jwt as pyjwt
    user = make(conn)
    # Correctly signed, but its jti was never issued — this is what a
    # replayed or hand-crafted token looks like after a key leak of the
    # signing key alone.
    orphan = pyjwt.encode(
        {"sub": user["user_id"], "role": "student", "jti": "never-issued",
         "iss": accounts.ISSUER, "exp": int(time.time()) + 3600},
        accounts.SECRET, algorithm="HS256",
    )
    with pytest.raises(accounts.AuthError):
        accounts.verify_session(conn, orphan)


def test_expired_session_is_rejected(conn):
    user = make(conn)
    session = accounts.authenticate(conn, email="a@b.com", password="the quiet river runs north")
    conn.execute("UPDATE auth_sessions SET expires_at = ? WHERE user_id = ?",
                 (int(time.time() * 1000) - 1000, user["user_id"]))
    with pytest.raises(accounts.AuthError):
        accounts.verify_session(conn, session["access_token"])


def test_role_is_read_from_the_database_not_the_token(conn):
    user = make(conn, role="admin")
    session = accounts.authenticate(conn, email="a@b.com", password="the quiet river runs north")
    # Demote after the token was minted. The token still claims admin.
    conn.execute("UPDATE users SET role = 'student' WHERE user_id = ?", (user["user_id"],))
    # Resolution must reflect the demotion immediately, not at token expiry.
    assert accounts.verify_session(conn, session["access_token"])["role"] == "student"


# ---------------------------------------------------------------------------
# Privacy / audit
# ---------------------------------------------------------------------------

def test_ip_addresses_are_hashed_never_stored_raw(conn):
    make(conn)
    accounts.authenticate(conn, email="a@b.com", password="the quiet river runs north",
                          ip="203.0.113.42")
    rows = conn.execute("SELECT ip_hash FROM audit_log WHERE ip_hash IS NOT NULL").fetchall()
    assert rows
    for row in rows:
        assert "203.0.113.42" not in row["ip_hash"]


def test_security_events_are_audited(conn):
    make(conn)
    with pytest.raises(accounts.AuthError):
        accounts.authenticate(conn, email="a@b.com", password="wrong password here")
    accounts.authenticate(conn, email="a@b.com", password="the quiet river runs north")

    events = [r["event"] for r in conn.execute("SELECT event FROM audit_log ORDER BY id").fetchall()]
    # Without these an incident has nothing to reconstruct from.
    assert "user.created" in events
    assert "login.failed" in events
    assert "login.succeeded" in events
