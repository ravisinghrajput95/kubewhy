# Handoff prompt — paste this to start the next session

I'm working on kubewhy at /Users/ravirajput/Projects/AIOps-agent
(github.com/ravisinghrajput95/kubewhy, public, MIT). Air-gapped Kubernetes
root-cause analysis: a local model via Ollama chains read-only tools to explain
*why* a workload is broken. Six surfaces share one tool set — CLI (agent.py,
`--scan`), REST (app.py), MCP (mcp_server.py), watch controller (controller.py),
Streamlit UI (ui.py), Slack via Socket Mode (slack_socket.py).

**State: `main` at `2ae93bf`, tree clean and pushed, 624 tests pass, tags
through v0.1.6. Updated 2026-08-21, late. No clusters running anywhere;
Docker is stopped and the model is unloaded.**

**The summary drop is fixed and the cause was list length.** Holding workload
identity and order constant by permuting and varying only entry count:
8/8 complete summaries at five entries, 0/8 at ten, 0/8 at twenty, Fisher
exact p=0.000155. Drops spread evenly over fault class and position, so the
two suspects that survived the earlier rounds both die once count is the
variable being held rather than the one drifting. Latency was flat across the
arms (145s/144s/169s) and entries-named did not cap at a constant, so it is
proportional loss and not a timeout or a ceiling -- which is why no wording
change would have touched it.

The fix is a third deterministic policy (`uncovered_workloads()` plus a
re-ask, same budget as the tool nudge and the evidence policy) with a
deterministic appendix behind it, because at twenty entries a run named as
few as 7 and re-asking can trade one omission for another. **0/8 -> 8/8
complete, 47/160 -> 0/159 entries dropped, and 5/5 on the graded eval case at
n=5.** Six of the eight got there on the re-ask alone; two needed the
appendix, so both layers earn their place. **The cost is +43% latency on the
slowest case in the suite** (169s -> 242s median) and that is a real cost.

**Controller at n=5: 25/26 (96%).** `never-ready`'s earlier failure was noise
(5/5 here). The one failure is `crasher`, and it is the lead below.

**The agent-loop target invariant is built and now regression-tested.**
`targeting.py` fixes the entity-scoping defect that had
`unhealthy_question_about_a_healthy_pod` at 1/3 -- the model called
`list_pods(only_unhealthy=True)` without a workload, which excludes a healthy
pod by construction, and described the neighbours. The target is now extracted
once and every tool call is held to it: rewritten where the arguments can carry
the scope, refused where they name a different entity. Measured 5/5 on that
case, 32/32 on the suite at n=2, 0 entity violations across 5 multi-incident
runs.

Then it was widened, and **the widening is now verified.** An unlabelled name
("Why is crasher-svc unreachable?") is confirmed against the cluster before it
becomes a target -- one read-only lookup, and a name the cluster does not have
leaves the target unset. `results/widened-n3.json`: all 16 cases at n=3, 47/48,
95% CI [89-100], on a cluster rebuilt from scratch so nothing had drifted into
the namespaces `cluster_wide_scan` reads.

**The three cases the stopped run never reached all pass 3/3 and ground 3/3**
-- `healthy_workload_with_no_logs`, `stuck_volume_needs_events` (calling
`get_pod_events`, which the volume-reference ground truth requires) and
`unhealthy_question_about_a_healthy_pod`, the case that read 1/3 before the
invariant existed. Nothing over-scoped, which was the specific risk.

The one failure is `cluster_wide_scan` dropping `memory-hog` from its summary:
the known summary-drop defect, not a targeting regression.

**Correction to the previous handoff: `widened-n1` was 9/13 grounded, not
11/13.** The 11 counted `insufficient_evidence` as grounded, which
`grounding.py` rejects in as many words. There are four verdicts; never sum
two of them.

**The README benchmark table has not been re-taken since `scan_cluster` began
labelling a Running-but-unready workload `fault: not-ready`.** It is still
`results/baseline-n10-2.json` and the README says so. Reachability was checked
against the tool traces rather than assumed: of the ten cases, only
`cluster_wide_scan` calls `scan_cluster` on the whole cluster — six use
`list_pods`, which is unchanged, `host_not_cluster` asks about the host, and
both `healthy-*` cases call `scan_cluster(workload='healthy-web')`, which
returns one workload that is Running and ready, so the changed line cannot
fire. A 100-run set would spend two hours re-measuring nine cases that cannot
reach the change. Re-take it when the machine is idle and charged, for
provenance rather than verification.

**`nightly-sync` is fixed — 3/3, from 0/3.** The fix was already written and
the eval was not exercising it. See below before assuming any other "known
cause, fix outstanding" item is really outstanding.

**`cluster_wide_scan` is measured and its cause was the tool, not the model.**
An entry in an unhealthy-only list that stated no fault — a Running pod with a
failing readiness probe — was the entry the model dropped. `scan_cluster`
labels it `fault: not-ready` now: 7/18 dropped before, 2/37 after, p=0.0036,
with every other entry unmoved. Five other hypotheses died on the way, one of
them after its control group turned out to be contaminated. See open defects.

**The wrong-workload substitution was not the agent.** It was the grader
matching a forbidden name anywhere in the answer, so "healthy-web is running
normally; bad-image and memory-hog are unhealthy" scored as a substitution.
Measured before touching anything, which is the only reason the projection
was not rewritten to fix a defect that did not exist. Details below.

**Start here: an eval record now keeps the checker's inputs, and every set
recorded before 2026-08-21 evening cannot be re-scored.** `run_eval.py`
records `evidence` (what the tools returned, in `grounding.records()` shape)
and `draft` (the answer as `check()` read it). Re-scoring a recorded run
offline is

