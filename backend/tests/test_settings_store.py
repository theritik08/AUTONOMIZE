"""Server-side settings.

The interesting tests here are not the round-trips — they are the two ways
this feature can silently produce a lie:

  drift      the backend's idea of the settings shape diverging from the
             extension's, so a toggle saves successfully and changes
             nothing;
  echo       a UI showing the user the raw text they typed while the
             extension matches against a normalised form, so an excluded
             domain appears excluded and is still tracked.

Both are asserted directly.
"""
import json
import re
from pathlib import Path

import pytest

import db
import settings_store

BACKGROUND_JS = Path(__file__).parent.parent.parent / "extension" / "background.js"


# ---------------------------------------------------------------------------
# Drift — the failure a comment cannot prevent
# ---------------------------------------------------------------------------

def test_the_defaults_match_the_extensions_defaults_exactly():
    """settings_store.DEFAULTS mirrors background.js DEFAULT_SETTINGS.

    Parsed out of the JavaScript rather than trusted, because "keep these
    two in sync" is a comment that is wrong within a month, and the symptom
    is a settings screen that saves happily and changes nothing.
    """
    source = BACKGROUND_JS.read_text()
    match = re.search(r"const DEFAULT_SETTINGS = \{(.*?)\n\};", source, re.S)
    assert match, "DEFAULT_SETTINGS not found in background.js"
    block = match.group(1)

    # The tracking categories are the part that actually matters: a category
    # the backend accepts and the extension ignores is a dead toggle.
    tracking = re.search(r"tracking:\s*\{(.*?)\}", block, re.S).group(1)
    js_categories = set(re.findall(r"(\w+)\s*:", tracking))
    assert js_categories == set(settings_store.TRACKING_KEYS)
    assert js_categories == set(settings_store.DEFAULTS["tracking"])

    # And the top-level keys.
    js_keys = set(re.findall(r"^\s{2}(\w+):", block, re.M))
    assert js_keys == set(settings_store.DEFAULTS)


def test_the_extension_still_reads_settings_from_local_storage_on_the_hot_path():
    """The network must never be on the flush path.

    Settings became server-backed, and the tempting next step is to fetch
    them when a session flushes. That would make a dropped session the cost
    of a slow settings endpoint, which is a much worse trade than acting on
    a toggle that is up to fifteen minutes stale.
    """
    source = BACKGROUND_JS.read_text()
    body = re.search(r"async function getSettings\(\) \{(.*?)\n\}", source, re.S).group(1)
    assert "chrome.storage.local.get" in body
    assert "fetch" not in body


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("example.com", "example.com"),
    ("  Example.COM  ", "example.com"),
    ("https://example.com/", "example.com"),
    ("http://www.example.com/path?q=1", "example.com"),
    ("//example.com", "example.com"),
    ("example.com:8080", "example.com"),
    ("docs.google.com/document/d/abc", "docs.google.com"),
])
def test_domains_are_normalised_to_what_the_content_script_matches(raw, expected):
    out = settings_store.normalise({"excludedDomains": [raw]})
    assert out["excludedDomains"] == [expected]


@pytest.mark.parametrize("raw", ["", "   ", "not a hostname", "localhost", 42, None])
def test_entries_that_are_not_hostnames_are_dropped_not_stored(raw):
    """Storing them would show the user an exclusion that never matches."""
    out = settings_store.normalise({"excludedDomains": [raw, "keep.com"]})
    assert out["excludedDomains"] == ["keep.com"]


def test_duplicates_collapse_while_keeping_the_users_order():
    out = settings_store.normalise(
        {"excludedDomains": ["b.com", "https://A.com", "a.com", "b.com"]})
    assert out["excludedDomains"] == ["b.com", "a.com"]


def test_a_partial_update_leaves_everything_else_alone():
    """A panel that only renders tracking toggles must be able to save them
    without round-tripping a backendUrl it never displayed."""
    existing = {"backendUrl": "https://api.example.com",
                "tracking": {"ai_assistant": False, "writing": True,
                             "assessment": True},
                "excludedDomains": ["keep.com"]}
    out = settings_store.normalise({"tracking": {"writing": False}}, existing)
    assert out["backendUrl"] == "https://api.example.com"
    assert out["excludedDomains"] == ["keep.com"]
    assert out["tracking"] == {"ai_assistant": False, "writing": False,
                               "assessment": True}


