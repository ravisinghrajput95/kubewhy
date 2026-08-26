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
| Automated test suite | **PROVEN** | 1063 passing, no cluster or model required |
| Grounding replay | **PROVEN** | 907 recorded runs, 0 regressions on the last change |
| Investigation context integrity | **PROVEN** | 20 tests, two workloads in different namespaces, verified live |
| Entity scoping | **PROVEN** | 135/145 targets extracted; 0.7% / 0.0% wrong-target |
| Grounding + contradiction | **PROVEN** | caught a genuine wrong claim live, 5/5 reproducibly |
| Bounded investigation deadline | **PROVEN** | 38 tests incl. fallback-cannot-reset |
| Security regression (UI) | **PROVEN** | credentials absent from page, no client-side calls |
| Console authentication | **PROVEN** | kind + real OIDC issuer; console unreachable from another pod |
| Forged identity header | **PROVEN** | overwritten by the proxy, measured with a session held |
| Per-user authorization | **NOT TESTED** | deliberately not implemented — see SECURITY.md |
| Read-only RBAC | **PROVEN** | runtime validated on GKE by attempting operations |
| GKE runtime | **PROVEN** | released chart, real cluster |
| GKE / Calico NetworkPolicy | **PROVEN** | dataplane-enforced egress |
| Local Ollama inference | **PROVEN** | 145 live investigations |
| Hosted OpenAI API inference | **PROVEN** | 145 live investigations |
| In-cluster inference | **PARTIALLY PROVEN** | Ollama only |
| AKS runtime | **PARTIALLY PROVEN** | non-AAD single node |
| Model comparison | **UNDETERMINED** | p = 0.3438, paired, n=5 |
| Generalized diagnostic accuracy | **NOT TESTED** | one cluster, one prompt configuration |
| Real vLLM | **NOT TESTED** | protocol-level support only |
| EKS | **NOT TESTED** | auth verified by reading the client |
| Browser paint automation | **NOT TESTED** | designed in E2E.md, not implemented |
| Mutation testing | **NOT TESTED** | harness not in this repository |

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

## Test-harness failures worth recording

Three times a harness reported a clean result it had not earned. Recording them
because a validation document that only lists product defects is not honest
about how validation actually goes.

- **Two vacuous UI tests** passed against known-broken code — one asserted a
  caption that a previous test had already made absent; one stubbed a `namespaces=`
  kwarg while `ui.py` calls positionally, so the filter never narrowed anything.
- **The grounding replay reported "zero changes" twice** — first from stale
  `__pycache__`, then because copies of `grounding.py` and `contradiction.py` sat
  beside the replay script, and Python puts the script's directory ahead of
  `PYTHONPATH`. Both compared the new code against itself.
- **An adversarial eval case passed 3/3 while its payload never reached the
  model.** Cases now declare `payload` and the run fails if it did not arrive.

A "no regressions" result that has not proved it exercised two different versions
is not a result.

## Reproducing

```bash
pytest                                   # 1063, no cluster or model needed

kind create cluster --name kubewhy
kubectl apply -f demo/broken-pods.yaml -f demo/config-faults.yaml \
              -f demo/tricky-pods.yaml -f demo/adversarial.yaml

TRIAGE_INFERENCE_MODE=local TRIAGE_MODEL=qwen3 \
  caffeinate -is python evals/run_eval.py --repeat 5 --json results/local.json
python evals/report_baseline.py results/local.json
```

`caffeinate -is` is not optional for an unattended run: two months of "Ollama
stalls" turned out to be the laptop sleeping.
