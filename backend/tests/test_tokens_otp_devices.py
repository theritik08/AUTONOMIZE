"""Unit tests for the token, OTP and device modules.

test_security_auth.py drives these through HTTP, which is where the real
behaviour lives. These go underneath it, at the level where a wrong
constant or a lost commit is visible directly rather than as a confusing
status code three layers up.
"""
import os
import sqlite3
import tempfile
import time
import uuid
from pathlib import Path

import pytest


@pytest.fixture
def conn():
    """A real SQLite file with the real migrations. Not a mock — the
    behaviour under test here IS the SQL (rowcount claims, correlated
    deletes, the commit-before-raise), and a mock would assert that the
    code called the functions rather than that they did anything."""
    tmp = tempfile.mkdtemp(prefix="autonomize-unit-")
    path = Path(tmp) / "unit.db"
    os.environ["AUTONOMIZE_DB_PATH"] = str(path)

    import importlib
    import db as db_module
    importlib.reload(db_module)
    import migrations
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    migrations.apply_migrations(connection, False, int(time.time() * 1000))
    connection.commit()
    yield connection
    connection.close()
    for leftover in Path(tmp).glob("*"):
        leftover.unlink()
    Path(tmp).rmdir()
    os.environ.pop("AUTONOMIZE_DB_PATH", None)


# ===========================================================================
# tokens.py
# ===========================================================================

def test_a_refresh_token_is_never_stored_in_the_clear(conn):
    import tokens
    issued = tokens.issue_refresh(conn, user_id="u1")
    row = dict(conn.execute("SELECT * FROM refresh_tokens WHERE token_id = ?",
                            (issued["token_id"],)).fetchone())
    assert issued["token"] not in row["token_hash"]
    assert row["token_hash"] == tokens.hash_token(issued["token"])
    assert len(issued["token"]) >= 32


def test_rotation_starts_a_new_token_in_the_same_family(conn):
    import tokens
    first = tokens.issue_refresh(conn, user_id="u1")
    rotated = tokens.rotate(conn, first["token"])
    assert rotated["refresh"]["family_id"] == first["family_id"]
    assert rotated["refresh"]["token"] != first["token"]


def test_reuse_revokes_the_family_and_the_revocation_survives_the_raise(conn):
    """The commit-before-raise. `db.get_conn()` rolls back on an exception,
    so revoking the family and then raising would undo the revocation —
    the detector would fire, log, and leave the attacker's token live."""
    import tokens
    first = tokens.issue_refresh(conn, user_id="u1")
    second = tokens.rotate(conn, first["token"])

    with pytest.raises(tokens.TokenError) as caught:
        tokens.rotate(conn, first["token"])
    assert getattr(caught.value, "reuse_detected", False) is True

    live = conn.execute(
        "SELECT COUNT(*) n FROM refresh_tokens WHERE family_id = ? AND revoked_at IS NULL",
        (first["family_id"],)).fetchone()["n"]
    assert live == 0, "the family revocation was rolled back by the raise"

    with pytest.raises(tokens.TokenError):
        tokens.rotate(conn, second["refresh"]["token"])


def test_an_unknown_token_reveals_nothing_and_revokes_nothing(conn):
    import tokens
    live = tokens.issue_refresh(conn, user_id="u1")
    with pytest.raises(tokens.TokenError):
        tokens.rotate(conn, "not-a-real-token-at-all")
    still_live = conn.execute(
        "SELECT COUNT(*) n FROM refresh_tokens WHERE family_id = ? AND revoked_at IS NULL",
        (live["family_id"],)).fetchone()["n"]
    assert still_live == 1


def test_an_expired_refresh_token_is_refused(conn, monkeypatch):
    import tokens
    issued = tokens.issue_refresh(conn, user_id="u1")
    conn.execute("UPDATE refresh_tokens SET expires_at = ? WHERE token_id = ?",
                 (int(time.time() * 1000) - 1000, issued["token_id"]))
    with pytest.raises(tokens.TokenError):
        tokens.rotate(conn, issued["token"])


def test_revoking_one_device_leaves_the_others_signed_in(conn):
    import tokens
    keep = tokens.issue_refresh(conn, user_id="u1", device_id="phone")
    kill = tokens.issue_refresh(conn, user_id="u1", device_id="laptop")

    tokens.revoke_for_device(conn, "laptop", "u1")
    assert tokens.rotate(conn, keep["token"])["user_id"] == "u1"
    with pytest.raises(tokens.TokenError):
        tokens.rotate(conn, kill["token"])


