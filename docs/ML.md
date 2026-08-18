# The machine-learning layer

What is learned, what is not, what was measured, and what is still missing.
For the system as a whole see [ARCHITECTURE.md](ARCHITECTURE.md).

> **Every number on this page comes from simulated student histories.**
> There is no pilot cohort. `simulate_history.py` generates the data and
> replays it through the real scoring path, so these results demonstrate
> that the pipeline works — not that the construct it measures is valid.
> The `synthetic` flag is written into every model file and returned by
> `/api/score` so a metric cannot be quoted without its label.

## The two questions

| | Learned from the database? | Why |
|---|---|---|
| Where is this student heading over the next few sessions? | **Yes** | The label is the next few rows. The database supervises itself. |
| Does the 0–100 score measure AI over-reliance? | **No** | Construct validity. Needs a human study, not a model. `fit_weights.py` is the instrument and it is still waiting for labels. |

A model that predicts the score perfectly still says nothing about the
second question. That is the single most important sentence on this page.

## The framework decision, and the benchmark that made it

The brief for this work suggested scikit-learn, and specifically asked
whether `HistGradientBoostingRegressor`, Random Forest or another
lightweight tabular model was most appropriate. So rather than adopting one
because it was suggested, both stacks were run head-to-head on identical
data and the identical time-ordered split (4,161 rows, 18 features,
horizon 5, train 2,705 / validation 624 / test 832).

