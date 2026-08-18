"""Versioned schema migrations for both backends.

Why this exists: the original `init_db()` was a list of
`CREATE TABLE IF NOT EXISTS` statements. That is fine exactly once — it
cannot express "add a column", "backfill a value", or "add an index to a
table that already exists", so any schema change after the first release
would either silently not apply to existing installs or require the user
to delete their database. Every schema change now goes through here.

Design notes:

  - Migration 1 is the original schema, written idempotently, so an
    install created before this module existed picks up cleanly: the
    CREATE statements no-op, version 1 is recorded, and migrations 2+ run
    normally.
  - Each migration carries separate SQLite and Postgres statement lists.
    They are usually identical; where they differ it is for a real reason
    (BIGINT vs INTEGER for ms-epoch columns, `ADD COLUMN IF NOT EXISTS`
    which SQLite doesn't support), and the version table means a
    non-idempotent statement is still only ever executed once.
  - Migrations run inside the caller's transaction (see db.init_db), so a
    failure part-way through rolls the whole batch back rather than
    leaving a half-migrated schema recorded as complete.
"""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sqlite: list = field(default_factory=list)
    postgres: list = field(default_factory=list)

    def statements(self, use_postgres: bool) -> list:
        return self.postgres if use_postgres else self.sqlite


_SESSIONS_SQLITE = """CREATE TABLE IF NOT EXISTS sessions (
    session_id       TEXT PRIMARY KEY,
    user_id          TEXT NOT NULL,
    category         TEXT NOT NULL,
    domain           TEXT,
    path             TEXT,
    started_at       INTEGER,
    active_ms        INTEGER NOT NULL DEFAULT 0,
    typed_chars      INTEGER NOT NULL DEFAULT 0,
    pasted_chars     INTEGER NOT NULL DEFAULT 0,
    backspace_count  INTEGER NOT NULL DEFAULT 0,
    revision_count   INTEGER NOT NULL DEFAULT 0,
    prompt_count     INTEGER NOT NULL DEFAULT 0,
    likely_ai_pastes INTEGER NOT NULL DEFAULT 0,
    tab_switch_count INTEGER NOT NULL DEFAULT 0,
    finalized        INTEGER NOT NULL DEFAULT 0,
    score            REAL,
    created_at       INTEGER NOT NULL,
    updated_at       INTEGER NOT NULL
)"""

# BIGINT, not INTEGER: Postgres's INTEGER is 32-bit and overflows on a
# 13-digit millisecond epoch. SQLite's INTEGER is dynamically sized, so the
# same declaration is safe there.
_SESSIONS_POSTGRES = """CREATE TABLE IF NOT EXISTS sessions (
    session_id       TEXT PRIMARY KEY,
    user_id          TEXT NOT NULL,
    category         TEXT NOT NULL,
    domain           TEXT,
    path             TEXT,
    started_at       BIGINT,
    active_ms        BIGINT NOT NULL DEFAULT 0,
    typed_chars      INTEGER NOT NULL DEFAULT 0,
    pasted_chars     INTEGER NOT NULL DEFAULT 0,
    backspace_count  INTEGER NOT NULL DEFAULT 0,
    revision_count   INTEGER NOT NULL DEFAULT 0,
    prompt_count     INTEGER NOT NULL DEFAULT 0,
    likely_ai_pastes INTEGER NOT NULL DEFAULT 0,
    tab_switch_count INTEGER NOT NULL DEFAULT 0,
    finalized        INTEGER NOT NULL DEFAULT 0,
    score            DOUBLE PRECISION,
    created_at       BIGINT NOT NULL,
    updated_at       BIGINT NOT NULL
)"""

_BASELINE_SQLITE = """CREATE TABLE IF NOT EXISTS user_baseline (
    user_id          TEXT NOT NULL,
    category         TEXT NOT NULL,
    ema_mean         REAL,
    ema_var          REAL,
    streak_days      INTEGER NOT NULL DEFAULT 0,
    last_active_date TEXT,
    last_score       REAL,
    updated_at       INTEGER NOT NULL,
    PRIMARY KEY (user_id, category)
)"""

_BASELINE_POSTGRES = """CREATE TABLE IF NOT EXISTS user_baseline (
    user_id          TEXT NOT NULL,
    category         TEXT NOT NULL,
    ema_mean         DOUBLE PRECISION,
    ema_var          DOUBLE PRECISION,
    streak_days      INTEGER NOT NULL DEFAULT 0,
    last_active_date TEXT,
    last_score       DOUBLE PRECISION,
    updated_at       BIGINT NOT NULL,
    PRIMARY KEY (user_id, category)
)"""

_SHARED_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_sessions_user_cat ON sessions(user_id, category)",
]

