"""
Tests for the host power-state probe.

It exists because a benchmark was distorted twice by macOS power management
and the run record could not say so either time. A probe that quietly returns
False when it failed to ask would close that gap on paper and leave it open in
fact, which is the failure mode these tests are aimed at.
"""

import importlib.util
import os
import subprocess
from unittest.mock import patch

EVALS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "evals")

spec = importlib.util.spec_from_file_location(
    "host_state", os.path.join(EVALS, "host_state.py")
)
host_state = importlib.util.module_from_spec(spec)
spec.loader.exec_module(host_state)


def pmset(stdout):
    return patch.object(
        host_state.subprocess,
        "run",
        return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout),
    )


SAMPLE = """System-wide power settings:
Currently in use:
 standby              1
 powermode            {mode}
 hibernatemode        3
 lowpowermode         {mode}
"""


class TestLowPowerMode:
    def test_reads_the_flag_when_it_is_on(self):
        with patch.object(host_state.platform, "system", return_value="Darwin"):
            with pmset(SAMPLE.format(mode=1)):
                assert host_state.low_power_mode() is True

    def test_reads_the_flag_when_it_is_off(self):
        with patch.object(host_state.platform, "system", return_value="Darwin"):
            with pmset(SAMPLE.format(mode=0)):
                assert host_state.low_power_mode() is False

    def test_high_power_mode_is_not_low_power_mode(self):
        """
        `powermode 2` is the high-power setting on machines that have one.
        Reading it as truthy-means-throttled would be wrong in the one
        direction that matters, so the check is against 0 rather than for 1.
        """
        with patch.object(host_state.platform, "system", return_value="Darwin"):
            with pmset(SAMPLE.format(mode=2)):
                assert host_state.low_power_mode() is True

    def test_unknown_is_not_false(self):
        """
        "not throttled" and "not asked" are different claims. Collapsing them
        would let a future stall be explained away by a flag nothing read.
        """
        with patch.object(host_state.platform, "system", return_value="Linux"):
            assert host_state.low_power_mode() is None

        with patch.object(host_state.platform, "system", return_value="Darwin"):
            with pmset("Currently in use:\n standby              1\n"):
                assert host_state.low_power_mode() is None

    def test_a_failed_call_is_unknown_rather_than_a_crash(self):
        """The eval has to survive a probe that cannot run, like any tool."""
        with patch.object(host_state.platform, "system", return_value="Darwin"):
            with patch.object(host_state.subprocess, "run", side_effect=OSError("boom")):
                assert host_state.low_power_mode() is None

            with patch.object(
                host_state.subprocess, "run",
                side_effect=subprocess.TimeoutExpired(cmd="pmset", timeout=5),
            ):
                assert host_state.low_power_mode() is None
