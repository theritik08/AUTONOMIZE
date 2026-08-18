# Engineering review — Autonomize

Written as a senior engineer / hiring manager would review this repo before
an interview loop. Kept in the repo deliberately: a candidate who has
already found the holes in their own work is in a much stronger position
than one who hasn't.

**This document is not a claim that the project is beyond criticism.** No
project is, and any reviewer worth talking to will find something. The
goal is that nothing they find is a surprise to you.

---

## The one-line verdict

This reads like production work, not a tutorial project. The distinguishing
features are that the measurement idea is genuinely novel, the tests
verify behaviour against real infrastructure rather than mocks, and the
documentation states limitations plainly instead of overselling. The
weakest area is the one no amount of engineering can fix: **nobody has
used it.**

---

## What is genuinely strong

**The core idea is defensible and the implementation matches it.**
"Independence measured against your own baseline, not a population norm"
is a real thesis, and the code carries it through — per-user, per-category
EMA baselines, and anomaly detection that compares a session to that
person's own history. When the implementation *contradicted* that thesis
(`risk_level()` used fixed population thresholds), the contradiction was
found and fixed rather than papered over. Interviewers notice when a
stated principle survives contact with the code.

**Privacy is a constraint, not a marketing line.** The extension counts
characters and discards the string in the same tick. That constraint is
what forces the whole process-signal approach — you cannot grade output
you never see — and it makes the project *more* interesting, not less.

**Testing is unusually rigorous for a portfolio project.** 207 backend
tests that run against SQLite *and* a real PostgreSQL instance; a
Playwright suite that loads the actual unpacked Chrome extension and
drives its real service worker; CI that fails if the Postgres half
silently skips, if a dashboard script stops parsing, if an interactive
control is left unwired, or if a second copy of the dashboard reappears.
Most candidate repos have either no tests or tests that assert mocks
returned what they were told to return.

**Two bugs were caught by testing that code review had missed**, and both
are documented in the README:

1. `manifest.json` listed `chrome://*/*` in `exclude_matches`, which Chrome
   rejects — **the entire extension silently failed to load**, reporting
   only to stderr. Discovered within minutes of writing the first
   extension-context test.
2. `background.js` kept AI-paste correlation counts in a module-level
   object. MV3 tears the worker down between events, so any restart
   between a paste and its flush silently dropped the count — under-
   reporting `likely_ai_pastes` and therefore *inflating* the independence
   score. The regression test stops the real worker mid-session and
   asserts the count still reaches the backend.

**The ML is proportionate.** LinUCB over a 7-feature context, closed-form,
pure Python, fully inspectable — with a written justification for why a
heavier model would be worse at this data scale. Reaching for a neural
policy on tens of decisions per user would have been the weaker answer,
and the README says so.

**The bandit's reward design shows real thought.** Rewarding "did they tap
Accept" trains a model to produce agreeable pop-ups. Worse, the `none`
arm can never earn explicit feedback, so a feedback-only bandit
structurally cannot evaluate "leave them alone" and will always
over-nudge. The delayed outcome-attribution path exists specifically to
close that hole, and there's a test named after the property.

---

## Fixed during this review pass