# The three access patterns /api/score actually issues. Without these,
# every score request is a full scan of the user's sessions — invisible on
# a demo database, very visible on a shared Postgres instance.
_PERF_INDEXES = [
    # trend + 7-day rollups: filtered by (user, category) and ranged on started_at
    "CREATE INDEX IF NOT EXISTS idx_sessions_user_cat_started ON sessions(user_id, category, started_at)",
    # "most recent scored session" + the activity feed
    "CREATE INDEX IF NOT EXISTS idx_sessions_user_updated ON sessions(user_id, updated_at)",
]

_NUDGE_EVENTS_SQLITE = """CREATE TABLE IF NOT EXISTS nudge_events (
    event_id     TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL,
    arm          TEXT NOT NULL,
    context      TEXT NOT NULL,      -- JSON array, the exact feature vector scored
    decided_at   INTEGER NOT NULL,
    reward       REAL,               -- NULL until settled
    settled_at   INTEGER,
    settled_by   TEXT                -- 'feedback' | 'outcome' | 'expired'
)"""

_NUDGE_EVENTS_POSTGRES = """CREATE TABLE IF NOT EXISTS nudge_events (
    event_id     TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL,
    arm          TEXT NOT NULL,
    context      TEXT NOT NULL,
    decided_at   BIGINT NOT NULL,
    reward       DOUBLE PRECISION,
    settled_at   BIGINT,
    settled_by   TEXT
)"""

_BANDIT_STATE_SQLITE = """CREATE TABLE IF NOT EXISTS bandit_state (
    user_id    TEXT NOT NULL,
    arm        TEXT NOT NULL,
    a_matrix   TEXT NOT NULL,        -- JSON d x d
    b_vector   TEXT NOT NULL,        -- JSON d
    n_pulls    INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (user_id, arm)
)"""

_BANDIT_STATE_POSTGRES = """CREATE TABLE IF NOT EXISTS bandit_state (
    user_id    TEXT NOT NULL,
    arm        TEXT NOT NULL,
    a_matrix   TEXT NOT NULL,
    b_vector   TEXT NOT NULL,
    n_pulls    INTEGER NOT NULL DEFAULT 0,
    updated_at BIGINT NOT NULL,
    PRIMARY KEY (user_id, arm)
)"""

_SESSION_LABELS_SQLITE = """CREATE TABLE IF NOT EXISTS session_labels (
    session_id   TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL,
    understood   INTEGER NOT NULL,   -- self-reported 1-5
    note_present INTEGER NOT NULL DEFAULT 0,
    created_at   INTEGER NOT NULL
)"""

_SESSION_LABELS_POSTGRES = """CREATE TABLE IF NOT EXISTS session_labels (
    session_id   TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL,
    understood   INTEGER NOT NULL,
    note_present INTEGER NOT NULL DEFAULT 0,
    created_at   BIGINT NOT NULL
)"""


_USERS_SQLITE = """CREATE TABLE IF NOT EXISTS users (
    user_id        TEXT PRIMARY KEY,
    email          TEXT NOT NULL UNIQUE,
    password_hash  TEXT,               -- NULL for OAuth/OTP-only accounts
    role           TEXT NOT NULL DEFAULT 'student',
    display_name   TEXT,
    provider       TEXT NOT NULL DEFAULT 'password',
    email_verified INTEGER NOT NULL DEFAULT 0,
    failed_logins  INTEGER NOT NULL DEFAULT 0,
    locked_until   INTEGER,
    created_at     INTEGER NOT NULL,
    last_login_at  INTEGER
)"""

_USERS_POSTGRES = """CREATE TABLE IF NOT EXISTS users (
    user_id        TEXT PRIMARY KEY,
    email          TEXT NOT NULL UNIQUE,
    password_hash  TEXT,
    role           TEXT NOT NULL DEFAULT 'student',
    display_name   TEXT,
    provider       TEXT NOT NULL DEFAULT 'password',
    email_verified INTEGER NOT NULL DEFAULT 0,
    failed_logins  INTEGER NOT NULL DEFAULT 0,
    locked_until   BIGINT,
    created_at     BIGINT NOT NULL,
    last_login_at  BIGINT
)"""

# Sessions are stored, not just signed. A pure stateless JWT cannot be
# revoked before it expires, which means "log out everywhere" and
# "this account is compromised, kill its sessions now" are both impossible
# — the two things you most want during an incident.
#
# NOTE the _AUTH_ prefix: `_SESSIONS_SQLITE` is already taken by the
# tracked-activity `sessions` table above. Reusing the name silently
# rebound it and made migration 1 create the wrong table.
_AUTH_SESSIONS_SQLITE = """CREATE TABLE IF NOT EXISTS auth_sessions (
    jti          TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL,
    issued_at    INTEGER NOT NULL,
    expires_at   INTEGER NOT NULL,
    revoked_at   INTEGER,
    user_agent   TEXT,
    ip_hash      TEXT                  -- hashed, never the raw address
)"""

