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
