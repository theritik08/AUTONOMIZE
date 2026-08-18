r"""The machine-learning layer.

WHAT IS LEARNED HERE, AND WHAT IS NOT
-------------------------------------

One thing is learned from the database and one thing is not, and confusing
them is the fastest way to overclaim in a viva.

    LEARNED      given how a student has been working, where are they
                 heading over the next few sessions? The label is the next
                 few rows, so the database supervises itself and there are
                 thousands of examples the moment anyone uses the product.

    NOT LEARNED  whether the 0-100 score means what the project says it
                 means. That is a construct-validity question, it needs a
                 human study, and no amount of modelling substitutes for
                 it. `fit_weights.py` is the instrument and it is still
                 waiting for labels.

A model that predicts the score perfectly still says nothing about the
second question. Every docstring in this package is written so that nobody
reading it can come away believing otherwise.

LAYOUT
------

    features.py    session history  ->  strictly causal feature vectors
    models.py      the learners: gradient-boosted trees and ridge
    isolation.py   unsupervised per-user anomaly signal (isolation forest)
    coldstart.py   population prior -> personal baseline, with honest
                   confidence while the personal history is still thin
    training.py    the offline pipeline: split, fit, compare, calibrate
    validation.py  the guards that make the metrics trustworthy — leakage,
                   causality, ordering, feature-set identity
    evaluation.py  metrics, free baselines, conformal coverage checks
    explain.py     permutation importance and per-prediction attribution
    inference.py   serving: load, predict, refuse
    registry.py    versioned model files and their manifests
    manifest.py    the reproducibility record written beside every model

THE DEPENDENCY DECISION
-----------------------

Serving imports nothing outside the standard library. Training optionally
uses scikit-learn, and the reason it is optional rather than required is
recorded in `docs/ML.md` with the benchmark that decided it: on this data a
regularised linear model beats every tree method tried, hand-written or
imported, and the hand-written ridge matched scikit-learn's best to within
0.001 MAE. So scikit-learn is not here for accuracy. It is here as a
*validation oracle* — `tests/test_ml_against_sklearn.py` asserts that the
hand-written implementations agree with the reference ones — and for
permutation importance during training. Neither job is on the request path,
so `requirements.txt` stays six packages and a fresh clone still runs.

THE FALLBACK RULE
-----------------

Every entry point in this package returns None rather than a guess. No
model file, a model trained on a different feature set, a corrupt payload,
too little history — all of them refuse, and the caller falls back to the
deterministic pipeline that existed before any of this. A learned model
that silently degrades to guessing is worse than no model, because its
output is indistinguishable from a good one.
"""

from . import (  # noqa: F401
    coldstart,
    evaluation,
    explain,
    features,
    inference,
    isolation,
    manifest,
    models,
    registry,
    validation,
)

# The serving surface. Importing `ml` and calling these is the whole
# contract the API depends on; everything else is training-time.
from .inference import available, describe, predict  # noqa: F401

__all__ = ["available", "describe", "predict"]
