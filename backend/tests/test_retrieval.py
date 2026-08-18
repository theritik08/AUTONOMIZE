"""The learning-verification layer.

What matters here is not that grading works — it is that the layer cannot
be turned back into self-report. A client that could see the answers, or
grade itself, or replay one correct answer three times, would give exactly
the confident-looking number this whole module exists to replace.
"""
import json

import pytest

import bkt
import learning_state
import retrieval

NOW = 1_700_000_000_000


@pytest.fixture
def bank(sqlite_conn):
    retrieval.add_concept(sqlite_conn, "big-o", "Time complexity", "CS")
    for i in range(5):
        retrieval.add_question(
            sqlite_conn, f"q{i}", "big-o", f"Question {i}?",
            ["a", "b", "c", "d"], i % 4, 0.5)
    return sqlite_conn


# ---------------------------------------------------------------------------
# The answers must never leave the server
# ---------------------------------------------------------------------------

def test_questions_sent_to_a_client_carry_no_answer(bank):
    check = retrieval.open_check(bank, "u1", "big-o", "s1", NOW)
    blob = json.dumps(check)
    assert "answer_index" not in blob
    for question in check["questions"]:
        assert set(question) == {"question_id", "prompt", "options"}


def test_grading_happens_server_side_only(bank):
    """A client that graded itself would report whatever it liked, and the
    point of this layer is that it is objective rather than self-reported."""
    check = retrieval.open_check(bank, "u1", "big-o", "s1", NOW)
    qid = check["questions"][0]["question_id"]
    correct = int(qid[1:]) % 4

    result = retrieval.submit(bank, "u1", check["check_id"],
                              [{"question_id": qid, "choice": correct}], NOW)
    assert result["n_correct"] == 1


def test_the_same_right_answer_cannot_be_submitted_repeatedly(bank):
    """Otherwise one lucky guess scores a perfect check."""
    check = retrieval.open_check(bank, "u1", "big-o", "s1", NOW)
    qid = check["questions"][0]["question_id"]
    correct = int(qid[1:]) % 4

    result = retrieval.submit(bank, "u1", check["check_id"],
                              [{"question_id": qid, "choice": correct}] * 3, NOW)
    assert result["n_correct"] == 1


def test_answers_to_questions_this_check_did_not_ask_are_ignored(bank):
    check = retrieval.open_check(bank, "u1", "big-o", "s1", NOW)
    asked = {q["question_id"] for q in check["questions"]}
    other = next(f"q{i}" for i in range(5) if f"q{i}" not in asked)

    result = retrieval.submit(bank, "u1", check["check_id"],
                              [{"question_id": other, "choice": int(other[1:]) % 4}], NOW)
    assert result["n_correct"] == 0


def test_a_check_cannot_be_answered_twice(bank):
    check = retrieval.open_check(bank, "u1", "big-o", "s1", NOW)
    retrieval.submit(bank, "u1", check["check_id"], [], NOW)
    with pytest.raises(retrieval.RetrievalError):
        retrieval.submit(bank, "u1", check["check_id"], [], NOW)


def test_one_user_cannot_answer_another_users_check(bank):
    check = retrieval.open_check(bank, "u1", "big-o", "s1", NOW)
    with pytest.raises(retrieval.RetrievalError):
        retrieval.submit(bank, "u2", check["check_id"], [], NOW)


def test_a_concept_with_too_few_questions_is_refused(sqlite_conn):
    """Better to offer nothing than a one-question check, where a single
    guess is a 25% or 100% score."""
    retrieval.add_concept(sqlite_conn, "thin", "Thin concept", "CS")
    retrieval.add_question(sqlite_conn, "only", "thin", "?", ["a", "b"], 0)
    with pytest.raises(retrieval.RetrievalError):
        retrieval.open_check(sqlite_conn, "u1", "thin", "s1", NOW)


# ---------------------------------------------------------------------------
# When a check is offered
# ---------------------------------------------------------------------------

