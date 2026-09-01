# kubewhy — validation evidence

The authoritative record of what has been tested, how, and what the result does
and does not support. Four words are used and they mean specific things:

- **PROVEN** — tested directly, result reproducible
- **PARTIALLY PROVEN** — tested, but the evidence is narrower than the claim
- **UNDETERMINED** — measured, and the sample does not settle it
- **NOT TESTED** — no evidence; the claim is not made

## Summary

| Property | Status | Evidence |
|---|---|---|
| Automated test suite | **PROVEN** | 1319 passing; no cluster or model, and a real Postgres for the shared-state cases |
| Grounding replay | **PROVEN** | 1489 recorded runs, reproducible from the repository |
| Investigation context integrity | **PROVEN** | 20 tests, two workloads in different namespaces, verified live |
| Entity scoping | **PROVEN** | 135/145 targets extracted; 0.7% / 0.0% wrong-target |
| Grounding + contradiction | **PROVEN** | caught a genuine wrong claim live, 5/5 reproducibly |
| Bounded investigation deadline | **PROVEN** | 38 tests incl. fallback-cannot-reset |
| Security regression (UI) | **PROVEN** | credentials absent from page, no client-side calls |
| Console authentication | **PROVEN** | kind + real OIDC issuer; console unreachable from another pod |
| Rate limiting | **PROVEN** | real loop against the real API: 3 investigations at a ceiling of 3, then 429 with an accurate `Retry-After` |
| External token budget | **PARTIALLY PROVEN** | charged through the real gateway with a stub provider; no hosted provider was billed |
| Forged identity header | **PROVEN** | overwritten by the proxy, measured with a session held |
| Per-user authorization | **NOT TESTED** | deliberately not implemented — see SECURITY.md |
| Audit trail (CLI, REST) | **PROVEN** | live runs; evidence absent from the record |
| Audit trail (console) | **PROVEN** | live run through a browser against a real cluster |
| Audit trail (controller) | **PROVEN** | live unprompted run, attributed to controller/system |
| Audit trail (Slack) | **NOT TESTED** | unit tests only; needs a workspace |
| Restart-interrupted jobs | **PROVEN** | SIGKILL mid-run, restarted against the same state file |
| Shared state (Postgres) | **PARTIALLY PROVEN** | full store contract + lease race against a real Postgres 17; never run as two replicas in a cluster |
| High availability | **NOT TESTED** | no failover observed; the lease and the owner-scoped restart sweep are unit-proven, the behaviour they enable is not |
| Read-only RBAC | **PROVEN** | runtime validated on GKE by attempting operations |
| GKE runtime | **PROVEN** | released chart, real cluster |
| GKE / Calico NetworkPolicy | **PROVEN** | dataplane-enforced egress |
| Local Ollama inference | **PROVEN** | 145 live investigations |
| Hosted OpenAI API inference | **PROVEN** | 145 live investigations |
| In-cluster inference | **PARTIALLY PROVEN** | Ollama, and the `vllm` provider against a real OpenAI-protocol server |
| AKS runtime | **PARTIALLY PROVEN** | non-AAD single node |
| Model comparison | **UNDETERMINED** | p = 0.3438, paired, n=5 |
| Generalized diagnostic accuracy | **NOT TESTED** | one cluster, one prompt configuration |
| Real vLLM | **NOT TESTED** | wire path proven; vLLM's own tool-call parser is not |
| EKS | **NOT TESTED** | auth verified by reading the client |
| Browser paint automation | **NOT TESTED** | designed in E2E.md; one case (R-01) confirmed by hand and fixed |
| Mutation testing | **PARTIALLY PROVEN** | `evals/mutate.py`, 16 of 18 modules, 697 mutants, 508 killed (72.9%); 189 survivors unreviewed; `controller.py` and `ui.py` not surveyable until their tests stop needing a live cluster |

## Defects found and fixed

Each of these was found by testing, not by review. The pattern is the same
throughout: **test → failure → root cause → fix → regression test →
re-validation.**

### 1. Endpoint classification bypass (HIGH, adversarial validation)

**Problem.** An external endpoint could be spelled so it classified as internal,
defeating the external-data policy entirely.

**Detection.** Adversarial validation, deliberately attacking the egress
boundary.

**Root cause.** The classifier and the HTTP client parsed the endpoint
*separately*. IDN full stops (`。`) and integer-form IPv4 normalised differently
in each. Two parsers on one string agree only by coincidence.

**Fix.** Both normalise through the same parser, by construction. IP literals are
short-circuited before normalisation — a first repair broke IPv6, classifying
`::1` and `fd00::1` as external.

**Regression evidence.** Classifier tests including IDN, integer IPv4 and IPv6
literals. Shipped in v0.1.8; **v0.1.7 must not be used.**

### 2. Target re-derived from the prompt (HIGH)

**Problem.** Every scoped investigation died on
`{"result": "no workload named example exists in this cluster"}`.

**Detection.** Driving the console by hand. It reproduced identically on
gpt-4o-mini *and* qwen3, which is what showed it was not the model.

**Root cause.** The loop discarded the target it was handed and recovered it by
parsing the prompt `scoped_question()` had just written. `_NAME_FIRST` matches
`"<name> <kind>"` — the shape of "the crasher deployment" — so
`(for example pod nightly-sync-abc)` yielded a workload called `example`. Removing
that phrase yielded `other`, from "Do not report on any **other workload**".
`enforce()` then rewrote every call to the phantom, **including calls the model
had got right.**

**Fix.** `scoped_target()` builds the target from the same selection the prompt
is built from; `stream(target=…)` uses it verbatim. Parsing survives only for
surfaces that genuinely have only a sentence.

**Regression evidence.** 20 tests in `tests/test_investigation_identity.py`,
two workloads in different namespaces; 14 confirmed red against the previous
code. Verified live: 16/16 chain-identity checks across two real investigations.

### 3. Investigation deadline was per provider, not per investigation

**Problem.** A fallback got a fresh deadline. A 2s budget produced a 4.01s run.

**Detection.** A requirement written down, then tested against.

**Root cause.** The deadline was computed per `chat()` call rather than per
investigation.

**Fix.** One deadline per investigation, shared. When exhausted the fallback is
skipped and logged as `fallback_skipped_deadline_exhausted`.

**Regression evidence.** 38 deadline tests, including a boundary case where a
first repair raced (`remaining()` returned 0.001s positive) and an `int()`
truncation that fired a second early.

### 4. Contradiction detection: two false-positive classes

**Problem.** The first contradiction rules produced **six false positives and
zero true ones** against the recorded corpus.

**Detection.** Corpus replay, before shipping.

**Root cause.** (a) any number near "cpu"/"memory" was read as a limit — the
`stress` fixture logs `dispatching hogs: 0 cpu, 0 io` and six correct answers
quoting it were scored as claiming a CPU limit of zero; (b) negation was not
handled — `no OOMKilled reported` matched on presence.

**Fix.** The numeric rule requires the word `limit` or `request`; a negation
window was added.