def test_an_unknown_tracking_category_is_dropped():
    """The extension would ignore it; persisting it would make a typo look
    like a working feature until someone checked."""
    out = settings_store.normalise({"tracking": {"telepathy": True}})
    assert "telepathy" not in out["tracking"]


@pytest.mark.parametrize("bad", [
    {"backendUrl": ""},
    {"backendUrl": "ftp://example.com"},
    {"backendUrl": "example.com"},
    {"tracking": "yes"},
    {"excludedDomains": "example.com"},
])
def test_invalid_input_is_refused_with_a_message(bad):
    with pytest.raises(settings_store.SettingsError):
        settings_store.normalise(bad)


def test_too_many_excluded_domains_is_refused():
    many = [f"site{i}.com" for i in range(settings_store.MAX_EXCLUDED_DOMAINS + 1)]
    with pytest.raises(settings_store.SettingsError):
        settings_store.normalise({"excludedDomains": many})


def test_a_trailing_slash_on_the_backend_url_is_stripped():
    """Otherwise every request becomes host//api/score, which some servers
    route and some 404 — a difference that only appears in deployment."""
    out = settings_store.normalise({"backendUrl": "https://api.example.com/"})
    assert out["backendUrl"] == "https://api.example.com"


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def test_a_user_with_no_stored_settings_gets_the_defaults_not_a_failure(sqlite_conn):
    settings, updated_at = settings_store.load(sqlite_conn, "nobody")
    assert settings == settings_store.DEFAULTS
    assert updated_at is None


def test_saving_then_loading_returns_the_same_settings(sqlite_conn):
    saved, stamp = settings_store.save(
        sqlite_conn, "u1",
        {"tracking": {"writing": False}, "excludedDomains": ["Example.com"]})
    loaded, loaded_stamp = settings_store.load(sqlite_conn, "u1")
    assert loaded == saved
    assert loaded_stamp == stamp
    assert loaded["excludedDomains"] == ["example.com"]
    assert loaded["tracking"]["writing"] is False


def test_saving_twice_merges_rather_than_replacing(sqlite_conn):
    settings_store.save(sqlite_conn, "u1", {"excludedDomains": ["a.com"]})
    merged, _ = settings_store.save(sqlite_conn, "u1",
                                    {"tracking": {"assessment": False}})
    assert merged["excludedDomains"] == ["a.com"]
    assert merged["tracking"]["assessment"] is False


def test_the_timestamp_is_the_servers_not_the_clients(sqlite_conn):
    """A device with a wrong clock could otherwise pin its stale settings
    permanently by claiming a date in 2039."""
    _, stamp = settings_store.save(
        sqlite_conn, "u1", {"updated_at": 9_999_999_999_999,
                            "tracking": {"writing": False}})
    assert stamp < 9_999_999_999_999


def test_a_corrupt_row_falls_back_to_defaults_rather_than_erroring(sqlite_conn):
    """A settings screen must not be taken down by one bad row."""
    sqlite_conn.execute(
        "INSERT INTO user_settings (user_id, settings, updated_at) VALUES (?, ?, ?)",
        ("u1", "{not json", 1))
    settings, _ = settings_store.load(sqlite_conn, "u1")
    assert settings == settings_store.DEFAULTS


def test_settings_are_covered_by_export_and_erase(sqlite_conn):
    """They are user data — the row recording which sites someone chose to
    hide from tracking must not outlive the sessions it protected."""
    settings_store.save(sqlite_conn, "u1", {"excludedDomains": ["private.com"]})

    exported = db.export_user_data(sqlite_conn, "u1")
    assert "user_settings" in exported
    assert len(exported["user_settings"]) == 1
    assert "private.com" in json.dumps(exported["user_settings"])

    deleted = db.delete_user_data(sqlite_conn, "u1")
    assert deleted["user_settings"] == 1
    settings, _ = settings_store.load(sqlite_conn, "u1")
    assert settings == settings_store.DEFAULTS
