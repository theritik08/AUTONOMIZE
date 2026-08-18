r"""Does the score depend on who you are rather than what you did?

    python3 fairness.py                 # run every sensitivity sweep
    python3 fairness.py --trait speed   # one trait

THE QUESTION THIS ANSWERS
-------------------------

The independence score is built from typing counts and typing rhythm. Both
of those vary enormously between people for reasons that have nothing to
do with AI reliance: a touch-typist and a two-finger typist produce very
different histograms while doing identical work, as do a person using a
mechanical keyboard and a person using a phone, a native speaker and
someone composing in a second language, and — most importantly — a person
with a motor impairment and a person without.

If the score moves when those change while the underlying reliance is
held CONSTANT, the system is measuring the person rather than the
behaviour. For a tool that will be shown to faculty, that is not a
performance issue. It is the difference between a support instrument and a
discrimination engine.

THE METHOD
----------

For each trait, generate a population of synthetic students who differ ONLY
in that trait, all with identical AI reliance. Score every one through the
real pipeline. Then measure the spread of the resulting scores.

    identical reliance + different trait -> scores should barely move

The metric reported is the spread across trait levels (max minus min mean
score) alongside the spread the reliance level itself produces. A trait
whose effect approaches the effect of reliance is a trait the score is
confounded by.

WHAT THIS IS NOT
----------------

It is not an audit against protected attributes, and it deliberately does
not model race, gender, or disability status as variables. It models the
*mechanical* consequences — slower typing, more pauses, shorter sessions,
more even rhythm — because those are what the pipeline actually sees, and
because collecting the attributes themselves in order to audit them would
create exactly the sensitive dataset this project exists without.

All populations here are SYNTHETIC. This measures a property of the
scoring code, which is a real thing to measure, and says nothing about how
real students of any group would score.
"""
import argparse
import statistics

import rhythm
import scoring

# Reliance levels swept as the reference effect. If a trait moves the score
# as much as moving between these does, the trait is a confounder.
RELIANCE_LEVELS = (0.0, 0.25, 0.5, 0.75)

# Above this share of the reliance effect, a trait is reported as a
# confounder rather than as noise. A judgement, stated as one: there is no
# principled cut, and the raw numbers are printed so the threshold can be
# argued with.
CONFOUND_RATIO = 0.35

STUDENTS_PER_CELL = 40


def _jitter(buckets, rng, spread=0.18):
    """Per-student variation.

    Without this every student at a trait level produces a byte-identical
    histogram, the personal baseline has zero variance, and the rhythm
    z-score is zero for everyone — which is how the first two versions of
    this harness reported a perfect 0.0 trait effect while testing nothing.
    """
    return [max(0, int(v * (1 + rng.uniform(-spread, spread)))) for v in buckets]


def _flatten_toward_transcription(buckets, reliance):
    """Higher reliance means more of the session is transcribed rather than
    composed, and transcription is more EVEN — mass moves into the middle
    bucket. This is the mechanism the rhythm signal exists to detect, so a
    sweep that held the histogram fixed across reliance levels could never
    exercise it."""
    if reliance <= 0:
        return list(buckets)
    total = sum(buckets)
    out = [max(0, int(v * (1 - reliance * 0.55))) for v in buckets]
    out[2] += total - sum(out)
    return out


def _session(typed, pasted, active_ms, buckets, long_pauses, burst_keys,
             ai_pastes=0, backspaces=None, revisions=6, tabs=3):
    return {
        "category": "writing",
        "typed_chars": int(typed), "pasted_chars": int(pasted),
        "backspace_count": int(backspaces if backspaces is not None else typed * 0.05),
        "revision_count": revisions, "likely_ai_pastes": int(ai_pastes),
        "tab_switch_count": tabs, "active_ms": int(active_ms),
        "iki_buckets": buckets, "long_pauses": long_pauses,
        "burst_keys": burst_keys,
    }


def _score_with_history(generator, level, reliance, rng, warmup=8):
    """Scores a session the way production does — AGAINST A PERSONAL BASELINE.

    The first version of this harness passed rhythm_penalty=0.0, on the
    reasoning that a single session has no baseline to compare against.
    That made every sweep return exactly 0.0 trait effect, which looked
    like a clean bill of health and was actually a vacuous test: the
    rhythm signal is the ONLY part of the pipeline a trait like input
    device can reach, and it had been switched off.

    The question worth asking is the one this now asks. A phone or
    assistive-input user has a naturally flatter, slower rhythm. Does the
    personal baseline absorb that — the project's central claim — or does
    the rhythm penalty fire more readily for them at the same reliance?

    So: build `warmup` sessions of that student's own ordinary work, let
    scoring.update_baseline learn their rhythm from it, then score one
    more session against that baseline exactly as the API does.
    """
    baseline = None
    for day in range(warmup):
        # Their normal working sessions, at low reliance — this is the
        # personal norm the later session is judged against.
        prior = generator(level, 0.1, rng)
        features = rhythm.features(
            iki_buckets=prior["iki_buckets"], long_pauses=prior["long_pauses"],
            burst_keys=prior["burst_keys"], typed_chars=prior["typed_chars"])
        score = scoring.compute_session_score(prior, rhythm_penalty=0.0)
        if score is None:
            continue
        # Distinct dates so the streak logic advances the way it does in
        # production; the streak itself is irrelevant here, but a repeated
        # date would take a different branch than the real path.
        baseline = scoring.update_baseline(
            baseline, score, f"2026-08-{day + 1:02d}",
            regularity=features.get("regularity_index"))

    session = generator(level, reliance, rng)
    features = rhythm.features(
        iki_buckets=session["iki_buckets"], long_pauses=session["long_pauses"],
        burst_keys=session["burst_keys"], typed_chars=session["typed_chars"])

    penalty = 0.0
    if baseline and features.get("regularity_index") is not None:
        deviation = rhythm.rhythm_deviation(features["regularity_index"], baseline)
        penalty = rhythm.penalty_weight(deviation)

    return scoring.compute_session_score(session, rhythm_penalty=penalty), features, penalty


