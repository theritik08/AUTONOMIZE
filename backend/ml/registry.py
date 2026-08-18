r"""Versioned model files: writing them, finding them, refusing them.

WHY A REGISTRY RATHER THAN A FILE PATH
--------------------------------------

`model.json` on its own has three problems. A retrain overwrites the
previous model with no way back, so a regression cannot be diagnosed by
comparing artefacts. There is nowhere to record what produced it. And a
loader that reads whatever is at that path will happily interpret a model
trained on a different feature set, because JSON does not object.

This module writes every trained model to `models/model-<timestamp>-<hash>.json`
with its manifest embedded, and points `model.json` at the newest one by
copying it. Old versions stay on disk, which costs a few hundred kilobytes
and buys the ability to answer "what changed between these two runs".

THE REFUSAL PATH IS THE IMPORTANT PATH
--------------------------------------

`load()` returns None, never an exception and never a partially valid
model, for: a missing file, malformed JSON, a manifest that fails
`manifest.compatible`, an unknown model kind, or a payload missing anything
the predictor needs. Callers treat None as "fall back to the deterministic
pipeline", which is the behaviour the system had before any model existed
and is therefore known to work.
"""
import json
import os
import shutil

from . import manifest as manifest_module
from . import models as model_module

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The file the serving path reads. Overridable so tests can point at a
# temporary directory without touching the repository.
MODEL_PATH = os.environ.get(
    "AUTONOMIZE_MODEL_PATH", os.path.join(BACKEND_DIR, "model.json")
)

# Where every version is archived.
MODEL_DIR = os.environ.get(
    "AUTONOMIZE_MODEL_DIR", os.path.join(BACKEND_DIR, "models")
)


def version_name(manifest):
    """A filename that sorts chronologically and names its own data."""
    stamp = manifest.get("trained_at", 0)
    fingerprint = manifest.get("data_fingerprint", "unknown")[:8]
    return f"model-{stamp}-{fingerprint}.json"


def save(payload, model_path=None, model_dir=None):
    """Archives the model and points the serving path at it.

    Returns the archived path. The current-model file is written by copy
    rather than symlink so it survives being checked out on a filesystem
    that does not do symlinks, and so a deployment that ships only
    `model.json` still works.
    """
    model_path = model_path or MODEL_PATH
    model_dir = model_dir or MODEL_DIR
    os.makedirs(model_dir, exist_ok=True)

    archived = os.path.join(model_dir, version_name(payload.get("manifest") or {}))
    # Written to a temporary name and moved into place: a reader that opens
    # the file midway through a write would otherwise see truncated JSON,
    # and `load` would report a corrupt model that is in fact fine.
    temporary = archived + ".partial"
    with open(temporary, "w") as handle:
        json.dump(payload, handle, separators=(",", ":"))
    os.replace(temporary, archived)

    shutil.copyfile(archived, model_path)
    return archived


def list_versions(model_dir=None):
    """Every archived model, newest first."""
    model_dir = model_dir or MODEL_DIR
    try:
        names = [n for n in os.listdir(model_dir)
                 if n.startswith("model-") and n.endswith(".json")]
    except OSError:
        return []
    return sorted(names, reverse=True)


def _validate(payload):
    """Everything that must be true before a payload may be served."""
    if not isinstance(payload, dict):
        raise ValueError("model payload is not an object")

    ok, reason = manifest_module.compatible(payload.get("manifest"))
    if not ok:
        raise ValueError(reason)

    if "model" not in payload:
        raise ValueError("payload has no model")

    model = model_module.load_model(payload["model"])

    forest = None
    if payload.get("isolation_forest"):
        from . import isolation

        forest = isolation.IsolationForest.from_dict(payload["isolation_forest"])

    return model, forest


def load(path=None):
    """Reads and validates a model file. Returns (payload, model, forest).

    Any failure yields (None, None, None). See the module docstring for why
    that is a return value rather than an exception.
    """
    path = path or MODEL_PATH
    try:
        with open(path) as handle:
            payload = json.load(handle)
        model, forest = _validate(payload)
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None, None, None
    return payload, model, forest


def reason_unavailable(path=None):
    """Why the model at `path` is not being used, in one sentence.

    Exists so that a deployment with a broken model file does not look
    identical to one that has simply never trained. Shown in the startup
    log and in `/api/score`'s transparency block.
    """
    path = path or MODEL_PATH
    if not os.path.exists(path):
        return "no model has been trained yet"
    try:
        with open(path) as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return "the model file is unreadable or not valid JSON"
    try:
        _validate(payload)
    except ValueError as error:
        return str(error)
    return None
