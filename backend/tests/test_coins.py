"""Autonomize Coins.

The rule is printed on the card the number appears on:

    +10 for a session with nothing pasted · −1 for every 100 characters pasted

So the tests that matter are the ones a user could run themselves by
reading that sentence and doing the arithmetic. A gamification number that
disagrees with the rule beside it is the one bug here that anybody would
actually catch.
"""
import pytest

import coins


def session(pasted, category="writing", score=80.0, started_at=1_700_000_000_000,
            domain="docs.google.com"):
    return {"category": category, "domain": domain, "started_at": started_at,
            "pasted_chars": pasted, "score": score}


NOW = 1_700_000_000_000 + 30 * 86_400_000


# ---------------------------------------------------------------------------
# The rule, exactly as printed
# ---------------------------------------------------------------------------

def test_a_session_with_nothing_pasted_earns_ten():
    assert coins.delta_for(0) == 10


@pytest.mark.parametrize("pasted,expected", [
    (100, -1), (199, -1), (200, -2), (320, -3), (610, -6), (1000, -10),
])
def test_one_coin_is_spent_per_hundred_characters(pasted, expected):
    assert coins.delta_for(pasted) == expected


def test_a_partial_hundred_costs_nothing():
    """The rule says "for every 100 characters". Rounding up would charge
    for a fraction the sentence does not mention — and 99 characters
    costing a coin is the kind of thing a user notices and distrusts."""
    assert coins.delta_for(99) == 0
    assert coins.delta_for(1) == 0


def test_a_negative_paste_count_cannot_mint_coins():
    """Defensive: the extension never sends one, but a clamp here is
    cheaper than discovering that a corrupt row made someone rich."""
    assert coins.delta_for(-500) == 10


# ---------------------------------------------------------------------------
# Tiers — where the first implementation was wrong
# ---------------------------------------------------------------------------

def test_a_negative_balance_still_names_the_next_tier_correctly():
    """The rule sets no floor, so a heavy paster goes below zero.

    The first version picked "the first tier above the balance", which at
    -285 returned Bronze as BOTH the current and the next tier — the card
    read "Bronze — 285 to Bronze". This is that regression.
    """
    current, following = coins.tier_for(-285)
    assert current["name"] == "Bronze"
    assert following["name"] == "Silver"
    assert following["at"] - (-285) == 385


@pytest.mark.parametrize("balance,tier,nxt", [
    (0, "Bronze", "Silver"),
    (99, "Bronze", "Silver"),
    (100, "Silver", "Gold"),
    (249, "Silver", "Gold"),
    (250, "Gold", "Platinum"),
    (500, "Platinum", None),
    (9999, "Platinum", None),
])
def test_every_balance_lands_in_the_right_tier(balance, tier, nxt):
    current, following = coins.tier_for(balance)
    assert current["name"] == tier
    assert (following["name"] if following else None) == nxt


def test_the_top_tier_has_no_next_and_a_full_bar():
    summary = coins.summarise([session(0)] * 60, NOW)
    assert summary["tier"] == "Platinum"
    assert summary["next_tier"] is None
    assert summary["to_next"] is None
    assert summary["tier_progress"] == 1.0


def test_progress_never_escapes_zero_to_one():
    for balance in (-1000, -1, 0, 50, 100, 499, 500, 10_000):
        current, following = coins.tier_for(balance)
        assert 0.0 <= coins._progress(balance, current, following) <= 1.0


# ---------------------------------------------------------------------------
# What counts
# ---------------------------------------------------------------------------

def test_ai_assistant_sessions_neither_earn_nor_spend():
    """Visiting an AI tool is not the thing being measured. The project's
    claim is that using AI is fine and substituting it for your own work
    is not, so charging for the visit would score the wrong thing."""
    summary = coins.summarise([session(0, category="ai_assistant"),
                               session(900, category="ai_assistant")], NOW)
    assert summary["balance"] == 0
    assert summary["ledger"] == []


