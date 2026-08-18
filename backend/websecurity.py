"""Cookies, CSRF, and the CORS rule that must not be got wrong.

TWO CLIENTS, TWO STORAGE MODELS, AND WHY
-----------------------------------------

    The Chrome extension  gets its refresh token in the JSON body and
                          keeps it in `chrome.storage.local`. It has no
                          cookie jar shared with any website, it is not
                          reachable by a `<form>` on a malicious page,
                          and there is no CSRF surface — a cross-site
                          request cannot make Chrome attach an
                          extension's storage to it.

    The web dashboard     gets its refresh token in an HttpOnly cookie.
                          HttpOnly is the point: a successful XSS on the
                          dashboard can read anything in `localStorage`
                          and cannot read this. The access token still
                          lives in memory, because it has to be readable
                          to be put in a header — but it expires in ten
                          minutes, whereas a stolen refresh token is
                          thirty days of access.

Cookies buy XSS resistance and cost CSRF exposure, so the cookie path
carries a CSRF defence and the header path does not need one.

THE CSRF DESIGN — DOUBLE SUBMIT, AND ITS LIMIT
-----------------------------------------------

`SameSite=Lax` already blocks the classic case: a POST from another
site does not carry the cookie. It is not sufficient on its own —
`Lax` is a browser default that varies by version, some clients ignore
it, and a subdomain the attacker controls is same-site.

So `/api/auth/refresh` and every cookie-authenticated mutation also
require a CSRF token: a random value set in a READABLE cookie and echoed
back in the `X-CSRF-Token` header. Same-origin JavaScript can read the
cookie and set the header; cross-origin JavaScript can do neither,
because reading a cookie for another origin and setting a custom header
on a cross-origin request are both things the browser refuses.

What this does NOT stop, stated plainly: an attacker with XSS on our own
origin can read the CSRF cookie and forge the header. CSRF tokens have
never defended against XSS and claiming otherwise is how they get
misused. The defence against XSS is the CSP below and not putting
untrusted HTML on the page.

THE CORS RULE
-------------

`allow_origins=["*"]` with `allow_credentials=True` is a configuration
that browsers refuse — and the dangerous part is what FastAPI does about
it, which is to quietly reflect the requesting origin instead. That
means EVERY origin is allowed with credentials, which is the exact
opposite of what the person who wrote `*` believed they were doing.

So: credentials are only enabled when an explicit origin list is
configured, and starting with a wildcard plus cookies is refused at
startup rather than downgraded silently. A misconfiguration that fails
loudly on boot is worth ten that log a warning nobody reads.
"""
import os
import secrets

# Cookie names. The `__Host-` prefix is a browser-enforced guarantee: the
# cookie must be Secure, path=/, and have NO Domain attribute, which means
# a subdomain cannot set or overwrite it. That last part matters — without
# it, an attacker who gets XSS on any subdomain can overwrite the session
# cookie and fixate a session. Only usable over HTTPS, so the insecure
# local-dev name is different rather than silently dropping the guarantee.
REFRESH_COOKIE_SECURE = "__Host-autonomize_refresh"
REFRESH_COOKIE_DEV = "autonomize_refresh"
CSRF_COOKIE_SECURE = "__Host-autonomize_csrf"
CSRF_COOKIE_DEV = "autonomize_csrf"
CSRF_HEADER = "X-CSRF-Token"

# Off by default so `git clone && uvicorn main:app` still works over
# plain http on localhost, where Secure cookies are simply not sent.
# MUST be on in production; the startup banner says so.
SECURE_COOKIES = os.environ.get("AUTONOMIZE_SECURE_COOKIES", "").strip().lower() in (
    "1", "true", "yes", "on",
)

