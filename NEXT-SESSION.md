# Handoff prompt — paste this to start the next session

I'm working on kubewhy at /Users/ravirajput/Projects/AIOps-agent
(github.com/ravisinghrajput95/kubewhy, public, MIT). Air-gapped Kubernetes
root-cause analysis: a local model via Ollama chains read-only tools to explain
*why* a workload is broken. Six surfaces share one tool set — CLI (agent.py,
--scan), REST (app.py), MCP (mcp_server.py), watch controller (controller.py),
Streamlit UI (ui.py), Slack via Socket Mode (slack_socket.py).

**State: main at f3bab66, everything pushed, tree clean, 301 tests pass, tags
through v0.1.6. No clusters running anywhere — local or cloud.**

Read README.md and CONTRIBUTING.md first. Three rules are non-negotiable: tools
stay read-only (no writes to the cluster, ever); tool output stays projected (a
raw pod is ~1,700 tokens); errors come back as {"error": ...} data, never
raised.

## Pending work, roughly in priority order

**1. `nightly-sync` gets a plan instead of a diagnosis.** Seen live on GKE: the
controller's finding for a failing CronJob was *"To find the root cause: 1.
Check termination reason: call describe_pod… 2. Inspect logs: use
get_pod_logs…"* — it wrote out the tool calls instead of making them. This is
the same "won't go and look" shape as the hedging-sentence data. Model
behaviour, so changing it needs an A/B via evals/ab_prompt.py, not a confident
patch. **Read evals/ask_ai/ first** and note the lesson from
[[hedging-sentence-does-not-replicate]]: the last prompt change looked
compelling at p=0.056 and did not replicate.

**2. Ollama stalls — instrumented, not solved.** 371s to 1013s against a ~70s
median. Every eval run now records `model_resident` (evals/ollama_state.py), so
the *next* run can attribute a stall instead of guessing. The evidence so far:
stalls arrive in adjacent pairs, and in results/hedging-allcases.json they land
on runs 20 and 21 — the first two of repeat 2, on a case that took 82s and 49s
in repeat 1. But a cold model load measured only 1.37s against 0.25s warm on a
2GB model, so the simple unload/reload story does **not** explain 1013s. Run an
eval with `OLLAMA_KEEP_ALIVE=24h` and check whether stalls land on runs where
`model_resident` was false. If they land on resident runs, the hypothesis is
wrong and the search moves elsewhere.

**3. Secret existence is the one broken-edge case scan_references cannot
close.** A pod referencing a Secret that does not exist is undetectable,
because verifying existence means listing Secrets and a list returns their
data. The proper fix is `PartialObjectMetadata` responses, which return names
without contents — fiddly through the Python client, but it would close the
edge without weakening the read-only posture.

**4. Test-suite order dependence.** `tests/test_ui.py::TestContextIsPerSession
::test_the_context_is_part_of_every_cache_key` does `import ui`, which executes
the Streamlit script in bare mode and leaves form state in the process — any
later test that drives a form then fails with "st.button() can't be used in an
st.form()". Pre-existing. It forced the spinner fix to be tested as a pure
function instead of through AppTest.

**5. Untested surfaces.** Slack reply path (Socket Mode connects to real Slack,
but replying needs a real SLACK_BOT_TOKEN). EKS. `store.py` under more than one
replica — the chart still pins one, and two pods means two SQLite files.

**6. Frozen benchmarks are provisional.** The 30 cases in
evals/ask_ai/tiers.yaml were chosen on judgement. The stated criterion is that
a benchmark must *discriminate* — show both passes and failures — and that
cannot be known until the suite has run once. Get a baseline before treating
that list as fixed.

## What shipped last session, so you don't redo it

- `demo/tricky-pods.yaml` — 12 seeded faults, 10 invisible to `kubectl get
  pods`, plus a healthy control. `evals/ask_ai/example-findings.yaml` is the
  ground truth.
- `evals/ask_ai/` — 427-prompt suite, red team, tiers, `validate.py` (CI-ready).
- `describe_pod` now reports which ConfigMaps/Secrets each container consumes
  and by which route (`updates_in_place`), names only, no new RBAC needed.
- `get_service_endpoints` catches ready-endpoints-but-refused (targetPort vs
  containerPort), hedged for numeric ports, stated as fact for named ones.
- `scan_references` — new tool on all three surfaces plus `GET /references`.
  Cost five list-only RBAC grants; verified under a minted SA token.
- Fixed the `--target base` trap in `tests.yml` **and** `docker-compose.yml`
  (CI had been red on main; compose was silently serving Streamlit on 8000).
- Spinner "defect": root-caused as a CSS animation freezing when the browser
  stops painting frames (rAF fired 0 times in 1.2s in a hidden tab). Progress
  now goes in the status *label* as text.
- UI top padding trimmed; `watchdog` added to requirements-dev.txt.

## Work style

Verify against real systems rather than asserting. State the measurement
method when publishing a number. Report intervals, not point estimates. Commit
each piece once verified and tested; **no Co-Authored-By or any assistant
attribution in commit messages.** Be blunt about what is untested — and when
evidence contradicts your own hypothesis, say so rather than working around it.

Budget note: ~₹260 of ₹300 GCP credit remains. A 1-node e2-small zonal cluster
is ~₹11/hour; e2-standard-4 plus a LoadBalancer is ~₹20–25/hour. Delete in the
same session, and remember a reserved static IP keeps billing after the cluster
is gone.
