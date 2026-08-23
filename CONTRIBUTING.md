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

## Adding a model provider

`backends.py` owns the protocol, `inference.py` owns where inference happens
and whether evidence may go there, and `agent.py` knows neither. The full
recipe -- including the two things that are not obvious, the wire shape of
messages and the `wire` label that decides whether a mid-run failover is
possible -- is in [docs/INFERENCE.md](docs/INFERENCE.md#adding-a-provider).

The rule worth repeating here: **a backend is not done when it runs.** The
suite in `evals/` is the only check on a model change, and a backend merged
without those numbers is unverified whatever its unit tests say. `grounding.py`
in particular is calibrated to how qwen3 writes, so a different model keeps
producing verdicts -- just less accurate ones.

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

### Hold the machine awake, or the numbers are fiction

```bash
caffeinate -is env OLLAMA_KEEP_ALIVE=24h python evals/run_eval.py \
    --context kind-triage-demo --repeat 10 --json results/qwen3.json
```

**The stalls this project chased for two months were the laptop going to
sleep.** Runs of 371s to 2217s against a ~62s median, arriving in adjacent
pairs and triples, on ordinary two-tool chains.

Measured 2026-08-17. A run took **725.0s**; its own instrumentation
attributed **180.0s** to the model and 0.05s to tools, leaving 545s
unaccounted. `pmset -g log` covering that window:

| event | at | asleep |
| --- | --- | --- |
| run starts | 10:43:57 | |
| `Idle Sleep` → `DarkWake` | 10:44:34 → 10:47:38 | 184s |
| `Maintenance Sleep` → `Wake` | 10:47:48 → 10:53:52 | 364s |
| run ends | 10:56:02 | **548s** |

548s asleep against 545s unaccounted. macOS idle sleep counts HID input, not
CPU load, so an unattended benchmark on battery sleeps *because* nobody is
typing — which is why the stalls preferred idle machines, and why they arrive
in neighbouring runs: one nap spans several.

Every run records this now. `timing` carries `wall_ms`, `unaccounted_ms` and
`slept_ms` alongside `model_ms`, `tool_ms`, `round_ms` and
`slowest_round_ms`; `slept_ms` is the wall clock minus the monotonic clock
over the same interval, since a monotonic clock does not advance while the
host is suspended. `run_eval.py` prints `[host asleep Ns]` next to any run it
happened in. A stall with `slept_ms` near zero is a different animal and
worth reporting.

Two earlier hypotheses were killed by measurement, and both stay dead — the
sleep evidence explains what they could not. Over 61 runs on 2026-08-15
(`results/interrupted-6cases-n10.json`), nine exceeded 200s:

- **Not the loader.** All nine had `model_resident: True`. A plain unload and
  reload is cheap anyway — 1.37s cold against 0.25s warm on a 2GB model.
- **Not contention.** Median `load_before` was 2.73 for stalls against 2.06
  for normal runs, and three stalls landed on an idle 15-CPU machine: 611s at
  load 1.06, 322s at 1.20, 248s at 1.83. A sleeping host is an idle host, so
  low load next to a long run was the clue, not the contradiction.

**Latency figures taken before 2026-08-17 include naps and cannot be
separated after the fact** — `slept_ms` did not exist to record them. Take
new ones under `caffeinate`.

### When the answer is wrong about the tool result, keep the tool result

`run_eval.py` records the answer, which tools were called, and -- since
2026-08-21 -- both of the checker's inputs: `evidence`, what those tools
returned, and `draft`, the answer as it was checked. Re-scoring a recorded
run offline is

```python
grounding.check(record["draft"], record["evidence"])
```

with the same ids and ordering the live check saw.

**Re-check `draft`, never `answer`.** `answer` is the published text, which
has been through `verify()` (unsupported values rewritten) and `annotate()`
(markers and the evidence audit appended). Replaying the check against it
returns a plausible verdict that is not the one the run recorded -- measured
on five live runs the day the field was added, two came back with a different
unverified list, one having lost a claim and gained two contributed by the
audit footer's own digits. `draft` is never returned by `ask()` or sent over
`/ask/stream`: it still carries the figures `verify()` exists to keep out of
a reader's way. **This is the only field in
the record that a re-run cannot reproduce**, because the cluster has moved on
and the model answers differently the second time. It costs about 2.6 KB per
run against the 1.8 KB of answer text already stored.

It exists because of a question that could not be answered on 2026-08-21.
`grounding.py` stopped treating a hedged status as a fabrication, and the
obvious check -- re-score the existing sets under both versions -- was
impossible, since no set held the tool output the checker reads. The
comparison had to fall back on `probe_scan_summary.py`'s records, which cover
one case. Sets recorded before that date still cannot be re-scored.

`ask()` returns it only for callers that pass `evidence=True`. Every other
caller puts its result on a wire -- REST, MCP, Slack -- and a projected scan
of a busy cluster is a large thing to add to a reply nobody asked for. The
`/ask/stream` answer event drops it too, since that event is documented as
matching what `/ask` returns and every result in it has already been sent as
its own `tool_result` event.

`probe_scan_summary.py` remains the tool for going deeper on one case: many
repeats, every `scan_cluster` result in full beside the answer, and which of
the returned workloads the answer named -- plus `--shuffle`, which the eval
has no equivalent of.

```bash
caffeinate -is env OLLAMA_KEEP_ALIVE=24h python evals/probe_scan_summary.py \
    --context kind-triage-demo --repeat 20 --json results/probe.json
python evals/analyse_scan_summary.py results/probe.json
```

`--shuffle` permutes the entry order per run without touching a value. It
exists because the demo fixtures sort deterministically, so an entry's
identity and its index are the same fact and no live run can say which one the
model is responding to. Two arms, one shuffled, separate them.

Two things that arm taught, both worth stealing for the next probe:

**A wrapper around a tool changes the tool.** Ollama builds the schema by
introspecting the callables in `agent.TOOLS`, so a closure without
`functools.wraps` hands the model a tool named `wrapper` taking `**kwargs`
with no description. It cost twelve runs that called no tool at all before
anyone looked at why they were fast.

**A run that wrote no summary is not a run that dropped everything.** One
exhausted `MAX_ROUNDS`; two answered about a single pod. Pooled into the
marginals they add a phantom drop to every position and every fault class at
once, which is flat noise across exactly the thing being measured. Count them,
report them, exclude them.

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