**Then two more, live.** After 432 real investigations: an absence claim attached
to the wrong entity, and "to avoid OOMKilled" read as an assertion. Fixed with
`_absence_is_about()` and a prospective-framing guard. **Only running live found
these** — no recorded answer had ever phrased it that way.

**Regression evidence.** `tests/test_contradiction.py`, where the
false-positive half is the larger half.

### 5. Grounding could say CONTRADICTED but not SUPPORTED

**Problem.** Both models answered `service_selector_typo` correctly and both
scored `insufficient_evidence` — "nothing here could be checked" — while
`get_service_endpoints` had returned the empty lists that settle the claim.

**Detection.** The n=1 evaluation baseline, then confirmed as systematic across
45 recorded runs.

**Root cause.** The contract recognised measurable figures and known statuses.
The answer asserts a *relation* with neither, so `checked == 0`.

**Fix.** The same `_ABSENCE` predicate and `endpoints_total` fact already driving
the contradiction rule, in the other direction. A confirmation requires the tool
to have been **called** — confirming an absence from silence would be an
unfalsifiable tick.

**Regression evidence.** Replayed over 907 runs **three times**, because the
first two drafts were wrong: draft 1 called 9 correct answers contradicted
(ready and not-ready endpoints were counted together); draft 2 called 2 more
contradicted (a heading and a conditional antecedent). Final: **45
`insufficient_evidence → grounded`, 0 regressions.** 9 new tests.

### 6. A console that had never rendered its worst case

**Problem.** `st.error(icon="✕")` is not a valid emoji. Streamlit raised and
**blanked the page on every contradiction** — the one verdict most worth reading.

**Detection.** The first test that rendered a contradiction at all.

**Root cause.** No test had ever produced a contradiction to render.

**Fix.** A valid icon, and the AppTest helper now asserts `app.exception` is
empty on every panel test.

### 7. The investigation target moved on its own

**Problem.** The selected workload silently became a different one.

**Detection.** Observed in the browser, then reproduced deterministically.

**Root cause.** Two, found in sequence: the selectbox had no `key`, so selection
was positional and a re-ordered scan moved it; and when the selected workload
*left* the scan — which `only_unhealthy` and a CronJob both cause routinely —
the index fell back to 0. Measured: `demo/nightly-sync → demo/bad-image`, no
warning.

**Fix.** Keyed and re-anchored by value; a workload that leaves the scan stays
selected with a warning naming it.

**Regression evidence.** Both cases tested and confirmed red against the
previous code. Verified live by repairing a workload underneath a selection.

### 8. A nondeterministic evaluation fixture

**Problem.** `cronjob_runs_are_one_workload` failed because the pod it was told
about was deleted mid-investigation.

**Root cause.** The CronJob fired every minute keeping two failures — ~2 minutes
of pod life against a 72s-median investigation.

**Fix.** In the **fixture**, not the agent and not the expectation: `*/5` with six
retained, ~30 minutes against a 4-minute worst case.

### 9. A console probe that could never pass

**Problem.** With `ui.auth.enabled=true` the console pod sat `1/2 Running` with
four restarts, forever. The proxy beside it was healthy the whole time.

**Detection.** Installing the chart on kind. `helm template` renders the broken
probe and the working one identically, and twenty chart tests were green.

**Root cause.** The kubelet probes the **pod IP**. Authentication binds the
console to `127.0.0.1`, so `httpGet` dialled `10.244.0.7:8501` and got
`connection refused` — readiness kept the pod out of the Service and liveness
killed the container every 40 seconds.

**Fix.** An `exec` probe reaching the console over loopback from inside the
container, which is also the address the proxy actually uses. The proxy keeps
its `httpGet`: it binds every interface, and converting it too would be
cargo-culting the fix. After: `2/2 Running`, 0 restarts, endpoint 4180 only.

**Regression evidence.** Five tests asserting the probes do not dial the pod IP
and do follow a changed `ui.port`; the defect restored turns three of them red.

### 10. NOTES.txt told operators the console was unauthenticated

**Problem.** After installing *with* authentication, `helm install` printed "it
has no authentication" and a `port-forward` to a port that is in no Service.

**Detection.** Reading what the install printed. Nothing else could have: `helm
template` does not produce NOTES.txt at all, so no test had ever rendered it.

**Fix.** Both branches written, and the notes now say plainly that
authentication is not authorization — the single most likely misreading of this
feature, and the one that would put kubewhy in front of two teams that must not
see each other.

**Regression evidence.** Four tests rendering NOTES.txt through a dry-run
install, which is the only way a test can see what an operator is told.

## What the console authentication was tested against

Not a mock. Dex v2.41.1 as a real OIDC issuer and oauth2-proxy v7.7.1, first in
containers sharing one network namespace — which reproduces a pod's, so the
proxy's `--upstream=http://127.0.0.1:8501` was the chart's argument verbatim
rather than one rewritten for the test — and then on kind v1.32.2 through the
installed chart.

| Check | Result |
|---|---|
| console port from another pod | `ConnectionRefused` |
| proxy port from another pod | open |
| unauthenticated through the Service | 302 to the issuer, app never reached |
| `/_stcore/stream` unauthenticated | 302 — the websocket is gated, not just `/` |
| forged `X-Forwarded-Email`, no session | 302 |
| **forged `X-Forwarded-Email`, valid session** | **upstream received the real address** |
| `Authorization` header forwarded upstream | none |
| websocket handshake through the proxy | `HTTP/1.1 101 Switching Protocols` |
| console rendered in a real browser | yes, sidebar reads the issuer's address |

The forged-header row is the one that matters: it is the property the whole
design rests on, and it is measured rather than assumed. oauth2-proxy
overwrites the client's header rather than appending to it.

**A measurement that changed the design.** uvicorn 0.51.0 rewrites
`request.client.host` from `X-Forwarded-For` by default, trusting the header
from `127.0.0.1` — precisely the sidecar case. Against a live server with
`X-Forwarded-For: 203.0.113.9` from loopback, `client.host` reads `203.0.113.9`
under the default flags and under an explicit `--proxy-headers`, and
`127.0.0.1` under `--no-proxy-headers`. So the API's loopback peer check
refuses every *legitimate* proxied request unless that flag is set, while still
catching a direct one. The refusal message names the rewrite, because in a
working deployment a missing `--no-proxy-headers` is a likelier cause than an
intruder.

That one is documentation rather than an enforced property, and the gap is
worth naming: the chart ships the controller and the console, not the API, so
there is no template to pin the flag in. The console is unaffected — Streamlit
exposes no peer address, so it passes `peer=None` and relies on the bind.

**What this does not establish.** One issuer, and a self-hosted one. No SaaS
provider has been tested, and `networkPolicy.enabled=true` cannot reach one
anyway — it selects the console pod and permits egress only to private address
space. Nothing here is evidence about authorization, which does not exist.

### 11. The audit trail credited every API investigation to nobody

**Problem.** An investigation run through `POST /ask` produced an audit record
reading `principal: anonymous, auth: unknown, surface: unknown`. The request
log line immediately beside it named the caller correctly.

