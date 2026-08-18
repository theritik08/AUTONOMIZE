"""Autonomize backend — FastAPI service consumed by the Chrome extension.

Run locally:
    pip install -r requirements.txt
    uvicorn main:app --port 8787 --reload

Endpoints:
    GET    /api/health
    POST   /api/session/upsert   (called by the extension's background worker)
    GET    /api/sessions         (activity feed)
    GET    /api/score            (called by the popup dashboard)
    POST   /api/nudge/decide     (contextual bandit — see nudge.py)
    POST   /api/nudge/feedback
    POST   /api/session/label    (self-reported comprehension, see fit_weights.py)
    GET    /api/me/export        (data portability)
    DELETE /api/me/data          (right to erasure)

Three session categories flow through /api/session/upsert:
    ai_assistant  -> counted for the weekly assisted-minutes total, never scored
    writing       -> scored with the normal formula, own baseline
    assessment    -> STRICT scoring (quizzes/exams/graded assignment portals),
                      own separate baseline — see scoring.py
"""
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator

# Must run before `import db` / `import auth` — both read their respective
# env vars (DATABASE_URL, SUPABASE_JWT_SECRET) at import time.
from _env import load_dotenv

load_dotenv()

import accounts
import anomaly
import cohort
import coins
import conformal
import auth
import db
import dependency_risk
import devices
import events
import mailer
import ml
import nudge
import oauth_google
import otp
import passwords
import bkt
import learning_state
import retrieval
import ratelimit
import settings_store
import rhythm
import scoring
import tokens
import websecurity

logger = logging.getLogger("autonomize")