# ---------------------------------------------------------------------------
# Trait models — what each difference actually does to the telemetry
# ---------------------------------------------------------------------------

def typing_speed(level, reliance, rng):
    """Fast vs slow typists. Same words written, different keystroke rate.

    A slow typist produces the same characters over a longer session with
    longer intervals — the histogram shifts right.
    """
    chars_per_min = {"very_slow": 12, "slow": 25, "average": 45, "fast": 80}[level]
    words = 320
    typed = int(words * 5 * (1 - reliance))
    pasted = int(words * 5 * reliance)
    active_ms = int((typed / max(1, chars_per_min)) * 60_000) or 60_000
    shift = {"very_slow": 3, "slow": 2, "average": 1, "fast": 0}[level]
    buckets = [0] * 8
    base = [10, 45, 160, 90, 40, 18, 8, 3]
    for i, v in enumerate(base):
        buckets[min(7, i + shift)] += v
    buckets = _jitter(_flatten_toward_transcription(buckets, reliance), rng)
    return _session(typed, pasted, active_ms, buckets,
                    long_pauses=6 + shift * 3, burst_keys=max(0, 120 - shift * 35))


def session_length(level, reliance, rng):
    """A ten-minute burst versus a two-hour sitting."""
    minutes = {"very_short": 6, "short": 18, "medium": 45, "long": 120}[level]
    typed = int(minutes * 40 * (1 - reliance))
    pasted = int(minutes * 40 * reliance)
    scale = minutes / 45.0
    buckets = [max(0, int(v * scale)) for v in (10, 45, 160, 90, 40, 18, 8, 3)]
    buckets = _jitter(_flatten_toward_transcription(buckets, reliance), rng)
    return _session(typed, pasted, minutes * 60_000, buckets,
                    long_pauses=int(8 * scale), burst_keys=int(120 * scale))


def input_device(level, reliance, rng):
    """Mechanical keyboard, laptop, phone, assistive/switch input.

    Phone and assistive input produce far longer and far more variable
    intervals — which is exactly what the rhythm signal reads.
    """
    profile = {
        "mechanical": ([14, 60, 170, 80, 30, 12, 5, 2], 5, 150),
        "laptop":     ([10, 45, 160, 90, 40, 18, 8, 3], 8, 120),
        "phone":      ([2, 12, 70, 110, 80, 40, 20, 8], 22, 40),
        "assistive":  ([1, 5, 30, 70, 90, 70, 45, 20], 45, 12),
    }[level]
    buckets, pauses, bursts = profile
    words = 260
    typed = int(words * 5 * (1 - reliance))
    pasted = int(words * 5 * reliance)
    minutes = {"mechanical": 30, "laptop": 35, "phone": 55, "assistive": 90}[level]
    buckets = _jitter(_flatten_toward_transcription(buckets, reliance), rng)
    return _session(typed, pasted, minutes * 60_000, buckets, pauses, bursts)


def language(level, reliance, rng):
    """First language versus composing in a second one.

    Second-language composition means more pausing to search for wording
    and more rewriting — more long pauses, more backspaces.
    """
    profile = {
        "first": ([12, 55, 165, 85, 35, 14, 6, 2], 6, 140, 0.05),
        "fluent_second": ([8, 38, 150, 95, 48, 22, 10, 4], 14, 100, 0.09),
        "learning_second": ([4, 22, 120, 105, 65, 35, 18, 8], 26, 60, 0.16),
    }[level]
    buckets, pauses, bursts, backspace_rate = profile
    words = 280
    typed = int(words * 5 * (1 - reliance))
    pasted = int(words * 5 * reliance)
    buckets = _jitter(_flatten_toward_transcription(buckets, reliance), rng)
    return _session(typed, pasted, 40 * 60_000, buckets, pauses, bursts,
                    backspaces=typed * backspace_rate,
                    revisions=int(6 + backspace_rate * 40))