def test_one_account_cannot_revoke_anothers_device(conn):
    """revoke_for_device is scoped by user_id as well as device_id — a
    device id is guessable enough that accepting it alone would let any
    account sign out any other account's device."""
    import tokens
    victim = tokens.issue_refresh(conn, user_id="victim", device_id="shared-id")
    tokens.revoke_for_device(conn, "shared-id", "attacker")
    assert tokens.rotate(conn, victim["token"])["user_id"] == "victim"


def test_purge_keeps_expired_rows_long_enough_to_detect_reuse(conn):
    """A deleted token reads as an UNKNOWN token, and an unknown token
    revokes nothing. Purging too eagerly is how reuse detection quietly
    stops firing for the oldest — i.e. most likely stolen — tokens."""
    import tokens
    issued = tokens.issue_refresh(conn, user_id="u1")
    conn.execute("UPDATE refresh_tokens SET expires_at = ? WHERE token_id = ?",
                 (int(time.time() * 1000) - 1000, issued["token_id"]))
    assert tokens.purge_expired(conn, older_than_days=60) == 0
    assert tokens.purge_expired(conn, older_than_days=0) == 1


# ===========================================================================
# otp.py
# ===========================================================================

def test_codes_are_uniform_over_the_whole_six_digit_space():
    """Including the leading-zero range. A generator that returned
    `randint(100000, 999999)` would silently shrink the space by 10% and
    nobody would notice."""
    import otp
    seen = {otp.generate_code() for _ in range(3000)}
    assert all(len(c) == 6 and c.isdigit() for c in seen)
    assert any(c.startswith("0") for c in seen), "leading zeros never generated"
    assert len(seen) > 2500


def test_the_code_hash_is_bound_to_the_email(conn):
    """So a row cannot be lifted from one account to another, and two
    users issued the same six digits do not share a hash."""
    import otp
    assert otp.hash_code("123456", "a@x.com") != otp.hash_code("123456", "b@x.com")


def test_a_wrong_guess_is_counted_even_though_verify_raises(conn):
    """The commit-before-raise again. Without it the attempt counter sits
    at zero forever and MAX_ATTEMPTS never bites."""
    import otp
    otp.issue(conn, email="a@x.com", purpose="login", send=False)
    with pytest.raises(otp.OtpError):
        otp.verify(conn, email="a@x.com", purpose="login", code="000000")
    attempts = conn.execute(
        "SELECT attempts FROM otp_codes WHERE email = ?", ("a@x.com",)).fetchone()["attempts"]
    assert attempts == 1, "the attempt counter was rolled back"


def test_the_attempt_cap_burns_the_code(conn):
    import otp
    otp.issue(conn, email="a@x.com", purpose="login", send=False)
    for _ in range(otp.MAX_ATTEMPTS):
        with pytest.raises(otp.OtpError):
            otp.verify(conn, email="a@x.com", purpose="login", code="000000")
    row = dict(conn.execute("SELECT * FROM otp_codes WHERE email = ?", ("a@x.com",)).fetchone())
    assert row["attempts"] >= otp.MAX_ATTEMPTS
    with pytest.raises(otp.OtpError):
        otp.verify(conn, email="a@x.com", purpose="login", code="000000")


def test_purposes_do_not_share_a_code(conn):
    """The takeover bug: a code mailed for one purpose spent on another."""
    import otp
    otp.issue(conn, email="a@x.com", purpose="verify_email", send=False)

    # A `reset` verify must find NO outstanding row at all, whatever code
    # is supplied — the purpose filters the lookup, so the verify_email
    # row is invisible to it and cannot be spent or even damaged.
    with pytest.raises(otp.OtpError):
        otp.verify(conn, email="a@x.com", purpose="reset", code="000000")
    outstanding = conn.execute(
        "SELECT COUNT(*) n FROM otp_codes WHERE purpose = 'verify_email' AND consumed_at IS NULL"
    ).fetchone()["n"]
    assert outstanding == 1, "the wrong-purpose attempt consumed the right-purpose code"


def test_the_resend_cooldown_is_enforced(conn):
    import otp
    otp.issue(conn, email="a@x.com", purpose="login", send=False)
    with pytest.raises(otp.OtpError) as caught:
        otp.issue(conn, email="a@x.com", purpose="login", send=False)
    assert caught.value.status == 429
    assert caught.value.retry_after


def test_the_hourly_quota_backstops_purpose_rotation(conn, monkeypatch):
    """The per-purpose cooldown can be sidestepped by alternating
    purposes. The hourly quota is what stops that becoming an unlimited
    mail bomb."""
    import otp
    monkeypatch.setattr(otp, "RESEND_COOLDOWN_SECONDS", 0)
    issued = 0
    for _ in range(otp.MAX_PER_HOUR + 4):
        try:
            otp.issue(conn, email="a@x.com",
                      purpose=otp.PURPOSES[issued % len(otp.PURPOSES)], send=False)
            issued += 1
        except otp.OtpError as error:
            assert error.status == 429
            break
    assert issued == otp.MAX_PER_HOUR