# `print()` is invisible to every log aggregator, has no severity, and no
# timestamp. Configured here rather than at import so uvicorn's own handler
# isn't fought with; --log-config still overrides it.
logging.basicConfig(
    level=os.environ.get("AUTONOMIZE_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Replaces the deprecated @app.on_event("startup"). The pool has to be
    # opened before the first request and closed on the way out, which the
    # old decorator pair couldn't express in one place.
    db.open_pool()
    applied = db.init_db()
    logger.info("storage backend: %s", db.backend_description())
    if applied:
        logger.info("applied schema migrations: %s", applied)
    logger.info("auth: %s", auth.describe())
    if auth.ALLOW_ANONYMOUS_IDS:
        # This is the pre-device-account behaviour and it is an IDOR:
        # any caller can name any user_id. Kept only as an upgrade path.
        logger.warning(
            "AUTONOMIZE_ALLOW_ANONYMOUS_IDS is set — telemetry endpoints will trust a "
            "client-supplied user_id. Any caller can read or delete any user's data. "
            "Do not run this on a public URL."
        )
    model_info = ml.describe()
    if model_info["available"]:
        logger.info(
            "next-horizon predictor: %s trained on %s rows from %s users (test MAE %.2f)%s",
            model_info["kind"], model_info.get("rows"),
            model_info.get("users"), model_info.get("test_mae") or 0.0,
            "  [SYNTHETIC TRAINING DATA]" if model_info.get("synthetic") else "",
        )
    else:
        # Says *why*, not just "no": a deployment with a stale or corrupt
        # model file otherwise looks identical to one that never trained,
        # and those need different fixes.
        logger.info("next-horizon predictor: none (%s) — falling back to the linear "
                    "forecast (train one with `python3 train_model.py`)",
                    model_info.get("reason") or "unavailable")
    logger.info("rate limit: %s", ratelimit.describe())
    logger.info("passwords: %s", passwords.describe())
    logger.info("tokens: access %ss, refresh %sd rotating with reuse detection",
                tokens.ACCESS_TTL_SECONDS, tokens.REFRESH_TTL_SECONDS // 86400)
    logger.info("web security: %s", websecurity.describe())
    logger.info("google oauth: %s", oauth_google.describe())
    logger.info("mail: %s", mailer.describe())
    # Printed at startup because the multi-worker caveat is silent
    # otherwise: real-time delivery would simply stop working for some
    # users with nothing in the logs to explain it.
    logger.info("real-time: SSE, %s", events.describe())
    if not mailer.ENABLED:
        # Every flow that depends on mail — email verification, OTP login,
        # password reset — is non-functional for a real user in this mode.
        # It is a supported way to develop and an unsupported way to run.
        logger.warning(
            "NO MAIL TRANSPORT CONFIGURED. Email verification, OTP sign-in and "
            "password reset will not deliver anything to real users. Set "
            "AUTONOMIZE_SMTP_HOST before opening signup."
        )
    if accounts.EPHEMERAL_SECRET:
        logger.warning(
            "AUTONOMIZE_AUTH_SECRET is unset — a random key was generated for this "
            "process. Every session, refresh token and OTP dies on restart, and two "
            "replicas will reject each other's tokens. Set it in production."
        )
    logger.info("cors allowed origins: %s (credentials: %s)",
                ALLOWED_ORIGINS, CORS_CREDENTIALS)
    if ALLOWED_ORIGINS == ["*"]:
        # Bearer tokens aren't attached automatically by browsers the way
        # cookies are, so a wildcard is not itself an exploit while the
        # only client is the extension. It becomes one the moment cookie
        # auth is switched on, which is why websecurity.validate_cors
        # refuses that combination outright rather than warning again.
        logger.warning(
            "CORS is open to all origins — set AUTONOMIZE_ALLOWED_ORIGINS for a "
            "public deployment. Cookie-based dashboard login requires it."
        )
    try:
        yield
    finally:
        db.close_pool()


API_VERSION = "1"

app = FastAPI(title="Autonomize API", version="0.6.0", lifespan=lifespan)


@app.middleware("http")
async def stamp_api_version(request: Request, call_next):
    """Every response carries the API contract version.

    There is no `/v1/` path prefix, and adding one now would break every
    installed extension — which is precisely the problem worth naming. The
    client is a Chrome extension that updates on Google's schedule, not
    ours, so old and new clients coexist for days after any deploy.

    Today the payload only ever grows: new columns have defaults and old
    clients simply omit them, which is why nothing has broken yet. That
    holds for additions and fails for any rename or change of meaning. A
    header is the cheap half of the fix — it lets a client detect a
    contract it does not understand and stop rather than misinterpret. The
    expensive half, a versioned prefix with parallel handlers, is only
    worth paying for once a breaking change is actually needed.
    """
    response = await call_next(request)
    response.headers["X-Autonomize-API-Version"] = API_VERSION
    return response

# Defaults to "*" because the only client in the default setup is the
# user's own extension, whose origin is chrome-extension://<id> — an id
# that isn't known until the extension is installed, so it can't be
# pre-registered. Set AUTONOMIZE_ALLOWED_ORIGINS (comma-separated) for a
# public deployment.
ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.environ.get("AUTONOMIZE_ALLOWED_ORIGINS", "*").split(",")
    if origin.strip()
] or ["*"]

# Credentials are only on when an explicit origin list exists. The
# wildcard-plus-credentials combination is refused rather than downgraded
# — see websecurity.validate_cors for why a silent downgrade is the
# dangerous option here.
CORS_CREDENTIALS = ALLOWED_ORIGINS != ["*"]
websecurity.validate_cors(ALLOWED_ORIGINS, CORS_CREDENTIALS)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=CORS_CREDENTIALS,
    allow_methods=["*"],
    # Explicit rather than "*": with credentials enabled a wildcard header
    # list is rejected by browsers anyway, and naming them documents the
    # contract. X-CSRF-Token is what the dashboard echoes back.
    allow_headers=["Authorization", "Content-Type", "X-CSRF-Token",
                   "X-Autonomize-Device-Id"],
    expose_headers=["X-Autonomize-API-Version"],
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    for header, value in websecurity.SECURITY_HEADERS.items():
        response.headers.setdefault(header, value)
    return response

# Endpoints that create or mutate rows. Reads are cheap and idempotent;
# it's the writes that can fill someone's database.
RATE_LIMITED_PATHS = (
    "/api/session/upsert", "/api/session/label", "/api/nudge/decide",
    # Auth endpoints are rate limited even when the general limiter is
    # off — see AUTH_ALWAYS_LIMITED below. Listed here too so an operator
    # who turns the limiter on gets the stricter combined behaviour.
    "/api/auth/login", "/api/auth/register",
    # NOT /api/auth/device — it has its own always-on limiter (see
    # DEVICE_LIMITED). Listing it here too would double-count a single
    # registration against the general write budget.
)

# Credential-guessing doesn't care whether you configured a rate limit, so
# these two get a hard ceiling regardless. Per-account lockout (see
# accounts.py) stops an attack on one account; this stops one client
# spraying one password across many accounts, which lockout cannot see.
#
# Every endpoint here either accepts a guessable secret (a password, a
# six-digit code, a link code) or mints one and mails it. Per-account
# controls — lockout, OTP attempt caps — stop an attack on ONE account;
# this is the control that stops one client spraying across many, which
# per-account counters cannot see because each account sees only one
# attempt.
#
AUTH_ALWAYS_LIMITED = (
    "/api/auth/login", "/api/auth/register",
    "/api/auth/otp/request", "/api/auth/otp/verify",
    "/api/auth/password/forgot", "/api/auth/password/reset",
    "/api/auth/password/change", "/api/auth/password/set",
    "/api/auth/email/send-verification", "/api/auth/email/verify",
    "/api/devices/link/complete",
    # Refresh is here too. It is not a guessing target — the token is 256
    # random bits — but an unlimited refresh endpoint is an unauthenticated
    # way to make the server do JWT work, and every reuse-detection event
    # writes to the audit log.
    "/api/auth/refresh",
)
AUTH_MAX_ATTEMPTS = int(os.environ.get("AUTONOMIZE_AUTH_RATE_LIMIT", "20"))
AUTH_WINDOW_SECONDS = 300.0

# Device registration is always limited too, but on its own counter and a
# looser ceiling, because it defends a different thing. /api/auth/login is
# guarding a secret, so 20 attempts in five minutes is already generous.
# /api/auth/device guards no secret — the only abuse is flooding the users
# table — and it is called legitimately once per browser profile, including
# by every tab of a shared machine and by CI. Sharing the credential
# counter would have made a lab of students look like an attack.
DEVICE_LIMITED = ("/api/auth/device",)
DEVICE_MAX_ATTEMPTS = int(os.environ.get("AUTONOMIZE_DEVICE_RATE_LIMIT", "60"))

# The link claim gets its own counter for the same reason device
# registration does: its legitimate call volume looks nothing like the
# endpoints beside it. This one is POLLED — the extension asks every few
# seconds while the user is reading a code off the popup and typing it
# into the dashboard — so a credential-guessing ceiling of 20 per five
# minutes would cut off a perfectly normal link partway through.
#
# Sharing the auth counter would also have been actively harmful: a user
# who linked, polled, and then went to sign in would find the login
# endpoint's budget already spent by their own extension.
#
# The looser ceiling is safe because guessing is not the threat here. The
# claim secret is 256 bits from `secrets.token_urlsafe`; no rate limit is
# what stops it being guessed. This counter exists so an unauthenticated
# endpoint cannot be used to make the server do unbounded index lookups.
CLAIM_LIMITED = ("/api/devices/link/claim",)
CLAIM_MAX_ATTEMPTS = int(os.environ.get("AUTONOMIZE_CLAIM_RATE_LIMIT", "400"))

_auth_attempts: dict = {}
_device_attempts: dict = {}
_claim_attempts: dict = {}


def _bucket_ok(store: dict, client: str, ceiling: int) -> bool:
    now = time.monotonic()
    cutoff = now - AUTH_WINDOW_SECONDS
    hits = [t for t in store.get(client, []) if t > cutoff]
    if len(hits) >= ceiling:
        store[client] = hits
        return False
    hits.append(now)
    store[client] = hits
    if len(store) > 10_000:
        for key in [k for k, v in store.items() if not v or v[-1] < cutoff]:
            store.pop(key, None)
    return True


def _auth_rate_ok(client: str) -> bool:
    return _bucket_ok(_auth_attempts, client, AUTH_MAX_ATTEMPTS)


def _device_rate_ok(client: str) -> bool:
    return _bucket_ok(_device_attempts, client, DEVICE_MAX_ATTEMPTS)


def _claim_rate_ok(client: str) -> bool:
    return _bucket_ok(_claim_attempts, client, CLAIM_MAX_ATTEMPTS)


def reset_rate_limits() -> None:
    """Test hook — clears the auth and device buckets.

    These live in module-level dicts and a whole pytest session shares one
    process, so without this the last tests to run start failing with 429s
    that have nothing to do with what they assert. `ratelimit.reset()`
    covers the generic limiter; these two counters are deliberately
    separate from it (see DEVICE_LIMITED) and so need clearing too.
    """
    _auth_attempts.clear()
    _device_attempts.clear()
    _claim_attempts.clear()


@app.middleware("http")
async def rate_limit_writes(request: Request, call_next):
    client_ip = request.client.host if request.client else "unknown"

    # A CORS preflight is never an attempt at anything.
    #
    # The browser issues OPTIONS on its own, before the request the user
    # actually made, and it carries no credentials — so counting it against
    # an auth bucket both double-charges every cross-origin call and, far
    # worse, breaks the error path: once the bucket is empty the PREFLIGHT
    # gets the 429, and a failed preflight is not a status the page can
    # read. `fetch` rejects with an opaque network error, so a rate-limited
    # user is told "cannot reach the server" and goes looking for a backend
    # outage that isn't happening.
    #
    # Answering preflights normally means the real request still gets its
    # 429 — with the CORS headers attached — and the UI can say so.
    if request.method == "OPTIONS":
        return await call_next(request)

    if request.url.path in AUTH_ALWAYS_LIMITED and not _auth_rate_ok(client_ip):
        logger.warning("auth rate limit exceeded for %s on %s", client_ip, request.url.path)
        return JSONResponse(
            status_code=429,
            content={"detail": "Too many attempts. Try again shortly."},
            headers={"Retry-After": str(int(AUTH_WINDOW_SECONDS))},
        )

    if request.url.path in DEVICE_LIMITED and not _device_rate_ok(client_ip):
        logger.warning("device registration rate limit exceeded for %s", client_ip)
        return JSONResponse(
            status_code=429,
            content={"detail": "Too many device registrations. Try again shortly."},
            headers={"Retry-After": str(int(AUTH_WINDOW_SECONDS))},
        )

    if request.url.path in CLAIM_LIMITED and not _claim_rate_ok(client_ip):
        logger.warning("link claim rate limit exceeded for %s", client_ip)
        return JSONResponse(
            status_code=429,
            content={"detail": "Too many link attempts. Try again shortly."},
            headers={"Retry-After": str(int(AUTH_WINDOW_SECONDS))},
        )

    if ratelimit.ENABLED and request.url.path in RATE_LIMITED_PATHS:
        # Keyed on the client address rather than the body's user_id: the
        # user_id is attacker-controlled when auth is off, so limiting by
        # it would let anyone bypass the limit by rotating the field.
        client = request.client.host if request.client else "unknown"
        if not ratelimit.check(client):
            logger.warning("rate limit exceeded for %s on %s", client, request.url.path)
            return JSONResponse(
                status_code=429,
                content={"detail": "rate limit exceeded"},
                headers={"Retry-After": str(int(ratelimit.WINDOW_SECONDS))},
            )
    return await call_next(request)


class Metrics(BaseModel):
    # Every counter is bounded. Pydantic types these but does not constrain
    # them, and an unbounded counter is not a cosmetic problem: the score
    # feeds an EMA baseline, so one absurd value permanently distorts that
    # user's personal norm with no repair path. The ceilings are set far
    # above any plausible session (1e7 characters is ~40 novels) so they
    # never reject real work — they exist to stop a malformed or hostile
    # client, not to model human limits.
    typed_chars: int = Field(default=0, ge=0, le=10_000_000)
    pasted_chars: int = Field(default=0, ge=0, le=10_000_000)
    backspace_count: int = Field(default=0, ge=0, le=10_000_000)
    revision_count: int = Field(default=0, ge=0, le=1_000_000)
    prompt_count: int = Field(default=0, ge=0, le=1_000_000)
    likely_ai_pastes: int = Field(default=0, ge=0, le=100_000)
    tab_switch_count: int = Field(default=0, ge=0, le=1_000_000)

    # Typing-rhythm histogram — counts of inter-keystroke intervals per
    # bucket, never a series. See rhythm.py and the extension's privacy
    # contract for why the shape matters. Optional so an extension build
    # that predates this still upserts cleanly.
    iki_buckets: Optional[List[int]] = None
    long_pauses: int = Field(default=0, ge=0, le=10_000_000)
    burst_keys: int = Field(default=0, ge=0, le=10_000_000)

    @field_validator("iki_buckets")
    @classmethod
    def _check_buckets(cls, v):
        if v is None:
            return None
        if len(v) != rhythm.IKI_BUCKET_COUNT:
            raise ValueError(f"iki_buckets must have {rhythm.IKI_BUCKET_COUNT} entries")
        if any(c < 0 or c > 10_000_000 for c in v):
            raise ValueError("iki_buckets entries out of range")
        return v


class SessionUpsertRequest(BaseModel):
    user_id: str
    session_id: str
    category: str = Field(pattern="^(ai_assistant|writing|assessment)$")
    domain: Optional[str] = None
    path: Optional[str] = None
    started_at: Optional[int] = None
    active_ms: int = 0
    metrics: Metrics
    # Which client-side detector/adapter produced this row, and whether the
    # signals it needs were actually observable on that surface.
    #
    # "limited" means the browser genuinely does not expose keystroke/paste
    # events for the editor in use (a canvas-rendered or cross-origin
    # editor). Recording that distinction is what lets the dashboard say
    # "limited tracking" instead of showing a zero, which is
    # indistinguishable from "wrote nothing" and is therefore a lie.
    #
    # Both are optional and free-form-but-bounded so a new adapter can ship
    # in the extension without a backend release. Unknown values are stored
    # and ignored rather than rejected.
    detector: Optional[str] = Field(default=None, max_length=40)
    capability: Optional[str] = Field(default=None, pattern="^(full|limited)$")
    is_final: bool = False
    client_ts: Optional[int] = None


SCORED_CATEGORIES = ("writing", "assessment")


def _date_str(ms: Optional[int] = None) -> str:
    ts = (ms or int(time.time() * 1000)) / 1000
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def _parse_buckets(raw):
    """The rhythm histogram round-trips through a JSON text column.

    Returns None for anything unreadable rather than raising: a corrupt
    histogram should cost the student nothing, and rhythm.features()
    already treats None as 'no data'.
    """
    if not raw:
        return None
    if isinstance(raw, list):
        return raw
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, list) else None


@app.get("/api/health")
def health():
    # Actually touches the database. A process that is up but can't reach
    # its storage is not healthy, and reporting "ok" for it is how an
    # outage gets missed by every monitor pointed at this endpoint.
    db_ok = db.ping()
    return {
        "status": "ok" if db_ok else "degraded",
        "database": {"reachable": db_ok, "backend": db.backend_description()},
        "time": int(time.time() * 1000),
    }


@app.get("/api/sessions")
def list_sessions(user_id: Optional[str] = None, limit: int = 20, authorization: Optional[str] = Header(None)):
    """Chronological activity feed across all categories — powers the
    dashboard's "Recent activity" panel. Unlike /api/score (which is tuned
    for the two scored categories), this is a raw, unopinionated log: every
    session the user's browser reported, finalized or not."""
    user_id = auth.resolve_user_id(user_id, authorization)
    # The ceiling is set by the dashboard's activity heatmap, which covers
    # 20 weeks and needs every session in that window to colour it
    # honestly — a truncated response would render older days as "no
    # activity" rather than as "not fetched". Rows here are a handful of
    # small integer columns, so a few hundred of them is a cheap query, not
    # a loophole.
    limit = max(1, min(limit, 500))

    with db.get_conn() as conn:
        # typed_chars / pasted_chars / likely_ai_pastes are returned because
        # a per-day breakdown cannot be reconstructed without them. The
        # activity calendar shows "what you typed and what you pasted, site
        # by site" for a chosen day, and /api/score's composition_trend is
        # already aggregated per day across every site — so it can answer
        # "how much did I paste on the 4th" but never "on which site".
        #
        # These are counts, not content: the same integers the extension
        # already uploads. No text crosses this boundary, which is the
        # property that made the feature affordable at all.
        rows = conn.execute(
            db.q("""SELECT session_id, category, domain, started_at, active_ms,
                           typed_chars, pasted_chars, likely_ai_pastes, score
               FROM sessions WHERE user_id = ?
               ORDER BY updated_at DESC LIMIT ?"""),
            (user_id, limit),
        ).fetchall()

    return {"sessions": [dict(r) for r in rows]}


@app.post("/api/session/upsert")
def upsert_session(payload: SessionUpsertRequest, authorization: Optional[str] = Header(None)):
    # Once auth is on, the JWT's `sub` is the only accepted identity —
    # payload.user_id (whatever the extension's local cache happened to
    # send) is overwritten, not merely validated against, so a request
    # can never write into another student's rows by claiming their id.
    resolved_user_id = auth.resolve_user_id(payload.user_id, authorization)
    payload_dict = payload.model_dump()
    payload_dict["user_id"] = resolved_user_id

    with db.get_conn() as conn:
        before = db.get_session(conn, payload.session_id)
        was_finalized = bool(before["finalized"]) if before else False

        try:
            row = db.upsert_session_row(conn, payload_dict)
        except db.SessionOwnershipError as error:
            # 409, not 403: the caller is authenticated and permitted to
            # upload sessions — this specific id is simply already taken by
            # someone else. 403 would suggest their credential is wrong and
            # send them to re-authenticate, which would not help.
            #
            # The message deliberately does not confirm WHO owns it. That
            # would turn this endpoint into an oracle for enumerating other
            # users' session ids.
            logger.warning("rejected cross-user session write for %s", payload.session_id)
            raise HTTPException(status_code=409, detail=str(error))

        newly_finalized = row["finalized"] and not was_finalized
        if newly_finalized and payload.category in SCORED_CATEGORIES:
            # The baseline is read once, before anything is written, and
            # used for three separate decisions below — the rhythm
            # comparison, the bandit's reward attribution, and the EMA
            # update. All three have to see the state as it stood *before*
            # this session, or the session gets compared to a mean it has
            # already moved.
            baseline = db.get_baseline(conn, resolved_user_id, payload.category)

            rhythm_features = rhythm.features(
                iki_buckets=_parse_buckets(row.get("iki_buckets")),
                long_pauses=row.get("long_pauses"),
                burst_keys=row.get("burst_keys"),
                typed_chars=row.get("typed_chars"),
            )
            regularity = rhythm_features.get("regularity_index")
            deviation = rhythm.rhythm_deviation(regularity, baseline)
            penalty = rhythm.penalty_weight(deviation)

            score = scoring.compute_session_score(row, rhythm_penalty=penalty)
            if score is not None:
                db.set_session_score(conn, payload.session_id, score, regularity=regularity)

                prior_mean = baseline["ema_mean"] if baseline else None
                nudge.settle_pending_outcomes(conn, resolved_user_id, score, prior_mean)

                updated = scoring.update_baseline(
                    baseline, score, _date_str(row["started_at"]), regularity=regularity
                )
                # The calibration window is appended to *after* the score is
                # used, never before — a session must not be part of the
                # reference set it was judged against.
                updated["score_window"] = conformal.dump_window(
                    conformal.push_window((baseline or {}).get("score_window"), score)
                )
                db.save_baseline(conn, resolved_user_id, payload.category, **updated)
            elif regularity is not None:
                # The session wasn't scorable (too short, or no input), but
                # the rhythm it produced is still a real observation of how
                # this person types. Recording it makes the rhythm baseline
                # converge on short sessions too, which is most of them.
                db.set_session_score(conn, payload.session_id, None, regularity=regularity)
                updated = scoring.update_baseline(
                    baseline, baseline["last_score"] if baseline else 0.0,
                    _date_str(row["started_at"]), regularity=regularity
                ) if baseline else None
                if updated is not None:
                    # Only the rhythm half is written back — the score half
                    # is restored from the untouched baseline so an
                    # unscored session cannot move the score EMA.
                    db.save_baseline(
                        conn, resolved_user_id, payload.category,
                        ema_mean=baseline["ema_mean"], ema_var=baseline["ema_var"],
                        streak_days=baseline["streak_days"],
                        last_active_date=baseline["last_active_date"],
                        last_score=baseline["last_score"],
                        n_observations=baseline["n_observations"],
                        rhythm_mean=updated["rhythm_mean"], rhythm_var=updated["rhythm_var"],
                        rhythm_n=updated["rhythm_n"],
                        # Unscored session: the window is untouched, since
                        # there is no score to calibrate against.
                        score_window=baseline.get("score_window"),
                    )

    # Notify this user's open dashboards. AFTER the database work and
    # outside the connection block, deliberately: the durable record is the
    # commit above, and publishing is best-effort notification on top of
    # it. A slow or disconnected listener must never be able to apply
    # backpressure to a write, and a failed publish must never roll one
    # back — it costs a few seconds of freshness, never data.
    #
    # Only fields the privacy model already permits to leave the device
    # travel here, and events.publish allow-lists them again rather than
    # trusting this call site.
    events.publish(resolved_user_id, "activity", {
        "session_id": payload.session_id,
        "category": payload.category,
        "domain": payload.domain,
        "active_ms": payload.active_ms,
        "typed_chars": payload.metrics.typed_chars,
        "pasted_chars": payload.metrics.pasted_chars,
        "backspace_count": payload.metrics.backspace_count,
        "revision_count": payload.metrics.revision_count,
        "prompt_count": payload.metrics.prompt_count,
        "likely_ai_pastes": payload.metrics.likely_ai_pastes,
        "tab_switch_count": payload.metrics.tab_switch_count,
        "capability": payload.capability,
        "detector": payload.detector,
        "is_final": payload.is_final,
    })

    return {"ok": True}


@app.get("/api/score")
def get_score(user_id: Optional[str] = None, authorization: Optional[str] = Header(None)):
    user_id = auth.resolve_user_id(user_id, authorization)

    with db.get_conn() as conn:
        writing_baseline = db.get_baseline(conn, user_id, "writing")
        assessment_baseline = db.get_baseline(conn, user_id, "assessment")

        # The learned predictor works from the raw session stream, not from
        # the daily rollup below — its features include per-session process
        # signals (paste ratio, typing regularity) that a daily average has
        # already destroyed. Bounded to the most recent window: the features
        # only look back seven sessions, so fetching a term of history would
        # be work thrown away.
        history_rows = conn.execute(
            db.q("""SELECT session_id, user_id, category, started_at, active_ms,
                           typed_chars, pasted_chars, backspace_count,
                           revision_count, likely_ai_pastes, regularity, score
                    FROM sessions
                    WHERE user_id = ? AND category = 'writing' AND score IS NOT NULL
                    ORDER BY started_at DESC LIMIT 12"""),
            (user_id,),
        ).fetchall()
        history = [dict(r) for r in reversed(history_rows)]

        coin_summary = coins.load(conn, user_id, int(time.time() * 1000))

        # The learning-verification layer. Behaviour says how the work was
        # produced; retrieval says whether the student can still do it
        # unaided. Neither alone supports a claim about learning.
        now_ms = int(time.time() * 1000)
        retrieval.expire_stale(conn, now_ms)
        retrieval_summary = retrieval.summarise(conn, user_id, now_ms)
        mastery = bkt.estimate_all(retrieval.per_concept(conn, user_id, now_ms))

        prediction = ml.predict(history, is_assessment=False,
                                with_explanation=True)

        # The unsupervised signal, judged on the most recent session against
        # the ones before it. This is the only check in the system that looks
        # at more than one number at a time: it catches a session whose SHAPE
        # is unusual for this student even when the score lands exactly where
        # it always does. Returns a status of 'unavailable' or
        # 'insufficient_data' rather than a number when it cannot say.
        behavioural_anomaly = (
            ml.inference.behavioural_anomaly(history[:-1], history[-1])
            if history else
            {"status": "insufficient_data", "score": None, "unusual": None,
             "n_reference": 0}
        )

        # 14-day daily average score, writing sessions only, finalized+scored.
        trend_rows = conn.execute(
            db.q(f"""SELECT {db.date_expr("started_at")} AS day, AVG(score) AS avg_score
               FROM sessions
               WHERE user_id = ? AND category = 'writing' AND score IS NOT NULL
                 AND started_at >= ?
               GROUP BY day ORDER BY day ASC"""),
            (user_id, int((time.time() - 14 * 86400) * 1000)),
        ).fetchall()
        trend = [{"date": r["day"], "score": round(r["avg_score"], 1)} for r in trend_rows]

        # Daily composition: how much of the work produced each day was
        # actually typed versus arriving by paste. This is the raw material
        # the independence score is computed from, shown directly.
        #
        # Includes 'assessment' as well as 'writing' — unlike `trend` above,
        # which is writing-only because the two categories are scored by
        # different formulas and averaging them would be meaningless. Raw
        # character counts have no such problem (a typed character is a
        # typed character), and excluding assessment would blank out exam
        # days entirely, which are exactly the days worth seeing.
        # 'ai_assistant' sessions are excluded: characters typed there are
        # prompts, not work produced.
        composition_rows = conn.execute(
            db.q(f"""SELECT {db.date_expr("started_at")} AS day,
                      SUM(typed_chars) AS typed,
                      SUM(pasted_chars) AS pasted,
                      SUM(likely_ai_pastes) AS ai_pastes
               FROM sessions
               WHERE user_id = ? AND category IN ('writing', 'assessment')
                 AND started_at >= ?
               GROUP BY day ORDER BY day ASC"""),
            (user_id, int((time.time() - 14 * 86400) * 1000)),
        ).fetchall()
        composition_trend = [
            {
                "date": r["day"],
                "typed_chars": int(r["typed"] or 0),
                "pasted_chars": int(r["pasted"] or 0),
                "ai_linked_pastes": int(r["ai_pastes"] or 0),
            }
            for r in composition_rows
        ]

        cutoff_7d = int((time.time() - 7 * 86400) * 1000)
        indep_ms = conn.execute(
            db.q("""SELECT COALESCE(SUM(active_ms), 0) AS s FROM sessions
               WHERE user_id = ? AND category = 'writing' AND started_at >= ?"""),
            (user_id, cutoff_7d),
        ).fetchone()["s"]
        assisted_ms = conn.execute(
            db.q("""SELECT COALESCE(SUM(active_ms), 0) AS s FROM sessions
               WHERE user_id = ? AND category = 'ai_assistant' AND started_at >= ?"""),
            (user_id, cutoff_7d),
        ).fetchone()["s"]

        most_recent_writing = conn.execute(
            db.q("""SELECT score FROM sessions
               WHERE user_id = ? AND category = 'writing' AND score IS NOT NULL
               ORDER BY updated_at DESC LIMIT 1"""),
            (user_id,),
        ).fetchone()

        recent_assessment_rows = conn.execute(
            db.q("""SELECT session_id, domain, path, started_at, score,
                      typed_chars, pasted_chars, likely_ai_pastes, tab_switch_count
               FROM sessions
               WHERE user_id = ? AND category = 'assessment' AND score IS NOT NULL
               ORDER BY updated_at DESC LIMIT 5"""),
            (user_id,),
        ).fetchall()

    def _delta(current, baseline_row):
        mean = baseline_row["ema_mean"] if baseline_row else None
        if mean is None or current is None:
            return None, mean
        return round(current - mean, 1), round(mean, 1)

    # None, not 50.0, when there is nothing to report.
    #
    # A fabricated neutral score is the worst of the three options. It is
    # indistinguishable from a real measurement of exactly 50, so a brand
    # new account shows a confident mid-range gauge that never moves, and
    # the only honest reading — "not enough data yet" — becomes
    # unreachable. Both clients already handle null (`app.js` renders an
    # em dash, and the signal-readiness panel explains what is still
    # warming up); this line was the reason that path could never fire.
    #
    # It also contradicted the rest of the scoring stack, which refuses to
    # answer rather than guess: the z-score waits for 5 observations,
    # conformal for 19, rhythm for 5. Only this one invented a number.
    writing_current = (
        most_recent_writing["score"]
        if most_recent_writing
        else (writing_baseline["ema_mean"] if writing_baseline else None)
    )
    writing_delta, writing_mean = _delta(writing_current, writing_baseline)

    assessment_current = assessment_baseline["last_score"] if assessment_baseline else None
    assessment_delta, assessment_mean = _delta(assessment_current, assessment_baseline)

    # Per-session risk now combines the absolute threshold with how far the
    # session sits from THIS user's own assessment baseline — see
    # anomaly.py for why the absolute-only version contradicted the
    # project's own "never a population average" premise.
    # The calibration window belongs to the same (user, category) row as
    # the EMA, so it is read once and reused for every session below.
    assessment_window = conformal.load_window(
        (assessment_baseline or {}).get("score_window")
    )

    recent_assessment_sessions = []
    for r in recent_assessment_rows:
        absolute = scoring.risk_level(r["score"])
        deviation = anomaly.calibrated_deviation(r["score"], assessment_baseline,
                                                 assessment_window)
        level, driver = anomaly.combined_risk(absolute, deviation)
        recent_assessment_sessions.append({
            "date": _date_str(r["started_at"]),
            "domain": r["domain"],
            "score": round(r["score"], 1),
            "risk_level": level,
            "risk_driver": driver,               # 'personal' | 'absolute'
            "absolute_risk_level": absolute,
            "personal_z_score": deviation["z_score"],
            # Reported alongside the z-score, not instead of it: the z is
            # the effect size, the p-value is the calibrated decision.
            "conformal_p": (deviation.get("conformal") or {}).get("p_value"),
            "decided_by": deviation.get("decided_by"),
            "typed_chars": r["typed_chars"],
            "pasted_chars": r["pasted_chars"],
            "likely_ai_pastes": r["likely_ai_pastes"],
            "tab_switch_count": r["tab_switch_count"],
        })

    assessment_deviation = anomaly.calibrated_deviation(
        assessment_current, assessment_baseline, assessment_window
    )
    assessment_absolute = (
        scoring.risk_level(assessment_current) if assessment_current is not None else None
    )
    assessment_level, assessment_driver = anomaly.combined_risk(
        assessment_absolute, assessment_deviation
    )

    # ------------------------------------------------------------------
    # Signal transparency
    # ------------------------------------------------------------------
    # Two signals were added to the backend without any way for a student to
    # see them: the typing-rhythm comparison and the conformal calibration.
    # Both change the score, and both spend a warm-up period doing nothing.
    # Surfacing "this is still learning, N of M sessions" is not a nicety —
    # a measurement that silently switches on partway through looks like the
    # instrument is unstable, and the student has no way to tell the
    # difference between "no signal" and "signal says you are fine".
    writing_window = conformal.load_window(
        (writing_baseline or {}).get("score_window")
    )
    rhythm_n = int((writing_baseline or {}).get("rhythm_n") or 0)
    rhythm_mean = (writing_baseline or {}).get("rhythm_mean")

    signals = {
        "rhythm": {
            "observations": rhythm_n,
            "required": rhythm.MIN_OBSERVATIONS_FOR_RHYTHM,
            "ready": rhythm_n >= rhythm.MIN_OBSERVATIONS_FOR_RHYTHM,
            "typical_regularity": round(float(rhythm_mean), 3) if rhythm_mean is not None else None,
        },
        "calibration": {
            "observations": len(writing_window),
            "required": conformal.MIN_CALIBRATION,
            "ready": len(writing_window) >= conformal.MIN_CALIBRATION,
            # The guarantee, stated as the rate it actually bounds rather
            # than as a sigma the distribution does not support.
            "flag_rate": conformal.ALPHA_STRONG,
        },
        # How much of this student's own comparison is still borrowed from a
        # population prior. Answers "why is it saying so little about me yet"
        # with a number instead of an empty panel — see ml/coldstart.py.
        "personalisation": ml.inference.cold_start(
            writing_mean,
            int((writing_baseline or {}).get("n_observations") or 0),
            category="writing",
        ),
        # What the learned model is, and whether its numbers came from real
        # students or simulated ones. Shipped in the response rather than
        # only in the logs so a metric can never be read without its label.
        "model": ml.describe(),
    }

    state = learning_state.classify(
        behaviour_score=writing_current,
        behaviour_trend=(anomaly.forecast(trend) or {}).get("direction"),
        retrieval=retrieval_summary,
        n_sessions=len(history),
        baseline_mean=writing_mean,
    )

    # One estimate from every signal, weighted by how EXPENSIVE each is to
    # fake rather than by how discriminative it looks — see
    # dependency_risk.py. Confidence is reported separately from risk
    # because "several weak signals agreed while retrieval was missing" is
    # a real state that a single number would hide.
    recent = history[-1] if history else {}
    recent_total = (recent.get("typed_chars") or 0) + (recent.get("pasted_chars") or 0)
    risk = dependency_risk.estimate(
        score=writing_current,
        baseline_mean=writing_mean,
        paste_ratio=((recent.get("pasted_chars") or 0) / recent_total)
                    if recent_total else None,
        rhythm_deviation=None if not writing_baseline else rhythm.rhythm_deviation(
            recent.get("regularity"), writing_baseline),
        behavioural_anomaly=behavioural_anomaly,
        retrieval=retrieval_summary,
        tab_switch_rate=recent.get("tab_switch_count"),
        n_sessions=len(history),
    )

    return {
        "signals": signals,

        # NEVER a verdict. Carries `not_proof` on every response, names its
        # contributors so a student can disagree with any of them, and
        # reports confidence separately from level.
        "dependency_risk": risk,

        # The two axes combined. A score of 55 means something different
        # depending on whether the student can still recall the material,
        # and only both axes together tell them apart.
        "learning_state": state,
        "current_score": round(writing_current, 1) if writing_current is not None else None,
        "baseline_mean": writing_mean,
        "delta_vs_baseline": writing_delta if writing_delta is not None else 0.0,
        "trend": trend,
        # Two comparable series for the dashboard's composition chart:
        # what you wrote yourself vs. what was pasted in.
        "composition_trend": composition_trend,
        "independent_minutes_7d": round(indep_ms / 60000, 1),
        "assisted_minutes_7d": round(assisted_ms / 60000, 1),
        "streak_days": writing_baseline["streak_days"] if writing_baseline else 0,

        # Where the writing trend is heading, or null when there's too
        # little history to fit a line worth showing (see anomaly.forecast).
        "forecast": anomaly.forecast(trend),

        # The learned alternative to that straight line. Null when no model
        # has been trained or the history is too short — the client then
        # keeps using `forecast`, which is what the model exists to improve
        # on. Never a silent substitution: `source` says which produced it.
        "prediction": prediction,

        # Autonomize Coins. The rule is printed on the card itself
        # ("+10 for a session with nothing pasted, -1 per 100 characters
        # pasted") and implemented once, server-side, so a second client
        # cannot arrive at a different balance from the same sessions.
        "coins": coin_summary,

        # Objective evidence of independent recall — see retrieval.py for
        # why self-reported "did you understand this" was not enough.
        "retrieval": retrieval_summary,
        # Per-concept mastery, from Bayesian Knowledge Tracing over those
        # checks. Justified only because the checks create the per-concept
        # attempt sequence BKT needs; see bkt.py.
        "mastery": mastery,

        # Was the most recent session put together differently from how this
        # student usually works, regardless of where the score landed?
        "behavioural_anomaly": behavioural_anomaly,
        "behavioural_explanation": ml.isolation.explain(behavioural_anomaly),

        # Strict / exam-integrity fields — only meaningful once the user has
        # at least one finalized assessment session.
        "assessment_score": round(assessment_current, 1) if assessment_current is not None else None,
        "assessment_baseline_mean": assessment_mean,
        "assessment_delta": assessment_delta,
        "assessment_risk_level": assessment_level,
        "assessment_risk_driver": assessment_driver,
        "assessment_deviation": assessment_deviation,
        "assessment_explanation": (
            anomaly.explain(assessment_absolute, assessment_deviation, assessment_current)
            if assessment_current is not None else None
        ),
        "assessment_streak_days": assessment_baseline["streak_days"] if assessment_baseline else 0,
        "recent_assessment_sessions": recent_assessment_sessions,
    }


# ---------------------------------------------------------------------------
# Contextual bandit — nudge timing (see nudge.py and bandit.py)
# ---------------------------------------------------------------------------

class NudgeDecideRequest(BaseModel):
    user_id: Optional[str] = None
    # Optional client hint; defaults to the server's UTC hour. The client
    # knows the student's actual local hour, which is what "late-night
    # work" should mean, so it can override.
    hour_of_day: Optional[int] = Field(default=None, ge=0, le=23)


class NudgeFeedbackRequest(BaseModel):
    user_id: Optional[str] = None
    event_id: str
    outcome: str = Field(pattern="^(accepted|engaged|dismissed|ignored)$")


@app.post("/api/nudge/decide")
def nudge_decide(payload: NudgeDecideRequest, authorization: Optional[str] = Header(None)):
    """Asks the bandit whether — and how — to intervene right now.

    Note that 'none' is a real, returnable arm: a caller must be prepared
    for the answer "do nothing", which is frequently the correct one.
    """
    user_id = auth.resolve_user_id(payload.user_id, authorization)

    with db.get_conn() as conn:
        writing_baseline = db.get_baseline(conn, user_id, "writing")
        cutoff_7d = int((time.time() - 7 * 86400) * 1000)

        indep_ms = conn.execute(
            db.q("""SELECT COALESCE(SUM(active_ms), 0) AS s FROM sessions
               WHERE user_id = ? AND category = 'writing' AND started_at >= ?"""),
            (user_id, cutoff_7d),
        ).fetchone()["s"]
        assisted_ms = conn.execute(
            db.q("""SELECT COALESCE(SUM(active_ms), 0) AS s FROM sessions
               WHERE user_id = ? AND category = 'ai_assistant' AND started_at >= ?"""),
            (user_id, cutoff_7d),
        ).fetchone()["s"]
        most_recent = conn.execute(
            db.q("""SELECT score FROM sessions
               WHERE user_id = ? AND category = 'writing' AND score IS NOT NULL
               ORDER BY updated_at DESC LIMIT 1"""),
            (user_id,),
        ).fetchone()

        current_score = most_recent["score"] if most_recent else None
        baseline_mean = writing_baseline["ema_mean"] if writing_baseline else None
        delta = (
            current_score - baseline_mean
            if current_score is not None and baseline_mean is not None
            else 0.0
        )

        context = nudge.build_context(
            current_score=current_score,
            delta_vs_baseline=delta,
            independent_minutes_7d=indep_ms / 60000,
            assisted_minutes_7d=assisted_ms / 60000,
            streak_days=writing_baseline["streak_days"] if writing_baseline else 0,
            hour_of_day=(
                payload.hour_of_day
                if payload.hour_of_day is not None
                else datetime.now(timezone.utc).hour
            ),
            recent_nudges=nudge.recent_nudge_count(conn, user_id),
        )
        return nudge.decide(conn, user_id, context)


@app.post("/api/nudge/feedback")
def nudge_feedback(payload: NudgeFeedbackRequest, authorization: Optional[str] = Header(None)):
    user_id = auth.resolve_user_id(payload.user_id, authorization)
    with db.get_conn() as conn:
        result = nudge.record_feedback(conn, user_id, payload.event_id, payload.outcome)
    if not result["ok"]:
        status = 404 if result["reason"] in ("unknown_event", "wrong_user") else 409
        raise HTTPException(status, result["reason"])
    return result


# ---------------------------------------------------------------------------
# Comprehension labels — the ground truth the heuristic weights lack
# ---------------------------------------------------------------------------

class SessionLabelRequest(BaseModel):
    user_id: Optional[str] = None
    session_id: str
    # "Could you explain what you just produced, without looking at it?"
    understood: int = Field(ge=1, le=5)
    note_present: bool = False


@app.post("/api/session/label")
def label_session(payload: SessionLabelRequest, authorization: Optional[str] = Header(None)):
    """Records a self-reported comprehension rating for one session.

    This is the labelled outcome data scoring.py's weights were never fitted
    against (they're hand-tuned — see the README). Collecting it is a
    prerequisite for fitting them; backend/fit_weights.py is the offline
    analysis that consumes it.
    """
    user_id = auth.resolve_user_id(payload.user_id, authorization)
    with db.get_conn() as conn:
        session = db.get_session(conn, payload.session_id)
        if session is None or session["user_id"] != user_id:
            raise HTTPException(404, "session not found")
        db.upsert_session_label(
            conn, payload.session_id, user_id, payload.understood, payload.note_present
        )
    return {"ok": True}


# ---------------------------------------------------------------------------
# Data portability and erasure
# ---------------------------------------------------------------------------

@app.get("/api/me/export")
def export_me(user_id: Optional[str] = None, authorization: Optional[str] = Header(None)):
    """Everything stored about this user, in full. Small by construction —
    the whole dataset is aggregate counters, never text."""
    user_id = auth.resolve_user_id(user_id, authorization)
    with db.get_conn() as conn:
        data = db.export_user_data(conn, user_id)
    return {"user_id": user_id, "exported_at": int(time.time() * 1000), "data": data}


class SettingsRequest(BaseModel):
    """A PARTIAL settings update.

    Every field optional on purpose: a settings panel that only renders the
    tracking toggles must be able to save them without round-tripping a
    `backendUrl` it never showed the user. `settings_store.normalise` merges
    over what is stored and drops anything it does not recognise.
    """
    backendUrl: Optional[str] = Field(default=None, max_length=500)
    dashboardUrl: Optional[str] = Field(default=None, max_length=500)
    tracking: Optional[dict] = None
    excludedDomains: Optional[list] = None


class RetrievalStartRequest(BaseModel):
    concept_id: str = Field(min_length=1, max_length=80)
    session_id: Optional[str] = Field(default=None, max_length=200)


class RetrievalAnswer(BaseModel):
    question_id: str = Field(max_length=80)
    choice: int
    latency_ms: Optional[int] = None


class RetrievalSubmitRequest(BaseModel):
    check_id: str = Field(min_length=1, max_length=80)
    answers: list[RetrievalAnswer]


@app.get("/api/retrieval/concepts")
def retrieval_concepts(user_id: Optional[str] = None,
                       authorization: Optional[str] = Header(None)):
    """The concept list a student picks from before a check.

    The concept is DECLARED, never inferred from the document — inferring
    it would mean reading the work, which is the one thing this project
    does not do. See retrieval.py.
    """
    auth.resolve_user_id(user_id, authorization)
    with db.get_conn() as conn:
        return {"concepts": retrieval.list_concepts(conn)}


@app.post("/api/retrieval/start", status_code=201)
def retrieval_start(payload: RetrievalStartRequest, user_id: Optional[str] = None,
                    authorization: Optional[str] = Header(None)):
    """Opens a check and returns its questions WITHOUT the answers."""
    user_id = auth.resolve_user_id(user_id, authorization)
    now = int(time.time() * 1000)
    try:
        with db.get_conn() as conn:
            retrieval.expire_stale(conn, now)
            return retrieval.open_check(conn, user_id, payload.concept_id,
                                        payload.session_id, now)
    except retrieval.RetrievalError as error:
        raise HTTPException(status_code=400, detail=str(error))


@app.post("/api/retrieval/submit")
def retrieval_submit(payload: RetrievalSubmitRequest, user_id: Optional[str] = None,
                     authorization: Optional[str] = Header(None)):
    """Grades a check. Grading is server-side and only server-side —
    a client that graded itself would turn objective evidence back into
    self-report, which is the weakness this layer exists to remove."""
    user_id = auth.resolve_user_id(user_id, authorization)
    try:
        with db.get_conn() as conn:
            return retrieval.submit(
                conn, user_id, payload.check_id,
                [a.model_dump() for a in payload.answers],
                int(time.time() * 1000))
    except retrieval.RetrievalError as error:
        raise HTTPException(status_code=400, detail=str(error))


@app.get("/api/me/settings")
def get_settings(user_id: Optional[str] = None,
                 authorization: Optional[str] = Header(None)):
    """This user's settings, from the server rather than chrome.storage.

    Exists because a dashboard served as an ordinary web page cannot read
    chrome.storage at all — see settings_store.py. Always answers: a user
    who has never opened the settings screen gets the defaults rather than
    a 404 the caller has to special-case.
    """
    user_id = auth.resolve_user_id(user_id, authorization)
    with db.get_conn() as conn:
        settings, updated_at = settings_store.load(conn, user_id)
    return {"settings": settings, "updated_at": updated_at}


@app.put("/api/me/settings")
def put_settings(payload: SettingsRequest, user_id: Optional[str] = None,
                 authorization: Optional[str] = Header(None)):
    """Merges an update and returns the full stored result.

    Returns the merged settings rather than an `{ok: true}` so a client
    never has to guess what the server decided — excluded domains in
    particular come back normalised (`https://Example.com/` becomes
    `example.com`), and a UI that kept its own copy would show the user
    something the extension will not match against.
    """
    user_id = auth.resolve_user_id(user_id, authorization)
    incoming = payload.model_dump(exclude_none=True)
    try:
        with db.get_conn() as conn:
            settings, updated_at = settings_store.save(conn, user_id, incoming)
    except settings_store.SettingsError as error:
        raise HTTPException(status_code=400, detail=str(error))
    return {"settings": settings, "updated_at": updated_at}


class ProfileRequest(BaseModel):
    """What a user may change about their own profile.

    Only the display name. Email is an identity key and changing it is a
    verification flow, not a text field; role is an authorization decision
    and a user who could set their own would be an admin by typing.
    """
    display_name: Optional[str] = Field(default=None, max_length=80)


@app.patch("/api/me/profile")
def patch_profile(payload: ProfileRequest, request: Request,
                  authorization: Optional[str] = Header(None)):
    """Updates the signed-in user's display name.

    Any authenticated identity may do this, including an anonymous device
    account. That is deliberate rather than an oversight: a device row is a
    real user row, a display name grants nothing and is visible only to
    whoever holds that session, and "name this browser" is a reasonable
    thing to want before deciding to register.

    Display name is the ONLY editable field. Email is an identity key, so
    changing it is a verification flow rather than a text input; role is an
    authorization decision, and a user who could set their own would be an
    admin by typing.
    """
    user = current_user(request, authorization)
    name = (payload.display_name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Display name cannot be empty.")
    with db.get_conn() as conn:
        updated = accounts.set_display_name(conn, user["user_id"], name)
    return {"user": accounts.public_user(updated)}


@app.delete("/api/me/data")
def delete_me(user_id: Optional[str] = None, authorization: Optional[str] = Header(None)):
    """Hard-deletes every row belonging to this user, across every table.

    The README has always claimed the data is the user's to destroy; until
    now the only way to act on that was deleting the SQLite file by hand,
    which isn't an option once the backend is Postgres/Supabase.
    """
    user_id = auth.resolve_user_id(user_id, authorization)
    with db.get_conn() as conn:
        deleted = db.delete_user_data(conn, user_id)
    return {"ok": True, "user_id": user_id, "deleted": deleted}


# ---------------------------------------------------------------------------
# Authentication — first-party accounts (see accounts.py)
# ---------------------------------------------------------------------------

class RegisterRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=256)
    display_name: Optional[str] = Field(default=None, max_length=80)


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=256)


class EmailRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)


class OtpVerifyRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    code: str = Field(min_length=1, max_length=12)


class ResetRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    code: str = Field(min_length=1, max_length=12)
    new_password: str = Field(min_length=1, max_length=256)


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=1, max_length=256)


class SetPasswordRequest(BaseModel):
    """For accounts that have never had one — OTP-only or Google-only.

    Separate from ChangePasswordRequest because it cannot ask for the
    current password, and quietly accepting an empty `current_password`
    on the change endpoint would be an authentication bypass for every
    account with a NULL hash.
    """
    code: str = Field(min_length=1, max_length=12)
    new_password: str = Field(min_length=1, max_length=256)


class DeviceRegisterRequest(BaseModel):
    device_id: Optional[str] = Field(default=None, max_length=64)
    label: Optional[str] = Field(default=None, max_length=80)
    platform: Optional[str] = Field(default=None, max_length=40)


class DeviceRenameRequest(BaseModel):
    label: str = Field(min_length=1, max_length=80)


class LinkCompleteRequest(BaseModel):
    code: str = Field(min_length=4, max_length=16)


class LinkClaimRequest(BaseModel):
    # token_urlsafe(32) is 43 characters; the bounds are a parser guard,
    # not the security control. The control is that this value is 256 bits
    # of entropy the server generated and only the extension ever saw.
    claim_secret: str = Field(min_length=20, max_length=128)