def test_an_unscored_session_is_ignored_until_it_finalises():
    """Its counters are still moving; awarding coins now would let the
    balance jump around while the document is still open."""
    summary = coins.summarise([session(0, score=None), session(0)], NOW)
    assert summary["balance"] == 10


def test_assessment_sessions_count():
    summary = coins.summarise([session(0, category="assessment")], NOW)
    assert summary["balance"] == 10


# ---------------------------------------------------------------------------
# The summary the card renders
# ---------------------------------------------------------------------------

def test_the_balance_is_the_sum_of_every_session():
    rows = [session(0), session(0), session(320), session(610)]
    assert coins.summarise(rows, NOW)["balance"] == 10 + 10 - 3 - 6


def test_the_week_delta_counts_only_the_last_seven_days():
    old = session(0, started_at=NOW - 30 * 86_400_000)
    recent = session(320, started_at=NOW - 2 * 86_400_000)
    summary = coins.summarise([old, recent], NOW)
    assert summary["balance"] == 10 - 3
    assert summary["week_delta"] == -3


def test_the_ledger_is_capped_and_keeps_the_most_recent():
    rows = [session(0, started_at=NOW - (40 - i) * 86_400_000) for i in range(40)]
    summary = coins.summarise(rows, NOW)
    assert len(summary["ledger"]) == coins.LEDGER_LIMIT
    # Newest last, so the card's reverse() shows the most recent first.
    assert summary["ledger"][-1]["started_at"] > summary["ledger"][0]["started_at"]


def test_the_payload_says_task_names_are_unavailable():
    """The card's ledger has a "task" column and the extension captures no
    document titles by design. Saying so in the payload is what stops a
    client rendering "unknown" and looking broken."""
    summary = coins.summarise([session(0)], NOW)
    assert summary["task_names_available"] is False


def test_the_rule_travels_with_the_numbers():
    """So a client renders the rule it is actually applying rather than a
    sentence hard-coded in markup that could drift from the arithmetic."""
    summary = coins.summarise([session(0)], NOW)
    assert summary["rule"]["bonus_clean_session"] == coins.BONUS_CLEAN_SESSION
    assert summary["rule"]["penalty_per_chars"] == coins.PENALTY_PER_CHARS


def test_no_sessions_is_a_zero_balance_not_a_crash():
    summary = coins.summarise([], NOW)
    assert summary["balance"] == 0
    assert summary["tier"] == "Bronze"
    assert summary["ledger"] == []


def test_a_missing_paste_count_is_treated_as_zero_pasted():
    """An older extension build, or a row written before the column
    existed, must not poison the balance with a None."""
    row = session(0)
    row["pasted_chars"] = None
    assert coins.summarise([row], NOW)["balance"] == 10


# ---------------------------------------------------------------------------
# End to end through the database
# ---------------------------------------------------------------------------

def test_load_reads_sessions_and_summarises(sqlite_conn):
    import db

    for i, pasted in enumerate([0, 0, 450]):
        db.upsert_session_row(sqlite_conn, {
            "session_id": f"s{i}", "user_id": "u1", "category": "writing",
            "domain": "docs.google.com", "path": "/d", "started_at": NOW - i * 3600_000,
            "active_ms": 60_000,
            "metrics": {"typed_chars": 900, "pasted_chars": pasted,
                        "backspace_count": 0, "revision_count": 0,
                        "likely_ai_pastes": 0, "tab_switch_count": 0},
            "is_final": True,
        })
        sqlite_conn.execute("UPDATE sessions SET score = 80 WHERE session_id = ?", (f"s{i}",))

    summary = coins.load(sqlite_conn, "u1", NOW)
    assert summary["balance"] == 10 + 10 - 4
    assert len(summary["ledger"]) == 3
    assert summary["ledger"][0]["site"] == "docs.google.com"