def test_a_check_is_not_offered_immediately_after_the_session(bank):
    """Answered with the material still on screen it measures short-term
    recall, not retrieval."""
    session = {"session_id": "s1", "category": "writing", "started_at": NOW}
    ok, reason = retrieval.should_offer(bank, "u1", session, NOW + 60_000)
    assert ok is False and "too soon" in reason


def test_a_check_is_offered_after_the_delay(bank):
    session = {"session_id": "s1", "category": "writing", "started_at": NOW}
    ok, _ = retrieval.should_offer(
        bank, "u1", session, NOW + (retrieval.MIN_DELAY_MINUTES + 5) * 60_000)
    assert ok is True


def test_ai_assistant_sessions_are_not_checked(bank):
    session = {"session_id": "s1", "category": "ai_assistant", "started_at": NOW}
    ok, _ = retrieval.should_offer(bank, "u1", session, NOW + 3600_000)
    assert ok is False


def test_a_session_is_only_checked_once(bank):
    session = {"session_id": "s1", "category": "writing", "started_at": NOW}
    later = NOW + 3600_000
    retrieval.open_check(bank, "u1", "big-o", "s1", later)
    ok, reason = retrieval.should_offer(bank, "u1", session, later)
    assert ok is False and "already" in reason


def test_unanswered_checks_expire_rather_than_blocking_forever(bank):
    retrieval.open_check(bank, "u1", "big-o", "s1", NOW)
    retrieval.expire_stale(bank, NOW + (retrieval.EXPIRY_HOURS + 1) * 3600_000)
    row = bank.execute("SELECT status FROM retrieval_checks WHERE session_id='s1'").fetchone()
    assert dict(row)["status"] == "skipped"


# ---------------------------------------------------------------------------
# The summary, and its honesty about small samples
# ---------------------------------------------------------------------------

def test_no_checks_is_no_data_not_a_zero_rate(bank):
    summary = retrieval.summarise(bank, "u1", NOW)
    assert summary["status"] == "no_data"
    assert summary["rate"] is None


def test_too_few_checks_reports_warming_up(bank):
    for i in range(retrieval.MIN_CHECKS_FOR_RATE - 1):
        check = retrieval.open_check(bank, "u1", "big-o", f"s{i}", NOW + i)
        retrieval.submit(bank, "u1", check["check_id"], [], NOW + i)
    summary = retrieval.summarise(bank, "u1", NOW + 10)
    assert summary["status"] == "warming_up"


def test_the_rate_is_corrected_for_guessing(bank):
    """With four options a blind guess scores 25%, so a raw 40% is barely
    above chance. Reporting it unadjusted would overstate what happened."""
    summary_all_wrong = retrieval.summarise(bank, "u1", NOW)
    assert summary_all_wrong["adjusted_rate"] is None  # no data yet

    for i in range(4):
        check = retrieval.open_check(bank, "u1", "big-o", f"s{i}", NOW + i)
        retrieval.submit(bank, "u1", check["check_id"], [], NOW + i)
    summary = retrieval.summarise(bank, "u1", NOW + 100)
    assert summary["rate"] == 0.0
    # Zero raw is zero adjusted, and never negative.
    assert summary["adjusted_rate"] == 0.0


def test_the_message_never_claims_to_measure_work_quality(bank):
    for i in range(4):
        check = retrieval.open_check(bank, "u1", "big-o", f"s{i}", NOW + i)
        retrieval.submit(bank, "u1", check["check_id"], [], NOW + i)
    message = retrieval.summarise(bank, "u1", NOW + 100)["message"]
    assert "not the quality of your work" in message


# ---------------------------------------------------------------------------
# Bayesian Knowledge Tracing
# ---------------------------------------------------------------------------

def test_mastery_rises_with_repeated_correct_answers():
    curve = bkt.trace([(3, 3), (3, 3), (3, 3), (3, 3)])
    assert curve == sorted(curve)
    assert curve[-1] > curve[0]