class DeleteAccountRequest(BaseModel):
    """Deleting an account requires re-proving who you are.

    `confirm` must be the literal string "DELETE". That is not
    decoration: it is the difference between a mis-click on a phone and
    an intention, and it costs one line.
    """
    confirm: str = Field(min_length=1, max_length=32)
    password: Optional[str] = Field(default=None, max_length=256)


def _client_ip(request: Request) -> Optional[str]:
    # X-Forwarded-For is NOT read here. Behind a proxy it is the real
    # client address; without one it is attacker-controlled, and trusting
    # it means every rate limit and lockout in this service can be evaded
    # by setting a header. If you deploy behind a load balancer, strip and
    # re-set it at the edge and run uvicorn with --proxy-headers, which
    # populates request.client for you.
    return request.client.host if request.client else None


def _bearer(authorization: Optional[str]) -> Optional[str]:
    if not authorization or not authorization.startswith("Bearer "):
        return None
    return authorization[len("Bearer "):]


def _device_id(request: Request) -> Optional[str]:
    """The install's random id, from a header.

    Advisory only — it labels a session so the user can revoke one device
    without touching the others. It is NOT a credential and grants
    nothing: every route derives `user_id` from the verified token, so a
    forged device id at most mislabels a row the caller already owns.
    """
    value = (request.headers.get("X-Autonomize-Device-Id") or "").strip()
    return value[:64] or None