**Detection.** Running a real investigation through the API against a real
cluster and reading the record. Every unit test passed, because they drove
`agent.stream()` directly and never crossed the ASGI boundary.

**Root cause.** FastAPI runs a **sync dependency on an AnyIO worker thread**.
A ContextVar set there lives in that thread's copied context and is discarded
when the dependency returns, so `audit.actor()` — called from
`require_caller` — never reached the loop. Measured against a live app: a
value set in middleware is seen by both sync and async endpoints; one set in a
sync dependency is seen by neither.

**Fix.** Computing identity was separated from refusing on it.
`authenticate()` decides who a request is and whether it should be refused,
never raising; the middleware calls it before dispatch and stores both on
`request.state`; `require_caller` only enforces. Identity is now computed once
rather than twice, which is also why the two log lines can no longer disagree.

**Regression evidence.** Three tests, one of which asserts the request line
and the audit record agree — they disagreed, and that is what made the defect
survivable. Restoring `audit.actor()` to the dependency turns all three red.

## What the audit trail was verified against

Live runs on a kind cluster with the `demo/broken-pods.yaml` fixtures and
qwen3 on local Ollama, one per surface that has a different actor:

| Surface | principal | auth | Record |
|---|---|---|---|
| CLI | the OS account | `os` | 5 tool calls, verdict `partial` |
| REST `/ask` | `sre@example.com` | `proxy` | 4 tool calls, verdict `grounded` |
| Console | `anonymous` (no proxy in that run) | `anonymous` | 3 tool calls, verdict `grounded`, question recorded as typed rather than as scaffolded |
| Controller | `controller` | `system` | unprompted run on a newly-failing workload, verdict `grounded` |

**Slack is not in this table and is not claimed.** It uses the same hook and is
covered by unit tests, but testing it needs a workspace, and the API defect
below is exactly what a unit test could not see.

