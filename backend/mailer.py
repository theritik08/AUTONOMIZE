"""Email delivery, and an honest answer when there isn't any.

THE FAILURE MODE THIS FILE EXISTS TO AVOID
-------------------------------------------

A password-reset flow that returns 200 OK while sending nothing is worse
than one that isn't implemented, because it looks finished. Every account
whose owner is waiting for that mail is locked out, and the logs say
success. `accounts.py` used to name this as a known gap — "no mail
transport is configured; accounts are created email_verified = 0 and the
flag is honoured but nothing sends the mail" — and that was the honest
thing to do at the time.

So there are exactly two modes here and the running one is printed at
startup and exposed on `/api/auth/config`:

    smtp     AUTONOMIZE_SMTP_HOST is set. Real mail. A send failure
             raises, and the endpoint reports it.

    console  Nothing is configured. The message is written to the log
             and, if AUTONOMIZE_MAIL_DIR is set, to a file. The API
             response says `"delivery": "console"` so a developer knows
             where to look and a deployment cannot mistake this for
             working mail.

There is no third mode that silently drops mail.

WHY THE OTP IS NOT IN THE API RESPONSE
---------------------------------------

Not even in console mode, and not behind a debug flag. A flag like that
gets turned on in production exactly once, and then the endpoint that
mails a login code returns the login code to anyone who asks for it —
which is a complete authentication bypass for every account whose email
address is known. The dev-mode code goes to the log and the file sink,
both of which require access to the server.
"""
import logging
import os
import pathlib
import smtplib
import ssl
import time
from email.message import EmailMessage

logger = logging.getLogger("autonomize.mail")

SMTP_HOST = os.environ.get("AUTONOMIZE_SMTP_HOST", "").strip()
SMTP_PORT = int(os.environ.get("AUTONOMIZE_SMTP_PORT", "587"))
SMTP_USER = os.environ.get("AUTONOMIZE_SMTP_USER", "").strip()
SMTP_PASSWORD = os.environ.get("AUTONOMIZE_SMTP_PASSWORD", "")
SMTP_STARTTLS = os.environ.get("AUTONOMIZE_SMTP_STARTTLS", "1").strip().lower() not in ("0", "false", "no")
MAIL_FROM = os.environ.get("AUTONOMIZE_MAIL_FROM", "Autonomize <no-reply@autonomize.local>")
MAIL_DIR = os.environ.get("AUTONOMIZE_MAIL_DIR", "").strip()

# Whether mail can REACH someone, which is not the same question as
# whether SMTP is configured.
#
# `MODE` is the transport. `ENABLED` is the capability, and it is what
# /api/auth/config reports so the UI can offer OTP, email verification and
# password reset only when a code will actually be retrievable.
#
# SMTP obviously counts. An explicitly configured AUTONOMIZE_MAIL_DIR also
# counts: setting it is a deliberate statement that someone is reading that
# directory, which is exactly how local development and the e2e suite
# consume codes. Bare console mode does NOT count — writing to a log nobody
# is watching and then advertising "sign in with an email code" gives the
# user a button that silently does nothing, which is worse than not
# offering it.
#
# This distinction matters in both directions. The old rule
# (`ENABLED = bool(SMTP_HOST)`) under-reported: the OTP and reset endpoints
# worked perfectly in console mode, but the dashboard hid them, so a
# feature that existed could not be reached or tested.
SMTP_ENABLED = bool(SMTP_HOST)
ENABLED = SMTP_ENABLED or bool(MAIL_DIR)
MODE = "smtp" if SMTP_ENABLED else "console"

# Test hook. Populated only when MODE == "console".
_sent: list = []


class MailError(Exception):
    """Delivery failed. The message is safe to show a user."""


def describe() -> str:
    if SMTP_ENABLED:
        return f"smtp {SMTP_HOST}:{SMTP_PORT} as {SMTP_USER or '(anonymous)'}"
    return ("console — NO MAIL IS SENT. OTP codes and reset links go to the "
            "server log" + (f" and {MAIL_DIR}" if MAIL_DIR else "") +
            ". Set AUTONOMIZE_SMTP_HOST before letting real users sign up.")


def outbox() -> list:
    """Everything console mode has 'sent' this process. Tests read this;
    nothing in the request path does."""
    return list(_sent)


def reset_outbox() -> None:
    _sent.clear()


def send(to: str, subject: str, body: str) -> str:
    """Delivers one message. Returns the mode used. Raises MailError.

    Returning the mode rather than True is what lets the API tell a client
    "we accepted this but nothing was actually mailed" instead of implying
    a delivery that did not happen.
    """
    if not SMTP_ENABLED:
        _sent.append({"to": to, "subject": subject, "body": body, "at": time.time()})
        logger.warning("MAIL (console mode, not delivered) to=%s subject=%s\n%s",
                       to, subject, body)
        if MAIL_DIR:
            try:
                directory = pathlib.Path(MAIL_DIR)
                directory.mkdir(parents=True, exist_ok=True)
                stamp = time.strftime("%Y%m%d-%H%M%S")
                path = directory / f"{stamp}-{to.replace('@', '_at_')}.txt"
                path.write_text(f"To: {to}\nSubject: {subject}\n\n{body}\n")
            except OSError as error:
                # A broken file sink must not break the flow it is logging.
                logger.warning("could not write mail sink: %s", error)
        return "console"

    message = EmailMessage()
    message["From"] = MAIL_FROM
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)

    try:
        if SMTP_PORT == 465:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=15,
                                  context=ssl.create_default_context()) as server:
                if SMTP_USER:
                    server.login(SMTP_USER, SMTP_PASSWORD)
                server.send_message(message)
        else:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
                if SMTP_STARTTLS:
                    # Not optional in practice: without it the SMTP AUTH
                    # below sends the mailbox password in the clear.
                    server.starttls(context=ssl.create_default_context())
                if SMTP_USER:
                    server.login(SMTP_USER, SMTP_PASSWORD)
                server.send_message(message)
    except (smtplib.SMTPException, OSError, ssl.SSLError) as error:
        # The exception text can name the SMTP host and the account it
        # tried to authenticate as. Log it, do not return it.
        logger.error("SMTP delivery failed to %s: %s", to, error)
        raise MailError("Could not send that email. Please try again shortly.") from error
    return "smtp"


# ---------------------------------------------------------------------------
# Templates. Plain text on purpose — HTML mail is a phishing-training
# surface and buys nothing for a six-digit code.
# ---------------------------------------------------------------------------

def send_otp(to: str, code: str, purpose: str, ttl_minutes: int) -> str:
    headline = {
        "signup": "Confirm your email address",
        "login": "Your sign-in code",
        "reset": "Reset your password",
        "verify_email": "Confirm your email address",
    }.get(purpose, "Your verification code")

    return send(to, f"Autonomize — {headline}", f"""{headline}

Your code is: {code}

It expires in {ttl_minutes} minutes and can only be used once.

If you didn't ask for this, you can ignore this email — nothing has
changed on your account. Nobody can use this code without also having
access to this mailbox.

We will never ask you for this code by phone, chat, or reply.
""")