def _refresh_token_from(request: Request, payload) -> tuple:
    """`(token, "cookie"|"body"|None)`.

    Cookie first: if both are present the cookie is the browser's real
    credential, and preferring a body value would let a page pin the
    refresh to a token of its own choosing — session fixation, with the
    CSRF check bypassed because the body path does not require one.
    """
    cookie = request.cookies.get(websecurity.refresh_cookie_name())
    if cookie:
        return cookie, "cookie"
    if payload is not None and payload.refresh_token:
        return payload.refresh_token, "body"
    return None, None


def _with_cookies(session: dict, *, verification: Optional[dict] = None,
                  status_code: int = 200) -> JSONResponse:
    """Serialises a session and attaches the cookie pair.

    Both transports are served at once on purpose: the JSON body carries
    the refresh token for the extension, and the same value goes into an
    HttpOnly cookie for the dashboard. A browser client should use the
    cookie and ignore the body field. It cannot be omitted without
    splitting this into two endpoints, and the body value is no more
    exposed than the access token sitting beside it in the same response.
    """
    body = dict(session)
    if verification is not None:
        body["verification"] = verification
    if session.get("refresh_token"):
        body["csrf_token"] = websecurity.new_csrf_token()

    response = JSONResponse(content=body, status_code=status_code)
    if session.get("refresh_token"):
        websecurity.set_auth_cookies(
            response, refresh_token=session["refresh_token"],
            max_age_seconds=tokens.REFRESH_TTL_SECONDS,
            csrf_token=body["csrf_token"],
        )
    return response