def test_an_unknown_purpose_is_refused(conn):
    import otp
    with pytest.raises(otp.OtpError):
        otp.issue(conn, email="a@x.com", purpose="whatever_i_like", send=False)


# ===========================================================================
# devices.py
# ===========================================================================

def test_a_server_minted_device_id_is_random(conn):
    import devices
    ids = {devices.register(conn, user_id="u1")["device_id"] for _ in range(20)}
    assert len(ids) == 20


def test_a_device_id_claimed_by_another_account_does_not_move(conn):
    """The client sends the device id, so it can send anyone's. The row's
    user_id comes from the session, so claiming one re-points nothing —
    the claimant just gets a fresh id of their own."""
    import devices
    mine = devices.register(conn, user_id="victim", device_id="contested")
    theirs = devices.register(conn, user_id="attacker", device_id="contested")
    assert theirs["device_id"] != "contested"
    assert devices.get(conn, "contested")["user_id"] == "victim"


def test_the_link_code_is_not_stored_in_the_clear(conn):
    import devices
    started = devices.start_link(conn, device_user_id="anon", device_id="d1")
    row = conn.execute("SELECT code_hash FROM device_link_codes").fetchone()
    assert started["code"] != row["code_hash"]
    assert row["code_hash"] == devices._hash_code(started["code"])


def test_a_second_link_request_supersedes_the_first(conn):
    """A popup left open for an hour must not leave a trail of live codes."""
    import devices
    first = devices.start_link(conn, device_user_id="anon", device_id="d1")
    devices.start_link(conn, device_user_id="anon", device_id="d1")
    with pytest.raises(devices.DeviceError):
        devices.complete_link(conn, code=first["code"], account_user_id="real")


def test_the_merge_moves_row_keyed_tables_wholesale(conn):
    import devices
    now = int(time.time() * 1000)
    for i in range(3):
        conn.execute(
            """INSERT INTO sessions (session_id, user_id, category, created_at, updated_at)
               VALUES (?,?,?,?,?)""", (f"s{i}", "anon", "writing", now, now))
    moved = devices.merge_user_data(conn, from_user_id="anon", to_user_id="real")
    assert moved["sessions"] == 3
    assert conn.execute("SELECT COUNT(*) n FROM sessions WHERE user_id = 'real'"
                        ).fetchone()["n"] == 3


def test_the_merge_keeps_the_target_accounts_own_settings(conn):
    """user_settings is keyed by user_id alone, so both sides can hold a
    row. A plain UPDATE raises a PK violation — and on Postgres that
    aborts the whole transaction, taking the link-code consumption and
    every other table's move with it. The account's own data wins."""
    import devices
    now = int(time.time() * 1000)
    conn.execute("INSERT INTO user_settings (user_id, settings, updated_at) VALUES (?,?,?)",
                 ("anon", '{"from":"device"}', now))
    conn.execute("INSERT INTO user_settings (user_id, settings, updated_at) VALUES (?,?,?)",
                 ("real", '{"from":"account"}', now))

    devices.merge_user_data(conn, from_user_id="anon", to_user_id="real")
    rows = [dict(r) for r in conn.execute("SELECT * FROM user_settings").fetchall()]
    assert len(rows) == 1
    assert rows[0]["user_id"] == "real"
    assert rows[0]["settings"] == '{"from":"account"}'


def test_the_merge_handles_a_composite_key_per_discriminator(conn):
    """user_baseline is (user_id, category). The target's `writing` row
    must survive while the device's `assessment` row still moves across —
    an all-or-nothing rule on this table would lose real history."""
    import devices
    now = int(time.time() * 1000)
    for user, category, mean in (("anon", "writing", 10.0), ("anon", "assessment", 20.0),
                                 ("real", "writing", 99.0)):
        conn.execute("""INSERT INTO user_baseline
                          (user_id, category, ema_mean, streak_days, updated_at)
                        VALUES (?,?,?,?,?)""", (user, category, mean, 1, now))

    devices.merge_user_data(conn, from_user_id="anon", to_user_id="real")
    rows = {r["category"]: r["ema_mean"] for r in
            conn.execute("SELECT * FROM user_baseline WHERE user_id = 'real'").fetchall()}
    assert rows == {"writing": 99.0, "assessment": 20.0}
    assert conn.execute("SELECT COUNT(*) n FROM user_baseline WHERE user_id = 'anon'"
                        ).fetchone()["n"] == 0


