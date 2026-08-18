r"""Retrieval checks — the learning-verification layer.

THE WEAKNESS THIS EXISTS TO CLOSE
---------------------------------

Everything else in this project measures BEHAVIOUR. Typed versus pasted,
typing rhythm, tab switching, deviation from a personal baseline — all of
it describes *how work was produced*. The claim the product wants to make
is about *learning*: whether the student can now do the thing themselves.

Those are different quantities, and the distance between them is the first
place a reviewer should push. A student who types every character of a
paragraph they do not understand scores well. A student who pastes a
correct derivation they worked out on paper scores badly. Behaviour is a
proxy, and until this module existed it was the only evidence in the system.

The pre-existing instrument was `session_labels`: "did you understand this,
1-5". Self-report is evidence, but it is the weakest kind available —
it tracks confidence rather than competence, and the student who leaned
hardest on AI is precisely the one most likely to feel they understood.
That table is kept (see `fit_weights.py`) and is now the *secondary*
signal.

A retrieval check is objective. Some minutes after a work session, with
the document closed, the student answers two or three questions on the
concept they declared they were working on. Whether they can retrieve it
unaided is a fact.

    behavioural independence  +  independent retrieval  ->  learning signal

WHY THIS IS PRIVACY-PRESERVING, AND WHERE THE CONCEPT COMES FROM
-----------------------------------------------------------------

The obvious design is to read the student's document, extract the topic,
and generate questions from it. That is exactly what this project does
not do, and it is not a limitation to work around — it is the property
that makes the whole thing defensible.

So the concept is *declared*, never inferred:

  - the student picks it when they start (a dropdown, one click), or
  - faculty attaches it to an assignment.

The question bank is authored by the institution and keyed by concept. No
document text, no titles, no URLs, no generated content. The system never
learns what the essay was about — only which concept the student said they
were studying, and whether they could answer questions about it afterwards.

WHAT A RETRIEVAL SCORE DOES AND DOES NOT MEAN
---------------------------------------------

It means: on this occasion, against these questions, this student
retrieved this concept unaided at this rate.

It does NOT mean the student has or has not learned the material. Two or
three multiple-choice questions is a thin instrument — guessing floors the
score well above zero, question difficulty is uncalibrated until enough
responses exist, and a student can know a concept and still misread a
prompt. `MIN_QUESTIONS` and the confidence reported alongside every score
exist to keep that visible rather than buried.

The honest framing, which the dashboard uses verbatim: this is *evidence*
about retrieval, combined with *evidence* about behaviour. Neither alone
is a verdict.
"""
import json
import math
import statistics
import time
import uuid

import db

# Questions per check. Small on purpose: this has to be answerable in under
# a minute or students will not do it, and an unanswered check is worth
# nothing. Two is the floor at which a single lucky guess cannot produce a
# perfect score.
QUESTIONS_PER_CHECK = 3
MIN_QUESTIONS = 2

# A check offered immediately is answered while the material is still on
# screen in the student's memory, which measures short-term recall rather
# than retrieval. Offered days later it measures forgetting. The literature
# on spaced retrieval puts something useful in between; this is the low end
# of that, and it is a judgement rather than a derived constant.
MIN_DELAY_MINUTES = 10

# Beyond this a check is stale: the session it refers to is no longer what
# the student was thinking about, so the answer says little about that
# work.
EXPIRY_HOURS = 48

# Below this many completed checks, a retrieval rate is one or two
# questions' worth of luck. Reported as `warming_up` rather than as a
# number, for the same reason the rhythm and conformal layers withhold.
MIN_CHECKS_FOR_RATE = 3

# Four options, so a blind guess scores 0.25. Subtracted when reporting an
# adjusted rate — a raw 40% "correct" is barely above chance and showing it
# unadjusted would overstate what happened.
GUESS_RATE = 0.25


class RetrievalError(ValueError):
    """Rejected input, with a message safe to show a user."""


# ---------------------------------------------------------------------------
# The question bank
# ---------------------------------------------------------------------------

def list_concepts(conn):
    rows = conn.execute(
        db.q("SELECT concept_id, name, subject FROM concepts ORDER BY subject, name")
    ).fetchall()
    return [dict(r) for r in rows]


def add_concept(conn, concept_id, name, subject=None):
    conn.execute(
        db.q("""INSERT INTO concepts (concept_id, name, subject, created_at)
                VALUES (?, ?, ?, ?)"""),
        (concept_id, name, subject, int(time.time() * 1000)),
    )


def add_question(conn, question_id, concept_id, prompt, options, answer_index,
                 difficulty=0.5):
    if not isinstance(options, (list, tuple)) or len(options) < 2:
        raise RetrievalError("A question needs at least two options.")
    if not 0 <= answer_index < len(options):
        raise RetrievalError("answer_index is outside the options list.")
    conn.execute(
        db.q("""INSERT INTO questions
                  (question_id, concept_id, prompt, options, answer_index, difficulty)
                VALUES (?, ?, ?, ?, ?, ?)"""),
        (question_id, concept_id, prompt,
         json.dumps(list(options), separators=(",", ":")),
         int(answer_index), float(difficulty)),
    )


