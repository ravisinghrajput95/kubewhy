"""
Was the model resident before this run, or did the run have to load it?

This project has repeatedly measured runs of 371s to 1013s against a median
near 70s, on ordinary two-tool chains, and has carried "Ollama stalls
intermittently, undiagnosed" as an open defect for weeks. The stalls have a
shape that argues against the model being slow to think:

  - they arrive in ADJACENT PAIRS
  - in one 60-run set they landed on runs 20 and 21, the first two runs of the
    second repeat, on a case that took 82s and 49s in the first repeat
  - the same question, the same prompt and the same cluster, eleven times
    slower

Nothing about a question changes between repeats. What can change is whether
the weights are still resident: a multi-gigabyte model that has been unloaded,
or whose mmap'd pages have been evicted under memory pressure, has to come off
disk before the first token, and the run after it pays again while pages fault
back in. That would produce exactly a pair.

This does not prove that is the cause, and it is not written as though it does.
It records the one fact that would settle it, so the next set of numbers can
attribute a stall instead of arguing about one. If stalls keep landing on runs
where the model was resident all along, the hypothesis is wrong and the search
moves elsewhere -- which is worth just as much.

Mitigation while the question is open: OLLAMA_KEEP_ALIVE=24h holds the weights
in memory between runs, which is what an eval wants anyway. A benchmark should
measure the agent, not the loader.
"""

import json
import os
import urllib.request

HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434").rstrip("/")


def resident(model, timeout=3):
    """
    True if `model` is loaded right now, False if it is not, None if unknown.

    Three states rather than two on purpose. A failed probe is not evidence
    that the model was absent, and recording it as False would manufacture
    support for the very hypothesis this exists to test.
    """
    try:
        with urllib.request.urlopen(f"{HOST}/api/ps", timeout=timeout) as response:
            loaded = json.load(response).get("models", []) or []
    except Exception:
        return None

    # Ollama reports "qwen3:latest" for a model asked for as "qwen3".
    wanted = model.split(":")[0]
    return any(entry.get("name", "").split(":")[0] == wanted for entry in loaded)