_AUTH_SESSIONS_POSTGRES = """CREATE TABLE IF NOT EXISTS auth_sessions (
    jti          TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL,
    issued_at    BIGINT NOT NULL,
    expires_at   BIGINT NOT NULL,
    revoked_at   BIGINT,
    user_agent   TEXT,
    ip_hash      TEXT
)"""

# Append-only record of security-relevant events. Without one, a breach
# investigation has nothing to reconstruct from.
_AUDIT_SQLITE = """CREATE TABLE IF NOT EXISTS audit_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    at         INTEGER NOT NULL,
    actor_id   TEXT,
    event      TEXT NOT NULL,
    detail     TEXT,
    ip_hash    TEXT
)"""

_AUDIT_POSTGRES = """CREATE TABLE IF NOT EXISTS audit_log (
    id         BIGSERIAL PRIMARY KEY,
    at         BIGINT NOT NULL,
    actor_id   TEXT,
    event      TEXT NOT NULL,
    detail     TEXT,
    ip_hash    TEXT
)"""


MIGRATIONS = [
    Migration(
        version=1,
        name="initial schema",
        sqlite=[_SESSIONS_SQLITE, _BASELINE_SQLITE, *_SHARED_INDEXES],
        postgres=[_SESSIONS_POSTGRES, _BASELINE_POSTGRES, *_SHARED_INDEXES],
    ),
    Migration(
        version=2,
        name="baseline observation count (confidence gating for z-scores)",
        # scoring.update_baseline has always tracked an EMA variance, but
        # a variance over two observations is not a distribution you can
        # responsibly flag an outlier against. anomaly.py needs to know how
        # many scores the baseline is actually built from.
        sqlite=["ALTER TABLE user_baseline ADD COLUMN n_observations INTEGER NOT NULL DEFAULT 0"],
        postgres=["ALTER TABLE user_baseline ADD COLUMN IF NOT EXISTS n_observations INTEGER NOT NULL DEFAULT 0"],
    ),
    Migration(
        version=3,
        name="contextual bandit tables",
        sqlite=[
            _NUDGE_EVENTS_SQLITE,
            _BANDIT_STATE_SQLITE,
            "CREATE INDEX IF NOT EXISTS idx_nudge_user_pending ON nudge_events(user_id, reward)",
        ],
        postgres=[
            _NUDGE_EVENTS_POSTGRES,
            _BANDIT_STATE_POSTGRES,
            "CREATE INDEX IF NOT EXISTS idx_nudge_user_pending ON nudge_events(user_id, reward)",
        ],
    ),
    Migration(
        version=4,
        name="self-reported comprehension labels",
        sqlite=[_SESSION_LABELS_SQLITE],
        postgres=[_SESSION_LABELS_POSTGRES],
    ),
    Migration(
        version=5,
        name="query-pattern indexes",
        sqlite=_PERF_INDEXES,
        postgres=_PERF_INDEXES,
    ),
    Migration(
        version=6,
        name="first-party accounts, revocable sessions, audit log",
        sqlite=[
            _USERS_SQLITE, _AUTH_SESSIONS_SQLITE, _AUDIT_SQLITE,
            "CREATE INDEX IF NOT EXISTS idx_users_email ON users(email)",
            "CREATE INDEX IF NOT EXISTS idx_auth_sessions_user ON auth_sessions(user_id)",
            "CREATE INDEX IF NOT EXISTS idx_audit_actor ON audit_log(actor_id, at)",
        ],
        postgres=[
            _USERS_POSTGRES, _AUTH_SESSIONS_POSTGRES, _AUDIT_POSTGRES,
            "CREATE INDEX IF NOT EXISTS idx_users_email ON users(email)",
            "CREATE INDEX IF NOT EXISTS idx_auth_sessions_user ON auth_sessions(user_id)",
            "CREATE INDEX IF NOT EXISTS idx_audit_actor ON audit_log(actor_id, at)",
        ],
    ),
    Migration(
        version=7,
        name="typing-rhythm histogram and per-user rhythm baseline",
        # The histogram is stored as JSON text rather than eight columns.
        # It is read as a unit, never filtered or aggregated on in SQL, and
        # the bucket edges are a property of the extension build — pinning
        # them into the schema would mean a migration every time they were
        # retuned. `regularity` is stored separately because that IS
        # queried, and recomputing it from the histogram on every read
        # would be wasteful.
        #
        # Rhythm gets its own observation counter rather than reusing
        # n_observations: a session can be scored while contributing no
        # usable rhythm (too few keystrokes, or an older extension), so the
        # two counts genuinely diverge and sharing one would let rhythm
        # z-scores fire on a variance built from far fewer points than the
        # counter claims.
        sqlite=[
            "ALTER TABLE sessions ADD COLUMN iki_buckets TEXT",
            "ALTER TABLE sessions ADD COLUMN long_pauses INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE sessions ADD COLUMN burst_keys INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE sessions ADD COLUMN regularity REAL",
            "ALTER TABLE user_baseline ADD COLUMN rhythm_mean REAL",
            "ALTER TABLE user_baseline ADD COLUMN rhythm_var REAL",
            "ALTER TABLE user_baseline ADD COLUMN rhythm_n INTEGER NOT NULL DEFAULT 0",
        ],
        postgres=[
            "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS iki_buckets TEXT",
            "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS long_pauses INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS burst_keys INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS regularity DOUBLE PRECISION",
            "ALTER TABLE user_baseline ADD COLUMN IF NOT EXISTS rhythm_mean DOUBLE PRECISION",
            "ALTER TABLE user_baseline ADD COLUMN IF NOT EXISTS rhythm_var DOUBLE PRECISION",
            "ALTER TABLE user_baseline ADD COLUMN IF NOT EXISTS rhythm_n INTEGER NOT NULL DEFAULT 0",
        ],
    ),
    Migration(
        version=8,
        name="rolling score window for conformal calibration",
        # A bounded JSON array of recent scores per (user, category).
        #
        # Stored on the baseline row rather than derived from `sessions` on
        # demand for two reasons. It is read on every scoring call, and
        # re-querying and re-sorting the session history each time would put
        # a query on the hot path for data that is append-only and tiny.
        # And the calibration set has to be exactly what the conformal
        # p-value was computed against — deriving it from a table that can
        # be deleted or backfilled would silently change past decisions.
        #
        # Capped at conformal.WINDOW_SIZE entries, so the column stays a few
        # hundred bytes regardless of how long a student has been using it.
        sqlite=["ALTER TABLE user_baseline ADD COLUMN score_window TEXT"],
        postgres=["ALTER TABLE user_baseline ADD COLUMN IF NOT EXISTS score_window TEXT"],
    ),
    Migration(
        version=9,
        name="server-side settings (one source of truth across surfaces)",
        # Settings used to live only in chrome.storage.local, which was
        # correct while the only two readers were the background worker and
        # a dashboard running INSIDE the extension. It stops working the
        # moment a dashboard is served as an ordinary web page: a page on
        # localhost:5173 cannot read chrome.storage at all, so every
        # tracking toggle it drew was either inert or a second copy of the
        # truth that immediately drifted from the extension's.
        #
        # Stored as one JSON blob rather than a column per setting. The
        # shape is owned by the extension (background.js DEFAULT_SETTINGS)
        # and will keep growing; a column per key means a migration every
        # time someone adds a checkbox, and this row is read once per
        # settings screen rather than on any hot path.
        #
        # `updated_at` is what makes two-way sync decidable: the extension
        # and the dashboard can both write, and last-write-wins needs a
        # clock that is not the client's.
        sqlite=["""CREATE TABLE IF NOT EXISTS user_settings (
                     user_id     TEXT PRIMARY KEY,
                     settings    TEXT NOT NULL,
                     updated_at  INTEGER NOT NULL
                   )"""],
        postgres=["""CREATE TABLE IF NOT EXISTS user_settings (
                       user_id     TEXT PRIMARY KEY,
                       settings    TEXT NOT NULL,
                       updated_at  BIGINT NOT NULL
                     )"""],
    ),
    Migration(
        version=10,
        name="retrieval checks — objective evidence of independent recall",
        # THE PROJECT'S LARGEST WEAKNESS, ADDRESSED IN SCHEMA
        #
        # Everything before this migration measures BEHAVIOUR: how work was
        # produced. The claim the product wants to make is about LEARNING:
        # whether the student can now do it themselves. Those are different
        # things, and the gap between them is the first thing a reviewer
        # should attack.
        #
        # `session_labels` was the existing instrument and it asks the
        # student "did you understand this, 1-5". Self-report is evidence,
        # but it is the weakest kind: it correlates with confidence rather
        # than competence, and a student who leaned on AI is precisely the
        # one most likely to feel they understood.
        #
        # A retrieval check is objective. A few minutes after a work
        # session, the student answers two or three questions on the
        # concept they declared they were working on, from a bank the
        # institution controls, with no access to the document. Whether
        # they can retrieve it unaided is a fact, not a feeling.
        #
        # PRIVACY: the questions come from a bank keyed by CONCEPT, and the
        # concept is declared by the student or assigned by faculty. It is
        # never inferred from their document, because inferring it would
        # require reading the document — which is the one thing this
        # project does not do.
        sqlite=[
            """CREATE TABLE IF NOT EXISTS concepts (
                 concept_id  TEXT PRIMARY KEY,
                 name        TEXT NOT NULL,
                 subject     TEXT,
                 created_at  INTEGER NOT NULL
               )""",
            """CREATE TABLE IF NOT EXISTS questions (
                 question_id TEXT PRIMARY KEY,
                 concept_id  TEXT NOT NULL,
                 prompt      TEXT NOT NULL,
                 options     TEXT NOT NULL,
                 answer_index INTEGER NOT NULL,
                 difficulty  REAL NOT NULL DEFAULT 0.5
               )""",
            """CREATE TABLE IF NOT EXISTS retrieval_checks (
                 check_id    TEXT PRIMARY KEY,
                 user_id     TEXT NOT NULL,
                 session_id  TEXT,
                 concept_id  TEXT NOT NULL,
                 asked_at    INTEGER NOT NULL,
                 answered_at INTEGER,
                 question_ids TEXT NOT NULL,
                 n_questions INTEGER NOT NULL,
                 n_correct   INTEGER,
                 median_latency_ms INTEGER,
                 status      TEXT NOT NULL DEFAULT 'open'
               )""",
            "CREATE INDEX IF NOT EXISTS idx_checks_user_time ON retrieval_checks(user_id, asked_at)",
            "CREATE INDEX IF NOT EXISTS idx_checks_user_concept ON retrieval_checks(user_id, concept_id, asked_at)",
            "CREATE INDEX IF NOT EXISTS idx_questions_concept ON questions(concept_id)",
        ],
        postgres=[
            """CREATE TABLE IF NOT EXISTS concepts (
                 concept_id  TEXT PRIMARY KEY,
                 name        TEXT NOT NULL,
                 subject     TEXT,
                 created_at  BIGINT NOT NULL
               )""",
            """CREATE TABLE IF NOT EXISTS questions (
                 question_id TEXT PRIMARY KEY,
                 concept_id  TEXT NOT NULL,
                 prompt      TEXT NOT NULL,
                 options     TEXT NOT NULL,
                 answer_index INTEGER NOT NULL,
                 difficulty  DOUBLE PRECISION NOT NULL DEFAULT 0.5
               )""",
            """CREATE TABLE IF NOT EXISTS retrieval_checks (
                 check_id    TEXT PRIMARY KEY,
                 user_id     TEXT NOT NULL,
                 session_id  TEXT,
                 concept_id  TEXT NOT NULL,
                 asked_at    BIGINT NOT NULL,
                 answered_at BIGINT,
                 question_ids TEXT NOT NULL,
                 n_questions INTEGER NOT NULL,
                 n_correct   INTEGER,
                 median_latency_ms INTEGER,
                 status      TEXT NOT NULL DEFAULT 'open'
               )""",
            "CREATE INDEX IF NOT EXISTS idx_checks_user_time ON retrieval_checks(user_id, asked_at)",
            "CREATE INDEX IF NOT EXISTS idx_checks_user_concept ON retrieval_checks(user_id, concept_id, asked_at)",
            "CREATE INDEX IF NOT EXISTS idx_questions_concept ON questions(concept_id)",
        ],
    ),
    Migration(
        version=11,
        name="four-class session labels — the ground-truth pipeline",
        # `session_labels.understood` is a self-reported 1-5. It is the
        # weakest evidence in the system and it answers the wrong question:
        # a student rates how they FELT, and the quantity a model needs is
        # what actually happened.
        #
        # The four classes below are what a supervised model of this
        # problem would train on, and the middle two are the ones that make
        # it worth collecting at all:
        #
        #   independent          did it themselves
        #   assisted_understood  used AI AND can do it unaided   <- not a problem
        #   transcription        reproduced output they cannot reconstruct
        #   uncertain            the labeller could not tell
        #
        # `uncertain` is a first-class option on purpose. A labelling
        # scheme without one forces a guess and quietly fills the dataset
        # with noise that looks like signal.
        #
        # `labelled_by` distinguishes a student's own label from a tutor's,
        # because the two have different error modes and a model trained on
        # a mixture cannot tell them apart afterwards. `confidence` lets a
        # labeller say how sure they were, so low-confidence rows can be
        # down-weighted or excluded rather than silently trusted.
        #
        # NO ROWS ARE GENERATED. This is an empty table and it stays empty
        # until humans label sessions. `fit_weights.py` already refuses to
        # report a fit below its minimum, and nothing in the codebase
        # writes a synthetic row here.
        sqlite=[
            """CREATE TABLE IF NOT EXISTS session_ground_truth (
                 session_id   TEXT PRIMARY KEY,
                 user_id      TEXT NOT NULL,
                 label        TEXT NOT NULL,
                 confidence   INTEGER NOT NULL DEFAULT 3,
                 labelled_by  TEXT NOT NULL DEFAULT 'student',
                 note_present INTEGER NOT NULL DEFAULT 0,
                 created_at   INTEGER NOT NULL
               )""",
            "CREATE INDEX IF NOT EXISTS idx_truth_user ON session_ground_truth(user_id, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_truth_label ON session_ground_truth(label)",
        ],
        postgres=[
            """CREATE TABLE IF NOT EXISTS session_ground_truth (
                 session_id   TEXT PRIMARY KEY,
                 user_id      TEXT NOT NULL,
                 label        TEXT NOT NULL,
                 confidence   INTEGER NOT NULL DEFAULT 3,
                 labelled_by  TEXT NOT NULL DEFAULT 'student',
                 note_present INTEGER NOT NULL DEFAULT 0,
                 created_at   BIGINT NOT NULL
               )""",
            "CREATE INDEX IF NOT EXISTS idx_truth_user ON session_ground_truth(user_id, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_truth_label ON session_ground_truth(label)",
        ],
    ),
    Migration(
        version=12,
        name="production auth: refresh families, OTP, OAuth identities, devices",
        # ------------------------------------------------------------------
        # WHY EACH TABLE EXISTS
        #
        # refresh_tokens
        #   Access tokens are now short (10 minutes) so a stolen one expires
        #   before it is useful. That only helps if there is something to
        #   exchange for a new one, and a long-lived refresh token that
        #   never changes is just a long-lived access token with extra
        #   steps. So refresh tokens ROTATE: every use issues a replacement
        #   and marks the old one used.
        #
        #   `family_id` is what makes rotation a detector rather than
        #   bookkeeping. All tokens descended from one login share a family.
        #   If a token that has already been used is presented again, either
        #   the client replayed it or an attacker stole it — and we cannot
        #   tell which. The safe response is identical in both cases: revoke
        #   the entire family. The legitimate user re-logs in; the thief
        #   loses the only credential they had. This is the standard OAuth
        #   2.0 BCP refresh-token-rotation-with-reuse-detection design.
        #
        #   `token_hash`, never the token. A database leak must not hand
        #   over working credentials. SHA-256 is correct here and Argon2
        #   would be wrong: these are 256-bit random values, not
        #   user-chosen passwords, so there is no dictionary to slow down
        #   and a 70ms KDF on every API refresh would be a self-inflicted
        #   DoS.
        #
        # otp_codes
        #   Six digits is a 1-in-a-million guess, which is only safe
        #   because of `attempts` and `expires_at`. Without an attempt cap
        #   a million requests walks the space. `purpose` is bound INTO the
        #   row so a code mailed for "verify your email" cannot be replayed
        #   against "reset my password" — that confusion is a full account
        #   takeover, and it is the single most common OTP bug.
        #
        #   `code_hash`, again never the code: an operator reading the
        #   table, or a leaked backup, must not be able to log in as
        #   anyone. `email` is stored alongside `user_id` because signup
        #   OTPs exist before the user row does.
        #
        # oauth_states
        #   CSRF protection for the OAuth callback. Without a state the
        #   attacker completes their own Google login in the victim's
        #   browser and the victim's data silently attaches to the
        #   attacker's account. `code_verifier` is the PKCE secret, kept
        #   server-side and never sent to the browser.
        #
        # identities
        #   Separate from `users` so one account can hold several login
        #   methods (password AND Google) without a provider column that
        #   can only name one. UNIQUE(provider, subject) is what stops two
        #   accounts claiming the same Google user.
        #
        # devices
        #   A random UUID minted by the extension at install. NOT a
        #   hardware fingerprint: no MAC address, no CPU id, no serial
        #   number, nothing derived from them. A fingerprint is stable
        #   across uninstall, follows a person between accounts, and cannot
        #   be revoked by the person it identifies — all three are the
        #   opposite of what a device list is for. A random id is
        #   revocable, and revoking it is the whole feature.
        #
        # device_link_codes
        #   The bridge between an anonymous install and a real account. See
        #   devices.py — short, single-use, attempt-capped, and consumed by
        #   an AUTHENTICATED request, so knowing a code is not by itself
        #   enough to attach a device to someone.
        # ------------------------------------------------------------------
        sqlite=[
            """CREATE TABLE IF NOT EXISTS refresh_tokens (
                 token_id      TEXT PRIMARY KEY,
                 family_id     TEXT NOT NULL,
                 user_id       TEXT NOT NULL,
                 token_hash    TEXT NOT NULL UNIQUE,
                 device_id     TEXT,
                 session_jti   TEXT,
                 issued_at     INTEGER NOT NULL,
                 expires_at    INTEGER NOT NULL,
                 used_at       INTEGER,
                 revoked_at    INTEGER,
                 revoked_reason TEXT,
                 user_agent    TEXT,
                 ip_hash       TEXT
               )""",
            """CREATE TABLE IF NOT EXISTS otp_codes (
                 otp_id      TEXT PRIMARY KEY,
                 user_id     TEXT,
                 email       TEXT NOT NULL,
                 purpose     TEXT NOT NULL,
                 code_hash   TEXT NOT NULL,
                 created_at  INTEGER NOT NULL,
                 expires_at  INTEGER NOT NULL,
                 consumed_at INTEGER,
                 attempts    INTEGER NOT NULL DEFAULT 0,
                 ip_hash     TEXT
               )""",
            """CREATE TABLE IF NOT EXISTS oauth_states (
                 state         TEXT PRIMARY KEY,
                 code_verifier TEXT NOT NULL,
                 nonce         TEXT NOT NULL,
                 redirect_to   TEXT,
                 created_at    INTEGER NOT NULL,
                 expires_at    INTEGER NOT NULL,
                 consumed_at   INTEGER
               )""",
            """CREATE TABLE IF NOT EXISTS identities (
                 identity_id TEXT PRIMARY KEY,
                 user_id     TEXT NOT NULL,
                 provider    TEXT NOT NULL,
                 subject     TEXT NOT NULL,
                 email       TEXT,
                 created_at  INTEGER NOT NULL,
                 UNIQUE (provider, subject)
               )""",
            """CREATE TABLE IF NOT EXISTS devices (
                 device_id    TEXT PRIMARY KEY,
                 user_id      TEXT NOT NULL,
                 label        TEXT,
                 platform     TEXT,
                 client       TEXT,
                 created_at   INTEGER NOT NULL,
                 last_seen_at INTEGER,
                 revoked_at   INTEGER,
                 ip_hash      TEXT
               )""",
            """CREATE TABLE IF NOT EXISTS device_link_codes (
                 code_hash   TEXT PRIMARY KEY,
                 device_id   TEXT NOT NULL,
                 device_user_id TEXT NOT NULL,
                 created_at  INTEGER NOT NULL,
                 expires_at  INTEGER NOT NULL,
                 consumed_at INTEGER,
                 attempts    INTEGER NOT NULL DEFAULT 0
               )""",
            "ALTER TABLE users ADD COLUMN deleted_at INTEGER",
            "ALTER TABLE users ADD COLUMN password_changed_at INTEGER",
            "ALTER TABLE users ADD COLUMN email_verified_at INTEGER",
            "CREATE INDEX IF NOT EXISTS idx_refresh_family ON refresh_tokens(family_id)",
            "CREATE INDEX IF NOT EXISTS idx_refresh_user ON refresh_tokens(user_id, revoked_at)",
            "CREATE INDEX IF NOT EXISTS idx_otp_lookup ON otp_codes(email, purpose, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_identities_user ON identities(user_id)",
            "CREATE INDEX IF NOT EXISTS idx_devices_user ON devices(user_id)",
        ],
        postgres=[
            """CREATE TABLE IF NOT EXISTS refresh_tokens (
                 token_id      TEXT PRIMARY KEY,
                 family_id     TEXT NOT NULL,
                 user_id       TEXT NOT NULL,
                 token_hash    TEXT NOT NULL UNIQUE,
                 device_id     TEXT,
                 session_jti   TEXT,
                 issued_at     BIGINT NOT NULL,
                 expires_at    BIGINT NOT NULL,
                 used_at       BIGINT,
                 revoked_at    BIGINT,
                 revoked_reason TEXT,
                 user_agent    TEXT,
                 ip_hash       TEXT
               )""",
            """CREATE TABLE IF NOT EXISTS otp_codes (
                 otp_id      TEXT PRIMARY KEY,
                 user_id     TEXT,
                 email       TEXT NOT NULL,
                 purpose     TEXT NOT NULL,
                 code_hash   TEXT NOT NULL,
                 created_at  BIGINT NOT NULL,
                 expires_at  BIGINT NOT NULL,
                 consumed_at BIGINT,
                 attempts    INTEGER NOT NULL DEFAULT 0,
                 ip_hash     TEXT
               )""",
            """CREATE TABLE IF NOT EXISTS oauth_states (
                 state         TEXT PRIMARY KEY,
                 code_verifier TEXT NOT NULL,
                 nonce         TEXT NOT NULL,
                 redirect_to   TEXT,
                 created_at    BIGINT NOT NULL,
                 expires_at    BIGINT NOT NULL,
                 consumed_at   BIGINT
               )""",
            """CREATE TABLE IF NOT EXISTS identities (
                 identity_id TEXT PRIMARY KEY,
                 user_id     TEXT NOT NULL,
                 provider    TEXT NOT NULL,
                 subject     TEXT NOT NULL,
                 email       TEXT,
                 created_at  BIGINT NOT NULL,
                 UNIQUE (provider, subject)
               )""",
            """CREATE TABLE IF NOT EXISTS devices (
                 device_id    TEXT PRIMARY KEY,
                 user_id      TEXT NOT NULL,
                 label        TEXT,
                 platform     TEXT,
                 client       TEXT,
                 created_at   BIGINT NOT NULL,
                 last_seen_at BIGINT,
                 revoked_at   BIGINT,
                 ip_hash      TEXT
               )""",
            """CREATE TABLE IF NOT EXISTS device_link_codes (
                 code_hash   TEXT PRIMARY KEY,
                 device_id   TEXT NOT NULL,
                 device_user_id TEXT NOT NULL,
                 created_at  BIGINT NOT NULL,
                 expires_at  BIGINT NOT NULL,
                 consumed_at BIGINT,
                 attempts    INTEGER NOT NULL DEFAULT 0
               )""",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS deleted_at BIGINT",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS password_changed_at BIGINT",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS email_verified_at BIGINT",
            "CREATE INDEX IF NOT EXISTS idx_refresh_family ON refresh_tokens(family_id)",
            "CREATE INDEX IF NOT EXISTS idx_refresh_user ON refresh_tokens(user_id, revoked_at)",
            "CREATE INDEX IF NOT EXISTS idx_otp_lookup ON otp_codes(email, purpose, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_identities_user ON identities(user_id)",
            "CREATE INDEX IF NOT EXISTS idx_devices_user ON devices(user_id)",
        ],
    ),
    Migration(
        version=13,
        name="link claim: hand the extension a credential for the account it was linked to",
        # ------------------------------------------------------------------
        # THE DEFECT THIS CLOSES
        #
        # Linking worked in one direction only. /api/devices/link/complete
        # re-pointed the device's rows at the real account, revoked every
        # session of the anonymous device account, and soft-deleted it —
        # correct, because leaving that long-lived device token alive would
        # be a second key to the same history.
        #
        # But nothing ever told the EXTENSION. It kept the token it already
        # had: a token for an account that no longer exists, whose sessions
        # were just revoked, with no refresh token to recover with (device
        # accounts are issued without one by design). Every subsequent
        # /api/session/upsert returned 401, the upload queue filled, and the
        # dashboard showed nothing — the exact symptom of "I type on a page
        # and it never appears".
        #
        # So linking, the feature whose entire purpose is to make telemetry
        # show up on your account, was the thing that stopped telemetry
        # arriving at all.
        #
        # WHY A SEPARATE SECRET RATHER THAN REUSING THE CODE
        #
        # The claim has to be an UNAUTHENTICATED call: by the time the
        # extension makes it, its only credential is dead. That rules out
        # authenticating the poll with a bearer token, and it rules out
        # authenticating it with the six-character code — six characters
        # from a 32-symbol alphabet is fine for a value that is only ever
        # consumed by an already-signed-in request, and nowhere near enough
        # for one that hands out a session on its own.
        #
        # `claim_secret_hash` is the HMAC of 256 random bits generated at
        # link/start and returned only to the extension that asked. It is
        # never displayed, never typed, and never crosses to the dashboard.
        # Guessing it is not a threat model.
        #
        # `linked_user_id` records which account complete_link attached the
        # device to, so the claim knows whose session to mint. `claimed_at`
        # makes the claim single-use: the second call gets nothing, so a
        # secret read out of storage after the fact is spent.
        # ------------------------------------------------------------------
        sqlite=[
            "ALTER TABLE device_link_codes ADD COLUMN claim_secret_hash TEXT",
            "ALTER TABLE device_link_codes ADD COLUMN linked_user_id TEXT",
            "ALTER TABLE device_link_codes ADD COLUMN claimed_at INTEGER",
            "CREATE INDEX IF NOT EXISTS idx_link_claim "
            "ON device_link_codes(claim_secret_hash)",
        ],
        postgres=[
            "ALTER TABLE device_link_codes ADD COLUMN IF NOT EXISTS claim_secret_hash TEXT",
            "ALTER TABLE device_link_codes ADD COLUMN IF NOT EXISTS linked_user_id TEXT",
            "ALTER TABLE device_link_codes ADD COLUMN IF NOT EXISTS claimed_at BIGINT",
            "CREATE INDEX IF NOT EXISTS idx_link_claim "
            "ON device_link_codes(claim_secret_hash)",
        ],
    ),
]

