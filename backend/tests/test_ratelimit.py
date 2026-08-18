"""Tests for the write-endpoint rate limiter.

Time is injected rather than slept through, so the window-expiry test
takes microseconds instead of a minute — a test suite nobody wants to run
is a test suite that stops being run.
"""
import importlib

import pytest

import ratelimit


@pytest.fixture()
def limiter(monkeypatch):
    """A limiter configured to 3 requests per 10 seconds."""
    monkeypatch.setattr(ratelimit, "ENABLED", True)
    monkeypatch.setattr(ratelimit, "MAX_REQUESTS", 3)
    monkeypatch.setattr(ratelimit, "WINDOW_SECONDS", 10.0)
    ratelimit.reset()
    yield ratelimit
    ratelimit.reset()


def test_disabled_by_default_allows_everything():
    # The default local-dev experience must be completely unchanged.
    ratelimit.reset()
    assert ratelimit.ENABLED is False
    assert all(ratelimit.check("anyone") for _ in range(1000))


def test_allows_up_to_the_limit(limiter):
    assert [limiter.check("a", now=0) for _ in range(3)] == [True, True, True]


def test_blocks_past_the_limit(limiter):
    for _ in range(3):
        limiter.check("a", now=0)
    assert limiter.check("a", now=0) is False


def test_window_slides_so_the_limit_recovers(limiter):
    for _ in range(3):
        limiter.check("a", now=0)
    assert limiter.check("a", now=5) is False       # still inside the window
    assert limiter.check("a", now=10.1) is True     # first hits have aged out


def test_clients_are_limited_independently(limiter):
    for _ in range(3):
        limiter.check("a", now=0)
    assert limiter.check("a", now=0) is False
    # One noisy client must not lock everyone else out.
    assert limiter.check("b", now=0) is True


def test_key_tracking_is_bounded(limiter, monkeypatch):
    # An attacker rotating keys must not be able to turn the limiter into
    # the memory leak it exists to prevent.
    monkeypatch.setattr(ratelimit, "MAX_TRACKED_KEYS", 50)
    for i in range(500):
        limiter.check(f"client-{i}", now=0)
    assert len(ratelimit._hits) <= 50


def test_malformed_config_raises_rather_than_silently_disabling(monkeypatch):
    # The dangerous failure mode is thinking you're protected when you
    # aren't, so a bad value must fail loudly at startup.
    monkeypatch.setenv("AUTONOMIZE_RATE_LIMIT", "not-a-number/60")
    with pytest.raises(ValueError):
        importlib.reload(ratelimit)
    monkeypatch.delenv("AUTONOMIZE_RATE_LIMIT", raising=False)
    importlib.reload(ratelimit)


def test_config_parsing(monkeypatch):
    monkeypatch.setenv("AUTONOMIZE_RATE_LIMIT", "120/60")
    importlib.reload(ratelimit)
    assert ratelimit.ENABLED is True
    assert ratelimit.MAX_REQUESTS == 120
    assert ratelimit.WINDOW_SECONDS == 60.0
    assert "120 requests" in ratelimit.describe()

    monkeypatch.delenv("AUTONOMIZE_RATE_LIMIT", raising=False)
    importlib.reload(ratelimit)
    assert ratelimit.ENABLED is False
    assert ratelimit.describe() == "off"