TRAITS = {
    "speed": (typing_speed, ("very_slow", "slow", "average", "fast")),
    "length": (session_length, ("very_short", "short", "medium", "long")),
    "device": (input_device, ("mechanical", "laptop", "phone", "assistive")),
    "language": (language, ("first", "fluent_second", "learning_second")),
}


# ---------------------------------------------------------------------------
# The sweep
# ---------------------------------------------------------------------------

def sweep(trait, rng):
    """Mean score per (trait level, reliance level), plus rhythm readiness."""
    generator, levels = TRAITS[trait]
    table = {}
    rhythm_ok = {}
    penalties_seen = []

    for level in levels:
        for reliance in RELIANCE_LEVELS:
            scores = []
            ready = 0
            for _ in range(STUDENTS_PER_CELL):
                score, features, penalty = _score_with_history(
                    generator, level, reliance, rng)
                penalties_seen.append(penalty)
                if score is not None:
                    scores.append(score)
                if features.get("status") == "ok":
                    ready += 1
            table[(level, reliance)] = statistics.mean(scores) if scores else None
            rhythm_ok[(level, reliance)] = ready / STUDENTS_PER_CELL
    return table, levels, rhythm_ok, penalties_seen


def analyse(table, levels):
    """Trait effect against reliance effect, at matched reliance."""
    trait_spreads = []
    for reliance in RELIANCE_LEVELS:
        column = [table[(level, reliance)] for level in levels
                  if table[(level, reliance)] is not None]
        if len(column) >= 2:
            trait_spreads.append(max(column) - min(column))
    trait_effect = max(trait_spreads) if trait_spreads else 0.0

    reliance_means = []
    for reliance in RELIANCE_LEVELS:
        column = [table[(level, reliance)] for level in levels
                  if table[(level, reliance)] is not None]
        if column:
            reliance_means.append(statistics.mean(column))
    reliance_effect = (max(reliance_means) - min(reliance_means)) if reliance_means else 0.0

    ratio = (trait_effect / reliance_effect) if reliance_effect > 0 else 0.0
    return trait_effect, reliance_effect, ratio


def main(argv=None):
    import random

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trait", choices=sorted(TRAITS), default=None)
    parser.add_argument("--seed", type=int, default=20260816)
    args = parser.parse_args(argv)

    rng = random.Random(args.seed)
    chosen = [args.trait] if args.trait else sorted(TRAITS)

    print("SENSITIVITY OF THE INDEPENDENCE SCORE TO LEARNER TRAITS")
    print("Synthetic populations. Reliance is held constant within each column,")
    print("so any spread across a row is the score reacting to the trait alone.")
    print()

    findings = []
    for trait in chosen:
        table, levels, rhythm_ok, penalties = sweep(trait, rng)
        trait_effect, reliance_effect, ratio = analyse(table, levels)

        print(f"=== {trait} " + "=" * (56 - len(trait)))
        header = "  " + "level".ljust(16) + "".join(
            f"reliance {r:.0%}".rjust(15) for r in RELIANCE_LEVELS)
        print(header)
        for level in levels:
            row = "  " + level.ljust(16)
            for reliance in RELIANCE_LEVELS:
                value = table[(level, reliance)]
                row += (f"{value:15.1f}" if value is not None else "".rjust(15))
            print(row)

        # THE SELF-CHECK. A sweep in which the rhythm penalty never fired
        # is not evidence of fairness — it is evidence that the only
        # trait-sensitive part of the pipeline was switched off. Two
        # earlier versions of this harness reported a perfect 0.0 for
        # exactly that reason, so the harness now refuses to call itself
        # clean without having exercised the thing it audits.
        fired = sum(1 for p in penalties if p > 0)
        if fired == 0:
            print("  VACUOUS: the rhythm penalty never fired in this sweep, so a")
            print("  0.0 trait effect means nothing. Not reporting a verdict.")
            print()
            findings.append((trait, float("nan"), "vacuous"))
            continue
        print(f"  rhythm penalty fired on {fired}/{len(penalties)} sessions")

        verdict = "CONFOUNDER" if ratio >= CONFOUND_RATIO else "acceptable"
        print(f"  trait effect {trait_effect:5.1f} pts · "
              f"reliance effect {reliance_effect:5.1f} pts · "
              f"ratio {ratio:.0%}  -> {verdict}")

        ready_rates = sorted(set(round(v, 2) for v in rhythm_ok.values()))
        if len(ready_rates) > 1 or ready_rates[0] < 1.0:
            print(f"  rhythm signal available for: {ready_rates} of sessions "
                  "(a trait that suppresses it is not penalised, only unmeasured)")
        print()
        findings.append((trait, ratio, verdict))

    print("SUMMARY")
    for trait, ratio, verdict in findings:
        shown = "  n/a" if ratio != ratio else f"{ratio:5.0%}"
        print(f"  {trait:10} {shown} of the reliance effect  {verdict}")
    print()
    print("Synthetic populations only. This measures a property of the scoring")
    print("code — it says nothing about how real students of any group score.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