| | MAE | RMSE | R² | fit time |
|---|---|---|---|---|
| **Free baselines — no model at all** | | | | |
| predict last score | 8.734 | 11.577 | +0.664 | — |
| predict their EMA *(the product's current behaviour)* | 6.112 | 8.275 | +0.829 | — |
| predict 7-session mean | 6.345 | 8.689 | +0.811 | — |
| **Pure Python — `ml/models.py`** | | | | |
| **ridge (hand-written)** | **5.756** | 7.446 | +0.861 | 0.10 s |
| gradient-boosted trees (hand-written) | 5.828 | 7.620 | +0.855 | 0.91 s |
| **scikit-learn** | | | | |
| RidgeCV | 5.767 | 7.454 | +0.861 | 0.04 s |
| ElasticNetCV | 5.803 | 7.479 | +0.860 | 0.08 s |
| HistGradientBoosting | 5.927 | 7.573 | +0.856 | 0.12 s |
| HistGradientBoosting, tuned | 5.886 | 7.517 | +0.858 | 0.37 s |
| RandomForest (300) | 5.942 | 7.628 | +0.854 | 3.50 s |
| ExtraTrees (300) | 5.910 | 7.475 | +0.860 | 1.05 s |
| **scikit-learn, predicting the residual from the EMA** | | | | |
| RidgeCV on residual | 5.755 | 7.444 | +0.861 | 0.02 s |
| HistGradientBoosting on residual | 5.789 | 7.502 | +0.859 | 0.17 s |

Two conclusions, both of which changed what shipped.

**Trees lose.** Every tree method tried — mine and scikit-learn's — is
beaten by a regularised linear model. That is not a surprise once you look
at the features: they are lagged values, rolling means and a slope of a
strongly autocorrelated series, so the target is very close to a weighted
sum of them by construction, there are few genuine interactions to find,
and four thousand rows is not enough for an ensemble to recover a smooth
response better than a linear fit states it directly. **Ridge is the
primary model**, chosen on the held-out slice. The boosted trees stay as a
challenger that must beat it by `CHALLENGER_MARGIN` every run.

**scikit-learn's accuracy contribution is nil.** 5.756 against 5.755 is the
same answer computed twice. So it is *not* a serving dependency — that
would be ~100 MB of compiled packages for 0.001 MAE, and it would break the
property that `pip install -r requirements.txt` works from a fresh clone
with six pure-Python packages.

It is instead in `requirements-ml.txt`, dev and CI only, doing the job it is
actually best at here: **being the oracle my implementations are checked
against.** `tests/test_ml_against_sklearn.py` asserts

- `RidgeRegression` matches `sklearn.linear_model.Ridge` to 1e-6 on both
  predictions *and* coefficients,
- `GradientBoostedTrees` stays within 35% of `HistGradientBoostingRegressor`
  on a problem built to favour trees,
- `IsolationForest` ranks anomalies at Spearman ρ > 0.9 against
  `sklearn.ensemble.IsolationForest`.

All three pass. A hand-written learner nobody cross-checked is a liability;
one that provably agrees with the reference is a demonstration.

## What the current model actually is

Trained on 60 simulated students over 120 days — 5,000 sessions, 4,161
training rows, horizon 5.

```
chosen: ridge regression                MAE 5.76   RMSE 7.45   R² +0.861
best free baseline: their own EMA       MAE 6.11
  -> 5.8% lower MAE than the best free baseline

conformal interval  ±11.9 points at 90% nominal coverage
                    measured coverage on the held-out slice: 90.1%
```

5.8% is a modest, believable improvement, and it is reported rather than
inflated. If it had come out negative the pipeline would have refused to
write a model at all — `training.py` treats "the free baseline wins" as a
result, not a bug.

### What the model relies on

Permutation importance — the MAE cost of destroying each feature on
held-out rows — rather than split counts, which describe the model's
structure rather than what it needs.

```
is_assessment          +2.474
mean_regularity_5      +1.345      <- the typing-rhythm signal
ema                    +0.938
mean_3                 +0.903
last_paste_ratio       +0.752
last_typed_ratio       +0.743
last_regularity        +0.565
last_score             +0.362
```

The rhythm signal ranking second is the most interesting line here: the
eight-bucket histogram that was added *because* it was the most that could
be collected without leaking content turns out to carry more predictive
weight than the score history it sits beside.

Caveat, stated because it matters: these features are heavily correlated by
construction (`ema`, `mean_7` and `last_score` are three views of one
series), and permutation importance understates correlated groups because
the model recovers the shuffled signal from its neighbours.

## The other three additions

### Isolation forest — the multivariate gap

Everything else that judges a session judges its **score**: one number. That
is structurally blind to a session whose score is perfectly ordinary but
whose *shape* is not — same score, but produced from 70% pasted text in a
third of the usual time with a machine-flat rhythm.

`ml/isolation.py` fits on **within-user deviation vectors**: each session
expressed as how far each behavioural attribute sits from that same
student's own running mean, in their own units, from strictly earlier
sessions only.

**The honest caveat**, because it is the first cross-user model in the
codebase: the *origin* of the space is personal, but the *shape of the
deviation cloud* — "how big a change is a big change" — is pooled across
students and assumed exchangeable. That is the cost of getting a
multivariate signal at all at this data volume, and it is the weaker of the
two things that could have been pooled.

The threshold is not a constant. An earlier version used 0.62, picked by
eye; measuring the distribution showed the 99th percentile at 0.584 and the
maximum at 0.648, so that "top few percent" was really the top fraction of
a percent. It is now calibrated at fit time to the 98th percentile of the
training scores, which turns it into a statable promise — *this flags
roughly the most unusual 2% of sessions* — the same discipline
`conformal.py` uses. Measured flag rate on the demo data: **2.0%**.

### Cold start — empirical Bayes, not a hand-set rule

A new student has no baseline, and the three options are show nothing,
quietly show the population average, or blend and say how much is borrowed.
The second is dishonest; the first is what the code did before and wastes
the window when a habit is forming.

`ml/coldstart.py` shrinks toward a population prior with weight
`n / (n + k)`, where `k` is measured — within-student variance over
between-student variance — rather than chosen. On the demo data
`k ≈ 1.3`, meaning a student's own history outweighs the prior after
roughly one session, which is itself a finding: these simulated students
differ from each other much more than their own sessions vary.

Four fields are exposed separately rather than collapsed into one
percentage, because a warm-up state must not be able to look like a
confident one: `insufficient_data`, `warm_up {have, need}`, `reliability`
(the share that is actually their own data), and `confidence`
(learning / provisional / established).

### Explainability

- **Global**: permutation importance, recorded at training time because it
  needs the held-out set.
- **Local**: for a linear model the attribution is *exact* — the prediction
  is the intercept plus the per-feature terms, with no sampling error. This
  is worth saying plainly because "we used SHAP" is the expected answer, and
  SHAP's entire job is to approximate a decomposition this model has in
  closed form.

Context features (`is_assessment`, `n_prior`) are excluded from the
student-facing local explanation. They are constant or monotone within a
stream, so standardised coefficients make them dominate every prediction —
the first version of the explanation read *"the biggest factor was whether
this is graded work"* on every single writing session. A local explanation
should name things that could have been otherwise.

## How the credibility of these numbers is defended

Six guards in `ml/validation.py` run before any model is fitted, and each
one raises rather than logging, because a leak makes the result *better*
and therefore survives review unless something is actively looking for it.

| Guard | Catches |
|---|---|
| `assert_causal` | the future leaking into the features |
| `assert_time_ordered` | a random split of a time series |
| `assert_feature_contract` | a renamed or reordered column |
| `assert_no_constant_label` | an undefined R² reported as a good one |
| `assert_no_duplicate_rows` | a held-out set that is a copy of training |
| `assert_finite` | a NaN that silently poisons the normal equations |

`tests/test_ml_validation.py` constructs the specific broken input each one
exists to catch and asserts it fires. A guard nobody has seen fail is a
guard nobody knows works — and that is not hypothetical here: the first
version of `assert_causal` compared a prefix against a copy of the same
prefix, so it could never have failed. It was rewritten to poison the
source data and check that training rows built entirely before the cut do
not move, with an inclusive bound so that a window reaching *one* session
into the future is caught.

## Reproducibility

Every model file carries a manifest, and `registry.py` refuses to load one
whose feature-set hash or format version does not match the running code —
a stale model does not error, it confidently reinterprets paste ratio as a
session count.

```
feature_set_hash   745b0a2956b3bbbf
data_fingerprint   bec81e30fcbc031f     (sha256 of session ids + scores)
n_rows / n_users   4161 / 60
seed               20260813
python / sklearn   3.11.15 / 1.8.0
git_commit         b1ca55a
synthetic          true
```

Two runs on the same data produce byte-identical models, intervals and
forests. Verified, not assumed.

## The fallback

The system must work when the ML layer does not, and
`tests/test_ml_fallback.py` asserts it for each failure mode separately:
no model file, unreadable JSON, a missing manifest, a moved format version,
a changed feature set. In every case `/api/score` returns 200, the
deterministic pipeline is untouched, `prediction` is `null` rather than a
guess, and `signals.model.reason` says *why* — because a deployment with a
corrupt model file and one that has never trained need different fixes and
must not look identical.

## What is still missing

1. **The construct is still unvalidated.** No amount of this work touches
   whether the score means what the project says it means. That is the main
   open gap and it is a study, not a code change.
2. **All results are synthetic.** The pipeline is demonstrated; the
   population is invented.
3. **The population prior assumes exchangeability across students**, as does
   the isolation forest's deviation cloud. With one institution's data that
   is plausible. Across institutions it is not obviously true.
4. **Permutation importance understates correlated feature groups**, so the
   ranking above should be read as ordinal, not as a budget.
5. **The conformal interval assumes exchangeability**, which a drifting time
   series strains. Measured coverage (90.1% against a nominal 90%) says it
   is holding on this data; that check is run every training run and warns
   when it drops below 82%.
