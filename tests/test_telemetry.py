"""
Tests for the metrics registry.

Mostly about exposition format, because that is the part with no feedback
loop: a counter that increments wrongly shows up the first time someone reads
it, while a histogram whose buckets are not cumulative renders, scrapes, and
produces a quantile that is simply wrong.
"""

import pytest

import telemetry


@pytest.fixture(autouse=True)
def clean():
    telemetry.reset()
    yield
    telemetry.reset()


class TestCounter:
    def test_it_counts_per_label_set(self):
        counter = telemetry.Counter("c", "help", ("a",))
        counter.inc(a="x")
        counter.inc(a="x")
        counter.inc(a="y")

        assert dict(counter.values) == {("x",): 2, ("y",): 1}

    def test_label_order_does_not_create_a_second_series(self):
        counter = telemetry.Counter("c", "help", ("a", "b"))
        counter.inc(a="1", b="2")
        counter.inc(b="2", a="1")

        assert len(counter.values) == 1

    def test_a_partial_label_set_is_refused(self):
        """
        Not filled in with a default. A metric emitted with a missing label
        produces a second time series that looks like a different thing, and
        the two then disagree in a dashboard for reasons nobody can find.
        """
        counter = telemetry.Counter("c", "help", ("a", "b"))

        with pytest.raises(KeyError):
            counter.inc(a="1")


class TestHistogram:
    def test_buckets_are_cumulative(self):
        """
        `le="5"` means "at most 5 seconds", not "between 2.5 and 5". Getting
        this wrong renders and scrapes cleanly and produces wrong quantiles.
        """
        hist = telemetry.Histogram("h", "help", (), buckets=(1, 10, 100))
        for value in (0.5, 5, 50):
            hist.observe(value)

        samples = {labels["le"]: value
                   for name, labels, value in hist.samples()
                   if name.endswith("_bucket")}

        assert samples == {"1": 1, "10": 2, "100": 3, "+Inf": 3}

    def test_sum_and_count_are_reported(self):
        hist = telemetry.Histogram("h", "help", ())
        hist.observe(2.0)
        hist.observe(3.0)

        by_name = {name: value for name, _, value in hist.samples()}

        assert by_name["h_sum"] == 5.0
        assert by_name["h_count"] == 2

    def test_an_observation_above_every_bucket_still_counts(self):
        """
        The one that matters here: a model call that hit its 300s timeout is
        above every finite bucket, and +Inf is the only place it lands.
        """
        hist = telemetry.Histogram("h", "help", (), buckets=(1, 10))
        hist.observe(600)

        samples = {labels["le"]: value
                   for name, labels, value in hist.samples()
                   if name.endswith("_bucket")}

        assert samples == {"1": 0, "10": 0, "+Inf": 1}

    def test_bucket_edges_are_written_as_prometheus_writes_them(self):
        hist = telemetry.Histogram("h", "help", (), buckets=(0.5, 1, 30))
        hist.observe(0.1)

        edges = [labels["le"] for name, labels, _ in hist.samples()
                 if name.endswith("_bucket")]

        assert edges == ["0.5", "1", "30", "+Inf"]


class TestExposition:
    def test_every_metric_declares_help_and_type(self):
        rendered = telemetry.render()

        for metric in telemetry.REGISTRY:
            assert f"# HELP {metric.name} " in rendered
            assert f"# TYPE {metric.name} {metric.kind}" in rendered

    def test_a_metric_with_no_observations_renders_no_samples(self):
        """
        Absent and zero mean different things to an alert, and inventing a
        zero for every label combination would need every combination known in
        advance.
        """
        rendered = telemetry.render()

        assert "kubewhy_inference_fallbacks_total{" not in rendered

    def test_a_sample_carries_its_labels(self):
        telemetry.INFERENCE_REQUESTS.inc(
            mode="cluster", provider="vllm", model="qwen3", outcome="ok")

        assert ('kubewhy_inference_requests_total{mode="cluster",'
                'provider="vllm",model="qwen3",outcome="ok"} 1'
                ) in telemetry.render()

    def test_a_label_value_with_a_quote_cannot_break_the_format(self):
        telemetry.INVESTIGATIONS.inc(outcome='we"ird\nvalue')

        line = [l for l in telemetry.render().splitlines()
                if l.startswith("kubewhy_investigations_total{")][0]

        # The quote is escaped rather than closing the label early, and the
        # newline never reaches the output -- a raw one would split this into
        # two lines and make the second unparseable.
        assert line == (
            'kubewhy_investigations_total{outcome="we\\"ird\\nvalue"} 1')

    def test_the_output_ends_with_a_newline(self):
        # Prometheus rejects an exposition whose last line is unterminated.
        assert telemetry.render().endswith("\n")

    def test_reset_clears_everything(self):
        telemetry.INVESTIGATIONS.inc(outcome="grounded")
        telemetry.reset()

        assert all(metric.values == {} for metric in telemetry.REGISTRY)


class TestTimer:
    def test_it_measures_on_the_monotonic_clock(self):
        """
        perf_counter, not time.time. A laptop that suspends mid-run makes the
        wall clock report minutes of model latency that never happened -- this
        project has recorded a 725s run with a 548s nap inside it.
        """
        with telemetry.timer() as clock:
            pass

        assert clock.seconds >= 0
        assert clock.seconds < 1


class TestTheEdgeIsInsideItsBucket:
    """
    `le` is Prometheus for "less than or equal", and the existing cumulative
    case observes 0.5, 5 and 50 against edges of 1, 10 and 100 -- every value
    strictly inside a bucket, so `<=` and `<` produce the same table and the
    label's own meaning was never checked.

    A histogram that drops the edge renders and scrapes cleanly and reports
    wrong quantiles, which is the failure mode the cumulative test above
    names.
    """

    def test_an_observation_exactly_on_an_edge_is_in_that_bucket(self):
        hist = telemetry.Histogram("h", "help", (), buckets=(1, 10, 100))
        hist.observe(10)

        samples = {labels["le"]: value
                   for name, labels, value in hist.samples()
                   if name.endswith("_bucket")}

        assert samples == {"1": 0, "10": 1, "100": 1, "+Inf": 1}

    def test_a_hair_above_the_edge_is_in_the_next_one(self):
        """The counter: the case above must not pass by counting everything."""
        hist = telemetry.Histogram("h", "help", (), buckets=(1, 10, 100))
        hist.observe(10.001)

        samples = {labels["le"]: value
                   for name, labels, value in hist.samples()
                   if name.endswith("_bucket")}

        assert samples == {"1": 0, "10": 0, "100": 1, "+Inf": 1}


class TestTheTimerDoesNotSwallowWhatHappensInside:
    """
    `__exit__` returning True suppresses the exception. A timer that ate every
    failure in the block it measures would turn a crashed investigation into a
    silent one that reports a duration -- and every existing timer case runs a
    block that succeeds, so nothing noticed.
    """

    def test_an_exception_inside_the_block_still_propagates(self):
        with pytest.raises(ValueError, match="the tool failed"):
            with telemetry.timer():
                raise ValueError("the tool failed")

    def test_it_still_records_the_time_it_took_to_fail(self):
        """A failed call is one worth having the latency of."""
        clock = telemetry.timer()
        with pytest.raises(ValueError):
            with clock:
                raise ValueError("boom")

        assert clock.seconds >= 0.0
