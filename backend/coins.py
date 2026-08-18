r"""Autonomize Coins — the reward the dashboard shows.

THE RULE IS NOT INVENTED HERE
-----------------------------

It is stated on the dashboard itself, in the card's own footer:

    +10 for a session with nothing pasted · −1 for every 100 characters pasted

This module implements exactly that and nothing else. The temptation with a
gamification feature is to "improve" the formula while wiring it up —
weight it by session length, add streak multipliers, round differently.
That would make the number on screen disagree with the rule printed
directly beneath it, which is the one failure a user can actually catch.

WHY IT LIVES IN THE BACKEND
---------------------------

It could be computed in the browser: the dashboard already receives every
session's `pasted_chars`. But the same argument that keeps `scoring.py` on
the server applies here. The moment a second client exists — the extension
popup, a mobile view, an institution report — a browser-side formula has to
be reimplemented, and two implementations of one rule drift. They drift
silently, because both look plausible, and there is no way to say which
balance is the real one.

So the arithmetic happens once, next to the session data it reads.

WHAT COUNTS
-----------

Only scored categories — `writing` and `assessment`. An `ai_assistant`
session is time spent with a tool, not work submitted, and awarding or
docking coins for visiting ChatGPT would be scoring the wrong thing: the
project's whole claim is that using AI is not the problem, and that
*substituting* it for your own work is.
"""

# Straight from the card footer. Named rather than inlined so a future
# change has one place to happen and one test to update.
BONUS_CLEAN_SESSION = 10
PENALTY_PER_CHARS = 100
CHARS_PER_PENALTY = 1

# Thresholds the dashboard renders as a tier and a progress bar. Ordered,
# and the first entry must be 0 so every balance has a tier.
TIERS = [
    {"name": "Bronze", "at": 0},
    {"name": "Silver", "at": 100},
    {"name": "Gold", "at": 250},
    {"name": "Platinum", "at": 500},
]

# How many ledger rows the card shows. The dashboard lists recent entries;
# sending the user's whole history would grow /api/score without bound for
# a panel that displays a handful.
LEDGER_LIMIT = 6

# Categories that can earn or spend. See the module docstring.
SCORABLE = ("writing", "assessment")


def delta_for(pasted_chars):
    """Coins earned or spent by one session.

    Integer division on purpose: 99 pasted characters costs nothing and 199
    costs 1. The rule says "for every 100 characters", and rounding up would
    charge for a fraction the sentence does not mention.
    """
    pasted = max(0, int(pasted_chars or 0))
    if pasted == 0:
        return BONUS_CLEAN_SESSION
    return -(pasted // PENALTY_PER_CHARS) * CHARS_PER_PENALTY


def tier_for(balance):
    """(current tier, next tier or None) for a balance.

    A balance can be NEGATIVE — the rule docks a coin per 100 pasted
    characters and sets no floor, so a heavy paster goes below zero. The
    first version of this function picked the next tier as "the first tier
    above the balance", which for -285 returned Bronze as both the current
    and the next tier: the card read "Bronze — 285 to Bronze", which is
    nonsense the user would see immediately.

    The next tier is the first one above the CURRENT tier's threshold.
    Someone at -285 is in Bronze and needs 385 to reach Silver, which is
    both true and useful.
    """
    current = TIERS[0]
    for tier in TIERS:
        if balance >= tier["at"]:
            current = tier
    following = None
    for tier in TIERS:
        if tier["at"] > current["at"]:
            following = tier
            break
    return current, following


def summarise(rows, now_ms):
    """Balance, tier, weekly movement and a recent ledger.

    `rows` are session rows oldest-or-newest, order does not matter — the
    balance is a sum. Returns a shape the dashboard renders directly rather
    than a bag of numbers it has to combine, because combining them in the
    client is where the second implementation would start.
    """
    balance = 0
    week = 0
    ledger = []
    week_ago = now_ms - 7 * 86_400_000

    for row in rows:
        if row["category"] not in SCORABLE:
            continue
        # An unscored session has not been finalised, so its counters are
        # still moving. Awarding coins for it would let the balance jump
        # around while a document is still open.
        if row["score"] is None:
            continue

        pasted = int(row["pasted_chars"] or 0)
        delta = delta_for(pasted)
        balance += delta

        started = row["started_at"] or 0
        if started >= week_ago:
            week += delta

        ledger.append({
            # The dashboard's ledger has a "task" column. There is no task
            # name to put in it — see the note in the API response below —
            # so the site stands in as the most specific label the data
            # honestly supports.
            "site": row["domain"] or "unknown",
            "category": row["category"],
            "started_at": started,
            "pasted": pasted,
            "delta": delta,
        })

    ledger.sort(key=lambda entry: entry["started_at"])
    current, following = tier_for(balance)

    return {
        "balance": balance,
        "week_delta": week,
        "tier": current["name"],
        "next_tier": following["name"] if following else None,
        "to_next": (following["at"] - balance) if following else None,
        # The bar's fill, computed here so two clients cannot disagree about
        # whether it measures progress within the tier or toward the next.
        "tier_progress": _progress(balance, current, following),
        "ledger": ledger[-LEDGER_LIMIT:],
        # Echoed so a client can render the rule it is displaying rather
        # than hard-coding a sentence that could fall out of step with the
        # arithmetic above.
        "rule": {
            "bonus_clean_session": BONUS_CLEAN_SESSION,
            "penalty_per_chars": PENALTY_PER_CHARS,
        },
        # Stated in the payload because the dashboard's ledger asks for a
        # task name the extension deliberately never captures. A client that
        # did not know this would show "unknown" and look broken.
        "task_names_available": False,
    }


def _progress(balance, current, following):
    if not following:
        return 1.0
    span = following["at"] - current["at"]
    if span <= 0:
        return 1.0
    return max(0.0, min(1.0, (balance - current["at"]) / span))


def load(conn, user_id, now_ms):
    """Reads this user's sessions and summarises their coins."""
    import db

    rows = conn.execute(
        db.q("""SELECT category, domain, started_at, pasted_chars, score
                FROM sessions
                WHERE user_id = ?
                ORDER BY started_at ASC"""),
        (user_id,),
    ).fetchall()
    return summarise([dict(r) for r in rows], now_ms)