def _try_issue_otp(conn, *, email: str, purpose: str, user_id: Optional[str],
                   ip: Optional[str]) -> dict:
    """Issues a code and reports what happened to it, without ever
    reporting the code itself.

    Cooldown and quota rejections are returned as `{"status": ...}`
    rather than raised, because the caller here is a signup that has
    already succeeded — failing the whole registration because a
    verification mail was rate-limited would be the wrong trade.
    """
    try:
        issued = otp.issue(conn, email=email, purpose=purpose, user_id=user_id,
                           ip_hash=accounts.hash_ip(ip))
    except otp.OtpError as error:
        return {"status": "not_sent", "reason": error.message}
    except mailer.MailError as error:
        logger.error("verification mail failed for purpose=%s: %s", purpose, error)
        return {"status": "failed", "reason": str(error)}
    return {"status": "sent", "delivery": issued["delivery"],
            "expires_at": issued["expires_at"], "ttl_minutes": issued["ttl_minutes"]}


def current_user(request: Request, authorization: Optional[str] = Header(None)) -> dict:
    """Resolves the signed-in account, or 401s. Re-reads the role from the
    database on every request rather than trusting the token's claim, so a
    revoked or downgraded role takes effect immediately."""
    token = _bearer(authorization)
    if not token:
        raise HTTPException(401, "Not signed in.")
    try:
        with db.get_conn() as conn:
            return accounts.verify_session(conn, token)
    except accounts.AuthError as e:
        raise HTTPException(e.status, e.message)


def require_admin(request: Request, authorization: Optional[str] = Header(None)) -> dict:
    user = current_user(request, authorization)
    if user["role"] != "admin":
        # 403, not 404: the caller is authenticated, just not permitted.
        # Also audited — repeated hits here are a meaningful signal.
        with db.get_conn() as conn:
            accounts.audit(conn, "authz.denied", actor_id=user["user_id"],
                           detail="admin_required", ip=_client_ip(request))
        raise HTTPException(403, "This area is for institution accounts.")
    return user


@app.post("/api/auth/device", status_code=201)
def register_device(request: Request):
    """Mints an anonymous identity for a fresh extension install.

    This is what keeps the zero-configuration first run alive now that
    telemetry endpoints require a real credential. The client does not
    choose its own id — it asks for one and is told. See
    accounts.create_device_user for the full reasoning.

    Rate limited with the other credential-issuing routes: without that,
    it is an unauthenticated endpoint that creates a database row.
    """
    ip = _client_ip(request)
    with db.get_conn() as conn:
        created = accounts.create_device_user(
            conn, ip=ip, user_agent=request.headers.get("user-agent")
        )
    return {
        "user": accounts.public_user(created["user"]),
        "access_token": created["session"]["access_token"],
        "expires_at": created["session"]["expires_at"],
    }


@app.post("/api/auth/register", status_code=201)
def register(payload: RegisterRequest, request: Request):
    """Creates a student account.

    The admin role is never self-assignable over HTTP — the first account
    can be promoted with `python3 make_admin.py <email>` on the server.
    Allowing a request body to pick its own role would make the whole
    role system decorative.
    """
    ip = _client_ip(request)
    try:
        with db.get_conn() as conn:
            user = accounts.create_user(
                conn, email=payload.email, password=payload.password,
                display_name=payload.display_name, role=accounts.DEFAULT_ROLE, ip=ip,
            )
            session = accounts.issue_session(
                conn, user, ip=ip, user_agent=request.headers.get("user-agent"),
                with_refresh=True, device_id=_device_id(request),
            )
            # Mailed, not returned. The account exists and is usable
            # immediately; `email_verified` stays false until the code is
            # entered, and any policy that depends on it reads that flag.
            verification = _try_issue_otp(conn, email=user["email"], purpose="verify_email",
                                          user_id=user["user_id"], ip=ip)
    except passwords.PasswordPolicyError as e:
        raise HTTPException(400, str(e))
    except accounts.AuthError as e:
        raise HTTPException(e.status, e.message)
    # status_code passed explicitly: returning a Response object bypasses
    # the decorator's status_code, so `@app.post(..., status_code=201)`
    # silently became a 200 the moment this started setting cookies.
    return _with_cookies(session, verification=verification, status_code=201)


@app.post("/api/auth/login")
def login(payload: LoginRequest, request: Request):
    try:
        with db.get_conn() as conn:
            session = accounts.authenticate(
                conn, email=payload.email, password=payload.password,
                ip=_client_ip(request), user_agent=request.headers.get("user-agent"),
                with_refresh=True, device_id=_device_id(request),
            )
    except accounts.AuthError as e:
        raise HTTPException(e.status, e.message)
    return _with_cookies(session)


# ---------------------------------------------------------------------------
# Token refresh — the rotation endpoint
# ---------------------------------------------------------------------------

class RefreshRequest(BaseModel):
    refresh_token: Optional[str] = Field(default=None, max_length=512)


@app.post("/api/auth/refresh")
def refresh(request: Request, payload: Optional[RefreshRequest] = None):
    """Exchanges a refresh token for a new access token AND a new refresh
    token. See tokens.py for why rotation without reuse detection is
    theatre, and what the detection does when it fires.

    The token is read from the cookie first and the JSON body second, so
    the same endpoint serves the dashboard (cookies, CSRF-protected) and
    the extension (body, no cookie jar to attack).
    """
    raw, source = _refresh_token_from(request, payload)
    if not raw:
        raise HTTPException(401, "Not signed in.")

    if source == "cookie" and not websecurity.csrf_ok(request):
        # A cross-site page can cause the cookie to be sent; it cannot
        # read the CSRF cookie to echo it back.
        raise HTTPException(403, "Missing or invalid CSRF token.")

    ip = _client_ip(request)
    try:
        with db.get_conn() as conn:
            rotated = tokens.rotate(
                conn, raw, ip_hash=accounts.hash_ip(ip),
                user_agent=request.headers.get("user-agent"),
            )
            user = accounts.get_user(conn, rotated["user_id"])
            if user is None:
                raise HTTPException(401, "That account no longer exists.")
            session = accounts.issue_session(
                conn, user, ip=ip, user_agent=request.headers.get("user-agent"),
                ttl_seconds=tokens.ACCESS_TTL_SECONDS,
            )
            session["refresh_token"] = rotated["refresh"]["token"]
            session["refresh_expires_at"] = rotated["refresh"]["expires_at"]
    except tokens.TokenError as e:
        if getattr(e, "reuse_detected", False):
            with db.get_conn() as conn:
                accounts.audit(conn, "token.reuse_detected",
                               detail=f"family_revoked sessions={e.sessions_revoked}", ip=ip)
            logger.warning("refresh token reuse detected from %s — family revoked", ip)
        response = JSONResponse(status_code=e.status, content={"detail": e.message})
        # Clear the cookies on the way out. Leaving a dead refresh cookie
        # in place makes the dashboard retry the same failing exchange on
        # every load instead of showing a login screen.
        websecurity.clear_auth_cookies(response)
        return response
    return _with_cookies(session)


@app.post("/api/auth/logout")
def logout(request: Request, payload: Optional[RefreshRequest] = None,
           authorization: Optional[str] = Header(None)):
    """Ends THIS session — the access token and the refresh family behind it.

    Revoking only the access token would be close to useless now that it
    lives ten minutes: the client would still hold a refresh token and
    could mint a new one immediately. So the family goes too.
    """
    token = _bearer(authorization)
    if token:
        with db.get_conn() as conn:
            accounts.revoke_session(conn, token, ip=_client_ip(request))

    raw, _source = _refresh_token_from(request, payload)
    if raw:
        with db.get_conn() as conn:
            row = conn.execute(
                db.q("SELECT family_id FROM refresh_tokens WHERE token_hash = ?"),
                (tokens.hash_token(raw),),
            ).fetchone()
            if row is not None:
                tokens.revoke_family(conn, row["family_id"], reason="logout")

    # Always 200: logging out an already-invalid session is a success from
    # the client's point of view, and a 401 here just strands a confused UI.
    response = JSONResponse(content={"ok": True})
    websecurity.clear_auth_cookies(response)
    return response


@app.post("/api/auth/logout-everywhere")
def logout_everywhere(request: Request, authorization: Optional[str] = Header(None)):
    """Kills every session for this account — the thing you need when a
    device is lost or a password may have leaked.

    Covers refresh tokens as well as access tokens; see
    accounts.revoke_all_sessions for why omitting them would make the
    button decorative.
    """
    user = current_user(request, authorization)
    with db.get_conn() as conn:
        revoked = accounts.revoke_all_sessions(conn, user["user_id"], ip=_client_ip(request))
    response = JSONResponse(content={"ok": True, "sessions_revoked": revoked})
    websecurity.clear_auth_cookies(response)
    return response


# ---------------------------------------------------------------------------
# Email verification, OTP sign-in, password reset
#
# The endpoints that take a bare email address all return the SAME body
# whether or not an account exists. That is not politeness — a difference
# here is a membership oracle: point it at a list of addresses and it
# tells you which students are enrolled. The generic response is repeated
# rather than factored into a helper so that no future edit can quietly
# make one branch more informative than the other.
# ---------------------------------------------------------------------------

_GENERIC_OTP_REPLY = {
    "ok": True,
    "detail": ("If that address has an account, a code is on its way. "
               "It expires in a few minutes and can only be used once."),
}


@app.post("/api/auth/otp/request")
def otp_request(payload: EmailRequest, request: Request):
    """Sends a sign-in code. Creates the account if the address is new —
    signup and sign-in are one button for OTP, the way most consumer apps
    do it.

    Note what is NOT here: no indication of whether the account already
    existed. `created` in the response would be the same enumeration leak
    the generic message exists to avoid.
    """
    ip = _client_ip(request)
    email = accounts.normalize_email(payload.email)
    if not accounts.EMAIL_RE.match(email):
        raise HTTPException(400, "That doesn't look like a valid email address.")

    try:
        with db.get_conn() as conn:
            user, _created = accounts.create_or_get_otp_user(conn, email=email, ip=ip)
            issued = otp.issue(conn, email=email, purpose="login",
                               user_id=user["user_id"], ip_hash=accounts.hash_ip(ip))
    except otp.OtpError as e:
        # Cooldown IS surfaced. It leaks only that somebody recently asked
        # for a code for this address — which the requester just did — and
        # hiding it would leave a client retrying into a wall with no idea
        # why. Rate-limit feedback is not an enumeration oracle.
        raise HTTPException(e.status, e.message,
                            headers={"Retry-After": str(e.retry_after or 60)})
    except mailer.MailError as e:
        raise HTTPException(502, str(e))
    except accounts.AuthError as e:
        raise HTTPException(e.status, e.message)

    return {**_GENERIC_OTP_REPLY, "delivery": issued["delivery"],
            "expires_at": issued["expires_at"]}


@app.post("/api/auth/otp/verify")
def otp_verify(payload: OtpVerifyRequest, request: Request):
    """Spends a login code and issues a session.

    Succeeding here also marks the address verified: entering a code that
    was mailed to it IS proof of control of the mailbox, and asking for a
    second confirmation of the same fact would be busywork.
    """
    ip = _client_ip(request)
    email = accounts.normalize_email(payload.email)
    try:
        with db.get_conn() as conn:
            otp.verify(conn, email=email, purpose="login", code=payload.code)
            user = accounts.get_user_by_email(conn, email)
            if user is None:
                # The code verified but the account is gone (deleted
                # between request and verify). Same generic failure.
                raise HTTPException(400, "That code is wrong or has expired. Request a new one.")
            user = accounts.mark_email_verified(conn, user["user_id"])
            accounts.audit(conn, "login.succeeded", actor_id=user["user_id"],
                           detail="method=otp", ip=ip)
            session = accounts.issue_session(
                conn, user, ip=ip, user_agent=request.headers.get("user-agent"),
                with_refresh=True, device_id=_device_id(request),
            )
    except otp.OtpError as e:
        raise HTTPException(e.status, e.message)
    return _with_cookies(session)