def _questions_for(conn, concept_id):
    rows = conn.execute(
        db.q("""SELECT question_id, prompt, options, answer_index, difficulty
                FROM questions WHERE concept_id = ?
                ORDER BY question_id"""),
        (concept_id,),
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Offering a check
# ---------------------------------------------------------------------------

def should_offer(conn, user_id, session, now_ms):
    """Is this session due a retrieval check?

    Returns (bool, reason). Deliberately conservative: a check the student
    dismisses teaches nothing and trains them to dismiss the next one, so
    the bar for interrupting is high.
    """
    if not session:
        return False, "no session"
    if session.get("category") not in ("writing", "assessment"):
        return False, "only scored categories are checked"

    age_minutes = (now_ms - (session.get("started_at") or 0)) / 60000.0
    if age_minutes < MIN_DELAY_MINUTES:
        return False, f"too soon — retrieval is measured after {MIN_DELAY_MINUTES} minutes"
    if age_minutes > EXPIRY_HOURS * 60:
        return False, "session is too old for the answer to be about that work"

    existing = conn.execute(
        db.q("SELECT check_id FROM retrieval_checks WHERE session_id = ?"),
        (session.get("session_id"),),
    ).fetchone()
    if existing:
        return False, "already checked"

    return True, "due"


def open_check(conn, user_id, concept_id, session_id, now_ms, rng=None):
    """Draws questions and records an open check.

    The correct answers are NOT returned — `public_questions` strips them.
    A client that received them could score itself, and a student reading
    the network tab could too.
    """
    pool = _questions_for(conn, concept_id)
    if len(pool) < MIN_QUESTIONS:
        raise RetrievalError(
            f"'{concept_id}' has {len(pool)} question(s); at least "
            f"{MIN_QUESTIONS} are needed for a check.")

    import random

    rng = rng or random.Random()
    chosen = rng.sample(pool, min(QUESTIONS_PER_CHECK, len(pool)))

    check_id = str(uuid.uuid4())
    conn.execute(
        db.q("""INSERT INTO retrieval_checks
                  (check_id, user_id, session_id, concept_id, asked_at,
                   question_ids, n_questions, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'open')"""),
        (check_id, user_id, session_id, concept_id, now_ms,
         json.dumps([q["question_id"] for q in chosen], separators=(",", ":")),
         len(chosen)),
    )
    return {"check_id": check_id, "concept_id": concept_id,
            "questions": public_questions(chosen)}


def public_questions(rows):
    """Questions as a client may see them — without the answers."""
    return [
        {"question_id": r["question_id"], "prompt": r["prompt"],
         "options": json.loads(r["options"])}
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Grading
# ---------------------------------------------------------------------------

def submit(conn, user_id, check_id, answers, now_ms):
    """Grades a check. `answers` is [{question_id, choice, latency_ms}].

    Grading happens here and only here. A client that graded itself could
    report whatever it liked, and the whole point of this layer is that it
    is objective evidence rather than another self-report.
    """
    row = conn.execute(
        db.q("""SELECT * FROM retrieval_checks
                WHERE check_id = ? AND user_id = ?"""),
        (check_id, user_id),
    ).fetchone()
    if not row:
        raise RetrievalError("No such check.")
    row = dict(row)
    if row["status"] != "open":
        raise RetrievalError("That check has already been answered.")

    expected = json.loads(row["question_ids"])
    lookup = {}
    for qid in expected:
        q = conn.execute(
            db.q("SELECT question_id, answer_index FROM questions WHERE question_id = ?"),
            (qid,),
        ).fetchone()
        if q:
            lookup[qid] = int(dict(q)["answer_index"])

    n_correct = 0
    latencies = []
    seen = set()
    for answer in answers or []:
        qid = answer.get("question_id")
        # Only questions this check actually asked, and only once each:
        # otherwise a client could submit the same right answer three times.
        if qid not in lookup or qid in seen:
            continue
        seen.add(qid)
        if answer.get("choice") == lookup[qid]:
            n_correct += 1
        latency = answer.get("latency_ms")
        if isinstance(latency, (int, float)) and latency > 0:
            latencies.append(int(latency))

    median_latency = int(statistics.median(latencies)) if latencies else None

    conn.execute(
        db.q("""UPDATE retrieval_checks
                SET answered_at = ?, n_correct = ?, median_latency_ms = ?,
                    status = 'answered'
                WHERE check_id = ?"""),
        (now_ms, n_correct, median_latency, check_id),
    )

    return {
        "check_id": check_id,
        "concept_id": row["concept_id"],
        "n_questions": row["n_questions"],
        "n_correct": n_correct,
        "rate": n_correct / row["n_questions"] if row["n_questions"] else None,
        "median_latency_ms": median_latency,
    }


def expire_stale(conn, now_ms):
    """Marks unanswered checks past the window as skipped.

    A skipped check is data — a student who never answers is telling you
    something — but it must not sit as 'open' forever, or `should_offer`
    would never offer another for that session.
    """
    cutoff = now_ms - EXPIRY_HOURS * 3600_000
    conn.execute(
        db.q("""UPDATE retrieval_checks SET status = 'skipped'
                WHERE status = 'open' AND asked_at < ?"""),
        (cutoff,),
    )


# ---------------------------------------------------------------------------
# The signal
# ---------------------------------------------------------------------------

def summarise(conn, user_id, now_ms, window_days=30):
    """This student's retrieval performance, with its confidence attached.

    Returns a dict whose `status` says how much to trust it, matching the
    vocabulary the rest of the codebase uses.
    """
    since = now_ms - window_days * 86_400_000
    rows = conn.execute(
        db.q("""SELECT concept_id, asked_at, n_questions, n_correct,
                       median_latency_ms, status
                FROM retrieval_checks
                WHERE user_id = ? AND asked_at >= ?
                ORDER BY asked_at ASC"""),
        (user_id, since),
    ).fetchall()
    rows = [dict(r) for r in rows]
    answered = [r for r in rows if r["status"] == "answered" and r["n_correct"] is not None]
    skipped = sum(1 for r in rows if r["status"] == "skipped")

    if not answered:
        return {"status": "no_data", "rate": None, "adjusted_rate": None,
                "n_checks": 0, "n_skipped": skipped,
                "needed": MIN_CHECKS_FOR_RATE, "trend": None,
                "message": "No retrieval checks answered yet."}

    total_q = sum(r["n_questions"] for r in answered)
    total_c = sum(r["n_correct"] for r in answered)
    rate = total_c / total_q if total_q else None

    # Corrected for guessing. With four options a blind guess scores 0.25,
    # so a raw 40% is barely above chance and reporting it unadjusted would
    # overstate what happened. Floored at 0 — a negative "knowledge" is not
    # a thing, it is noise.
    adjusted = max(0.0, (rate - GUESS_RATE) / (1 - GUESS_RATE)) if rate is not None else None

    if len(answered) < MIN_CHECKS_FOR_RATE:
        return {"status": "warming_up", "rate": round(rate, 3),
                "adjusted_rate": round(adjusted, 3), "n_checks": len(answered),
                "n_skipped": skipped, "needed": MIN_CHECKS_FOR_RATE,
                "trend": None,
                "message": (f"{len(answered)} of {MIN_CHECKS_FOR_RATE} checks — "
                            "too few to read as a rate yet.")}

    # Direction over the window: the mean of the newer half against the
    # older half. Deliberately cruder than a fitted slope, because with a
    # handful of points a slope invites more precision than exists.
    half = len(answered) // 2
    older = answered[:half] or answered[:1]
    newer = answered[half:]

    def mean_rate(group):
        q = sum(r["n_questions"] for r in group)
        return (sum(r["n_correct"] for r in group) / q) if q else 0.0

    delta = mean_rate(newer) - mean_rate(older)
    trend = "improving" if delta > 0.1 else "declining" if delta < -0.1 else "steady"

    return {
        "status": "ok",
        "rate": round(rate, 3),
        "adjusted_rate": round(adjusted, 3),
        "n_checks": len(answered),
        "n_skipped": skipped,
        "needed": MIN_CHECKS_FOR_RATE,
        "trend": trend,
        "trend_delta": round(delta, 3),
        "median_latency_ms": int(statistics.median(
            [r["median_latency_ms"] for r in answered if r["median_latency_ms"]] or [0])) or None,
        "message": _describe(rate, trend, len(answered)),
    }


def _describe(rate, trend, n):
    percent = round(rate * 100)
    if trend == "improving":
        tail = " and improving over recent checks"
    elif trend == "declining":
        tail = " and lower than your earlier checks"
    else:
        tail = ""
    return (f"You answered {percent}% of retrieval questions correctly across "
            f"{n} checks{tail}. This measures recall on the concepts you "
            "selected, not the quality of your work.")


def per_concept(conn, user_id, now_ms, window_days=90):
    """Answered checks grouped by concept, oldest first.

    This is the sequence Bayesian Knowledge Tracing consumes (`bkt.py`),
    and it is the reason that model became justified rather than
    decorative: a per-concept sequence of correct/incorrect attempts is
    exactly what BKT is for, and before this table existed there was no
    such sequence anywhere in the system.
    """
    since = now_ms - window_days * 86_400_000
    rows = conn.execute(
        db.q("""SELECT concept_id, asked_at, n_questions, n_correct
                FROM retrieval_checks
                WHERE user_id = ? AND status = 'answered' AND asked_at >= ?
                ORDER BY asked_at ASC"""),
        (user_id, since),
    ).fetchall()

    grouped = {}
    for row in (dict(r) for r in rows):
        grouped.setdefault(row["concept_id"], []).append(row)
    return grouped
