"""Aggregate-only cohort statistics for institution accounts.

THE DESIGN CONSTRAINT THIS MODULE EXISTS TO ENFORCE
---------------------------------------------------
The whole project's premise is that a student's own data is theirs, and
that exam-integrity signals are shown to the student and to nobody else.
An institution view is where that premise is easiest to quietly abandon —
"just let the dean see who's flagged" is one query away, and it would turn
a self-awareness tool into surveillance.

So this module can only produce numbers about a *group*, and it refuses to
produce even those when the group is small enough to be de-anonymising.
There is deliberately no endpoint, parameter, or code path here that
returns a `user_id`, an email, a per-student score, or a flagged session.

TWO RULES, BOTH ENFORCED IN SQL RATHER THAN IN THE UI
-----------------------------------------------------
1. **Minimum cohort size.** Below MIN_COHORT_SIZE contributing students,
   every statistic is withheld. With four students, "the cohort average is
   62" plus three people comparing notes identifies the fourth.

2. **No caller-supplied filtering.** This is the rule people miss. A view
   that is aggregate-only but lets the caller narrow the population —
   by class, by date range, by score band — is not aggregate-only at all:
   narrow far enough and the aggregate *is* one student. The cohort here
   is always "every student account", full stop. Adding a filter parameter
   later would silently defeat rule 1, so any future segmentation must
   re-check the minimum on the *segment*, not the whole.

WHAT THIS STILL DOESN'T PROTECT AGAINST — stated plainly
---------------------------------------------------------
A determined admin who can query repeatedly over time can learn things by
differencing: if the cohort mean moves the day after exactly one student
joins, that student's score is recoverable. Defending against that
properly needs differential privacy (calibrated noise plus a privacy
budget), which is a real piece of work and is not implemented here. The
mitigations that *are* here — a fixed unfilterable population, a size
floor, and coarse buckets — raise the cost without eliminating the attack.
Anyone deploying this for a real institution should know that.
"""
import time

import db

# Below this many contributing students, nothing is reported. Five is a
# judgement call, not a derived constant: small enough to be usable for a
# seminar group, large enough that no single person's number dominates the
# mean. k-anonymity literature typically starts at 5.
MIN_COHORT_SIZE = 5

# Score bands, chosen to match scoring.risk_level's thresholds so the
# institution view and the student view describe the world the same way.
BANDS = [
    ("low", 0, 40),
    ("medium", 40, 70),
    ("high", 70, 101),
]

TREND_DAYS = 14


def _student_ids(conn) -> list:
    """Every student account. Deliberately takes no filter — see rule 2."""
    rows = conn.execute(
        db.q("SELECT user_id FROM users WHERE role = ?"), ("student",)
    ).fetchall()
    return [r["user_id"] for r in rows]


def _withheld(contributing: int) -> dict:
    return {
        "available": False,
        "reason": "cohort_too_small",
        "min_cohort_size": MIN_COHORT_SIZE,
        "contributing_students": contributing,
        # Enough to explain the empty state without revealing who is in it.
        "students_needed": max(0, MIN_COHORT_SIZE - contributing),
    }


def summary(conn) -> dict:
    """Cohort-level statistics, or a withheld marker. Never per-student."""
    student_ids = _student_ids(conn)
    if not student_ids:
        return _withheld(0)

    placeholders = ",".join(["?"] * len(student_ids))

    # Per-student mean, computed in SQL and immediately aggregated — the
    # individual means never leave this function.
    rows = conn.execute(
        db.q(f"""SELECT user_id, AVG(score) AS mean_score, COUNT(*) AS n
                 FROM sessions
                 WHERE user_id IN ({placeholders})
                   AND category = 'writing' AND score IS NOT NULL
                 GROUP BY user_id"""),
        tuple(student_ids),
    ).fetchall()

    means = [float(r["mean_score"]) for r in rows if r["mean_score"] is not None]
    contributing = len(means)

    # The size floor is checked against students who actually contributed
    # data, not students who merely have accounts — otherwise creating four
    # empty accounts would unlock statistics about the one real student.
    if contributing < MIN_COHORT_SIZE:
        return _withheld(contributing)

    means.sort()
    mean = sum(means) / contributing
    median = (
        means[contributing // 2]
        if contributing % 2
        else (means[contributing // 2 - 1] + means[contributing // 2]) / 2
    )

    distribution = []
    for label, low, high in BANDS:
        count = sum(1 for m in means if low <= m < high)
        distribution.append({
            "band": label,
            "count": count,
            "share": round(count / contributing, 3),
        })

    cutoff = int((time.time() - TREND_DAYS * 86400) * 1000)
    trend_rows = conn.execute(
        db.q(f"""SELECT {db.date_expr("started_at")} AS day,
                        AVG(score) AS mean_score,
                        COUNT(DISTINCT user_id) AS students
                 FROM sessions
                 WHERE user_id IN ({placeholders})
                   AND category = 'writing' AND score IS NOT NULL
                   AND started_at >= ?
                 GROUP BY day ORDER BY day ASC"""),
        (*student_ids, cutoff),
    ).fetchall()

    # A single day can fall below the floor even when the cohort as a whole
    # clears it — one student working on a quiet Sunday would otherwise be
    # published as that day's "cohort average".
    trend = [
        {
            "date": r["day"],
            "mean_score": round(float(r["mean_score"]), 1),
            "students": int(r["students"]),
        }
        for r in trend_rows
        if int(r["students"]) >= MIN_COHORT_SIZE
    ]
    suppressed_days = len(trend_rows) - len(trend)

    totals = conn.execute(
        db.q(f"""SELECT
                   COALESCE(SUM(CASE WHEN category IN ('writing','assessment')
                                     THEN active_ms ELSE 0 END), 0) AS independent_ms,
                   COALESCE(SUM(CASE WHEN category = 'ai_assistant'
                                     THEN active_ms ELSE 0 END), 0) AS assisted_ms
                 FROM sessions
                 WHERE user_id IN ({placeholders}) AND started_at >= ?"""),
        (*student_ids, int((time.time() - 7 * 86400) * 1000)),
    ).fetchone()

    independent_hours = round(float(totals["independent_ms"] or 0) / 3_600_000, 1)
    assisted_hours = round(float(totals["assisted_ms"] or 0) / 3_600_000, 1)

    return {
        "available": True,
        "min_cohort_size": MIN_COHORT_SIZE,
        "contributing_students": contributing,
        "enrolled_students": len(student_ids),
        "mean_score": round(mean, 1),
        "median_score": round(median, 1),
        "distribution": distribution,
        "trend": trend,
        "suppressed_days": suppressed_days,
        "independent_hours_7d": independent_hours,
        "assisted_hours_7d": assisted_hours,
        "generated_at": int(time.time() * 1000),
    }
