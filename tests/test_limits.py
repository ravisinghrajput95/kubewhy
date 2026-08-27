"""
The two ceilings: how often a caller may ask, and how much evidence may leave.

The window arithmetic is where this goes wrong quietly, so most of these are
about the window rather than about the plumbing. A ceiling that lets twice the
allowance through across a boundary is worse than no ceiling, because it is
believed.
"""

import pytest

import limits


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for name in ("TRIAGE_MAX_INVESTIGATIONS_PER_HOUR",
                 "TRIAGE_MAX_EXTERNAL_TOKENS_PER_HOUR"):
        monkeypatch.delenv(name, raising=False)
    limits.reset()
    yield
    limits.reset()


class TestTheWindowSlides:
    def test_events_inside_the_window_count(self):
        window = limits.Window(seconds=100)
        window.add("k", now=1000)
        window.add("k", now=1050)

        assert window.total("k", now=1060) == 2

    def test_events_older_than_the_window_do_not(self):
        window = limits.Window(seconds=100)
        window.add("k", now=1000)
        window.add("k", now=1150)

        assert window.total("k", now=1160) == 1

    def test_it_is_sliding_not_a_fixed_bucket(self):
        """
        The failure a fixed hourly bucket has: spend the whole allowance in
        the last minute of one hour and the whole allowance again in the
        first minute of the next, which is twice the ceiling across two
        minutes.
        """
        window = limits.Window(seconds=100)
        for _ in range(5):
            window.add("k", now=1099)

        # One second later a fixed bucket would have rolled over. This has not.
        assert window.total("k", now=1100) == 5
        assert window.retry_after("k", limit=5, now=1100) > 0

    def test_an_event_exactly_one_window_old_is_outside_it(self):
        """
        The boundary, which nothing exercised until mutation testing pointed
        at it. The window is half-open: an event exactly `seconds` old has
        left. Swapping the index in _trim's comparison produced a mutant that
        kept it, and every existing test agreed with both versions.
        """
        window = limits.Window(seconds=10)
        window.add("k", now=1000)

        assert window.total("k", now=1009) == 1
        assert window.total("k", now=1010) == 0

    def test_keys_do_not_share_an_allowance(self):
        window = limits.Window(seconds=100)
        window.add("alice", now=1000)

        assert window.total("bob", now=1000) == 0

    def test_amounts_are_summed_not_counted(self):
        """Tokens arrive in lumps; an event is not one unit."""
        window = limits.Window(seconds=100)
        window.add("k", amount=500, now=1000)
        window.add("k", amount=300, now=1001)

        assert window.total("k", now=1002) == 800


class TestRetryAfterTellsTheTruth:
    def test_room_in_the_window_means_no_wait(self):
        window = limits.Window(seconds=100)
        window.add("k", now=1000)

        assert window.retry_after("k", limit=5, now=1000) == 0

    def test_the_wait_is_until_the_oldest_event_expires(self):
        """
        Not the window length. A caller told to wait an hour when the window
        frees up in ninety seconds will either give up or hammer.
        """
        window = limits.Window(seconds=100)
        window.add("k", now=1000)
        window.add("k", now=1090)

        # At t=1095 the limit of 2 is reached; the 1000 event leaves at 1100.
        assert window.retry_after("k", limit=2, now=1095) == 6

    def test_the_wait_clears_every_event_over_the_ceiling_not_just_one(self):
        """
        Two events against a ceiling of one: dropping only the oldest still
        leaves one, which is not below the limit. The wait has to run to the
        newest event's expiry, not the oldest's.

        Mutation testing found this: `running < limit` relaxed to `<=` inside
        the loop returned 10 instead of 11 and no test noticed.
        """
        window = limits.Window(seconds=10)
        window.add("k", now=1000)
        window.add("k", now=1001)

        assert window.retry_after("k", limit=1, now=1001) == 11

    def test_the_wait_rounds_up_rather_than_down(self):
        """
        A wait rounded down lands the caller back inside the window and into
        another 429. Mutation testing separated `+ 1` from `- 1` here: with a
        one-second window and the event just placed, the honest answer is 2,
        and every test agreed with 1.
        """
        window = limits.Window(seconds=1)
        window.add("k", now=1000)

        assert window.retry_after("k", limit=1, now=1000) == 2

    def test_a_sub_second_remainder_still_rounds_up_to_a_whole_second(self):
        """
        The truncation case. int() of 0.5 is 0, so the wait is the +1 alone --
        and the floor underneath it must not inflate that to 2, or every
        near-expiry caller is told to wait twice as long as it needs.

        This one needed fractional timestamps to find: 7280 integer-time
        scenarios could not separate the mutant.
        """
        window = limits.Window(seconds=10)
        window.add("k", now=1000)

        assert window.retry_after("k", limit=1, now=1009.5) == 1

    def test_a_wait_is_never_zero_when_the_ceiling_is_reached(self):
        """Zero would tell a caller to retry immediately, into another 429."""
        window = limits.Window(seconds=100)
        window.add("k", now=1000)

        assert window.retry_after("k", limit=1, now=1099) >= 1