SCHEMA_VERSION = max(m.version for m in MIGRATIONS)

_MIGRATIONS_TABLE_SQLITE = """CREATE TABLE IF NOT EXISTS schema_migrations (
    version    INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    applied_at INTEGER NOT NULL
)"""

_MIGRATIONS_TABLE_POSTGRES = """CREATE TABLE IF NOT EXISTS schema_migrations (
    version    INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    applied_at BIGINT NOT NULL
)"""


def applied_versions(conn, use_postgres: bool) -> set:
    conn.execute(_MIGRATIONS_TABLE_POSTGRES if use_postgres else _MIGRATIONS_TABLE_SQLITE)
    rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
    return {row["version"] for row in rows}


def apply_migrations(conn, use_postgres: bool, now_ms: int) -> list:
    """Applies every migration not yet recorded. Returns the versions run."""
    already = applied_versions(conn, use_postgres)
    placeholder = "%s" if use_postgres else "?"
    ran = []
    for migration in sorted(MIGRATIONS, key=lambda m: m.version):
        if migration.version in already:
            continue
        for statement in migration.statements(use_postgres):
            conn.execute(statement)
        conn.execute(
            f"INSERT INTO schema_migrations (version, name, applied_at) "
            f"VALUES ({placeholder},{placeholder},{placeholder})",
            (migration.version, migration.name, now_ms),
        )
        ran.append(migration.version)
    return ran