def test_mastery_falls_after_wrong_answers():
    rising = bkt.trace([(3, 3), (3, 3), (3, 3)])
    then_wrong = bkt.trace([(3, 3), (3, 3), (3, 3), (0, 3)])
    assert then_wrong[-1] < rising[-1]


def test_mastery_stays_a_probability():
    for attempts in ([(3, 3)] * 40, [(0, 3)] * 40, [(1, 3), (3, 3)] * 20):
        for value in bkt.trace(attempts):
            assert 0.0 <= value <= 1.0


def test_too_few_attempts_is_warming_up_not_a_mastery_claim():
    """With one attempt the posterior is mostly the prior, so the number
    would describe the model's assumption rather than the student."""
    out = bkt.estimate([(3, 3)])
    assert out["status"] == "warming_up"
    assert out["mastered"] is None


def test_no_attempts_is_no_data():
    assert bkt.estimate([])["status"] == "no_data"


def test_slip_and_guess_are_bounded_away_from_the_degenerate_range():
    """Above 0.5 a 'known' state explains any data at all and the model
    stops meaning anything."""
    params = bkt.Parameters(p_slip=0.9, p_guess=0.9)
    assert params.p_slip <= bkt.MAX_SLIP
    assert params.p_guess <= bkt.MAX_GUESS


def test_the_message_calls_it_an_estimate_not_a_grade():
    out = bkt.estimate([(3, 3), (2, 3), (3, 3), (3, 3)])
    assert out["status"] == "ok"
    assert "not a grade" in out["message"]


# ---------------------------------------------------------------------------
# The learning state — where the two axes meet
# ---------------------------------------------------------------------------

def ret(rate, n=8, trend="steady"):
    return {"status": "ok", "adjusted_rate": rate, "n_checks": n, "trend": trend}


def test_a_new_user_gets_insufficient_evidence_not_a_label():
    out = learning_state.classify(80.0, "steady", None, n_sessions=1)
    assert out["state"] == "insufficient_evidence"


def test_assisted_work_with_good_recall_is_not_a_dependency_state():
    """The central claim: using AI is not the problem. Substituting it for
    your own understanding is."""
    out = learning_state.classify(45.0, "steady", ret(0.85), n_sessions=20)
    assert out["state"] == "assisted_but_engaged"


def test_assisted_work_with_poor_recall_is_the_state_worth_acting_on():
    out = learning_state.classify(45.0, "declining", ret(0.2), n_sessions=20)
    assert out["state"] == "high_dependency_risk"


def test_independent_work_with_poor_recall_is_not_called_dependency():
    """A student doing their own work and finding it hard is a teaching
    question. Labelling them 'dependent' would be both wrong and unkind."""
    out = learning_state.classify(90.0, "steady", ret(0.2), n_sessions=20)
    assert out["state"] == "independent"
    assert "teaching question" in out["message"]


def test_without_retrieval_the_state_is_low_confidence_and_says_so():
    out = learning_state.classify(45.0, "declining", None, n_sessions=20)
    assert out["confidence"] == "low"
    assert any("retrieval" in e.lower() for e in out["evidence"])


def test_every_state_ships_its_evidence_and_a_disclaimer():
    for score, retr in [(90.0, ret(0.9)), (45.0, ret(0.2)), (65.0, ret(0.5)), (50.0, None)]:
        out = learning_state.classify(score, "steady", retr, n_sessions=20)
        assert out["evidence"], out
        assert "not a judgement" in out["disclaimer"]
        assert out["state"] in learning_state.STATES


def test_no_state_is_decided_by_a_single_feature():
    """Same behaviour score, opposite recall — the states must differ, or
    retrieval is not contributing anything."""
    good = learning_state.classify(45.0, "steady", ret(0.9), n_sessions=20)
    bad = learning_state.classify(45.0, "steady", ret(0.1), n_sessions=20)
    assert good["state"] != bad["state"]