@app.post("/api/auth/email/send-verification")
def send_verification(request: Request, authorization: Optional[str] = Header(None)):
    user = current_user(request, authorization)
    if user.get("email_verified"):
        return {"ok": True, "detail": "That address is already verified.",
                "already_verified": True}
    with db.get_conn() as conn:
        result = _try_issue_otp(conn, email=user["email"], purpose="verify_email",
                                user_id=user["user_id"], ip=_client_ip(request))
    if result["status"] == "not_sent":
        raise HTTPException(429, result["reason"], headers={"Retry-After": "60"})
    return {"ok": True, **result}


@app.post("/api/auth/email/verify")
def verify_email(payload: OtpVerifyRequest, request: Request,
                 authorization: Optional[str] = Header(None)):
    user = current_user(request, authorization)
    # The code is checked against the SIGNED-IN account's address, not the
    # one in the body. Otherwise a user could verify an address they do
    # not own by pasting in a code mailed to somebody else.
    try:
        with db.get_conn() as conn:
            otp.verify(conn, email=user["email"], purpose="verify_email", code=payload.code)
            updated = accounts.mark_email_verified(conn, user["user_id"])
    except otp.OtpError as e:
        raise HTTPException(e.status, e.message)
    return {"ok": True, "user": accounts.public_user(updated)}


@app.post("/api/auth/password/forgot")
def forgot_password(payload: EmailRequest, request: Request):
    """Starts a reset. ALWAYS returns the same body.

    A 404 for an unknown address, or a different message, turns this into
    a membership check for anyone with a list of email addresses. The
    cooldown is enforced silently for the same reason: a 429 that only
    ever fires for real accounts is the same oracle wearing a hat.
    """
    ip = _client_ip(request)
    email = accounts.normalize_email(payload.email)

    with db.get_conn() as conn:
        user = accounts.get_user_by_email(conn, email)
        if user is not None and user.get("provider") != "device":
            try:
                otp.issue(conn, email=email, purpose="reset",
                          user_id=user["user_id"], ip_hash=accounts.hash_ip(ip))
                accounts.audit(conn, "password.reset_requested",
                               actor_id=user["user_id"], ip=ip)
            except (otp.OtpError, mailer.MailError) as error:
                # Logged, never returned — see the docstring.
                logger.info("reset code not issued: %s", error)
    return _GENERIC_OTP_REPLY


@app.post("/api/auth/password/reset")
def reset_password(payload: ResetRequest, request: Request):
    """Completes a reset and signs every other session out.

    The revocation is the important half. If the account was taken over,
    the attacker is holding live tokens, and a reset that leaves them
    working has changed the lock while the intruder is still inside.
    """
    ip = _client_ip(request)
    email = accounts.normalize_email(payload.email)
    try:
        with db.get_conn() as conn:
            otp.verify(conn, email=email, purpose="reset", code=payload.code)
            user = accounts.get_user_by_email(conn, email)
            if user is None:
                raise HTTPException(400, "That code is wrong or has expired. Request a new one.")
            accounts.set_password(conn, user["user_id"], payload.new_password, ip=ip,
                                  reason="password.reset")
            revoked = accounts.revoke_all_sessions(conn, user["user_id"], ip=ip)
            fresh = accounts.get_user(conn, user["user_id"])
            session = accounts.issue_session(
                conn, fresh, ip=ip, user_agent=request.headers.get("user-agent"),
                with_refresh=True, device_id=_device_id(request),
            )
    except otp.OtpError as e:
        raise HTTPException(e.status, e.message)
    except passwords.PasswordPolicyError as e:
        raise HTTPException(400, str(e))
    except accounts.AuthError as e:
        raise HTTPException(e.status, e.message)
    session["sessions_revoked"] = revoked
    return _with_cookies(session)


@app.post("/api/auth/password/change")
def change_password(payload: ChangePasswordRequest, request: Request,
                    authorization: Optional[str] = Header(None)):
    """Changes a password from inside a live session.

    Requires the current password — see accounts.change_password for why
    that is not redundant with being signed in. Signs out every OTHER
    session and leaves this one alive, so doing the responsible thing
    does not log you out of the tab you did it in.
    """
    user = current_user(request, authorization)
    ip = _client_ip(request)
    try:
        with db.get_conn() as conn:
            accounts.change_password(conn, user["user_id"],
                                     current_password=payload.current_password,
                                     new_password=payload.new_password, ip=ip)
            accounts.revoke_all_sessions(conn, user["user_id"], ip=ip)
            fresh = accounts.get_user(conn, user["user_id"])
            session = accounts.issue_session(
                conn, fresh, ip=ip, user_agent=request.headers.get("user-agent"),
                with_refresh=True, device_id=_device_id(request),
            )
    except passwords.PasswordPolicyError as e:
        raise HTTPException(400, str(e))
    except accounts.AuthError as e:
        raise HTTPException(e.status, e.message)
    return _with_cookies(session)


@app.post("/api/auth/password/set")
def set_password(payload: SetPasswordRequest, request: Request,
                 authorization: Optional[str] = Header(None)):
    """Adds a password to an account that has never had one.

    Gated on a fresh emailed code rather than on the session alone. The
    session may belong to a Google or OTP login that a borrowed laptop is
    still holding; requiring the mailbox makes adding a permanent
    credential need the thing the account is actually anchored to.
    """
    user = current_user(request, authorization)
    if user.get("password_hash"):
        raise HTTPException(409, "This account already has a password. Use change instead.")
    ip = _client_ip(request)
    try:
        with db.get_conn() as conn:
            otp.verify(conn, email=user["email"], purpose="verify_email", code=payload.code)
            accounts.set_password(conn, user["user_id"], payload.new_password, ip=ip,
                                  reason="password.set")
            updated = accounts.get_user(conn, user["user_id"])
    except otp.OtpError as e:
        raise HTTPException(e.status, e.message)
    except passwords.PasswordPolicyError as e:
        raise HTTPException(400, str(e))
    except accounts.AuthError as e:
        raise HTTPException(e.status, e.message)
    return {"ok": True, "user": accounts.public_user(updated)}


@app.get("/api/auth/me")
def me(request: Request, authorization: Optional[str] = Header(None)):
    user = current_user(request, authorization)
    with db.get_conn() as conn:
        identities = oauth_google.list_identities(conn, user["user_id"])
    return {"user": accounts.public_user(user), "identities": identities}


# ---------------------------------------------------------------------------
# Devices and sessions — "where am I signed in", and the ability to say no
# ---------------------------------------------------------------------------

@app.post("/api/devices/register", status_code=201)
def device_register(payload: DeviceRegisterRequest, request: Request,
                    authorization: Optional[str] = Header(None)):
    """Records this install against the signed-in account.

    The device id is generated by the CLIENT and is a random UUID, never
    a hardware fingerprint. devices.py explains why at length; the short
    version is that a fingerprint cannot be revoked, and revocation is
    the entire point of a device list.
    """
    user = current_user(request, authorization)
    with db.get_conn() as conn:
        device = devices.register(
            conn, user_id=user["user_id"],
            device_id=payload.device_id or _device_id(request),
            label=payload.label, platform=payload.platform, client="extension",
            ip_hash=accounts.hash_ip(_client_ip(request)),
        )
    return {"device": devices.public_device(device)}


@app.get("/api/devices")
def device_list(request: Request, authorization: Optional[str] = Header(None)):
    user = current_user(request, authorization)
    with db.get_conn() as conn:
        return {"devices": devices.list_for_user(conn, user["user_id"]),
                "sessions": tokens.list_devices_with_sessions(conn, user["user_id"])}


@app.patch("/api/devices/{device_id}")
def device_rename(device_id: str, payload: DeviceRenameRequest, request: Request,
                  authorization: Optional[str] = Header(None)):
    user = current_user(request, authorization)
    try:
        with db.get_conn() as conn:
            return {"device": devices.rename(conn, device_id=device_id,
                                             user_id=user["user_id"], label=payload.label)}
    except devices.DeviceError as e:
        raise HTTPException(e.status, e.message)


@app.delete("/api/devices/{device_id}")
def device_revoke(device_id: str, request: Request,
                  authorization: Optional[str] = Header(None)):
    """Signs one device out without touching the others.

    404 for a device that is not this account's, and note that the check
    is a scoped UPDATE rather than a read-then-write: `WHERE device_id = ?
    AND user_id = ?` IS the authorization decision, so there is no window
    between checking ownership and acting on it.
    """
    user = current_user(request, authorization)
    with db.get_conn() as conn:
        ok = devices.revoke(conn, device_id=device_id, user_id=user["user_id"])
        if not ok:
            raise HTTPException(404, "No such device on this account.")
        killed = tokens.revoke_for_device(conn, device_id, user["user_id"])
        accounts.audit(conn, "device.revoked", actor_id=user["user_id"],
                       detail=f"sessions={killed}", ip=_client_ip(request))
    return {"ok": True, "sessions_revoked": killed}


# ---------------------------------------------------------------------------
# Linking an extension install to a real account
# ---------------------------------------------------------------------------

@app.post("/api/devices/link/start", status_code=201)
def link_start(request: Request, authorization: Optional[str] = Header(None)):
    """Called by the EXTENSION, holding its anonymous device session.

    Returns a short code for the user to read. The code is not a
    credential on its own — completing the link requires a signed-in
    dashboard session, so the worst a leaked code does is let its finder
    attach the device to their own account, which gives them access to
    their own data.
    """
    user = current_user(request, authorization)
    if user.get("provider") != "device":
        raise HTTPException(
            409, "This session is already a real account — nothing to link.")
    with db.get_conn() as conn:
        started = devices.start_link(conn, device_user_id=user["user_id"],
                                     device_id=_device_id(request))
        accounts.audit(conn, "device.link_started", actor_id=user["user_id"],
                       ip=_client_ip(request))
    return started


@app.post("/api/devices/link/complete")
def link_complete(payload: LinkCompleteRequest, request: Request,
                  authorization: Optional[str] = Header(None)):
    """Called by the DASHBOARD with a real signed-in session.

    `account_user_id` comes from that session, never from the body — the
    user types a code, never a user id or a device id.
    """
    user = current_user(request, authorization)
    if user.get("provider") == "device":
        raise HTTPException(
            409, "Sign in with a real account first, then enter the code.")
    ip = _client_ip(request)
    try:
        with db.get_conn() as conn:
            result = devices.complete_link(conn, code=payload.code,
                                           account_user_id=user["user_id"])
            # The anonymous account is finished: its data now belongs to
            # the real one, and leaving its long-lived device token alive
            # would be a second key to the same history.
            accounts.revoke_all_sessions(conn, result["from_user_id"], ip=ip)
            accounts.soft_delete(conn, result["from_user_id"], ip=ip)
            accounts.audit(conn, "device.linked", actor_id=user["user_id"],
                           detail=f"from={result['from_user_id']}", ip=ip)
    except devices.DeviceError as e:
        raise HTTPException(e.status, e.message)
    except accounts.AuthError as e:
        raise HTTPException(e.status, e.message)
    return {"ok": True, "device_id": result["device_id"],
            "rows_moved": result["rows_moved"]}