```python
grounding.check(record["draft"], record["evidence"])
```

**Re-check `draft`, never `answer`.** `answer` is the published text, after
`verify()` rewrote its unsupported values and `annotate()` appended the
markers and the audit footer -- and the footer's own digits read back as
fresh claims. Measured: 3 of 5 live runs re-scored correctly against
`answer`, 3 of 3 against `draft`.

This was built because a question could not be answered this session. The
grounding checker changed, and the obvious check -- re-score every existing
set under both versions -- was impossible, because no set held the tool
output. The comparison had to fall back on `probe_scan_summary.py`, which
covers one case of sixteen. It costs ~2.6 KB per run.

**The first version of that field shipped broken and its test passed.** The
test drained a mocked run whose answer was "cpu is low" -- nothing checkable,
so the verdict was empty, `annotate()` left the string alone, and draft and
answer were identical. **That is the fourth time in this repo a green test or
a clean eval has been measuring a code path production does not take.** The
others: `nightly-sync` (the eval called `diagnose()` directly and skipped the
prefetch), the wrong-workload substitution (the grader, not the agent), and
the un-`functools.wraps`'d tool wrapper that handed the model a tool named
`wrapper`. Before diagnosing a model, check the harness is exercising the
thing you think it is -- and write the test with an input that makes the
code under test actually do something.

Read README.md and CONTRIBUTING.md first.

## Open, in priority order

1. ~~**Finish the widened-targeting regression run.**~~ Done --
   `results/widened-n3.json`, 16 cases at n=3, 47/48. See above.
2. ~~**`cluster_wide_scan` grounding.**~~ **Largely closed 2026-08-21, and the
   stated cause was only a third of it.** Three checker defects, all measured:

   - A **hedged status** was scored as a fabrication. `scan_cluster` returns a
     phase, not a termination reason, so "CrashLoopBackOff (likely OOMKilled)"
     is an inference no tool in that run could settle. A hedged *cause* was
     already exempt. Hedged claims are recorded with status `"inferred"` now
     rather than dropped, so they still appear in `contract()`.
   - **`e.g.` had never been recognised at all.** Inside `_PRESCRIPTIVE`'s
     alternation it carried the group's trailing `\b`, which cannot match
     before the comma or space that always follows it. The branch was dead
     from the day it was written, so "crash reasons (e.g., OOMKilled)" scored
     as an assertion. 10 of 138 flags across every recorded set, in four cases.
   - **`scan_cluster` says `not-ready`; `KNOWN_STATUSES` said `notready`.** The
     substring test cannot bridge the hyphen, so the checker flagged a status
     its own tool had reported. Fixed with an explicit spelling table, because
     `cite()` has to find the value in the JSON to name its field.

   Replayed over the 34 probe runs with complete recorded evidence: **10/34
   grounded to 24/34**, no run moving down, McNemar exact p=0.0001. Live at
   n=5 afterwards: 3/5 grounded, from 0/3, **with no status flagged in any
   run** -- every residual flag is a number. n=5 is a smoke test; the replay
   is the load-bearing measurement.

   **The prediction in the old text was wrong and worth remembering.** The
   hedge fix alone was measured at n=3 and came back 0/3. The model words the
   same summary differently every run, so a fix aimed at one phrasing does not
   generalise; the other two defects were found only by reading why each
   individual flag fired.
3. ~~**The loop verifies evidence was gathered, never that it was read.**~~
   **Measured 2026-08-22: 0 of 20 runs ignored the log, 95% CI [0-16].** The
   instrument is `evals/evidence_read.py` and the probe is
   `evals/probe_evidence_read.py`; the set is `results/evidence-read-n10.json`.
   Ten controller runs each on `crasher` and `nightly-sync` -- the only two
   fixtures whose cause is in the log and nowhere else -- through
   `capture_evidence()` -> `diagnose()`, scored against `draft`:
   **read 20/20, Wilson 95% [84-100], complete 13/20, status-only 0, void 0.**

   **The 52-character ungrounded diagnosis did not reproduce.** No run came
   back `ungrounded`, the shortest draft was 428 characters, and the incident
   followed one wrong tool call where these runs made two to four each. So it
   is rarer than n=20 can see, or conditional on something this arm does not
   reproduce. The rule-of-three bound is 15%; another 20 runs would halve it.

   The residual is partial rather than absent: `crasher` names the database
   10/10, the port 8/10 and the refusal 7/10; `nightly-sync` names the
   upstream 10/10 and the 503 itself 7/10. A diagnosis that engaged with the
   log and dropped one specific is a different defect from one that ignored
   it, and only the second is what this item was opened about.

   **What made the earlier two instruments wrong is written into the module.**
   Token overlap cannot match `db:5432` against the log's `db:5432:`, and the
   JSON escape `capture_pod_logs` leaves in front of `FATAL` makes `\nfatal` a
   token no answer contains; stripping punctuation first turns `db:5432` into
   `db5432`, in neither text. The replacement keeps punctuation and decides
   boundaries at match time, and enumerates paraphrases per workload
   (`db-service:5432`, "the 503 error", `postgres` inferred from the port).
   `memory-hog` is excluded in code with the reason attached: its log is
   `stress` output and its cause is in the status, so ignoring it is correct.

   Two guards worth keeping. A run whose capture came back empty is **void**,
   not failed -- `capture_pod_logs` returns `[]` on a 404 -- and scoring
   "never mentioned 5432" against a run never shown it measures the harness;
   this set had zero voids, so the CronJob race ate nothing. And decoys
   (`CrashLoopBackOff`, exit code 1, restarts) are recorded alongside, so a
   run that read nothing can be told from a fluent restatement of the status.

   `draft` and `answer` carried identical facts on all 20 runs, so `verify()`
   and `annotate()` moved nothing this instrument reads -- recorded rather
   than assumed, since it is the reason the probe scores `draft`.

   Conditions: kind + qwen3, thinking on, `low_power_mode` true on every run,
   on battery. The 38-135s latencies here are throttled and are not a timing
   measurement.

