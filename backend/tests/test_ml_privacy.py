"""The privacy guarantee, asserted rather than documented.

The project's whole differentiator is that it measures *how* work was done
without ever seeing *what* was written. Adding a machine-learning layer is
the point at which that could quietly break, in three specific ways:

  1. a feature that encodes content (raw text, a URL, a document id, an
     ordered keystroke series rather than a histogram);
  2. an explanation string that surfaces something from the session body,
     since explanations are the one place model internals become prose;
  3. a model artefact that carries training text or per-user identifiers
     inside it, which would then travel with any exported model.

Every test here attacks one of those. They are deliberately written against
the emitted values rather than against the source, so a future refactor
that reintroduces content has to defeat the assertion rather than just move
the code.
"""
import json
import random

import pytest

from ml import explain, features, inference, isolation, manifest, models


# Substrings that must never appear in a feature name, an explanation, or a
# serialised model. Split between "content" (what was written) and "stats
# jargon" (which is a usability rule rather than a privacy one, but lives
# here because the same strings are being scanned).
CONTENT_MARKERS = (
    "text", "content", "body", "document", "doc_id", "url", "title",
    "keystroke", "keylog", "sequence", "transcript", "clipboard", "prose",
    "word_list", "ngram", "token",
)

JARGON = ("z-score", "z score", "sigma", "standard deviation", "p-value",
          "p value", "normal distribution", "variance", "quantile")


def session(i, score, started_at, regularity=0.4, typed=800, pasted=200):
    return {
        "session_id": f"s{i}", "user_id": "u1", "category": "writing",
        "started_at": started_at, "active_ms": 25 * 60_000,
        "typed_chars": typed, "pasted_chars": pasted, "backspace_count": 30,
        "revision_count": 4, "likely_ai_pastes": 0, "tab_switch_count": 0,
        "regularity": regularity, "score": score,
        # Deliberately planted. Nothing downstream should ever read these,
        # and if something starts to, these strings will surface.
        "secret_text": "the mitochondria is the powerhouse of the cell",
        "document_url": "https://docs.example.com/private-essay",
    }


def stream(scores, **kw):
    return [session(i, s, 1_700_000_000_000 + i * 86_400_000, **kw)
            for i, s in enumerate(scores)]


# ---------------------------------------------------------------------------
# 1. Features
# ---------------------------------------------------------------------------

def test_no_feature_name_refers_to_content():
    for name in features.FEATURE_NAMES:
        for marker in CONTENT_MARKERS:
            assert marker not in name.lower(), f"{name} looks like a content feature"


def test_no_anomaly_feature_name_refers_to_content():
    for name in isolation.ANOMALY_FEATURE_NAMES:
        for marker in CONTENT_MARKERS:
            assert marker not in name.lower()


def test_every_feature_is_a_finite_number_not_a_string():
    """A feature that could hold a string is a feature that could hold text."""
    vector = features.build_features(stream([70, 72, 68, 75]), False)
    for value in vector:
        assert isinstance(value, float)
        assert value == value and abs(value) != float("inf")


def test_planted_text_never_reaches_a_feature_vector():
    plain = features.build_features(stream([70, 72, 68, 75]), False)
    poisoned_rows = [dict(r, secret_text="X" * 5000, document_url="Y" * 5000)
                     for r in stream([70, 72, 68, 75])]
    assert features.build_features(poisoned_rows, False) == plain


# ---------------------------------------------------------------------------
# 2. Explanations
# ---------------------------------------------------------------------------

def _fit_ridge():
    rows = stream([60, 62, 64, 66, 68, 70, 72, 74, 76, 78])
    xs, ys, _g, _t = features.build_dataset(rows, horizon=1)
    return models.RidgeRegression().fit(xs, ys), xs


def test_a_local_explanation_contains_no_content_and_no_jargon():
    model, xs = _fit_ridge()
    for row in xs:
        attribution = explain.attribute(model, row)
        blob = json.dumps(attribution).lower()
        for marker in CONTENT_MARKERS:
            assert marker not in blob
        sentence = explain.sentence(attribution) or ""
        for term in JARGON:
            assert term not in sentence.lower()


def test_every_explanation_label_comes_from_the_fixed_dictionary():
    """There is no code path that formats a label from data, and this is
    what keeps it that way."""
    model, xs = _fit_ridge()
    allowed = set(features.FEATURE_LABELS.values())
    for row in xs[:20]:
        for term in explain.attribute(model, row)["terms"]:
            assert term["label"] in allowed


def test_the_behavioural_explanation_describes_process_not_product():
    verdict = {
        "status": "ok", "score": 0.9, "unusual": True, "n_reference": 8,
        "drivers": [{"feature": "paste_ratio", "deviation": 3.1,
                     "direction": "above"}],
    }
    text = isolation.explain(verdict)
    lowered = text.lower()
    for marker in CONTENT_MARKERS:
        assert marker not in lowered
    for term in JARGON:
        assert term not in lowered
    # And it must not imply anyone else looked at their work.
    assert "visible to you only" in lowered
    for phrase in ("other students", "your class", "compared to others",
                   "reported to", "your teacher"):
        assert phrase not in lowered


def test_no_explanation_is_emitted_when_nothing_is_unusual():
    assert isolation.explain({"status": "ok", "unusual": False}) is None
    assert isolation.explain({"status": "insufficient_data"}) is None


# ---------------------------------------------------------------------------
# 3. The model artefact
# ---------------------------------------------------------------------------

def test_a_serialised_model_carries_no_text_and_no_user_ids():
    rows = stream([60, 62, 64, 66, 68, 70, 72, 74, 76, 78])
    xs, ys, groups, _t = features.build_dataset(rows, horizon=1)
    payload = {
        "model": models.RidgeRegression().fit(xs, ys).to_dict(),
        "manifest": manifest.build(rows, xs, groups, horizon=1, seed=1,
                                   synthetic=True),
    }
    blob = json.dumps(payload)
    assert "powerhouse" not in blob
    assert "docs.example.com" not in blob
    # The manifest fingerprints the data; it must not contain the ids.
    assert "u1" not in blob
    for marker in CONTENT_MARKERS:
        assert marker not in blob.lower()


def test_the_isolation_forest_artefact_stores_thresholds_not_rows():
    rng = random.Random(2)
    points = [[rng.gauss(0, 1) for _ in range(isolation.ANOMALY_FEATURE_DIM)]
              for _ in range(300)]
    forest = isolation.IsolationForest(n_trees=10).fit(points)
    blob = json.dumps(forest.to_dict())
    # No session ids, no user ids, no strings at all beyond the schema keys.
    assert "s0" not in blob and "u1" not in blob
    assert "powerhouse" not in blob


def test_the_api_facing_description_leaks_nothing(tmp_path, monkeypatch):
    from ml import registry

    monkeypatch.setattr(registry, "MODEL_PATH", str(tmp_path / "absent.json"))
    inference.reset_cache()
    blob = json.dumps(inference.describe()).lower()
    for marker in CONTENT_MARKERS:
        assert marker not in blob