The records that read logs named the pod. **None carried the logs.**
The demo pod's actual output is `FATAL: could not connect to db:5432:
connection refused`; searching the record of the run that read it for
`connect`, `5432`, `db`, `Traceback` and `error` returns nothing, while
`sensitive_reads` names the pod. That is the property this design exists for,
and it is measured rather than asserted.

**What this does not establish.** Slack is wired and untested live. Its wiring
is covered by unit tests and the hook is the same one, but that is precisely
the evidence that failed to catch the API defect above, so it is listed as NOT
TESTED rather than assumed to follow.

**An environment note, because it affected the testing rather than the code.**
Another process on the same machine created and deleted a kind cluster
mid-session, which rewrote `current-context` and then unset it. The controller
kept retrying its watch against the API server port it had resolved at startup
and logged `watch_restarting` each time — the correct behaviour, and the same
hazard `active_context()` exists to describe. Nothing was wrong with kubewhy;
the run was repeated once the machine was quiet.

### 12. An /ask job that a restart left running forever

**Problem.** With `TRIAGE_STATE_DB` set, a job that was `running` when the
process died survived the restart still marked `running`, with no thread
anywhere that would ever finish it. A caller polling `/ask/jobs/{id}` waited on
an investigation that could not complete.

**Detection.** Writing the restart runbook — specifically, filling in the row
of a table that asked what each piece of state costs. Persistence made the bug
visible rather than causing it: without a state file the job vanished and the
404 told the caller to ask again.

**Fix.** `fail_interrupted()` at startup marks anything `queued` or `running`
as failed, with a message saying what happened and that re-asking will work.
Nothing resumes the work: the thread is gone, and re-running someone's question
unasked is not a decision this process makes quietly. An already-failed job is
left alone, because its own error is the only diagnosis anyone has.

**Regression evidence.** Seven tests across both store implementations, and a
live check: an API killed with SIGKILL mid-investigation and restarted against
the same state file read `running` before and `failed` with the message after,
with `jobs_interrupted_by_restart count: 1` in the startup log.

### 13. Audit records with no timestamp

**Problem.** Records appended to `TRIAGE_AUDIT_LOG` carried no time at all.

**Detection.** Writing the runbook's `jq` examples and running them against
real records — the query referenced `.ts`, which exists only on the copy the
log formatter stamps. The file copy, which is the one that gets shipped, had
nothing.

**Root cause.** The timestamp belonged to the logging framework rather than to
the record, so the second sink never got one.

**Fix.** `at`, UTC, in the payload, so both copies carry it. The record also
gained `cluster`: a namespace and a pod without a cluster name is ambiguous the
moment anyone works against two, and the console can switch context
mid-session.

**Regression evidence.** Four tests, including one asserting the file sink's
copy is parseable as a timestamp. Removing it from the payload turns two red;
using local time instead of UTC turns one red.

### 14. The grounding replay was not in the repository

**Problem.** This document claimed `Grounding replay — PROVEN — 907 recorded
runs, 0 regressions`. Nothing committed could reproduce it. The script existed
during development and was never checked in, which is the same criticism
FUTURE.md makes of the mutation harness.

**Detection.** Looking for a scheduled job to attach it to. A result nobody
else can re-derive is a claim, not evidence, and this document is supposed to
be the place that distinction is kept.

**Fix.** `evals/replay_grounding.py`, committed, with a CI job that runs it on
every push, every pull request and weekly. It re-scores each record's **draft**
and **evidence** — the two inputs the checker was originally handed — and never
`answer`, which has already been through `verify()` and `annotate()` and would
reproduce a different verdict for reasons that are the tooling rather than the
change under test.

It carries guards for all three ways a replay has lied here. The loaded
modules' paths and hashes are printed every run, so a stale `__pycache__` is
visible. `_assert_not_shadowed()` refuses to run when `grounding` resolves
anywhere but the repository root, because a copy beside the script silently
wins — Python puts the script's own directory ahead of `PYTHONPATH`. And
`--self-check` scores the corpus with a deliberately perturbed checker and
fails if the replay does not notice, which is the only way the script can
demonstrate it is exercising the code it claims to. **CI runs the self-check
first, and separately**, because a replay wired to nothing reports "no
regressions" and looks exactly like a clean run.

**What the replay now says.** Of 1489 replayable records, 1429 score
identically under current code and **60 moved**. Every transition is a
documented fix taking effect on records written before it:

| Transition | Count | Cause |
|---|---|---|
| `insufficient_evidence` → `grounded` | 45 | Defect 5, the absence rule in the SUPPORTED direction — the same 45 that entry reports |
| `partial` → `grounded` | 2 | Same fix; a relation claim that is now confirmable |
| `insufficient_evidence` → `contradicted` | 2 | Same fix in the other direction; inspected, and a true positive — the answer claimed a service had no endpoints while `get_service_endpoints` reported one |
| `contradicted` → `grounded` | 5 | Defect 4 (1) and defect 17 (4), both false positives removed; each draft is a correct diagnosis |
| `contradicted` → `partial` | 1 | Defect 4, with one claim still unsupported |
| `grounded` → `contradicted` | 4 | Defect 18, the OOM spelling hole — inspected, all true positives, all recorded `passed: True` |
| `partial` → `contradicted` | 1 | Same |

The four that moved on 2026-08-28 are defect 17 below — `_absence_is_about`
recognised a backticked identifier and not a bolded one, so the same clause
was a false contradiction or not depending on how the model chose to format a
name. This is the case the replay exists for: the rule had been fixed once,
against the spelling that happened to be in the corpus that day.

**1032 records were skipped** because they retain no `draft` or `evidence`.
Older runs did not keep them. The count is printed so a shrinking corpus is
visible rather than silently reducing the replay to nothing.

The 45 figure is worth noting on its own: it was written into this document
from a replay nobody could re-run, and a committed tool now reproduces it
exactly.

### 15. Four defects behind a green suite, found by breaking the code

`evals/mutate.py` applies one mutation at a time — a comparison flipped, a
boolean operator swapped, a `not` dropped, a constant nudged — and reports the
mutations no test failed on. FUTURE.md listed this as NOT TESTED from the
beginning: a harness existed during development, killed 28 guards, and was
never committed. It is the second of the two tools in that position; the
grounding replay was the first.

It never edits the working tree. Every mutant is written into a throwaway copy
of the repository, which costs about 4MB and a fraction of a second, and which
is why a mutant that hangs or a process killed at the wrong moment cannot leave
a comment-stripped source file behind.

**What it found, in three modules written the same week:**

> **Every number in the first version of this table was measured with a broken
> harness and has been re-measured.** See "The harness was scoring mutants it
> never ran" below. The corrected figures are in the second table; the numbers
> the repository published before 2026-08-31 were, in four modules, too
> generous.

| Module | Published | **Corrected** | Note |
|---|---|---|---|
| `identity.py` | 20/20 | **20/20** | unchanged |
| `audit.py` | 33/41 → 40/40 | **40/40** | unchanged |
| `redaction.py` | 6/6 | **6/6** | unchanged |
| `limits.py` | 24/28 → **28/28** | **27/28** | the "perfect coverage" was a false kill |
| `targeting.py` | 64/74 → **69/74** | **66/74** | 3 survivors were hidden |
| `contradiction.py` | 75/117 → **76/117** | **74/117** | 2 hidden |
| `grounding.py` | **93/118** | **92/118** | 1 hidden |

**Seven real survivors were hidden inside published numbers**, in both checker
modules and in `limits.py`, whose row in the table at the top of this file
reads PROVEN.

> **The `limits.py` survivor was described wrongly here, and the description
> was load-bearing.** Corrected 2026-09-01 — see "limits.py:140 is an
> equivalent mutant" below. The text said the survivor was the `+ 1` in
> `max(int(when + self.seconds - now) + 1, 1)` and that mutating it "shifts
> every `Retry-After` header by a second and no test notices". Both halves are
> false: that expression is line **139**, and its `+ 1` is **killed** by three
> tests that pin the value exactly. The survivor is the floor on line **140**,
> a different statement.

**The eleven modules that had never been surveyed at all**, measured
2026-08-31 with the fixed harness:

| Module | Killed | Survivors |
|---|---|---|
| `tool_schema.py` | 5/7 | 2, both equivalent — see below |
| `mcp_server.py` | 1/2 | 1 |
| `slack_socket.py` | 10/17 | 7 |
| `telemetry.py` | 16/27 | 11 |
| `podcache.py` | 7/18 | 11 |
| `sinks.py` | 18/31 | 13 |
| `store.py` | 18/30 | 12 |
| `backends.py` | 19/37 | 18 |
| `inference.py` | 89/125 | 36 |
| `controller.py` | — | **not surveyable** |
| `ui.py` | — | **not surveyable** |

**`controller.py` and `ui.py` cannot be surveyed, and the reason is a defect,
not a limitation.** Both baselines fail, so `mutate.py` refuses rather than
reporting perfect coverage. `tests/test_controller.py` and `tests/test_ui.py`
each reach a **real cluster** at the kubeconfig's current server and burn three
15-second retries per call. They pass inside the full suite — 1280 in 83s with
no cluster running — and hang when run alone, so something in another test
module is installing the Kubernetes mock they depend on. The side effect is
that mutation testing silently skips the two largest modules that have test
files. **Fixing the isolation is a prerequisite for surveying them**, and the
isolation bug is worth fixing on its own account.

**Totals, 16 modules surveyed: 697 mutants, 508 killed, 189 survivors, a 72.9%
kill rate.** The earlier note that "41 survivors remain and are NOT reviewed"
was an artifact of both the broken harness and of surveying 7 modules. The real
figure is 189, and they are still not reviewed.

#### The harness was scoring mutants it never ran

Found 2026-08-31, fixed in `2b8d58b`. `mutate.py` writes each mutant as
`ast.unparse` output back to the same path. Two consequences meet: consecutive
mutants differ by one character and are therefore **the same size**, and
adjacent sites are written well inside the same second. CPython validates a
cached `.pyc` against `(mtime-to-the-second, size)`, so the next run imported
the **previous mutant's bytecode** and the tests never saw the new code.
`-p no:cacheprovider` does not help; that is pytest's cache, not CPython's.

**The error is not conservative.** Which way a verdict goes depends on what was
cached:

- A benign predecessor cached, lethal mutant follows → the tests pass → a
  **false survivor**. Measured on `tool_schema.py`:
  `doc.split("\n\n", 1)[0] -> [1]`, which two tests assert against directly.
  Applied by hand it fails both.
- A lethal predecessor cached, any mutant follows → the tests fail → a
  **false kill**. This is the one that manufactured coverage:
  `limits.py` read 28/28 with the bug and 27/28 without it.

The fix is `PYTHONDONTWRITEBYTECODE=1` in the subprocess environment — with no
`.pyc` written there is nothing stale to reuse. Verified in both directions by
reintroducing the bug in a throwaway repository copy.

**The self-check needed three attempts to see it, and that is the part worth
carrying.** Version one let the clock run: `limits.py`'s suite is slow enough
that the two writes landed in different seconds, so it **passed against the
exact bug it was written to catch**. Version two forced the timestamp but made
both writes lethal, so the stale bytecode failed the tests too and it passed
either way. Only a green baseline followed by a lethal same-size mutant at a
forced identical mtime actually goes red when the bug is present. A counter
that cannot see the mechanism is worse than no counter, because it reads as
evidence.

**`tool_schema.py`'s two remaining survivors are equivalent mutants, not
gaps.** `doc.split("\n\n", 1)` → `split("\n\n", 2)` cannot change a `[0]`
subscript; and `name or func.__name__` → `and` is undetectable while the
registry key and `__name__` match, which is exactly what that line's comment
says it is guarding against for the day they diverge.

**The two checker modules were surveyed on 2026-08-30**, because this session
had just found two defects in them and the question "what else is in there
that no test would notice" was the obvious next one. `grounding.py` came back
93 of 118 and `contradiction.py` 75 of 117, and four new tests moved the latter
to 76 — **all three of those readings were taken with the broken harness and
are superseded by 92/118 and 74/117 above**. The four tests were real and are
kept; the movement they were credited with is not measurable. The reason so few
were added is still worth stating: most of
its survivors are guards that cannot change behaviour through the public API,
because every caller checks `phrase in text` before calling the function whose
`find() < 0` branch the mutant flips.

The three that were real:

- **`_asserted`'s `start < 0`.** As `<= 0` a claim opening the sentence reads
  as not asserted — so an answer that leads with the wrong cause, the most
  emphatic place to put one, would not be contradicted at all.
- **The readiness default.** `found.get("ready", True)` decides what an
  unseen container counts as; flipped, a pod with no readiness information
  reads as unready.
- **`_entity_present` skipping evidence that says "not found"**, without which
  the absence rule contradicts a correct answer using the very tool result
  that agrees with it.

**189 survivors were recorded as a count, and the list itself was not kept** —
the figure was 41 when only 7 modules had been surveyed and the harness was
miscounting both ways. A mutation score is not a quality score, and this
project has said so since the harness landed.

Reviewing them needs the list, not the number, so `mutate.py` now takes
`--json` and writes one. It also takes `--sites`, which is what makes a second
pass possible: pass 1 runs the narrow default suite, pass 2 re-runs *only pass
1's survivors* against every suite that exercises the module. A pass 1 survivor
that dies in pass 2 was never a gap — it was test selection, and `targeting.py`
is the standing example (64/74 narrow, 67/74 broad, no test written in
between). Two tests pin the property the second pass depends on: that a site
index names the same mutation in both passes. Both were confirmed red against
a filter-then-number implementation before being kept.

#### `limits.py:140` is an equivalent mutant, not the standing proof

This one survivor was carrying an argument — it was cited in the handoff and
above as proof that a real gap hides among the 189, on the strength of having
turned up inside a module the docs recorded as 28/28. Reviewed 2026-09-01, it
does not support that.

The survivor is site 24, `return max(int(self.seconds), 1)` → `max(..., 2)`:
the **fallback return after the loop**, not the round-up. Three facts settle
it:

- The round-up on line 139 is site 21, and it is **killed**
  (`mutate.py limits.py --sites 21` → 1 mutant, 1 killed). Three tests pin the
  value exactly — `== 2` at `seconds=1`, `== 11`, and `== 1` on a fractional
  remainder. The claim that "no test notices" was about the wrong site.
- Line 140 is reached only when the loop over every event never sees
  `running < limit`. Since `running` reaches 0, that requires `limit <= 0`.
- `limit <= 0` cannot arrive through the public API. `_int` raises on a
  negative (`use 0 for unlimited`), and both call sites in `check()` are
  gated on `if ceiling:` and `if budget:`, so 0 never reaches `retry_after`
  either.

Confirmed by search rather than by argument, which is this document's own
standard: 200,000 random trials over `seconds ∈ {0.5, 1, 1.5, 2, 3, 10, 100,
3600}`, `limit ∈ {-2 … 5}`, zero to four events with random amounts and a
random `now`, comparing the original and the mutant. **32,237 inputs separated
them and every one had `limit <= 0`; none had `limit >= 1`.** The floor also
only bites when `int(self.seconds) < 2`, against a `WINDOW_SECONDS` of 3600.

It belongs in the same category the harness already documents — a defensive
branch nothing can reach — and no test was written for it, deliberately.

**What this does not establish.** One of 189 is reviewed. It says nothing about
the other 188, and the reason the review is worth doing is unchanged: the
survivors were never read. What it does remove is the specific claim that a
real gap had already been demonstrated among them.

`targeting.py` is also the example of why the default test selection matters:
run against `tests/test_targeting.py` alone it scored 64/74, and against its
real test set (adding `test_investigation_identity.py`) 67/74 before any new
test was written. The three-mutant difference was test selection, not coverage.

**And the harness itself was wrong at first.** `Sites` recorded a node before
descending into its children while `Apply` mutated after, so the nth reported
site and the nth applied mutation were different things: the counts were
right and every line number pointed somewhere else. It was caught by reading
`targeting.py` survivors that made no sense — a mutant labelled `Eq -> NotEq`
had swapped an `and` for an `or` two lines away. The first `audit.py` result
published here, 40/40, was produced under that bug; the true figure was 39/40,
and the survivor was a rounding precision the test could not distinguish. Both
are fixed, and two tests now assert that the reported line is the line that
actually changed and that the named operator is the one that moved.

That is the third harness in this document to report something it had not
earned, and the second to do so while looking completely healthy.

**`mutate.py` has now done it twice.** The site-indexing bug above and the
stale-bytecode bug documented earlier on this page are independent defects in
the same 400-line tool, both silent, both found only by reading a specific
survivor and refusing to accept it. The pattern this project keeps rediscovering
is that a measurement harness fails in the direction of looking healthy, and
that the only reliable detector is a result that does not make sense on its own
terms — `limits.py` going *down* from a published 28/28 is what exposed the
second one.

The `identity.py` one was the most useful and the least expected. Nothing in
the project compares two Principals, so `__eq__` was unused surface — and
defining it without `__hash__` had silently made the class **unhashable**, a
trap for the next person to key a dict or set on a principal. Which is exactly
what per-caller rate limiting would reach for, and was written three days
later. Removing `__eq__` fixed it; a test now pins hashability.

Two more were docstring claims nobody had checked. `emit()` says it is
idempotent "because `finally` can run more than once"; flipping the flag left
every test green. `duration_ms` appeared in every audit record and four
separate mutants on that line survived, including one that divided where it
should multiply.

**Mutation score is deliberately not reported as a number.** Some mutations
cannot change behaviour — a bound never reached, a constant used only in a log
line — so a percentage invites raising it by writing tests for equivalent
mutants. Three of the survivors above were run through a search over thousands
of generated inputs to find one that separated the mutant from the original;
two were separable and became tests, and the third was not and is documented as
a defensive branch that `_trim` makes unreachable.

**What this does not establish.** Three modules out of roughly twenty were
surveyed, and they are three that were written this week with mutation testing
in mind by the end. The default test selection is `tests/test_<module>.py`,
which under-selects for modules exercised through other suites — a survivor
count taken that way is an upper bound on the gaps, not a measurement of them.
The rest of the codebase is unsurveyed and is not claimed otherwise.

### 16. The contradiction panel printed its own markup at the reader

**Problem.** Every contradicted verdict rendered
`<span class='kw-dim'>rule: ...</span>` as literal angle-bracket text inside
the red box — the one verdict this project says is most worth reading.

**Detection.** A browser. `st.error` accepts no `unsafe_allow_html` and
escapes its body, which was known from the API surface and recorded in
[E2E.md](E2E.md) as case R-01, the case that justified a browser suite
existing at all. It had never been confirmed visually. Confirming it took one
Streamlit page rendering the two variants side by side.

**Why no existing test could see it.** `tests/test_ui.py` has 37 tests over
the element tree, and `element.value` is the string that was *submitted*, not
the text that was *painted*. No assertion over that tree can distinguish them,
ever — which is exactly the argument E2E.md makes.

**Fix.** Markdown backticks, which `st.error` does render, and which suit a
rule name anyway.

**Regression evidence.** `tests/test_ui_markup.py` walks `ui.py`'s AST and
fails if any escaping widget is handed markup. A static check rather than a
browser test: weaker than a screenshot, far cheaper, and it covers all twelve
call sites rather than the ones a test happens to render. Reverting the fix
turns it red. It also asserts it found at least five call sites, because a
scanner that matched nothing would pass this file forever.

**What this says about the browser suite.** The finding that justified it was
delivered without it. That is not an argument against building the harness,
but it is an argument for reading E2E.md's own table first: two of the three
defects it lists were fixed in AppTest, and the third needed a screenshot once
rather than a suite forever.

### 17. Readiness evidence, a contradiction nobody acted on, and a bolded name

Three defects behind the two eval cases that had sat at 0/5, found by reading
the recorded runs rather than re-running them.

**17a. A Running-and-not-Ready pod had no evidence policy.** The two other
policies key on the status string, and this pod's status is `Running` — the
same word a healthy pod reports. Nothing terminated, nothing waiting, every
field in the status block normal, and the only record of the failure in the
kubelet's `Unhealthy` Event. All 5 recorded runs of
`never_ready_readiness_probe` answered without calling `get_pod_events`,
recorded `policies: 0`, and invented a cause.

The **ordering** is the measured part. Replayed over the 1472 recorded runs
whose case still exists, a readiness check placed first fires on 16 and takes
the policy slot from 4 `cluster_wide_scan` runs that had spent it on logs (3)
or events (1) — a crashing pod's logs traded for a not-ready pod's events,
which is the failure the logs policy was hardened against. Placed last it
fires on 12, every one a failing `never_ready` run, and displaces nothing.

**Live result: 0/5 → 5/5** on kind + qwen3, Fisher exact p=0.0079, the floor
at 5 against 5. `policies: 1` on all five and `get_pod_events` called 5/5
against 0/5, so no run reached the events unaided. 5/5 is Wilson 95%
[57-100] and remains a smoke test.

**17b. A contradiction was detected and nothing acted on it.**
`termination_reason_vs_memory_cause` caught the OOMKilled claim on
`scoping_quiet_workload_beside_loud_one` 5 times in 5, against
`last_termination.reason = error` from the same `describe_pod` result the run
already held. The finding was annotated under an answer whose prose still
named the wrong cause. A fourth re-ask sends the run back once. See
**defect 19** for what it did and did not achieve.

**17c. `_absence_is_about` recognised a backticked name and not a bolded
one.** The guard was written in August against this exact clause and the
corpus that day happened to spell the ConfigMap in backticks. The model
writes `nginx-conf` some runs and **nginx-conf** others, so the same sentence
was a false contradiction or not depending on formatting: 4 false
contradictions on `stuck_volume_needs_events`, each against the correct
answer. Markdown emphasis counts now, and the delimiter must close with
itself — any-of-three let the entity's own backtick pair with the next
apostrophe and silence a true contradiction. Undelimited names are
deliberately still uncovered, and a test records why.

### 17d. A fixture whose premise was false, and the sweep that followed

**Problem.** `never-ready` ran `sleep 3600`. That exits 0 after an hour and
the kubelet restarts it, so on any cluster older than an hour the pod whose
case asserts "the container never restarted once" carried a restart count and
a last termination of exit code 0. A recorded run read `restarts: 4` and
diagnosed "the container exits with exit code 0, triggering restarts". **The
number was real; the fixture was lying.**

**Measured, at a timescale that can be watched.** A pod running `sleep 5`
against one running `while true; do sleep 5; done`, same image, same node:
after 90 seconds the bare sleep had **3 restarts, `Completed`, exit code 0**
and the loop had **0**. At 3600s that is one exit-0 restart per hour, which is
exactly the 4 the recorded run saw on a four-hour cluster.

Confirmed independently on the real fixture: on a 7-hour cluster `never-ready`
showed **1** restart — exit code 255, reason Unknown, at the moment the node
stopped — which is the same single restart nginx-based `healthy-web` took. Not
one hourly exit.

**Fix.** Swept across all four fixture files: 21 containers, every
`sleep 3600` that ended a command replaced with a loop. Containers meant to
exit are untouched — `exit 1`, `exit 2`, the `stress` hog and both CronJobs.

**Regression evidence.** Applied to a fresh kind cluster and every fault class
still reaches its intended state: crasher and log-shipper `Error` with
restarts, memory-hog `OOMKilled`, needs-db `Init:Error`, slow-starter killed
twice by its liveness probe, backup `Completed`, the three
`CreateContainerConfigError` and `ContainerCreating` config faults unchanged —
and **every container meant to stay up sits at 0 restarts**.

**What it costs.** Cluster state that published numbers were measured against
has changed. It changed in the direction of removing an artefact, so a future
run stays comparable for any pod whose restart count was zero anyway, which on
a freshly applied cluster was already all of them.

### 18. A pattern hole that passed wrong answers as `grounded`

**Problem.** `_MEMORY_CAUSE` carried `"oom killed"` and `"oom-killed"` and
nothing else in that shape. An answer blaming **"the OOM killer"** — the
commonest English spelling of the same claim — matched nothing.

**Detection.** Not by reading it. A re-measurement of
`scoping_quiet_workload_beside_loud_one` came back **3/5**, up from 0/5, and
looked exactly like the fix in 17b working. It was not: `reconciles` was 0 on
every run, so the re-ask had never fired. All five answers named OOM as the
cause; two said "oomkilled" and were caught, three said "OOM killer" or "OOM
kills" and were scored `grounded`. **The case passed 3/5 while every one of
its five answers was wrong.**

**Root cause.** A phrase list that enumerates spellings, missing one.

**Fix.** The kill family is entered as the stem — `"oom kill"` subsumes
killed, killer and kills, `"oom-kill"` the hyphenated forms — plus
`"out-of-memory"` and `"oom termination"`. Still gated by `_asserted`, so
"to avoid the OOM killer" and "this was not an OOM kill" stay out, and still
inside the branch that fires only when the recorded reason is *not* an
imposed termination, so a genuinely OOMKilled pod is left alone.

**What the replay found.** Six more recorded runs, every one scored
`passed: True` on a contradicted claim. One of them is **gpt-4o-mini on this
very case**, whose published 5/5 is **4/5** under the corrected checker —
Fisher p 0.0079 → 0.0476. The other three discordant scenarios in
[AI_EVALUATION.md](AI_EVALUATION.md) do not move.

**Why this one matters beyond its own row.** A pattern that misses a spelling
does not report a smaller number, it reports the wrong one — and it reports
it in the direction that looks like success. This is the fifth time in this
project a harness has been caught reporting a result it had not earned, and
the only reason it was caught here is that `reconciles` had been added to the
eval record first, so "the mechanism fired" was a fact rather than an
assumption.

### 19. Telling a model its claim is contradicted is not enough

**Problem.** `scoping_quiet_workload_beside_loud_one` remains **open at 1/5**,
p=1.0 against the 0/5 baseline. It is recorded here because the failure is
now understood rather than merely counted.

**What the re-ask did.** Round 1 stated the claim and the measured value and
stopped there. It fired — `reconciles: 1` on 4 of 5 runs — and the model
argued back:

> "The `last_termination.reason` field shows Error, which is a generic
> placeholder in Kubernetes and does not specify the exact cause ... and does
> not contradict the exit code 137."

That is a false statement about Kubernetes, invented to protect a conclusion.
A conflict a model can dismiss as a technicality is one it will dismiss.

**Fix.** Each rule carries a sentence saying what the field would have read if
the claim were true — the kubelet writes `OOMKilled` when the OOM killer
fires, and exit 137 is SIGKILL, which says the container was killed and never
by whom. None of the sentences names a cause, and a test asserts that across
the whole table: naming the liveness probe would hand over the answer this
case exists to measure.

**Result: 1/5, and the one pass shows the mechanism works when accepted.**
That run adopted the sentence — "The OOM Killer would have set the reason to
OOMKilled if that were the cause" — withdrew the claim, and reached SIGKILL
plus the liveness probe. Four runs kept the claim anyway. **One of five is
not a fix**, the interval is Wilson 95% [4-62], and p=1.0 says the sample
cannot distinguish it from the baseline. The case stays open.

### 21. The full-suite regression run, and what it changed about defect 19

**145 runs, 29 cases at n=5**, on kind + qwen3, directly comparable to the
published `final-29-qwen3-n5.json`. Run because the readiness policy, the
contradiction re-ask, the OOM spelling fix and a system-prompt paragraph all
landed after that baseline while only 3 of 29 cases had been checked
individually. Zero void runs.

| | before | after |
|---|---|---|
| Suite pass rate | 127/145 (88%) [81-92] | **134/145 (92%) [87-96]** |
| Paired sign test | — | **p = 1.0000, UNDETERMINED** |
| Contradicted verdicts | 9 | **3** |
| Evidence-supported claims | 570 | 589 |
| Wrong-target rate | 0.7% | 0.7% |
| Median / p95 duration | 73s / 188s | 83s / 225s |

**The headline improvement is not established.** Six scenarios moved up, six
moved down, seventeen were identical. A four-point gain on 145 runs is what
this suite produces by chance, and the paired test says so.

Two scenarios reached the 5-versus-5 floor:

| Scenario | before | after | Fisher p |
|---|---|---|---|
| `never_ready_readiness_probe` | 0/5 | **5/5** | 0.0079 |
| `scoping_quiet_workload_beside_loud_one` | 0/5 | **4/5** | 0.0476 |
| `insufficient_no_such_workload` | 5/5 | **2/5** | 0.1667 |

**Neither survives Bonferroni** for 29 comparisons (p < 0.0017), and at 5
against 5 the design cannot reach it — 0.0079 is the floor.

**`insufficient_no_such_workload` dropping 5/5 to 2/5 is the finding to carry
forward.** It is not significant and it is the largest single move in the run,
so it is recorded rather than explained away. Three runs answered correctly
that `payments-gateway` does not exist and then went on to list the
neighbouring broken deployments with unverified claims about each — "likely
crashing", "exceeding resource limits" — which scored `partial` where the case
requires `insufficient_evidence`. **None of this session's mechanisms fired on
those runs**: `reconciles`, `policies` and `nudges` are all 0. So either the
system-prompt paragraph made the model more discursive about terminations it
was not asked about, or this is n=5 noise. **The next session should settle it
by re-running that case at n=10 with the paragraph removed**, and not by
reasoning about it.

A third explanation — that the grounding checker itself got stricter about
`partial` between the two runs — was tested and **ruled out**, 2026-08-31.
Replaying the published baseline through the current checker
(`evals/replay_grounding.py results/final-29-qwen3-n5.json`, after
`--self-check` passed 1650/1650) moves **2 of 140 records, both
`contradicted` -> `grounded`**, and none into `partial`. The extra `partial`
verdicts are changes in what the model wrote, not in how it was scored. What
did change on this case is answer length, **354 -> 804 characters mean, x2.27**
— against **+7% across the suite as a whole** — so the added text is
concentrated exactly where there was nothing to find.

**Defect 19's number is superseded.** Pooling every measurement of the scoping
case under current code — 3/10, 4/10, and 4/5 inside this suite — gives
**11/25 (44%), Wilson 95% [27-63]**, against a 0/5 baseline. **Even pooled,
that does not reach significance**: Fisher exact against 0/5 gives p=0.0816
one-sided, p=0.16 two-sided. The case moved off zero and is still wrong more
often than right, and the honest summary is that 25 runs cannot separate "the
change helped" from "0/5 was an unlucky floor". The three arms — 3/10, 4/10,
4/5 — disagree more than their intervals suggest they should, which is a
reason to distrust any single n=5 reading and a reason to distrust the pool.
The row stays **open**.

### 22. More than one replica, and the sweep that would have broken it

`ui.replicas > 1` failed the install from the day the chart shipped, and the
refusal was correct: the console keeps investigation history in
`store.build()`, so two pods were two histories, and the only way to share one
was SQLite over an RWX volume, which corrupts. The limit was never the design
— `store.py` said from the beginning that its interface was the seam a
Postgres implementation would slot into. This is that seam being used.

**What is proven, against a real PostgreSQL 17 in a container, 2026-08-31:**

- **The contract holds for the new backend.** The 17 cases in
  `tests/test_store.py` that every implementation must pass now run three
  times — memory, SQLite, Postgres — and the fixture skips loudly rather than
  quietly when no server is configured. CI runs one as a service container and
  **fails the job if those cases skipped**, because a skipped case and a
  passing one produce the same dot.
- **Two store handles share one state.** A report recorded through one is
  visible through the other, and the hourly ceiling is one budget rather than
  one per replica — otherwise scaling out would multiply the noise the ceiling
  exists to cap.
- **The lease excludes a second holder and expires.** Already used by
  `controller.py`; what is new is that it now spans processes.
- **The claim is atomic.** Twelve threads racing one row produce exactly one
  winner. This case was verified against a deliberately wrong implementation:
  the obvious read-then-write version produced **5 winners of 12**, so the test
  fails when the property is absent rather than passing on the happy path.

**The defect this work had to fix first.** `fail_interrupted()` closed out
every job left `queued` or `running` at startup, with no filter. For one
writer that is right. For two it is destructive: a restarting pod would mark
its live siblings' investigations `failed`, telling the person polling one
that their question was lost to a restart that happened to a different pod.
Jobs now carry the replica that created them and a replica closes out only its
own; `owner=None` keeps the old behaviour for the single-writer and CLI paths.
The SQLite twin gained the same scoping and a migration, because a state file
written before this has every column but that one.

**The second defect, found by reading the code the chart was about to
multiply.** A controller refused the lease called `return` — it exited. That
is right for a per-pod state file, where a second controller is a duplicate
rather than a peer. It is wrong the moment the chart ships a standby: the
container ends, kubelet restarts it, it loses the claim again, and the replica
that exists to take over sits in **CrashLoopBackOff** — indistinguishable, to
an operator reading `kubectl get pods`, from a broken deployment, and burning
its restart budget doing nothing. `wait_for_lease()` now waits when the state
is shared and still exits when it is not. Four tests cover it, and two of them
fail against the old behaviour: the standby's wait, and its takeover once the
holder stops renewing.

**What is NOT proven, and the row says so.** Nothing here has run as two
replicas in a cluster. The lease is unit-proven and a controller failover has
never been observed; `RollingUpdate` is now the strategy under
`sharedState.enabled` and no rollout has been watched. The honest summary is
that the mechanism is tested and the behaviour it enables is not, which is why
the availability row reads NOT TESTED rather than borrowing the store's
evidence.

### 20. Rate limiting, and what "in a cluster" could not mean

This row read PARTIALLY PROVEN with the note "never run against a real loop in
a cluster" from the day the limiter shipped. Closing it turned out to require
correcting the note rather than running the test it asked for.

**What was measured, 2026-08-30.** The real API process, ceiling set to 3 per
hour, five real `/ask` requests driven against a live kind cluster with Ollama
reachable:

| Request | Result | Duration |
|---|---|---|
| 1 | **200** | 106s |
| 2 | **200** | 175s |
| 3 | **200** | 163s |
| 4 | **429**, `Retry-After: 3156` | 0s |
| 5 | **429**, `Retry-After: 3156` | 0s |

**3156 is the part worth keeping.** The three investigations took 444 seconds
between them, and 3600 − 444 = 3156. The header reports *when the window
frees*, not the window length, which is what the code comment claims and what
a caller told to wait a flat hour would have no way to distinguish. That is
the property the unit tests could assert and only a real loop could confirm.

A separate run with the provider down established that a **503 still spends
the allowance** — the ceiling is charged in the dependency, before the
handler. That is deliberate and it is the only thing pacing retries while the
provider is unreachable, but it means an outage consumes a caller's quota.

**Why "in a cluster" was the wrong requirement.** The chart deploys the
controller and the console. It does not deploy the REST API, and the limiter
guards the model-driving endpoints on that API — the console reaches
`agent.stream()` directly and never passes through `budgeted`. So there is no
in-cluster surface for this ceiling to be tested on until the chart grows one,
and the original note asked for evidence that could not exist. `TRIAGE_MAX_
INVESTIGATIONS_PER_HOUR` and `TRIAGE_MAX_EXTERNAL_TOKENS_PER_HOUR` are also
absent from the chart's values for the same reason; the README documents them
as environment variables, which is what they are.

## What the `vllm` provider has and has not been run against

`vllm` in this project is the OpenAI chat-completions protocol under a name
that tells the telemetry where the request went. That makes most of it
testable without vLLM, and one part of it not.

**Run on 2026-08-27**, `TRIAGE_INFERENCE_MODE=cluster`,
`TRIAGE_INFERENCE_PROVIDER=vllm`, endpoint pointed at Ollama's
OpenAI-compatible `/v1`, model qwen3, asking a host question so no cluster was
involved:

| Exercised | Result |
|---|---|
| Tool schemas serialised to the OpenAI shape | 2 tools called (`get_platform_info`, `get_system_info`) |
| Tool calls parsed, results returned by `tool_call_id` | chain completed |
| Final answer and grounding | verdict `grounded` |
| Token usage reported by the provider | prompt 12, completion 261 |
| Recorded destination | `internal` — the endpoint classifier agrees the path stays on-network |

The token row matters beyond this test: it is the input the external-token
budget in `limits.py` consumes, and a provider that reported no usage would
leave that budget silently uncounted.

**What is still NOT TESTED, and it is the part with the real risk.** vLLM
needs `--enable-auto-tool-choice` and a `--tool-call-parser` chosen per model,
and the shape of what it emits for a tool call varies with that choice.
kubewhy's loop is entirely tool-driven, so that parser is the one thing most
likely to break against a real vLLM server — and it is precisely the thing
Ollama's `/v1` cannot stand in for. Everything either side of it is now
proven; the parser is not.

vLLM does not install on this machine at all (Darwin arm64: `Failed to build
'vllm' when installing build dependencies`), so closing this needs a Linux
host with a GPU, or a CPU build from source on Linux. Alternatives that
*would* add a second independent implementation of the same wire protocol —
llama.cpp's server, LocalAI — would raise confidence in the wire path that is
already proven, and would still not exercise vLLM's parser.

## Test-harness failures worth recording

Three times a harness reported a clean result it had not earned. Recording them
because a validation document that only lists product defects is not honest
about how validation actually goes.

- **Two vacuous UI tests** passed against known-broken code — one asserted a
  caption that a previous test had already made absent; one stubbed a `namespaces=`
  kwarg while `ui.py` calls positionally, so the filter never narrowed anything.
- **The grounding replay reported "zero changes" twice** (both now guarded
  against by `evals/replay_grounding.py`, see defect 14) — first from stale
  `__pycache__`, then because copies of `grounding.py` and `contradiction.py` sat
  beside the replay script, and Python puts the script's directory ahead of
  `PYTHONPATH`. Both compared the new code against itself.
- **An adversarial eval case passed 3/3 while its payload never reached the
  model.** Cases now declare `payload` and the run fails if it did not arrive.

- **Four chart tests passed on the laptop and failed every CI run for three
  pushes, and nobody looked.** `helm install --dry-run` contacts the API server
  for capability discovery in helm 3 and fails with "Kubernetes cluster
  unreachable"; helm 4 deprecated the flag and made it client-side, so the
  laptop (v4.2.0) rendered happily while CI (v3) failed. Emptying `HOME` and
  unsetting `KUBECONFIG` locally did **not** reproduce it — the difference was
  the tool version, not the environment, and chasing the environment would
  have found nothing. **The first fix was wrong too** — `--dry-run=client`
  fails identically on helm 3.16, and that took a second red CI run to learn,
  because it was reasoned about rather than reproduced. Downloading a helm 3
  binary and running it against an emptied `HOME` reproduced the failure in
  one command, and then four candidate fixes could be tested in a minute
  instead of a push each. The working answer renders NOTES.txt through
  `helm template` by copying the chart and duplicating NOTES.txt under a name
  helm will render — see `_notes()` for why each simpler form does not work.
  Two lessons, and the second is the bigger one: a green local suite is a
  claim about one machine's toolchain, and a fix for a failure you have not
  reproduced is a guess.

A "no regressions" result that has not proved it exercised two different versions
is not a result.

## Reproducing

```bash
pytest                                   # 1240, no cluster or model needed

kind create cluster --name kubewhy
kubectl apply -f demo/broken-pods.yaml -f demo/config-faults.yaml \
              -f demo/tricky-pods.yaml -f demo/adversarial.yaml

TRIAGE_INFERENCE_MODE=local TRIAGE_MODEL=qwen3 \
  caffeinate -is python evals/run_eval.py --repeat 5 --json results/local.json
python evals/report_baseline.py results/local.json
```

`caffeinate -is` is not optional for an unattended run: two months of "Ollama
stalls" turned out to be the laptop sleeping.