4. **Latency is unresolved, and thinking-off is NOT settled.** ~67s median,
   99.88% model generation. Three cases at n=5 per arm: thinking on 13/15,
   thinking off 15/15, **Fisher exact p=0.483**, median 43.7s against 8.0s.
   That retires the old claim that thinking-off degrades RCA -- which rested
   on one n=1 failure of `crashloop_root_cause`, now 5/5 in both arms -- and
   establishes nothing in its place. p=0.483 is undetermined, not equivalent.
   Do not flip the default; run the full sixteen-case suite in the off arm,
   where `inference_is_marked` and `service_unreachable_chain` have never been
   measured. Note thinking *on* lost two `image_pull_failure` runs, stopping
   at list_pods + get_pod_events.
4. **Controller and noise evidence is two rounds old.** Both passed when last
   measured (3s detect, 52.5s RCA; 10 failing pods -> 1 finding).
5. **The README benchmark table predates the `not-ready` projection change**
   and says so. Re-take for provenance when the machine is idle.

**Score, honestly assessed: 8.8/10.** Blockers are performance (unimproved,
benchmark incomplete) and production readiness (autonomous evidence stale).
Do not round it.

## Three rules that are not negotiable

1. **Tools stay read-only.** Nothing creates, updates, patches, deletes,
   scales or evicts. The whole security posture rests on this.
2. **Tool output stays projected.** A raw pod is ~1,700 tokens; never return a
   raw API object. New projections need a token-ceiling assertion.
3. **Errors come back as `{"error": ...}` data, never raised.** The agent loop
   has to survive a failing tool.

## Environment

```bash
cd /Users/ravirajput/Projects/AIOps-agent
.venv/bin/python -m pytest              # 603 tests, no cluster, no model needed
ollama list                             # qwen3 (5.2GB) is the default model
```

Everything below assumes `.venv/bin/python`. Docker Desktop must be running
before `kind`; it is usually not.

```bash
kind create cluster --name triage-demo
kubectl apply -f demo/broken-pods.yaml   # namespace demo — the eval fixtures
kubectl apply -f demo/tricky-pods.yaml   # namespace shop — relational faults
.venv/bin/python agent.py --scan
```

### Teardown, and check it with this rather than from memory

**Always `kind delete cluster --name aiops-test` before finishing**, unload
the model if you set a long keep-alive, and quit Docker. Then run the check
below -- all of it, not the half you remember arming.

```bash
kind delete cluster --name aiops-test
curl -s http://localhost:11434/api/generate \
    -d '{"model":"qwen3","prompt":"x","stream":false,"keep_alive":0}'
osascript -e 'quit app "Docker Desktop"'
kubectl delete namespace noise-test --ignore-not-found     # if a noise run ran
kubectl delete -f deploy/rbac.yaml --ignore-not-found      # if RBAC was tested

# The check. Every line must come back empty or DOWN.
ps -eo pid,command | grep -E 'sleep [0-9]+$' | grep -v grep   # waiter shells
ps -eo pid,command | grep -E 'until |caffeinate -is' | grep -v grep
pgrep -fl 'run_eval|run_controller_eval|probe_scan|ab_prompt'
kind get clusters
curl -s localhost:11434/api/ps          # want {"models":[]}
docker info >/dev/null 2>&1 && echo UP || echo DOWN
git status -sb | head -1                # want no ahead/behind
```

**The `sleep`/`until` lines are the ones that get missed, and they were missed
on 2026-08-21.** A polling loop written as
`until ! pgrep -f run_eval; do sleep 30; done` is not matched by a grep for
`run_eval` -- the *evals* were all long gone and eleven waiter shells were
still spinning, two of which surfaced later as `exit code 144`. Grepping for
the job names only finds jobs; the loops waiting on them are a separate class
of leftover and need their own line.

They accumulate because it is tempting to arm a fresh waiter every time
someone asks whether a run has finished. **One waiter per job, stopped when
that job reports** -- and if a background task is already armed for it, read
the log instead of arming a second.

Evals (need a cluster and a model, never run in CI). **Run them under
`caffeinate`** — on battery this Mac is set to `sleep 1`, so an unattended
benchmark suspends after a minute without keystrokes and the run reports the
nap as its own slowness. That was the stall defect; see below.

```bash
caffeinate -is env OLLAMA_KEEP_ALIVE=24h .venv/bin/python evals/run_eval.py \
    --context kind-triage-demo --repeat 10 --json results/baseline.json
caffeinate -is env OLLAMA_KEEP_ALIVE=24h .venv/bin/python evals/run_controller_eval.py --repeat 3
.venv/bin/python evals/summarise.py results/*.json
.venv/bin/python evals/ask_ai/validate.py        # CI gate, no model needed
```

**Do not pipe a long eval through `tail` or a narrow `grep`.** Both were done
last session and both destroyed detail that had cost an hour to produce —
`tail -40` ate three cases' results, and a `grep` for `PASS|FAIL` dropped every
failure reason. Redirect to a file and read that.

**`demo/broken-pods.yaml` is what the evals assert against.** Adding pods to
the `demo` namespace changes results — the `cluster_wide_scan` case asks about
the whole cluster, so even the `shop` namespace can contaminate it. A run
started before such a change is void.

## What was settled on 2026-08-17, evening

