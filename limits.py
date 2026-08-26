"""
Two ceilings: how often a caller may ask, and how much evidence may leave.

Both are guardrails against a loop that got away, not billing controls. The
distinction matters enough to state at the top: if money is at stake, set a
spend cap with your provider. This process can refuse to start work, and that
is all it can do -- it cannot claw back a request already in flight, and a
restart clears both windows.

## Why two, and why they are not the same control

**Investigations per caller** bounds work. A diagnosis takes tens of seconds
of model time and a handful of cluster reads, so a script in a retry loop is
expensive whatever the inference mode. The ceiling is per principal, which is
the thing identity.py exists to establish -- a shared bucket would let one
runaway client lock out the person trying to diagnose the incident it caused.

**External tokens** bounds spend, and applies only where evidence actually
leaves the network. Local inference costs nothing per token, so a ceiling
there is arbitrary friction; `mode: api` is where a runaway loop shows up on
an invoice. This is deliberately counted in **tokens, not currency**. A price
table per model goes stale, and this project does not ship numbers it cannot
check -- the provider reports tokens and they are exact.

## The window

A sliding window over timestamps rather than a fixed bucket. A fixed hourly
bucket lets a caller spend the whole allowance in the last minute of one hour
and the whole allowance again in the first minute of the next, which is twice
the ceiling across two minutes and is the failure mode the ceiling exists to
prevent.

## What a restart does

Clears both. In-memory on purpose: one replica is the documented design (see
docs/RUNBOOK.md), and putting this in the state file would mean a schema
migration on an existing database for a counter whose worst case is bounded by
the window length. A process restarting in a loop is not running
investigations, so the exposure is a restart mid-window, which costs at most
one window's allowance.
"""

import collections
import logging
import os
import threading
import time

log = logging.getLogger("triage.limits")

# Generous by design: at the recorded 41s median a single process cannot run
# much more than 88 investigations an hour serially, so this cannot bite a
# person working an incident. It bites a retry loop, which is the point.
DEFAULT_PER_HOUR = 60

WINDOW_SECONDS = 3600


def _int(name, default):
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(
            f"{name}={raw!r} is not a number. Refusing to guess: a typo in a "
            "ceiling would silently remove it."
        )
    if value < 0:
        raise ValueError(f"{name}={value} is negative; use 0 for unlimited.")
    return value


def per_hour():
    """Investigations per principal per hour. 0 disables the ceiling."""
    return _int("TRIAGE_MAX_INVESTIGATIONS_PER_HOUR", DEFAULT_PER_HOUR)


def token_budget():
    """
    External inference tokens per hour. 0 disables it, and that is the default.

    No defensible default exists: the right number is whatever the deployment
    is willing to spend, and a figure invented here would either be so high it
    protects nothing or so low it breaks a working install on upgrade.
    """
    return _int("TRIAGE_MAX_EXTERNAL_TOKENS_PER_HOUR", 0)


class Window:
    """
    A sliding window of (timestamp, amount) pairs, per key.

    Kept as a deque and trimmed on read, so a key nobody asks about costs
    nothing and a key under load is trimmed exactly when it is consulted.
    """

    def __init__(self, seconds=WINDOW_SECONDS):
        self.seconds = seconds
        self._events = collections.defaultdict(collections.deque)
        self._lock = threading.Lock()

    def _trim(self, key, now):
        events = self._events[key]
        cutoff = now - self.seconds
        while events and events[0][0] <= cutoff:
            events.popleft()
        return events

    def add(self, key, amount=1, now=None):
        now = time.time() if now is None else now
        with self._lock:
            self._trim(key, now).append((now, amount))

    def total(self, key, now=None):
        now = time.time() if now is None else now
        with self._lock:
            return sum(amount for _, amount in self._trim(key, now))

    def retry_after(self, key, limit, now=None):
        """
        Seconds until the window has room, or 0 if it already does.

        Derived from the oldest event rather than from the window length: a
        caller told to wait an hour when the window frees up in ninety seconds
        will either give up or hammer, and both are worse than the truth.
        """
        now = time.time() if now is None else now
        with self._lock:
            events = self._trim(key, now)
            running = sum(amount for _, amount in events)
            if running < limit:
                return 0
            # Drop the oldest events until one more unit would fit.
            for when, amount in events:
                running -= amount
                if running < limit:
                    return max(int(when + self.seconds - now) + 1, 1)
            return max(int(self.seconds), 1)

    def reset(self):
        with self._lock:
            self._events.clear()


class Refused(Exception):
    """A ceiling was reached. Carries what to tell the caller."""

    def __init__(self, reason, retry_after):
        super().__init__(reason)
        self.reason = reason
        self.retry_after = retry_after


_INVESTIGATIONS = Window()
_TOKENS = Window()

# One key: the budget is on what leaves the network in total, not per caller.
# A per-caller token ceiling would let N callers each spend the maximum.
_EXTERNAL = "external"


def check(principal="anonymous"):
    """
    Whether an investigation may start. Raises Refused if not.

    Checked before the work rather than during it, because the only thing this
    process can do about a ceiling is decline to begin -- once a model call is
    in flight the tokens are already committed.
    """
    ceiling = per_hour()
    if ceiling:
        wait = _INVESTIGATIONS.retry_after(principal, ceiling)
        if wait:
            raise Refused(
                f"{ceiling} investigations per hour per caller; try again in "
                f"{wait}s",
                wait,
            )

    budget = token_budget()
    if budget:
        wait = _TOKENS.retry_after(_EXTERNAL, budget)
        if wait:
            raise Refused(
                f"the hourly budget of {budget} external inference tokens is "
                f"spent; try again in {wait}s",
                wait,
            )


def record(principal="anonymous"):
    """One investigation started, against this caller's allowance."""
    if per_hour():
        _INVESTIGATIONS.add(principal)


def record_tokens(count, external):
    """
    Tokens the provider reported, counted only when they left the network.

    Called from inference._count_tokens, which is where the provider's usage
    is already being read. Local tokens are deliberately not counted: nothing
    is spent, and a ceiling on them would be friction with nothing behind it.
    """
    if external and count and token_budget():
        _TOKENS.add(_EXTERNAL, count)


def spent():
    """External tokens in the current window, for a status line."""
    return _TOKENS.total(_EXTERNAL)


def describe():
    """The posture, for a startup log and for /inference."""
    return {
        "investigations_per_hour": per_hour() or None,
        "external_tokens_per_hour": token_budget() or None,
        "external_tokens_spent": spent() if token_budget() else None,
    }


def startup_warning():
    """What to say at startup, or None."""
    if per_hour():
        return None
    return (
        "TRIAGE_MAX_INVESTIGATIONS_PER_HOUR is 0: there is no ceiling on how "
        "often a caller may drive an investigation. A retry loop will spend "
        "model time and cluster reads until someone notices."
    )


def reset():
    """Forget every window. For tests, and for a configuration reload."""
    _INVESTIGATIONS.reset()
    _TOKENS.reset()