# Lax, not Strict: with Strict, following a link from an email into the
# dashboard arrives with no cookie and the user lands on a login screen
# they just logged in from. Lax still blocks cross-site POST, which is
# the CSRF case. Not None — `SameSite=None` would send the cookie on
# every cross-site request and hand the whole defence away.
SAME_SITE = os.environ.get("AUTONOMIZE_COOKIE_SAMESITE", "lax").strip().lower()


def refresh_cookie_name() -> str:
    return REFRESH_COOKIE_SECURE if SECURE_COOKIES else REFRESH_COOKIE_DEV


def csrf_cookie_name() -> str:
    return CSRF_COOKIE_SECURE if SECURE_COOKIES else CSRF_COOKIE_DEV


def describe() -> str:
    posture = "Secure + __Host- prefix" if SECURE_COOKIES else "INSECURE (http, dev only)"
    return f"cookies {posture}, SameSite={SAME_SITE}, CSRF double-submit on cookie auth"


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def set_auth_cookies(response, *, refresh_token: str, max_age_seconds: int,
                     csrf_token: str | None = None) -> str:
    """Attaches the refresh + CSRF cookies. Returns the CSRF token so the
    JSON body can carry it too — a first-load client has no other way to
    learn it if the cookie is not yet readable to it."""
    csrf = csrf_token or new_csrf_token()
    response.set_cookie(
        refresh_cookie_name(), refresh_token,
        max_age=max_age_seconds,
        httponly=True,          # unreadable to JavaScript — the whole point
        secure=SECURE_COOKIES,
        samesite=SAME_SITE,
        path="/",               # required by the __Host- prefix
    )
    response.set_cookie(
        csrf_cookie_name(), csrf,
        max_age=max_age_seconds,
        httponly=False,         # deliberately readable: the page must echo it
        secure=SECURE_COOKIES,
        samesite=SAME_SITE,
        path="/",
    )
    return csrf


def clear_auth_cookies(response) -> None:
    for name in (refresh_cookie_name(), csrf_cookie_name()):
        # Cleared with the same attributes they were set with. A delete
        # that omits path or secure does not match the stored cookie and
        # silently leaves the user logged in.
        response.delete_cookie(name, path="/", secure=SECURE_COOKIES, samesite=SAME_SITE)


def csrf_ok(request) -> bool:
    """True if this request may act on a cookie-borne credential.

    Requests that authenticate with an `Authorization` header are exempt,
    because a browser will not attach that header to a cross-site request
    on its own — the token has to be read and set by same-origin script,
    which is the same barrier the CSRF token relies on. Requiring a CSRF
    token there would break the extension for no gain.
    """
    if request.headers.get("authorization"):
        return True

    cookie = request.cookies.get(csrf_cookie_name())
    header = request.headers.get(CSRF_HEADER)
    if not cookie or not header:
        return False
    return secrets.compare_digest(cookie, header)


def validate_cors(origins: list, credentials: bool) -> None:
    """Raises on the wildcard-plus-credentials combination.

    Refused at import rather than warned about: FastAPI's response to it
    is to reflect the caller's origin, which turns "I left CORS open for
    development" into "every website may make authenticated requests as
    my users". A boot failure is recoverable in thirty seconds; this is
    not recoverable at all once it is live.
    """
    if credentials and "*" in origins:
        raise RuntimeError(
            "AUTONOMIZE_ALLOWED_ORIGINS is '*' while cookie authentication is enabled. "
            "A wildcard with credentials makes every origin trusted. Set an explicit "
            "comma-separated origin list, e.g. "
            "AUTONOMIZE_ALLOWED_ORIGINS='https://dashboard.example.edu,chrome-extension://<id>'."
        )


SECURITY_HEADERS = {
    # This API returns JSON and serves no HTML of its own, so the CSP is
    # about what happens if a response is ever rendered as a document —
    # an error page, a misconfigured proxy, a browser sniffing a type.
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'; base-uri 'none'",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    # Cache-busting for authenticated responses is handled per-route; this
    # header stops shared proxies storing anything by default.
    "Cache-Control": "no-store",
}
