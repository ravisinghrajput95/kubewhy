# Contributing

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
pytest
```

The test suite needs no cluster and no model — the Kubernetes API and the
Ollama client are both mocked. If a test starts taking 10× longer, a mock has
stopped biting and you are talking to something real.

## Adding a tool

1. Write a plain function in `routers/` that returns a JSON-able dict.
2. Give it a docstring saying **when to use it**, not just what it returns.
   The docstring becomes the tool description the model reads — it is
   prompt engineering, not documentation.
3. Register it in `TOOLS` in `agent.py`, in the `for _tool in (...)` loop in
   `mcp_server.py`, and add a route in `app.py`.
4. Add tests, including a token-ceiling assertion.

### Two rules that are not negotiable

**Read-only.** No tool creates, updates, patches, deletes, scales or evicts.
Diagnosis is a far safer thing to automate than remediation, and the whole
security posture depends on this holding.

**Project the output.** Never return a raw API object. A raw pod is ~1,500
tokens; fifty of them exceed the model's entire context. Return the fields a
diagnosis depends on and drop everything else. If you cannot justify a field
against a failure mode, leave it out.

## Testing expectations

- Kubernetes fixtures use real `V1*` client models, not bare `MagicMock`, so
  a projection reaching for a nonexistent field fails the test.
- Errors must come back as `{"error": ...}` data, never raise. The agent loop
  has to survive a failing tool.
- New projections need a size assertion.

## Evals

`tests/` proves the code is right. `evals/` measures whether the agent reaches
the right conclusion, which is a different question.

```bash
kind create cluster --name triage-demo
kubectl apply -f demo/broken-pods.yaml
python evals/run_eval.py --repeat 10 --json results/qwen3.json
python evals/summarise.py results/*.json
```

If you change the system prompt, tool descriptions, or any projection, **run
the evals and report the before/after in your PR.** Prompt changes are code
changes with no compiler; the eval is the only check.

### Set `OLLAMA_KEEP_ALIVE` before a long run

```bash
OLLAMA_KEEP_ALIVE=24h python evals/run_eval.py --repeat 10 --json results/qwen3.json
```

Ollama unloads a model five minutes after its last request by default, and a
benchmark should measure the agent rather than the loader.

**This command was a no-op until 2026-08-10, and every latency figure this
project has published predates the fix.** `OLLAMA_KEEP_ALIVE` is a server-side
variable; the Ollama Python client never reads it, so exporting it in front of
`run_eval.py` set a variable that nothing on the path looked at. Measured by
unloading the model, running one chat through the client, and reading
`/api/ps`: the model came back with an expiry **five minutes** out, the server
default. `agent.py` now forwards it explicitly, and the same measurement
afterwards reports 1440 minutes. Unset, it is still omitted from the request
body, so the server default applies exactly as before.

This matters because of an open defect: runs of 371s to 1013s against a median
near 70s, on ordinary two-tool chains. The stalls arrive in **adjacent pairs**,
and in one 60-run set they landed on runs 20 and 21 — the first two runs of the
second repeat, on a case that had taken 82s and 49s in the first. Same
question, same prompt, same cluster, eleven times slower.

Every run now records `model_resident`, so the next set of numbers can say
whether a stall landed on a run that had to load the weights. Treat that as an
open question rather than a settled diagnosis: a plain unload and reload is
measurably cheap here (1.37s cold against 0.25s warm on a 2GB model), so it
does not on its own account for 1013s. Page eviction under memory pressure
would, and so would several other things. **Any latency figure from a long run
should state its outliers** until this is understood.

Note also what the keep-alive defect does *not* explain. Eval runs are
back-to-back, so the five-minute idle timer never elapsed between them anyway
— the weights should have stayed loaded regardless. A hypothesis that needs an
idle gap cannot account for a stall on run 21 of a continuous set.

The controller has its own eval, because it asks its own question:

```bash
python evals/run_controller_eval.py
```

`run_eval.py` measures the agent through a human's question. The controller
composes its own sentence about a pod it picked itself and hands the answer to
a sink, and none of that is exercised by asking `agent.ask` something. What it
grades is the delivered message rather than the raw answer -- whether it names
the workload rather than a pod hash, and whether it fits Slack's block limit,
since an oversized block means no alert at all.

Report intervals, not point estimates. A single run of a non-deterministic
system is an anecdote — `summarise.py` computes Wilson intervals for this
reason.

## Style

Match what is there: standard library first, comments that explain *why*
rather than restating the code, and docstrings written for the model as much
as the reader.