def test_public_device_allow_lists_rather_than_redacts(conn):
    import devices
    row = devices.register(conn, user_id="u1", ip_hash="deadbeef")
    assert "ip_hash" not in devices.public_device(row)
    assert "user_id" not in devices.public_device(row)


# ===========================================================================
# oauth_google.py — the parts testable without Google
# ===========================================================================

def test_an_open_redirect_is_refused(conn, monkeypatch):
    """Exact match, never a prefix test: `https://autonomize.example.com
    .evil.tld` passes a startswith check and is a different origin."""
    import oauth_google
    monkeypatch.setattr(oauth_google, "ALLOWED_REDIRECTS", ["https://dash.example.edu"])
    assert oauth_google.safe_redirect("https://dash.example.edu") == "https://dash.example.edu"
    for hostile in ("https://dash.example.edu.evil.tld", "https://evil.tld",
                    "//evil.tld", "javascript:alert(1)", None):
        assert oauth_google.safe_redirect(hostile) is None


def test_a_state_is_single_use(conn, monkeypatch):
    """Without this a replayed callback runs the exchange twice."""
    import oauth_google
    monkeypatch.setattr(oauth_google, "ENABLED", True)
    monkeypatch.setattr(oauth_google, "CLIENT_ID", "test-client")
    started = oauth_google.begin(conn)
    assert oauth_google._consume_state(conn, started["state"])["nonce"]
    with pytest.raises(oauth_google.OAuthError):
        oauth_google._consume_state(conn, started["state"])


def test_the_pkce_challenge_is_the_sha256_of_the_verifier_not_the_verifier(conn, monkeypatch):
    """`code_challenge_method=plain` would send the secret itself and
    defeat the whole mechanism."""
    import base64
    import hashlib
    import urllib.parse
    import oauth_google
    monkeypatch.setattr(oauth_google, "ENABLED", True)
    monkeypatch.setattr(oauth_google, "CLIENT_ID", "test-client")

    started = oauth_google.begin(conn)
    query = urllib.parse.parse_qs(urllib.parse.urlparse(started["authorize_url"]).query)
    assert query["code_challenge_method"] == ["S256"]

    verifier = conn.execute("SELECT code_verifier FROM oauth_states WHERE state = ?",
                            (started["state"],)).fetchone()["code_verifier"]
    expected = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    assert query["code_challenge"] == [expected]
    assert verifier not in started["authorize_url"]


def test_the_authorize_url_carries_state_and_nonce(conn, monkeypatch):
    import urllib.parse
    import oauth_google
    monkeypatch.setattr(oauth_google, "ENABLED", True)
    monkeypatch.setattr(oauth_google, "CLIENT_ID", "test-client")
    started = oauth_google.begin(conn)
    query = urllib.parse.parse_qs(urllib.parse.urlparse(started["authorize_url"]).query)
    assert query["state"] == [started["state"]]
    assert query["nonce"] and query["nonce"][0]
    assert query["prompt"] == ["select_account"]


def test_one_google_account_cannot_be_linked_to_two_local_accounts(conn):
    import oauth_google
    oauth_google.link_identity(conn, user_id="u1", provider="google",
                               subject="google-sub-1", email="a@x.com")
    with pytest.raises(oauth_google.OAuthError):
        oauth_google.link_identity(conn, user_id="u2", provider="google",
                                   subject="google-sub-1", email="a@x.com")


def test_unlinking_the_only_sign_in_method_is_refused(conn):
    """Otherwise 'unlink Google' on a password-less account is a
    self-inflicted permanent lockout discovered after the fact."""
    import oauth_google
    oauth_google.link_identity(conn, user_id="u1", provider="google",
                               subject="sub-1", email="a@x.com")
    with pytest.raises(oauth_google.OAuthError):
        oauth_google.unlink(conn, user_id="u1", provider="google", has_password=False)
    assert oauth_google.unlink(conn, user_id="u1", provider="google", has_password=True)


# ===========================================================================
# mailer.py
# ===========================================================================

def test_console_mode_says_it_did_not_send():
    """The failure this module exists to prevent: a 200 OK for mail that
    was never delivered."""
    import mailer
    mailer.reset_outbox()
    assert mailer.send("a@x.com", "subject", "body") == "console"
    assert "NO MAIL IS SENT" in mailer.describe()
    assert mailer.outbox()[-1]["to"] == "a@x.com"


def test_the_otp_mail_never_tells_the_reader_to_share_the_code():
    import mailer
    mailer.reset_outbox()
    mailer.send_otp("a@x.com", "123456", "login", 10)
    body = mailer.outbox()[-1]["body"].lower()
    assert "123456" in body
    assert "never ask you for this code" in body
    assert "expires" in body
