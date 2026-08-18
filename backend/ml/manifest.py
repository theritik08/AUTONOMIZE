r"""The reproducibility record written beside every model.

A model file with no manifest is an unfalsifiable artefact: six months
later nobody can say what data produced it, whether the feature definitions
have moved since, or whether re-running the pipeline would give the same
answer. In a viva that is the difference between "here is my result" and
"here is my result and here is how you would check it".

Every field below exists to answer one specific question a reviewer can
reasonably ask.

    feature_set_hash   have the feature definitions changed since training?
    data_fingerprint   was this trained on the data I think it was?
    n_rows / n_users   on how much, and how much of it is independent?
    seed               would re-running give the same model?
    library_versions   is the environment the same one?
    python_version     ditto, for the interpreter
    git_commit         which state of the code produced this?
    horizon            what question was it trained to answer?
    synthetic          IS THIS A REAL RESULT OR A DEMO?

The last one is not decoration. This project has no pilot cohort, so every
number it can currently produce about model accuracy comes from simulated
students (`simulate_history.py`). Recording that inside the artefact means
a metric cannot be quoted later without the label travelling with it, which
is the difference between a demonstration and a fabricated result.
"""
import hashlib
import os
import subprocess
import sys
import time

from . import features as feature_module
from . import models as model_module


def data_fingerprint(rows):
    """A short hash of exactly which sessions were used.

    Built from session ids and scores rather than a row count, because two
    different datasets of the same size are the case worth catching. Sorted
    first so the fingerprint does not depend on the order the database
    happened to return rows in.
    """
    digest = hashlib.sha256()
    # `rows` can be sqlite3.Row or psycopg dict rows depending on the
    # backend, and only one of those has `.get`. Indexing works on both.
    parts = sorted(
        f"{r['session_id']}:{r['score']}" for r in rows if r["score"] is not None
    )
    for part in parts:
        digest.update(part.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()[:16]


def _library_versions():
    """Versions of anything that could change a fitted number.

    scikit-learn is listed when present because it is used as the
    validation oracle — if a future run disagrees with it, knowing which
    version disagreed is the first thing anyone would want.
    """
    versions = {}
    for name in ("scikit-learn", "numpy", "scipy", "joblib", "pandas"):
        try:
            from importlib import metadata

            versions[name] = metadata.version(name)
        except Exception:
            versions[name] = None
    return versions


def _git_commit():
    """The commit that produced this model, or None outside a checkout."""
    try:
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=here, capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0:
            return out.stdout.strip() or None
    except Exception:
        return None
    return None


def build(rows, xs, groups, horizon, seed, synthetic, notes=None):
    """The manifest for a training run about to be saved."""
    return {
        "manifest_version": 1,
        "model_format_version": model_module.MODEL_FORMAT_VERSION,
        "feature_set_hash": feature_module.feature_set_hash(),
        "feature_names": list(feature_module.FEATURE_NAMES),
        "data_fingerprint": data_fingerprint(rows),
        "n_source_sessions": len(rows),
        "n_rows": len(xs),
        "n_users": len(set(groups)),
        "horizon": horizon,
        "seed": seed,
        "python_version": sys.version.split()[0],
        "library_versions": _library_versions(),
        "git_commit": _git_commit(),
        "trained_at": int(time.time() * 1000),
        # The single most important field in the file.
        "synthetic": bool(synthetic),
        "notes": notes or (
            "Trained on simulated student histories. No real pilot cohort "
            "exists; these metrics demonstrate that the pipeline works, not "
            "that the construct is valid."
            if synthetic else
            "Trained on collected session history."
        ),
    }


def compatible(manifest):
    """Is a stored model still safe to interpret with today's code?

    Returns (ok, reason). Only two things are hard incompatibilities: a
    changed feature contract, which silently re-points every coefficient at
    a different quantity, and a changed serialisation format, which the
    loader would misread. Everything else is metadata for a human.
    """
    if not manifest:
        # A model with no manifest predates this system. Refused rather
        # than trusted: the whole point is that unlabelled artefacts cannot
        # be verified.
        return False, "no manifest — cannot verify what this model was trained on"
    if manifest.get("model_format_version") != model_module.MODEL_FORMAT_VERSION:
        return False, (
            f"model format {manifest.get('model_format_version')} but this code "
            f"reads {model_module.MODEL_FORMAT_VERSION}"
        )
    if manifest.get("feature_set_hash") != feature_module.feature_set_hash():
        return False, ("the feature definitions have changed since this model "
                       "was trained")
    return True, "ok"


def summarise(manifest):
    """The short form the API and the startup log show."""
    if not manifest:
        return {"available": False}
    return {
        "trained_at": manifest.get("trained_at"),
        "rows": manifest.get("n_rows"),
        "users": manifest.get("n_users"),
        "horizon": manifest.get("horizon"),
        "synthetic": manifest.get("synthetic"),
        "git_commit": manifest.get("git_commit"),
        "data_fingerprint": manifest.get("data_fingerprint"),
    }