| Finding | Severity | Status |
|---|---|---|
| `migrate_sqlite_to_postgres.py` had gone stale after two schema migrations — silently dropped `n_observations` and skipped three whole tables, discarding users' entire learned bandit policy | **High** — silent data loss in a script whose only job is not losing data | Fixed: derives tables from `db.USER_SCOPED_TABLES` and columns from the live schema, plus four tests that fail if a new table is added without teaching the script about it |
| No `LICENSE` | High for a public repo — legally unusable without one | MIT added |
| No root `.gitignore` | High — first `git add .` would commit `.env`, `*.db`, `node_modules` | Added |
| No CI | High for credibility — a suite nobody sees run is worth much less | GitHub Actions running all four suites, incl. a Postgres service container |
| No screenshots in the README | High for a portfolio repo — most reviewers scroll before they read | Four added |
| `allow_origins=["*"]` | Medium — correct on localhost, too loose in public | Now `AUTONOMIZE_ALLOWED_ORIGINS`; logs a warning if left wildcard while auth is on |
| No rate limit on an unauthenticated write endpoint | Medium — "someone can fill my database" is the first question about a public deploy | Token-bucket limiter, opt-in, tested over real HTTP for a 429 |
| `print()` for startup logging | Low — invisible to any aggregator, no severity or timestamps | Real `logging`, level via env |
| Version drift (extension `0.1.0`, API `0.5.0`) | Low, but reads as carelessness | Aligned at `0.5.0` |
| `forecast` computed by the API and never displayed | Low — dead backend feature | Surfaced in the trend card, gated on r² so it stays quiet when the line doesn't describe the data |
| Not deployable | Medium for "live on a website" | Dockerfile (non-root, `$PORT`, real healthcheck) + compose with Postgres |

---

## What a sharp interviewer will still ask

Have an answer ready for each. None of these have a code fix — they're
honest limits of what a solo project can establish.

**"Has anyone actually used this?"**
No. That is the single biggest gap and it should be conceded immediately
rather than deflected. The measurement pipeline is built and tested; the
bandit has never served a nudge to a human; the scoring weights have never
been validated against a real outcome. Everything below follows from this.

**"How do you know the score measures anything real?"**
You don't yet. It's a hand-tuned heuristic (100 / 12 / 22) chosen for a
sane 0–100 range. `fit_weights.py` exists precisely to interrogate it
against self-reported comprehension, and it *refuses* to output a fitted
model below 30 labels — because a fit on less would look authoritative and
mean nothing. The strong version of this answer is: "I built the machinery
to find out, and deliberately stopped short of pretending I already had."

**"Isn't a paste-based signal trivially gameable?"**
Yes. Retyping AI output by hand defeats it entirely, as does using a phone
alongside the laptop. The honest framing is that this is a *self-awareness*
tool, not an anti-cheat system — the README says so, and the exam-integrity
panel is student-facing only, never reported to an institution. If you
pitch it as proctoring, you lose. Pitch it as a mirror.

**"Why is the extension's most important state in `chrome.storage` rather
than memory?"**
Because MV3 kills the worker between events — and there's a regression
test that proves it, by stopping the real worker mid-session. This is a
good question to *want*, since the answer is a bug you found and fixed.

**"Your rate limiter doesn't work across replicas."**
Correct, and `ratelimit.py` says so in its own docstring. It's one
process's memory; it stops a buggy client and a casual `curl` loop, not a
distributed flood, which belongs at the edge. Knowing what your mitigation
*doesn't* cover is the answer here.

**"Why is the dashboard plain HTML/CSS/JS rather than a framework app?"**
It used to be a React/TypeScript app whose build output was committed into
the extension, which meant three copies of the same UI (the source, the
committed build, and a stale duplicate) and two of everything user-facing:
two settings screens, two auth flows, two theme systems. The consolidation
deleted all of that in favour of one `dashboard-web/` — no build step, no
npm dependencies, no bundler, so what is served is exactly what is read.
The honest cost is that a larger UI would want a component model; the
honest gain is that a copy can no longer drift from the thing it copies.

**"What would you do with three more months?"**
Real users, in that order: a small pilot to collect comprehension labels →
fit the weights against them → only then turn the bandit on for anyone.
Followed by content-script test coverage (currently the one untested
layer), a shared rate limiter, and an instructor view that is aggregate-
only by construction.

---

## Where this sits

For a student/junior portfolio this is comfortably above the bar — the
gap between it and a typical CRUD project is not effort, it's judgement:
choosing a measurement problem, holding a privacy constraint that makes
the problem harder, testing against real infrastructure, and documenting
what isn't true yet.

For a mid-level backend/full-stack role the code is credible. The
conversation will turn on the user-validation gap, which is a question of
what you'd do next rather than a flaw in what's here.

The one thing that would most change how this is received is not more
code. It's ten real users and a paragraph about what you learned from
them.