**`nightly-sync` gets a diagnosis now: 3/3, from 0/3, and the whole
controller eval is 16/16.** The prefetch fix had been written and the eval
was not measuring it — `capture_evidence()` runs at enqueue time, only
`run()` passed the result through, and `run_controller_eval.py` called
`diagnose(pod, status)` directly. It captures at the same point production
does now, and re-resolves the pod per repeat rather than once per case.

**The race is not closed, and does not need to be.** Two of the three runs
had their live `describe_pod` come back
`{"error": "kubernetes API error 404: pods "..." not found"}` — the same 404
that used to leave the model writing an investigation plan. The diagnosis no
longer depends on winning the race because the line it turns on was read
while the pod was alive.

Each result line now prints `evidence=yes|NONE`. `bad-image` is `NONE` on all
three runs and passes anyway, correctly: a pod that never pulled its image has
no logs, and an empty capture must be visible or it reads as a real one.

**Read that failure mode across the rest of this file.** Two of today's three
"open defects" were not defects in the agent: one was the grader, one was an
eval measuring a code path production does not take. Before diagnosing a
model, check that the harness is exercising the thing you think it is.

**The wrong-workload substitution was a grading defect.** Three sets, all on
kind + qwen3, all re-scored under the corrected grader:

| set | n | old grader | re-scored |
| --- | --- | --- | --- |
| replay, full `list_deployments` projection | 30 | 28/30 | 30/30 |
| replay, projection filtered to `healthy-web` | 30 | 30/30 | 30/30 |
| live, real loop | 20 | 18/20 | 20/20 |

Every failure recorded with its answer text was the same shape: the correct
verdict on `healthy-web`, followed by a true remark that the neighbours are
unhealthy. `forbid` matched the neighbour's name as a substring anywhere in
the answer. The two replay arms are indistinguishable (Fisher exact p=0.49),
so **the projection is not the lever either** — the filtered arm only "wins"
by removing the model's ability to mention a name. Round-1 thinking on the
failing runs shows no substitution intent at all.

`forbid` now reads against whether the case's own expectations were met, the
same way `tools_named_but_not_called` reads against the root cause: unmet it
fails the run, met it is a note that is printed and recorded but not scored.
A case declaring no expectations keeps the unconditional behaviour, so a
forbid-only case cannot quietly become a check that never fails.

**The 2026-08-17 morning baseline's two failures on this case stay
unverified.** Same two reasons, but they predate answer text being kept.

**A new 100-run baseline exists on current `main`:
`results/baseline-n10-2.json`.** 99/100, 95% [95-100], median 54.0s, p95
132.7s, grounded 86/100, `slept_ms` zero on every run, `context` recorded on
every run. `cluster_wide_scan` 7/10 -> 9/10 (the nudge correction),
`healthy_workload_not_substituted` 8/10 -> 10/10 (the grader), everything
else 10/10. **The README table has been published from this set** — that
decision is closed.

**`scan_cluster` rejected the `namespaces` list the model sends.** 2 of 20
live runs called `namespaces=['demo']` and got
`{"error": "AttributeError: 'list' object has no attribute 'split'"}` back.
The loop survived, but those runs took three rounds instead of two, median
38.4s against 18.7s. Lists are accepted now.

**An eval can no longer measure the wrong cluster silently.** A 100-run set
started against kind and, one case in, `current-context` moved to a GKE
cluster something else on the machine had just created; every tool call after
that answered honestly about an empty cluster. `run_eval.py` takes
`--context`, preflights the demo namespace before spending any model time,
prints `context=... demo pods=N` in its header and records the context on
every run. **Run it as `--context kind-triage-demo`**, or with a `KUBECONFIG`
holding only that cluster.

## What was settled on 2026-08-17, earlier

**The stalls were the laptop sleeping.** Pending item 3, below, with the
`pmset` evidence. Two hypotheses died on this before it was measured, and the
answer was the third outcome the timing probe was built to distinguish: the
delay sat outside both the model and the tools.

**A diagnosis that names a tool now calls it.** `crashloop_root_cause` was
failing by writing "Next Step: get_pod_logs" instead of running it —
8/10 before, 10/10 after. The guard cost `cluster_wide_scan` two runs on its
first outing (it answered about the pod it had just drilled into) and the
correction was to quote the question back; 9/10 after that.

**The first honest baseline exists.** 100 runs, ten cases, no naps, 95%
[89-98]. Pending item 2.

**One measurement, three defects.** The baseline separated them cleanly by
whether the run had been nudged: the loop guard's own regression (nudged), the
wrong-workload substitution (never nudged), and a summary that drops one fault
from its own list. They would have read as one flaky suite without the
`nudges` field.

## What was settled the session before, and how

Two of the six open items closed outright, and two more had their stated cause
overturned by measuring it. Every one arrived with a plausible story attached,
and in three cases the story was wrong — including one where the planned fix
would have weakened the security posture in order to detect something that is
not a fault. **Read pending item 1 carefully: its cause is now known and its
symptom is not fixed.**

**`nightly-sync` got a plan instead of a diagnosis — a race, not a prompt.**
The previous handoff said this needed an A/B via ab_prompt.py. It did not. The
watch reports a pod and the diagnosis runs a minute or two later; `nightly-sync`
runs every minute with `failedJobsHistoryLimit: 2`, so the pod is collected
before the model asks about it. Every tool call returned
`{"error": "kubernetes API error 404: pods "nightly-sync-29772408-4bn2r" not
found"}` — three runs of three — and in one run a pod died *between*
`describe_pod` and `get_pod_logs`. Writing out an investigation plan is the sane
response to having no data. **An A/B on the wording would have measured noise.**

