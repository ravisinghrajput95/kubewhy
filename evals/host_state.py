"""
Host power state, for runs that are slow because of the machine rather than
the agent.

This is the third time a benchmark on this project has been distorted by
something outside both the model and the tools, and the second time the cause
was macOS power management. The first was idle sleep, which `slept_ms` catches
because a monotonic clock does not advance through a suspend. This one leaves
every clock intact and simply makes the model slower:

    2026-08-18, one 100-run set, same cluster and same resident model.
    Round 1 median 55.6s, round 2 median 114.8s, one run at 374.3s. Every
    run attributed its whole wall clock to `model_ms` -- tools 0.03s,
    `unaccounted_ms` 0.0, `slept_ms` 0.0 -- on a 15-CPU machine at load 0.63
    with no thermal warning recorded. `pmset -g` said `powermode 1`: the
    battery had drained to 1% over the session and macOS had switched Low
    Power Mode on by itself.

Low Power Mode throttles the GPU, so a local model halves in speed while
every timer in the loop stays honest and the machine looks idle. Nothing in
the run record could have said so, which is the same gap `slept_ms` was added
to close. Read it per run rather than once: the battery charges, macOS turns
it back off, and a set that changes speed halfway is worse than one that is
uniformly slow.
"""

import platform
import subprocess


def low_power_mode():
    """
    True, False, or None where the question does not apply or cannot be asked.

    None rather than False for a non-Mac or a failed call: "not throttled" and
    "not known" are different claims, and a record that cannot tell them apart
    would let a future stall be explained away by a flag that was never read.
    """
    if platform.system() != "Darwin":
        return None

    try:
        out = subprocess.run(
            ["pmset", "-g"], capture_output=True, text=True, timeout=5
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None

    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] == "powermode":
            return parts[1] != "0"
    return None
