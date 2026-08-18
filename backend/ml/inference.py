r"""Serving. Load once, predict, and refuse whenever refusing is honest.

Training happens offline in `training.py` and writes a JSON file. This
module loads it and predicts, so the request path never imports the trainer
and a deployment that has never trained anything works unchanged.

THE FALLBACK IS THE POINT
-------------------------

Every function here returns None, or a dict whose `status` is not "ok",
whenever it cannot answer honestly: no model file, a model trained on a
different feature set, a corrupt payload, or too little history for the
features to be defined. The caller then falls back to `anomaly.forecast`,
which is the straight-line fit the model was built to improve on, and the
product behaves exactly as it did before any of this existed.

That ordering matters. A learned model that silently degrades to guessing
when its inputs are missing is worse than no model, because the number it
emits is indistinguishable from a good one. Refusing is the same rule every
other signal in this codebase follows, and `tests/test_ml_fallback.py`
asserts each refusal path individually rather than trusting this paragraph.

THE INTERVAL
------------

`interval_90` is a conformal radius computed on the held-out slice during
training — the (1-alpha) quantile of absolute residuals on data the model
never saw. Adding and subtracting it gives a prediction interval with
distribution-free 90% coverage: no assumption that the errors are normal,
or symmetric, or anything else. It is reported as a range rather than a
point because a bare number invites more confidence than a model with a
~6-point mean absolute error deserves.

WHY THE CACHE IS KEYED ON MTIME
-------------------------------

Retraining should take effect without a restart, and a test that writes a
model file must not be defeated by a stale cache. Both fall out of keying
on the file's modification time rather than loading once at import.
"""
import os

from . import coldstart, explain as explain_module, features, isolation, manifest, registry

_cache = {"path": None, "mtime": None, "payload": None, "model": None, "forest": None}


def _load():
    path = registry.MODEL_PATH
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        _cache.update(path=None, mtime=None, payload=None, model=None, forest=None)
        return None, None, None

    if _cache["path"] == path and _cache["mtime"] == mtime:
        return _cache["payload"], _cache["model"], _cache["forest"]

    payload, model, forest = registry.load(path)
    _cache.update(path=path, mtime=mtime, payload=payload, model=model, forest=forest)
    return payload, model, forest


def reset_cache():
    """Forces the next call to re-read from disk. For tests and retrains."""
    _cache.update(path=None, mtime=None, payload=None, model=None, forest=None)


def available() -> bool:
    payload, model, _forest = _load()
    return payload is not None and model is not None


def describe() -> dict:
    """What is loaded, for the startup log and the API's transparency block.

    When nothing is loaded this says *why*, because a deployment with a
    broken model file otherwise looks identical to one that has never
    trained — and those need different fixes.
    """
    payload, model, forest = _load()
    if not payload or not model:
        return {"available": False, "reason": registry.reason_unavailable()}

    summary = manifest.summarise(payload.get("manifest"))
    test_metrics = payload.get("test_metrics") or {}
    return {
        "available": True,
        "kind": payload["model"].get("kind"),
        "horizon": payload.get("horizon"),
        "test_mae": test_metrics.get("mae"),
        "beat_baseline_by_percent": payload.get("beat_baseline_by_percent"),
        "anomaly_model": bool(forest),
        # Carried through so no consumer can quote a metric without the
        # label that says whether it came from real students.
        "synthetic": summary.get("synthetic"),
        **summary,
    }


def predict(history, is_assessment=False, with_explanation=False):
    """Predicted mean score over the next `horizon` sessions, or None.

    `history` is that user's scored sessions in this category, oldest first.
    Returns None rather than a guess whenever the answer would not be
    trustworthy — see the module docstring.
    """
    payload, model, _forest = _load()
    if not payload or not model:
        return None

    vector = features.build_features(history, is_assessment)
    if vector is None:
        return None

    value = model.predict_one(vector)
    # Clamped because the target is a bounded 0-100 quantity and neither a
    # linear model nor a tree ensemble knows that. An unclamped -4 would be
    # shown to a student as a forecast.
    value = max(0.0, min(100.0, value))
    radius = payload.get("interval_90")

    out = {
        "predicted_score": round(value, 1),
        "horizon_sessions": payload.get("horizon", 1),
        "model": payload["model"].get("kind"),
        "source": "learned",
        "trained_on_rows": (payload.get("manifest") or {}).get("n_rows"),
        "synthetic_training_data": (payload.get("manifest") or {}).get("synthetic"),
    }
    if radius:
        out["interval_low"] = round(max(0.0, value - radius), 1)
        out["interval_high"] = round(min(100.0, value + radius), 1)
        out["interval_coverage"] = 0.9
        # The measured coverage on the held-out slice, beside the claimed
        # one. If these diverge the exchangeability assumption is failing
        # and the interval is narrower than it says.
        if payload.get("empirical_coverage") is not None:
            out["interval_coverage_measured"] = payload["empirical_coverage"]

    if with_explanation:
        attribution = explain_module.attribute(model, vector)
        out["explanation"] = {
            "method": attribution["method"],
            "note": attribution["note"],
            "terms": attribution["terms"],
            "sentence": explain_module.sentence(attribution),
        }

    return out


def behavioural_anomaly(history, row):
    """The isolation-forest signal for one session.

    Separate from `predict` because it answers a different question — is
    this session's *shape* unusual, rather than where is this student
    heading — and because a deployment can perfectly well have one model
    and not the other.
    """
    _payload, _model, forest = _load()
    return isolation.assess(forest, history, row)


def cold_start(personal_mean, n_observations, category="writing"):
    """The empirical-Bayes blend and its honest confidence labels.

    Works with no model file at all: without a trained prior it returns the
    personal mean unchanged with `source: personal_only`, which is exactly
    the behaviour the product had before this module. The blend is a bonus
    when a prior exists, never a dependency.
    """
    payload, _model, _forest = _load()
    prior = ((payload or {}).get("population_prior") or {}).get(category)
    blended = coldstart.shrink(personal_mean, n_observations, prior)
    state = coldstart.readiness(n_observations, blended)
    return {
        **state,
        "estimate": blended["estimate"],
        "personal_weight": blended["personal_weight"],
        "population_mean": blended["population_mean"],
        "source": blended["source"],
        "message": coldstart.explain(state),
    }


def global_importance(limit=6):
    """The permutation-importance ranking recorded at training time.

    Read from the model file rather than recomputed, because permutation
    importance needs the held-out set and the held-out set does not exist
    at request time.
    """
    payload, _model, _forest = _load()
    ranking = (payload or {}).get("permutation_importance") or []
    return [
        {
            "feature": entry["feature"],
            "label": features.FEATURE_LABELS.get(entry["feature"], entry["feature"]),
            "mae_increase": entry["mae_increase"],
            "significant": entry.get("significant", False),
        }
        for entry in ranking[:limit]
    ]