`Controller.still_there()` now re-resolves to a live pod of the same workload
and fault before asking, and posts nothing if the workload has gone quiet. That
did **not** fix the case — 11/16 before and after, `nightly-sync` 0/3 both
times. The substitution fires (`diagnosing_a_replacement_pod`) and the
replacement is collected mid-diagnosis too, because runs take 89–126s against a
pod that lives ~120s.

That work also found **`count_affected` had been returning 1 for every finding
the controller eval ever produced** — it built a bare `client.CoreV1Api()`,
which only has a kubeconfig once `run()` has loaded one, and swallowed the
failure. Both use `_api()` now.

**`OLLAMA_KEEP_ALIVE` was read by nothing.** It is a server-side setting and
the Ollama Python client never reads the environment, so the command
CONTRIBUTING documented set a variable nothing on the path looked at. Measured:
unload the model, run one chat through the client with it exported → the model
comes back expiring in **5.0 minutes**. agent.py now forwards it; the same
measurement reports **1440 minutes**. *Every latency figure published before
2026-08-10 was taken under a five-minute unload window by someone who believed
they had disabled unloading.*

**Secret existence — the PartialObjectMetadata path was dropped, not built.**
Wrong on both halves. It is not undetectable: the kubelet cannot construct the
container and puts the name in the pod's status, so `describe_pod` returns
`CreateContainerConfigError` with `secret "x" not found`, and the volume form
leaves a `FailedMount` event naming it — both with **no Secrets grant of any
kind**. The only invisible case is `optional: true`, which is not a fault. And
it would not have been safe: content negotiation happens *after* authorization,
so RBAC cannot tell a metadata request from a full one, and `list` cannot be
narrowed by `resourceNames`. Demonstrated with one token granted exactly
`list secrets` — names with the `PartialObjectMetadataList` header, the
plaintext password without it. Written up in SECURITY.md.

**Test-suite order dependence — fixed.** `import ui` executes the Streamlit
script in bare mode and leaves `FormData(form_id='ask')` on the process-wide
main `DeltaGenerator`. An autouse fixture in tests/test_ui.py clears it; two
tests pin the mechanism so a Streamlit rename cannot turn the cleanup into dead
code.

Unplanned fixes along the way: all three `evals/ask_ai` scripts failed from the
repo root where their own README says to run them (`build_investigation.py`
opened `findings.yaml`, which does not exist — it is `example-findings.yaml`);
`run_eval.py` wrote its JSON only at the end, so a stopped run left nothing on
disk; and the README had drifted — 13 tools of 14, no `scan_references` or
`GET /references`, "five surfaces" followed by six, a Slack section still
claiming replies were impossible when slack_socket.py ships exactly that, and
no entry for `TRIAGE_STATE_DB`.

## Open defects

- ~~**`nightly-sync` still fails, 0/3.**~~ — **fixed 2026-08-17 evening,
  3/3.** The evidence is captured while the pod is alive and handed to the
  diagnosis; the 404 race still fires and no longer decides the outcome. The
  fix had been written before this session and the eval was calling a code
  path that skipped it.
- ~~**Ollama stalls, 371s–2217s against a ~62s median.**~~ — **solved
  2026-08-17, and it was never Ollama.** The host sleeps mid-run. A 725s run
  accounted for 180.0s of model and 0.05s of tools; `pmset -g log` puts the
  machine asleep for 548s inside that window (`Idle Sleep` 184s, then
  `Maintenance Sleep` 364s) against 545s unaccounted. `pmset -g custom` on
  battery: `sleep 1`. macOS idle sleep counts HID input, not CPU load, so an
  unattended benchmark suspends *because* nobody is typing — which is why the
  stalls preferred idle machines, arrived in adjacent runs (one nap spans
  several), and left the model resident throughout. Every loop timer was
  monotonic, and a monotonic clock does not advance through a suspend, so the
  nap could only ever appear as a hole. `timing` now carries `wall_ms`,
  `unaccounted_ms` and `slept_ms`; `run_eval` prints `[host asleep Ns]`. Run
  evals under `caffeinate -is`. **A stall with `slept_ms` near zero would be a
  new animal and is worth reporting.**
- ~~**The Helm chart never sets `TRIAGE_STATE_DB`**~~ — fixed.
  `persistence.enabled=true` wires a PVC, the env var and
  `podSecurityContext.fsGroup: 1000`; without the fsGroup the volume arrives
  root-owned and the controller crashloops on
  `sqlite3.OperationalError: unable to open database file`, so the chart now
  refuses to render persistence without one on a non-root pod. Verified
  against `csi-driver-host-path` (`fsGroupPolicy: File`, as Azure Disk has).
  **kind's default StorageClass cannot test this** — local-path hands out a
  world-writable 0777 directory, so the bug does not reproduce, and the
  kubelet does not apply fsGroup to it at all.
- ~~**`run_eval.py` iterates case-major**~~ — fixed 2026-08-15, the day it
  cost a run. The machine died 61 runs in and four of ten cases had never
  executed once, so the suite-wide number was unusable while the early cases
  were oversampled. Now repeat-major: an interruption leaves every case
  sampled roughly equally. It also prints one line per run, because an hour of
  silence is indistinguishable from the hang this suite exists to measure.