class TestTheInvestigationCeiling:
    def test_there_is_a_default(self, monkeypatch):
        """
        Generous rather than absent: at the recorded 41s median one process
        cannot run much past 88 an hour serially, so this cannot bite a person
        working an incident.
        """
        assert limits.per_hour() == 60

    def test_it_refuses_past_the_ceiling(self, monkeypatch):
        monkeypatch.setenv("TRIAGE_MAX_INVESTIGATIONS_PER_HOUR", "2")
        limits.check("sre@example.com"); limits.record("sre@example.com")
        limits.check("sre@example.com"); limits.record("sre@example.com")

        with pytest.raises(limits.Refused) as refused:
            limits.check("sre@example.com")

        assert refused.value.retry_after > 0
        assert "2 investigations per hour" in refused.value.reason

    def test_one_callers_loop_does_not_lock_out_another(self, monkeypatch):
        """
        Per principal, which is what identity.py exists to establish. A shared
        bucket would let a runaway client lock out the person trying to
        diagnose the incident it caused.
        """
        monkeypatch.setenv("TRIAGE_MAX_INVESTIGATIONS_PER_HOUR", "1")
        limits.check("robot"); limits.record("robot")

        limits.check("sre@example.com")          # raises if it does not

    def test_zero_disables_it(self, monkeypatch):
        monkeypatch.setenv("TRIAGE_MAX_INVESTIGATIONS_PER_HOUR", "0")
        for _ in range(200):
            limits.check("sre"); limits.record("sre")

    def test_a_typo_raises_rather_than_removing_the_ceiling(self, monkeypatch):
        monkeypatch.setenv("TRIAGE_MAX_INVESTIGATIONS_PER_HOUR", "sixty")
        with pytest.raises(ValueError, match="not a number"):
            limits.per_hour()

    def test_a_negative_ceiling_is_refused(self, monkeypatch):
        monkeypatch.setenv("TRIAGE_MAX_INVESTIGATIONS_PER_HOUR", "-1")
        with pytest.raises(ValueError, match="negative"):
            limits.per_hour()


class TestTheExternalTokenBudget:
    def test_it_is_off_by_default(self):
        """
        No defensible default exists — the right number is whatever the
        deployment will spend.
        """
        assert limits.token_budget() == 0

    def test_local_tokens_are_not_counted(self, monkeypatch):
        """
        Nothing is spent, so a ceiling on them is friction with nothing behind
        it. This is the assertion that keeps the budget meaning "money".
        """
        monkeypatch.setenv("TRIAGE_MAX_EXTERNAL_TOKENS_PER_HOUR", "1000")
        limits.record_tokens(5000, external=False)

        assert limits.spent() == 0
        limits.check("sre")                       # raises if it counted them

    def test_external_tokens_are_counted(self, monkeypatch):
        monkeypatch.setenv("TRIAGE_MAX_EXTERNAL_TOKENS_PER_HOUR", "1000")
        limits.record_tokens(600, external=True)

        assert limits.spent() == 600

    def test_a_spent_budget_refuses_the_next_investigation(self, monkeypatch):
        monkeypatch.setenv("TRIAGE_MAX_EXTERNAL_TOKENS_PER_HOUR", "1000")
        limits.record_tokens(1000, external=True)

        with pytest.raises(limits.Refused) as refused:
            limits.check("sre")

        assert "external inference tokens" in refused.value.reason

    def test_the_budget_is_shared_not_per_caller(self, monkeypatch):
        """
        A per-caller token ceiling would let N callers each spend the maximum,
        which is not a budget.
        """
        monkeypatch.setenv("TRIAGE_MAX_EXTERNAL_TOKENS_PER_HOUR", "100")
        limits.record_tokens(100, external=True)

        with pytest.raises(limits.Refused):
            limits.check("someone-else")

    def test_nothing_is_counted_when_the_budget_is_off(self, monkeypatch):
        limits.record_tokens(5000, external=True)
        assert limits.spent() == 0


class TestThePosture:
    def test_describe_reports_both_ceilings(self, monkeypatch):
        monkeypatch.setenv("TRIAGE_MAX_INVESTIGATIONS_PER_HOUR", "10")
        monkeypatch.setenv("TRIAGE_MAX_EXTERNAL_TOKENS_PER_HOUR", "999")
        limits.record_tokens(12, external=True)

        assert limits.describe() == {
            "investigations_per_hour": 10,
            "external_tokens_per_hour": 999,
            "external_tokens_spent": 12,
        }

    def test_an_absent_ceiling_reads_as_null_not_zero(self, monkeypatch):
        """Zero would read as "no investigations allowed", which is the opposite."""
        monkeypatch.setenv("TRIAGE_MAX_INVESTIGATIONS_PER_HOUR", "0")
        assert limits.describe()["investigations_per_hour"] is None

    def test_startup_warns_when_there_is_no_ceiling(self, monkeypatch):
        monkeypatch.setenv("TRIAGE_MAX_INVESTIGATIONS_PER_HOUR", "0")
        assert "retry loop" in limits.startup_warning()

    def test_no_warning_when_a_ceiling_is_set(self):
        assert limits.startup_warning() is None


def test_the_window_is_an_hour_because_every_message_says_so():
    """
    The ceilings are named "per hour", the refusals say "per hour", and the
    documented defaults are reasoned about per hour. Nothing pinned the
    constant, so the window could have drifted away from every sentence
    describing it. Found by mutation testing, which is the only thing that
    would notice a constant nobody asserts.
    """
    assert limits.WINDOW_SECONDS == 3600
    assert limits.Window().seconds == 3600