@app.post("/api/devices/link/claim")
def link_claim(payload: LinkClaimRequest, request: Request):
    """Called by the EXTENSION to collect the credential linking created.

    Deliberately unauthenticated. By the time this runs, link/complete has
    revoked every session of the anonymous device account and soft-deleted
    it, so the extension has no working token to present — and device
    accounts are issued without a refresh token, so it has nothing to
    exchange either. The 256-bit claim secret from link/start is what
    authenticates this call.

    Before this existed, linking left the extension holding a revoked
    token for a deleted account: every telemetry upload 401'd, the queue
    filled, and nothing reached the dashboard. Linking broke the thing it
    was for.
    """
    ip = _client_ip(request)
    try:
        with db.get_conn() as conn:
            result = devices.claim_link(conn, claim_secret=payload.claim_secret)
            if result["status"] != "linked":
                return result

            user = accounts.get_user(conn, result["user_id"])
            if user is None:
                raise HTTPException(410, "That account no longer exists.")

            # with_refresh=True, unlike the device session this replaces.
            # A real account's access token is short-lived, so without a
            # refresh token the extension would go dead again in ten
            # minutes — a slower version of the same bug.
            session = accounts.issue_session(
                conn, user, ip=ip,
                user_agent=request.headers.get("user-agent"),
                with_refresh=True,
                device_id=result["device_id"] or _device_id(request),
            )
            accounts.audit(conn, "device.link_claimed", actor_id=user["user_id"],
                           detail=f"device={result['device_id']}", ip=ip)
    except devices.DeviceError as e:
        raise HTTPException(e.status, e.message)
    except accounts.AuthError as e:
        raise HTTPException(e.status, e.message)

    return {
        "status": "linked",
        "user": accounts.public_user(user),
        "access_token": session["access_token"],
        "refresh_token": session.get("refresh_token"),
        "expires_at": session["expires_at"],
        "device_id": result["device_id"],
    }


# ---------------------------------------------------------------------------
# Google OAuth
# ---------------------------------------------------------------------------

@app.get("/api/auth/google/start")
def google_start(request: Request, redirect_to: Optional[str] = None):
    """Returns the Google URL to send the browser to.

    Returns it rather than 302-ing so the caller can decide — the
    dashboard opens it in the same tab, the extension popup opens it in a
    new one, and a redirect would force one of those to fight the browser.
    """
    try:
        with db.get_conn() as conn:
            return oauth_google.begin(conn, redirect_to=redirect_to)
    except oauth_google.OAuthError as e:
        raise HTTPException(e.status, e.message)


@app.get("/api/auth/google/callback")
def google_callback(request: Request, code: Optional[str] = None,
                    state: Optional[str] = None, error: Optional[str] = None):
    """Completes the flow: verifies state/nonce/PKCE, then signs in.

    Account resolution, in order, and each step's reason:

      1. a stored identity for this Google `sub`  — the stable key. An
         email can be renamed or reassigned; `sub` cannot.
      2. an existing local account with the same email, but ONLY if
         Google says `email_verified` — linking on an unverified address
         is a one-step takeover of any account whose email is known.
      3. otherwise a new account, already verified, no password.
    """
    if error:
        raise HTTPException(400, "Google sign-in was cancelled.")
    ip = _client_ip(request)
    try:
        with db.get_conn() as conn:
            identity = oauth_google.complete(conn, code=code or "", state=state or "")

            existing = oauth_google.find_identity(conn, provider="google",
                                                  subject=identity["subject"])
            if existing:
                user = accounts.get_user(conn, existing["user_id"])
                if user is None:
                    raise HTTPException(401, "That account no longer exists.")
            else:
                user = None
                if identity["email_verified"]:
                    user = accounts.get_user_by_email(conn, identity["email"])
                if user is None:
                    user, _created = accounts.create_or_get_otp_user(
                        conn, email=identity["email"], ip=ip)
                    conn.execute(db.q("UPDATE users SET provider = ? WHERE user_id = ?"),
                                 ("google", user["user_id"]))
                oauth_google.link_identity(conn, user_id=user["user_id"], provider="google",
                                           subject=identity["subject"],
                                           email=identity["email"])
                if identity["email_verified"]:
                    accounts.mark_email_verified(conn, user["user_id"])
                user = accounts.get_user(conn, user["user_id"])

            accounts.audit(conn, "login.succeeded", actor_id=user["user_id"],
                           detail="method=google", ip=ip)
            session = accounts.issue_session(
                conn, user, ip=ip, user_agent=request.headers.get("user-agent"),
                with_refresh=True, device_id=_device_id(request),
            )
    except oauth_google.OAuthError as e:
        raise HTTPException(e.status, e.message)
    except accounts.AuthError as e:
        raise HTTPException(e.status, e.message)

    target = oauth_google.safe_redirect(identity.get("redirect_to"))
    response = _with_cookies(session)
    if target:
        # 303, and the token is NOT in the URL. A fragment or query
        # parameter carrying a credential ends up in browser history, in
        # the Referer of the next request, and in every proxy log on the
        # way. The cookie set above is how the dashboard picks it up.
        response = RedirectResponse(target, status_code=303)
        websecurity.set_auth_cookies(
            response, refresh_token=session["refresh_token"],
            max_age_seconds=tokens.REFRESH_TTL_SECONDS,
        )
    return response


@app.delete("/api/auth/google/link")
def google_unlink(request: Request, authorization: Optional[str] = Header(None)):
    user = current_user(request, authorization)
    try:
        with db.get_conn() as conn:
            removed = oauth_google.unlink(conn, user_id=user["user_id"], provider="google",
                                          has_password=bool(user.get("password_hash")))
    except oauth_google.OAuthError as e:
        raise HTTPException(e.status, e.message)
    return {"ok": True, "unlinked": removed}


# ---------------------------------------------------------------------------
# Account deletion
# ---------------------------------------------------------------------------

@app.delete("/api/me/account")
def delete_account(payload: DeleteAccountRequest, request: Request,
                   authorization: Optional[str] = Header(None)):
    """Deletes the telemetry and tombstones the account.

    Requires the password when the account has one. Being signed in is
    not enough for an irreversible operation — a borrowed laptop should
    not be able to erase somebody's year of work.

    The account row is tombstoned rather than DELETEd, and the email is
    released so the person can sign up again with the same address. See
    accounts.soft_delete.
    """
    user = current_user(request, authorization)
    if payload.confirm != "DELETE":
        raise HTTPException(400, 'Type DELETE to confirm.')

    if user.get("password_hash"):
        if not payload.password or not passwords.verify(payload.password, user["password_hash"]):
            raise HTTPException(401, "Your password is incorrect.")

    ip = _client_ip(request)
    with db.get_conn() as conn:
        deleted_rows = db.delete_user_data(conn, user["user_id"])
        conn.execute(db.q("DELETE FROM devices WHERE user_id = ?"), (user["user_id"],))
        conn.execute(db.q("DELETE FROM refresh_tokens WHERE user_id = ?"), (user["user_id"],))
        conn.execute(db.q("DELETE FROM otp_codes WHERE user_id = ?"), (user["user_id"],))
        result = accounts.soft_delete(conn, user["user_id"], ip=ip)

    response = JSONResponse(content={"ok": True, "deleted": deleted_rows, **result})
    websecurity.clear_auth_cookies(response)
    return response


# ---------------------------------------------------------------------------
# Real-time activity stream (see events.py)
# ---------------------------------------------------------------------------

@app.post("/api/events/ticket", status_code=201)
def events_ticket(request: Request, authorization: Optional[str] = Header(None)):
    """Exchanges a session for a short-lived stream ticket.

    This indirection exists because `EventSource` cannot send an
    Authorization header, so the credential has to travel in the URL — and
    a session token must never go there. URLs land in access logs, proxy
    logs and Referer headers, and a leaked session token is a full account
    compromise. A ticket expires in a minute, is read-only, and can do
    nothing but open a stream for the user it names.
    """
    user = current_user(request, authorization)
    return {
        "ticket": events.issue_ticket(user["user_id"]),
        "expires_in": events.TICKET_TTL_SECONDS,
        "heartbeat_seconds": events.HEARTBEAT_SECONDS,
    }


@app.get("/api/events")
async def events_stream(request: Request, ticket: Optional[str] = None,
                        last_event_id: Optional[int] = None):
    """Server-sent activity events for ONE account.

    The stream is keyed by the user_id inside the verified ticket, never by
    anything else the caller supplies — there is no user_id parameter to
    tamper with, so one account receiving another's events is not a check
    that could be forgotten but a thing the code has no way to express.
    """
    user_id = events.verify_ticket(ticket or "")
    if not user_id:
        raise HTTPException(401, "Invalid or expired stream ticket.")

    # The browser resends the last id it saw on an automatic reconnect.
    header_id = request.headers.get("last-event-id")
    if last_event_id is None and header_id:
        try:
            last_event_id = int(header_id)
        except ValueError:
            last_event_id = None

    return StreamingResponse(
        events.stream(user_id, last_event_id),
        media_type="text/event-stream",
        headers={
            # Buffering is what breaks SSE behind a reverse proxy: frames
            # sit in the proxy until enough bytes accumulate, which for a
            # low-traffic stream can be minutes. The nginx-specific header
            # is included because that is the common deployment.
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.get("/api/auth/config")
def auth_config():
    """What the login panel should offer. Lets the UI show only the methods
    that are actually configured, instead of a Google button that 500s."""
    return {
        "password": True,
        "google": oauth_google.ENABLED,
        # OTP needs mail. Advertising it without a transport gives the user
        # a button that silently does nothing, which is worse than not
        # offering it — so the flag follows the transport, not a wish.
        "otp": mailer.ENABLED,
        "email_verification": mailer.ENABLED,
        "password_reset": mailer.ENABLED,
        "mail_mode": mailer.MODE,
        "supabase_configured": auth.AUTH_ENABLED,
        "access_ttl_seconds": tokens.ACCESS_TTL_SECONDS,
        "secure_cookies": websecurity.SECURE_COOKIES,
        "csrf_header": websecurity.CSRF_HEADER,
        # Surfaced so a deployment can't silently run on a per-process key
        # that invalidates every session on restart.
        "ephemeral_secret": accounts.EPHEMERAL_SECRET,
    }


# ---------------------------------------------------------------------------
# Institution view — aggregate only (see cohort.py)
# ---------------------------------------------------------------------------

@app.get("/api/admin/cohort")
def admin_cohort(request: Request, authorization: Optional[str] = Header(None)):
    """Cohort-level statistics for an institution account.

    Note what this route does NOT accept: no user_id, no class filter, no
    date range, no score band. That's not an oversight — a filterable
    "aggregate" view can be narrowed until the aggregate is one student,
    which would defeat the minimum-cohort rule entirely. See cohort.py.
    """
    admin = require_admin(request, authorization)
    with db.get_conn() as conn:
        # Reading cohort statistics is itself worth recording: it's the one
        # place in this system where one person looks at numbers derived
        # from other people.
        accounts.audit(conn, "cohort.viewed", actor_id=admin["user_id"], ip=_client_ip(request))
        return cohort.summary(conn)