- ~~**A vanished workload still spends its budget.**~~ — fixed.
  `Budget.spend()` returns a receipt, carried through the queue, and
  `refund()` hands the slot back when `diagnose()` finds nothing left to look
  at. The old reasoning ("nothing carries the fault, so nothing is being
  suppressed") held for that workload's cooldown and not for the hourly
  ceiling, which is global — twelve collected CronJob pods in an hour
  silenced every other workload in the cluster. A queue overflow refunds for
  the same reason. **A failed diagnosis deliberately does not**: the fault is
  still real, and the spent slot is the only thing pacing retries while
  Ollama is down.
- ~~**The plan detector conflates two behaviours.**~~ — settled.
  `planned_instead_of_looking` is now `tools_named_but_not_called`, which
  reports the fact and takes no view. `grade()` supplies the verdict, because
  it is the only caller that knows the case's root cause: tools named but not
  called **with the root cause missing** is the GKE failure and fails the run;
  **with the root cause present** it is a postscript, and is printed as a
  `~` note rather than scored. The old behaviour could fail a correct answer
  for ending with a suggestion.
- ~~**The controller never reports a volume-referenced ConfigMap or Secret.**~~
  Found and fixed 2026-08-15; evidence in
  `evals/ask_ai/config-reference-findings.yaml`. `STUCK_WHEN_SLOW`
  (`ContainerCreating`, `PodInitializing`) plus `TRIAGE_STUCK_AFTER` (300s,
  `watch.stuckAfterSeconds` in the chart) qualifies those statuses by
  duration instead of adding them to `WATCHED`, which would have diagnosed
  every image pull.

  Two mechanism checks made this work rather than guesswork. **The watch
  re-delivers stuck pods**: each 300s cycle re-lists and replays them as
  `ADDED` — measured over three cycles, both test pods re-delivered every
  time — so the threshold does fire; worst-case detection is the threshold
  plus one cycle. And **`status.start_time` is set on stuck pods** (all four
  fixtures), with `metadata.creation_timestamp` as the fallback.

  The default came from measurement, not taste: 22 healthy pods on the demo
  cluster reached Ready in a median of 21s, max 52s. Verified live afterwards
  — all four fixtures now report, a freshly created pod stays unreported
  through its `ContainerCreating` window, and `nightly-sync` at 17s still
  reports immediately, so the threshold does not delay real faults.
- ~~**`crashloop_root_cause` stops before `get_pod_logs`.**~~ — **fixed
  2026-08-17.** The model was not confused about where the cause lives: it
  read `describe_pod`, saw `exit_code: 1`, and ended with "Next Step: check
  the container logs (`get_pod_logs`)" — naming the tool holding
  `FATAL: could not connect to db:5432` rather than calling it. The loop now
  sends a run back once when its answer names a registered tool the run never
  called, with that fact and no hint about where to look. n=10 before: 8/10
  (17/23 pooled with the earlier sets). n=10 after: 9/10, `get_pod_logs`
  called 10/10. The count is on the answer event as `nudges`, so a run that
  got there alone can be told from one that was sent back.
- ~~**Wrong-workload substitution.**~~ — **not an agent defect; the grader
  was scoring a true aside as a wrong answer. Closed 2026-08-17 evening**,
  10/10 on the current baseline and 50/50 across two probes. The original
  entry is kept below because its reasoning was sound and its conclusion was
  wrong, which is the part worth remembering: every clue pointed at the
  projection, and the projection was innocent.

  One instance remains genuinely unexplained: an earlier session saw a run
  asked about `crasher` answer about `log-shipper`, on `crashloop_root_cause`
  rather than this case. That case is 10/10 in both baselines and no such run
  has been recorded since answer text was kept, so there is nothing to read.
  Do not treat it as closed; treat it as unobserved.

- **The original entry, kept for its reasoning:** `healthy_workload_not_substituted` scored **8/10** in the
  2026-08-17 baseline: asked what is wrong with the healthy `healthy-web`
  deployment, two runs reported `memory-hog` and `bad-image` instead. A third
  instance turned up on `crashloop_root_cause`, where a run asked about
  `crasher` diagnosed `log-shipper`. **Both baseline failures had
  `nudges: 0`**, so this is not the loop guard, and the prompt already forbids
  it in as many words ("Answering about a different workload is worse than
  saying nothing") — wording is not the lever. Both failures called only
  `list_deployments`, which returns every deployment in the namespace, so the
  wrong workload is right there in the tool output next to the right one.
  Worth measuring first: whether the substitution survives a projection that
  answers about the named workload alone.
- **`cluster_wide_scan` drops workloads from its own summary — measured at
  n=98 on 2026-08-18. The mechanism was the tool's, not the model's, and the
  fix is measured.** An entry that stated no fault was dropped from the
  summary; giving it one stopped it.

  `evals/probe_scan_summary.py` runs the case alone and keeps every
  `scan_cluster` result in full beside the answer — the one thing no eval
  record held. `evals/analyse_scan_summary.py` reads it back as marginals over
  position, entry count and fault class. `--shuffle` permutes the entry order
  per run, same keys and values and count, because the fixtures sort
  deterministically and `log-shipper` sat at index 3 on every run, so position
  and identity were otherwise the same fact.

  **What it is.** A pod that is Running with a failing readiness probe reports
  status `Running`, and `Running` is its own fault class, so `scan_cluster`
  omitted the `fault` field under the "only when it adds something" rule. The
  entry read `{"status": "Running", "pods": 1, "example": ...}` — a row in a
  list of *failing* workloads with nothing in it saying anything had failed.
  Dropping that row is a fair reading of it.

  | arm (n=20 each, runs writing no summary excluded) | `never-ready` dropped | complete summaries |
  | --- | --- | --- |
  | sorted, before | 1/19 | 11/19 (58% [36-77]) |
  | shuffled, before | 7/18 | 8/18 (44% [25-66]) |
  | shuffled, after ×2 | 2/37 | 29/38 (76% [61-87]) |

  Fisher p=0.0036 on the entry, p=0.0329 on complete summaries, and every
  other entry unmoved at 4/123 → 16/258 (p=0.33) — that last row is the one
  showing the change did not simply make the model chattier.

  **Five hypotheses died on the way, and the order matters.** Not entry count
  (7 against 8: p=1.0). Not a fixed index — no position effect survives
  permutation. Not the workload: `log-shipper` fell 7/19 → 2/18 when nothing
  but the order changed. Not the nudge (p=0.48), not drilling past
  `scan_cluster` (p=1.0). Not look-alike adjacency either: that test looked
  positive until the control group was checked, and `never-ready` — the only
  entry with a unique status — was supplying 7 of the 10 drops in it. With it
  removed, 2.8% against 3.4%, p=1.0.

  **The fault-class signal is arrangement-dependent and that is not
  explained.** The no-fault entry drops 7/18 in the shuffled arm and 1/19 in
  the sorted arm, where it sits at index 6 of 7–8. The fix works in both (it
  removes the shape), but *why* sorted order protected that entry is open.

  **Worth testing next, cheaply: list length.** Pooled over the three shuffled
  arms, every run that saw nine entries dropped something (3/3) against 16/53
  at eight or fewer, p=0.035. Entry count was ruled out at 7 against 8, a
  range too narrow to have tested it. Three runs is not a finding. Raise the
  fixture count deliberately rather than waiting for CronJob pods to do it.

  **The eval sees a fraction of this.** `cluster_wide_scan` asserts
  `memory-hog`, `crasher` and `bad-image` — three of the eight workloads the
  tool returns — so five of the eight incomplete sorted-arm runs scored as
  passes. Widening `expect_all` would make the defect visible to the suite and
  would change what the published README number means, so it is a decision and
  has deliberately not been taken.

  **A defect this found and did not fix:** a CronJob pod is visible to the
  scan for the moment between its container reporting `Completed` and its
  phase reaching `Succeeded`, so `only_unhealthy` occasionally lists a pod
  that finished cleanly. `_is_healthy` exempts phase `Succeeded` and that
  moment precedes it. The first cut of the readiness label put
  `fault: not-ready` on exactly that entry against a live cluster; cleanly
  terminated containers are excluded now, so the entry is merely spurious
  rather than mislabelled.

  Two harness traps caught this session, both the same shape as the grader and
  the sleeping laptop. The first shuffled arm produced 12 runs that called no
  tool at all: Ollama builds the tool schema by introspecting the callables in
  `agent.TOOLS`, and an un-`functools.wraps`'d wrapper handed the model a tool
  named `wrapper` taking `**kwargs` with no docstring. A test pins the schema
  now. The second was mine to nearly believe — the second after-arm looked
  contaminated by fixture drift until the entry-count distributions were
  actually compared and turned out to be within one run of each other.

- **Eval `n=3` per case hides flaky cases.** `crashloop_root_cause` really
  passes ~85%, so at n=3 it reads 3/3 about 61% of the time.
- **Grounding cannot check reasoning.** Speculation next to measured facts
  still reads `grounded`. `inference_is_marked` at least tests the labelling.
- ~~**UI search filters only what the scan returned**, not the cluster.~~ —
  fixed 2026-08-15. It still filters the page first, but on no match it falls
  through to `scan_cluster(workload=...)`, which reports one workload by name
  whether or not anything is wrong with it. That is what separates "not on
  this page" from "not in this cluster" — the old message was honest about its
  scope and unusable as an answer, since the workload could be outside the
  limit or healthy and therefore never scanned.

## Pending development, in priority order

**1. ~~Make `nightly-sync` diagnosable.~~** **Done — 3/3 on 2026-08-17
evening, controller eval 16/16.** The first option below was the one taken,
and the code for it already existed; what was missing was an eval that ran
it. `capture_evidence()` at enqueue, handed to `diagnose()`, which is what
`run()` had been doing all along and what the eval had not.

The other two options stay unbuilt and stay named:

- **Diagnose Job/CronJob workloads from the Job**, which outlives its pods and
  carries failure counts and conditions — but not the logs, and the logs are
  where `FATAL: upstream returned 503` lives. Still true, still unnecessary.
- **Raise `failedJobsHistoryLimit` in the demo fixture** — makes the eval pass
  and fixes nothing real. Named only so nobody does it by accident.

**2. ~~Get a real baseline.~~** **Taken 2026-08-17: `results/baseline-n10.json`,
100 runs, all ten cases at n=10, under `caffeinate` at bf1f571.**

95% [89-98] 95% CI, median 59.9s, p95 138.6s, grounded 89/100, and
`slept_ms` zero on every run -- so the latency is the agent's rather than
partly the laptop's, and the 176.2s maximum is real rather than a nap.

Per case: `oomkill_root_cause`, `crashloop_root_cause`, `image_pull_failure`,
`service_unreachable_chain`, `service_selector_typo`,
`healthy_not_reported_broken`, `inference_is_marked` and `host_not_cluster`
all 10/10. `cluster_wide_scan` 7/10 and `healthy_workload_not_substituted`
8/10.

Re-measured after the correction, n=10 each: `cluster_wide_scan` 9/10 (from
7/10) and `crashloop_root_cause` 10/10 (held).

**Superseded the same evening by `results/baseline-n10-2.json`**, 100 runs on
current `main` at 1186c1f, and the README table is published from that set.
Both files are kept: this one is the only measurement of the nudge as it was
before 5678f11, and deleting it would leave the improvement unattributable.

**3. ~~Settle the stalls.~~** **Answered 2026-08-17 — and the answer was the
third of the three outcomes that probe was built to distinguish: the delay
was outside both the model and the tools.**

A 725s run against a 62s median reported `model_ms` 180.0s and `tool_ms`
0.05s. `pmset -g log` over the same window:

| event | at | asleep |
| --- | --- | --- |
| run starts | 10:43:57 | |
| `Idle Sleep` -> `DarkWake` | 10:44:34 -> 10:47:38 | 184s |
| `Maintenance Sleep` -> `Wake` | 10:47:48 -> 10:53:52 | 364s |
| run ends | 10:56:02 | **548s** |

548s asleep, 545s unaccounted. The host naps mid-run; every timer in the loop
was monotonic, and monotonic clocks do not advance through a suspend, so the
nap could only ever show up as missing time. `pmset -g custom` on battery says
`sleep 1`.

It explains every earlier observation, including the ones that killed the
other two hypotheses. Idle machines because macOS idle sleep counts HID input
and not CPU load, so an unattended run sleeps *because* nobody is typing.
Adjacent runs because one nap spans several. Model resident throughout because
nothing unloaded it. The cheapest case because a nap lands where it lands.

`timing` now carries `wall_ms`, `unaccounted_ms` and `slept_ms`; `run_eval`
prints `[host asleep Ns]` and keeps `slept_ms` per run. Run under
`caffeinate -is`. Numbers published before 2026-08-17 contain naps that cannot
be separated out after the fact.

**4. ~~Ground truth for the new fixtures.~~** Done, as a *separate*
investigation — `evals/ask_ai/config-reference-findings.yaml`
(INV-2026-08-15-002), not appended to `example-findings.yaml`, whose header
describes a different cluster and whose integrity claim covers only its own
run. Covers `billing-worker`, `cert-rotator`, `experiment-runner` and all of
`config-faults`, measured on kind v1.36.1.

The result that matters: these faults split by **env-var versus volume**, not
by ConfigMap-versus-Secret and not by missing-object-versus-missing-key. An
env reference puts the name in the container's waiting message, so
`describe_pod` is sufficient. A volume reference leaves the pod in
`ContainerCreating` with **no waiting message at all** and the name only in a
`FailedMount` event, so `get_pod_events` is required. Any eval case for
`cert-rotator` must therefore expect `get_pod_events` in the trace.

**5. ~~Wire `TRIAGE_STATE_DB` into the chart.~~** Done — `persistence.enabled`
adds the PVC, the env var and `podSecurityContext.fsGroup: 1000`. The fsGroup
is the part that is easy to miss: the volume arrives root-owned, the pod is
UID 1000, and without it the install succeeds and the controller crashloops.
Test it on `csi-driver-host-path`, never on kind's default StorageClass.

**6. Frozen benchmarks are provisional.** The 30 cases in
`evals/ask_ai/tiers.yaml` were chosen on judgement. A benchmark must
*discriminate* — show both passes and failures — and that cannot be known until
the suite has run once. `validate.py` now runs from the repo root, so the CI
gate is available: 427 prompts, 27 categories, 19 controls, clean.

## Not verified

- **Slack reply path has never run.** Socket Mode connects to real Slack, but
  replying needs a real `SLACK_BOT_TOKEN`. The token is shared on request and
  revoked straight afterwards, so **batch every reply-path check into one
  run** — there is no second attempt without asking for it again.
- **EKS.** Wanted, but deliberately gated on cost: run it only when the change
  under test is expected to pass, so it confirms rather than debugs. AKS and
  GKE have both been run against for real, including GKE's exec credential
  plugin and a live token expiry.
- **GKE is available on demand** and is the right cloud for anything needing a
  real managed cluster. AKS is not — that subscription is empty and its
  billing state is `Warned`.
- **`store.py` under more than one replica.** Note the controller Deployment
  hardcodes `replicas: 1` with no value to override it, so this is theoretical
  rather than reachable by configuration.

## Backlog

- **On-demand AI log analysis** (deliberately parked): one model call over an
  already-fetched log, shown *beside* the raw log. Must NOT go in `TOOLS`, and
  needs its own character budget.
- Tighter benchmark `n` for the published README table — 21 runs cannot
  distinguish a perfect agent from one that fails 15% of the time.
- ~~A version constant~~ — done. `version.py` holds `__version__`, the MCP
  server passes it, and a test asserts it matches `Chart.yaml`'s `version` and
  `appVersion`, since Helm cannot read Python and two files carrying one
  number drift silently. Bump all three together when tagging.
- ~~`/ask` is still synchronous.~~ — `/ask/jobs` already detaches it and was
  shipped without the docs catching up: the README claimed in three places
  that no job API existed. Corrected 2026-08-15, with round-trip tests added
  (submit → poll → `done` carrying the answer, and a raising job surfacing as
  `failed` rather than vanishing). `/ask` stays synchronous on purpose — it is
  the obvious thing to curl. What remains genuinely open is that a job result
  lives in one process's store, so it is one replica or nothing.

## Settled — do not reopen without new evidence

- **Removing the hedging sentence: the answer is NO.** It looked compelling at
  p=0.056 and did not replicate. The prompt is unchanged in `agent.py` and
  `mcp_server.py`.
- **PartialObjectMetadata for Secret existence: do not build it** (above).
- Slack credential rotation was dropped from tracking on my instruction. Do not
  re-raise it unprompted.

## Work style

Verify against real systems rather than asserting. State the measurement method
when publishing a number. Report intervals, not point estimates. Commit each
piece once verified and tested; **no Co-Authored-By, no Claude-Session, no
assistant attribution of any kind in commit messages.** Be blunt about what is
untested — and when evidence contradicts your own hypothesis, say so rather
than working around it.

Two sessions running, a symptom filed as "model behaviour" turned out to be
infrastructure. Measure the mechanism before designing a prompt experiment.

Budget note: ~₹260 of ₹300 GCP credit remains. A 1-node e2-small zonal cluster
is ~₹11/hour; e2-standard-4 plus a LoadBalancer is ~₹20–25/hour. Delete in the
same session, and remember a reserved static IP keeps billing after the cluster
is gone.
