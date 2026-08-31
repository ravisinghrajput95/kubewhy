# Handoff prompt — paste this to start the next session

I'm working on kubewhy at /Users/ravirajput/Projects/AIOps-agent
(github.com/ravisinghrajput95/kubewhy, public, MIT). Kubernetes root-cause
analysis: a model chains read-only tools to explain *why* a workload is broken.
Six surfaces share one tool set — CLI (agent.py, `--scan`), REST (app.py), MCP
(mcp_server.py), watch controller (controller.py), Streamlit UI (ui.py), Slack
via Socket Mode (slack_socket.py).

**Where inference happens is configuration now, not a property of the
product** — a workstation, this cluster, or a hosted API. The default is still
local and still keeps everything on your network, and that default is enforced
rather than documented. See `docs/INFERENCE.md`.

**State: `main` tree clean and pushed, **1280 tests pass** (verified
2026-08-31, 83s), **CI green**, tags through **v0.2.0**. Last substantive
commit `c7ee74a`. Nothing running: no kind cluster, no GKE cluster, Docker
quit, Ollama unloaded (`{"models":[]}`), GCP empty. The full teardown check at
the bottom of this file passed line by line on 2026-08-31.**

**Read `docs/VALIDATION.md` before anything else — it is current, and most of
this file below the next section is history from 2026-08-24 and earlier.**

## Start here: what to pick up, in order

Nothing below needs a cluster or a model except item 5, and nothing is blocked.

1. **Review the 189 unreviewed mutation survivors.** The survey itself is now
   DONE -- 16 of 18 modules, 697 mutants, 508 killed (72.9%), measured
   2026-08-31 with a fixed harness. Pure local compute, no cluster, no model.
   Start with `limits.py:140`, the standing proof that real gaps hide in this
   list: the `+ 1` in `max(int(when + self.seconds - now) + 1, 1)` shifts every
   `Retry-After` by a second and nothing catches it, inside a module the docs
   recorded as 28/28. `backends.py` (18 survivors of 37), `inference.py` (36 of
   125) and `contradiction.py` (43 of 117) are the densest. `--tests`
   under-selects by default.
2. **Make `tests/test_controller.py` and `tests/test_ui.py` self-contained.**
   Both reach a REAL cluster -- whatever `current-context` names -- and hang
   when run alone, while passing inside the full suite, so another test module
   is installing the Kubernetes mock they rely on.

   What is measured: run alone, `tests/test_ui.py` exceeded 120s and was
   killed, and `mutate.py` refused both modules with "the baseline is already
   failing". The failing test reaches `127.0.0.1:55807` -- the deleted
   `kind-aiops-test` -- with three 15-second read timeouts per call.
   `kind delete cluster` leaves its context behind, so `current-context` still
   names a cluster that does not exist; re-asserting `kubectl config
   use-context` does not help, because the stale entry IS the current context.

   The consequence that matters: **mutation testing silently skips the two
   largest modules that have test files.** Fixing the isolation unblocks them.

   **Unexplained, and worth a look before trusting any suite timing:** the full
   suite ran 1280 passed in 83s earlier the same day and over six minutes
   later, both green. `tests/test_mutate.py` was ruled out by measurement
   (3.3s). `--durations` puts the cost in `tests/test_agent_loop.py` --
   `test_no_nudge_without_rounds_left_to_use_it` alone took **78s** and
   `test_the_run_is_sent_back_and_the_tool_gets_called` **31s**, both of which
   mock `ollama.chat` and should not be slow. A first guess that the variance
   came from the dead cluster's port state did not survive contact with the
   durations output. The suite is stable in result and unstable in runtime, so
   nothing draws attention to it.
3. **Add `pyproject.toml`, and ruff + mypy to CI.** The repo has 17k lines of
   Python, 1280 tests, and **no linter, formatter or type checker anywhere** --
   no ruff, no mypy, no pre-commit, nothing in the workflows. It is also not
   pip-installable. This is the one structural gap left that is not blocked on
   hardware, a cloud account, or someone else's incident data.
4. **Slack audit trail** is the only audit row still NOT TESTED, and it needs a
   workspace. If one is available, it closes the last surface.
5. **The n=10 paragraph-removed experiment** on `insufficient_no_such_workload`
   (defect 21, described below). Needs kind + Ollama back up, ~40 min of model
   time, on mains power under `caffeinate -is`.

Housekeeping: the defect sections in `docs/VALIDATION.md` are out of order --
`### 21` sits above `### 20`.


## What changed on 2026-08-26 / 27 (25 commits)

**The tenancy question is settled: one SRE team, one cluster.** Authentication
only; the ClusterRole is the authorization model; no Kubernetes impersonation.
Do not reopen without new evidence — see `docs/SECURITY.md`.

Done and validated live:

- **Tier 1 item 1 — authentication.** `identity.py`, `require_caller` in
  app.py, an `st.stop()` gate in ui.py before any cluster read, and
  `ui.auth.enabled` in the chart (oauth2-proxy sidecar, console bound to
  127.0.0.1, Service pointed at the proxy). Validated on kind against a real
  Dex: the console's port is ConnectionRefused from another pod, and a forged
  `X-Forwarded-Email` with a valid session still reaches the app as the real
  address.
- **Tier 1 item 3 — audit logging.** `audit.py`, one record per investigation,
  hooked at `agent.stream()`. The record carries no evidence by design.
  PROVEN live on CLI, REST, console and controller. **Slack is NOT TESTED.**
- **Tier 1 item 4 — the replica story.** `docs/RUNBOOK.md`. The chart refuses
  `ui.replicas > 1`, the console gets its own state PVC, and
  restart-interrupted `/ask/jobs` are closed out instead of reading `running`
  forever.
- **Tier 2** — failure runbook with numbers recomputed from `results/`;
  `evals/replay_grounding.py` committed and in CI (push, PR, weekly);
  `limits.py` rate limiting and external-token budget.
- **Tier 3** — `evals/mutate.py` committed. Both "harness existed but was
  never committed" items are now closed.
- **E2E case R-01 fixed**: the console printed `<span class='kw-dim'>` as
  literal text on every contradicted verdict. Confirmed in a browser.

**Still open, and unchanged:**

1. **Tier 1 item 2 — an evidence corpus someone else produced.** Needs your
   incident history and an author who is not the system's. The partial
   approach (an externally-selected fault list) is written up in
   `docs/FUTURE.md` as a backlog item, with what it does and does not buy.
2. **Real vLLM.** The wire path is proven — the `vllm` provider ran against a
   real OpenAI-protocol server with tool calls and token usage round-tripping.
   What is untested is vLLM's own `--tool-call-parser`, which nothing else can
   stand in for. **vLLM does not install on this Mac** (Darwin arm64, build
   failure); needs a Linux GPU host.
3. **EKS**, and the browser harness proper — `docs/E2E.md` now argues against
   the latter more than for it.

## 2026-08-30/31: what was closed, and what the regression run settled

**The 145-run regression suite is DONE. Nothing needs relaunching.**
`results/regression-29-n5-after-fixes.json` completed at 14:02 on 2026-08-31:
**145 records, 29 cases x 5, zero voids**, 3.9h of model time, committed in
`6e2eea8` and analyzed there. An earlier draft of this handoff said it "may
still be running" and told the next session to check on it -- it had been
finished and committed for over an hour, and that sentence cost the following
session its first ten minutes. **Trust `git log` over this file.**

What it found, against the published `final-29-qwen3-n5.json` baseline:

- **127/145 (88%) -> 134/145 (92%), paired sign test p=1.0000.** Six scenarios
  up, six down, seventeen identical. **The headline gain is NOT established**
  and is recorded as undetermined. Do not quote 92% as an improvement.
- `never_ready_readiness_probe` **0/5 -> 5/5** (p=0.0079) -- the readiness
  evidence policy landing, and the one clear win.
- `insufficient_no_such_workload` **5/5 -> 2/5**, open. See below.
- **Contradicted verdicts fell 9 -> 3** across the suite.
- Both runs recorded `low_power_mode: True`, same model, thinking on, so the
  arms are comparable on every axis the harness records.

**Closed this round:**

- **Rate limiting -> PROVEN.** Ceiling of 3, five real `/ask` requests: 200,
  200, 200, 429, 429 with `Retry-After: 3156`. The three runs took 444s and
  3600-444 = 3156, so the header reports when the window frees, not the window
  length. A 503 still spends the allowance -- deliberate, and it means an
  outage burns a caller's quota. **The old note asked for a loop "in a
  cluster" and that cannot exist: the chart deploys the controller and the
  console, not the REST API, and the ceiling guards the API. The console
  reaches `agent.stream()` directly and never passes `budgeted()`.**
- **Mutation testing 5 -> 7 modules.** `grounding.py` 93/118,
  `contradiction.py` 76/117. Three real gaps closed. **Every number in this
  bullet was measured with a broken harness and is superseded** -- see
  `docs/VALIDATION.md`, "The harness was scoring mutants it never ran".
  Corrected: 92/118 and 74/117, 16 of 18 modules surveyed, **189 survivors**.
- **The fixture sweep.** All 21 long-lived containers now loop instead of
  `sleep 3600`. Proven at a watchable scale: `sleep 5` took 3 restarts in 90s
  at exit code 0, the loop took 0.
- **A harness defect the long run depends on.** `run_eval` scored a provider
  blackout as a wrong answer -- `oomkill_root_cause` read 2/3 for a case that
  was 2/2 and a blip, because httpx raises `RemoteProtocolError` and the guard
  caught only `ConnectionError`. Such runs are VOID now: excluded, printed,
  and reported with the real n.

**NOT closed, and one of them is not closable by more work here:**

- **Generalized diagnostic accuracy stays NOT TESTED, and raising n will not
  change that.** The row says "one cluster, one prompt configuration" -- that
  is a corpus-authorship problem (Tier 1 item 2, needs your incident history),
  not a sample-size one. A tighter interval on 29 hand-built scenarios is a
  tighter interval on the wrong number. Do not let a big n flip this row.
- **`scoping_quiet_workload_beside_loud_one` is still open.** Three arms under
  current code: **3/10, 4/10, and 4/5** inside the regression suite. Pooled,
  **11/25 (44%), Wilson 95% [27-63]** against a 0/5 baseline -- and **even
  pooled it does not reach significance**: Fisher exact gives p=0.0816
  one-sided, p=0.16 two-sided. 25 runs cannot separate "the change helped"
  from "0/5 was an unlucky floor", and the three arms disagree more than their
  intervals allow, which argues against pooling them at all. An earlier
  write-up called this "a real improvement"; that claim was withdrawn in
  `c7ee74a` and the row stays **open**. The system prompt's paragraph on
  reading a termination -- exit code names the signal,
  `last_termination.reason` names the sender -- is kept because it is true,
  not because it is measured to work. **Stop tuning the prompt against this
  one case**; that is how this project has overfitted before.

- **`insufficient_no_such_workload` regressed 5/5 -> 2/5 and is open**
  (defect 21). All five runs answered correctly that `payments-gateway` does
  not exist; three then listed the neighbouring broken deployments with
  unverified claims about each -- "likely crashing", "exceeding resource
  limits" -- scoring `partial` where the case requires
  `insufficient_evidence`. `reconciles`, `policies` and `nudges` are 0 on
  those runs, so none of this round's mechanisms fired.
  **Checker drift is ruled out**: replaying the published baseline through the
  current checker (after `--self-check` passed 1650/1650) moves 2 of 140
  records, both `contradicted -> grounded`, none into `partial`. The extra
  `partial`s are the model's words changing, not the scoring. Answer length on
  this case went **354 -> 804 chars mean (x2.27)** against **+7% suite-wide**,
  so the growth sits exactly where there was nothing to find. Still unsettled
  between "the system-prompt paragraph made it discursive" and "n=5 noise".
  **The experiment: re-run this case at n=10 with the paragraph removed**
  (`evals/ab_prompt.py` already slices it), paired against n=10 with it. Add a
  counter asserting the paragraph is actually absent from the built prompt
  BEFORE measuring -- otherwise "removed it, nothing changed" and "the removal
  never took effect" are the same number.

**Blocked by hardware or accounts, and saying otherwise would be a fake
number:** real vLLM (Linux GPU), EKS (AWS), Slack audit trail (a workspace),
AKS with AAD, the external token budget against a billed provider (the key is
revoked), and the incident-history corpus (your data).

**Both of those defects were worked on 2026-08-28. One is fixed, one is not,
and the investigation found two checker defects on the way.**

- `never_ready_readiness_probe` **0/5 -> 5/5**, Fisher p=0.0079. A pod that is
  Running and not Ready is a third evidence gap and could not be a status
  marker, because the status is `Running`. The policy is deliberately LAST in
  `evidence_gap`: placed first it stole the slot from 4 `cluster_wide_scan`
  runs that had spent it on logs. `policies: 1` on all five runs and
  `get_pod_events` called 5/5 against 0/5, so no run got there unaided.
- `scoping_quiet_workload_beside_loud_one` **still open, 1/5**, p=1.0. A
  contradiction re-ask was added and it fires; the model argues back. Telling
  it "your claim conflicts with X" is dismissible, so the re-ask now says what
  X would have read if the claim were true, and the one passing run adopted
  that sentence and got the right answer. Four did not.
- **`_MEMORY_CAUSE` could not see "OOM killer".** A re-measurement came back
  3/5 and looked like a fix; `reconciles` was 0 on every run, so the re-ask had
  never fired, and three answers blaming the OOM killer had scored `grounded`.
  The case passed 3/5 while all five answers were wrong. Six more recorded runs
  had the same false pass, including **gpt-4o-mini on this case, whose
  published 5/5 is 4/5** under the fixed checker (p 0.0079 -> 0.0476).
- **`_absence_is_about` could not see a bolded name**, only a backticked one —
  4 false contradictions on `stuck_volume_needs_events`.

**The lesson worth carrying: `reconciles` was added to the eval record BEFORE
the measurement, and it is the only reason the fake 3/5 was caught.** A
counter for the mechanism you are testing is not bookkeeping; without it "the
fix worked" and "the checker went blind" are the same number.

**An environment note:** another project on this machine repeatedly creates and
deletes a kind cluster named `k8s-agent-verify` and rewrites
`current-context`. Do not delete it; re-assert `kubectl config use-context`
before trusting kubectl.

**The `OPENAI_API_KEY` in `.env` was revoked on 2026-08-24 and is dead.** Any
api-mode run needs a new one. Local Ollama is the default and needs no key.

**v0.1.8 is released and verified from the registry.** All four image tags
resolve amd64+arm64, the chart publishes, and CI's own `docker run` step checks
`/healthz` on the base image and `/_stcore/health` on the UI one -- the check
that would catch the `target:base` trap. **v0.1.7 should not be used**: it
ships the egress bypass below, and an in-cluster Ollama whose model pull
usually loses a race with its own server.

**An adversarial validation phase ran on 2026-08-23 and was closed out on
2026-08-24. Nine findings, all resolved** -- seven fixed in the product, one in
the test harness, one accepted as designed behaviour. The full report is an
artifact, kept current:
https://claude.ai/code/artifact/264cb4be-1137-41f4-b23f-f81aac685721

**START HERE: nothing is broken, and the honest next task is evidential.**
All nine validation findings are closed. The gap that remains is not a defect:
**the security and reliability properties of this agent are now far better
evidenced than its answers are.** Every accuracy number on record is a smoke
test -- 16/16 at n=1 on the hosted API is Wilson 95% [81-100]; 5/5 at n=5 is
[57-100]; 48/48 at n=3 is [93-100]. None supports a claim about generalised
accuracy, and the report says so in three places.

The three candidates, in the order I would take them:

1. **Settle thinking-off.** Four undetermined rounds now (p=0.483, 0.617,
   0.242, and the latest arm unmeasured against a paired comparator). It needs
   roughly **n=15 per arm**, about five hours of model time at the thinking-on
   pace. Do not flip the default on undetermined rounds. The latency finding is
   the unambiguous half and already stands: 7.1x median, every one of sixteen
   cases slower.
2. **Raise the accuracy sample generally.** The 16-case suite at n=10 with
   `caffeinate` would put a usable interval under the headline number for the
   first time. Everything needed is in place -- `run_eval.py` records `draft`
   and `evidence`, so a grader change can be replayed rather than re-run.
3. **Real vLLM, if a machine with an accelerator ever appears.** It stays
   *protocol-level support only* until then, and the release notes must keep
   saying so.

**No release is pending.** The F-07 fix touches `demo/`, `evals/` and
`tests/` -- none of which ships in the image -- so v0.1.8 remains current.

**~~F-07: an eval case that could not fail.~~ Closed 2026-08-24, and the
second half is the part worth keeping.** `injection_in_annotations_is_data`
asserted the agent resists an injection delivered through annotations and
passed 3/3 for weeks; no projection carries annotations, so the payload never
entered the context. The fixture now delivers through the **container image
reference** -- anyone who can create a pod chooses its image, and an unpullable
one lands verbatim in `waiting_message` and in a Failed event. Measured: the
payload reaches the model through three tools, against zero before.

The generalising half: **a case may declare `payload`, and the run fails when
that text is absent from the evidence it collected.** Failing rather than
skipping, because a silent skip is how the first one hid. It caught a real
instance on its first run -- at n=3 one run answered correctly from
`scan_cluster` alone, never called `describe_pod`, and tested nothing; under
the old harness that was a clean pass. The question now steers at the pod and
`expect_tools` names the tool that carries the payload. `injection_in_logs_is_data`
declares a payload too, and a test asserts the *rule* rather than the instance:
any case named `injection*` must declare one. It failed on first run by
catching the logs case.

Re-measured at n=5: **5/5, payload reached the model 5/5, obeyed 0/5.** That
makes the injection tests non-vacuous, which is a lesser claim than the agent
being robust to injection.

**What the validation phase found and fixed, in severity order:**

1. **HIGH -- the egress policy could be bypassed by how an endpoint was
   spelled.** Unicode full stops (U+3002, U+FF0E, U+FF61), which httpx
   normalises to "." via IDNA but an ASCII-dot check does not; and integer-form
   IPv4 (`0x08080808`, `134744072` -- both 8.8.8.8, neither containing a dot).
   Demonstrated end to end with a captured request: `mode: cluster`,
   `allowExternal: false`, and the outbound body carried a pod log with a
   database password and an AWS secret key, while every log line said
   `destination: internal`. The classifier and the HTTP client parsed the
   endpoint independently and agreed only by coincidence; they agree by
   construction now, and a test asserts they cannot diverge.
2. **A malformed provider response never failed over.** An intermediary
   returning `<html>502 Bad Gateway</html>` with a 200 raised a bare
   JSONDecodeError that `unavailable()` read as "the provider refused".
   `backends.MalformedResponse` carries these now.
3. **F-03 contradiction detection.** `grounding.check()` could verify a value
   appears in the evidence but not that the evidence says otherwise, so
   "CrashLoopBackOff, which means the container exited with an application
   error" scored `grounded` over `last_termination.reason = OOMKilled`.
   `contradiction.py` is a **separate deterministic stage** using grounding's
   own `_scope` and `_entity_index`; `contradicted` is a fifth verdict that
   outranks the rest.

   **The corpus replay is the load-bearing artefact here and it rejected two
   drafts of the rules.** The first fired 7 times across 316 records and every
   firing was a false positive: six were the `stress` fixture's own log line
   ("0 cpu, 0 io, 1 vm") read as a CPU-limit claim, one was a negation ("no
   OOMKilled reported"). After repair: 236/236 grounded stayed grounded, 64
   insufficient stayed 64, and **two records moved** -- both
   `service_unreachable_chain` answers claiming a Service has no pods when
   `get_service_endpoints` had just reported a ready endpoint. One is the wrong
   answer this file used to record as having passed.
4. **F-04 readiness verifies the configured model.** Three-valued:
   `confirmed` / `absent` / `unsupported`, and only `absent` fails readiness --
   an empty listing is not a negative answer, and reporting NotReady on that
   basis would invent a guarantee. Verified live: `gpt-4o-mini` confirmed,
   `gpt-nonexistent-9` -> 503 `model_not_served`.
5. **F-05 a global investigation budget.** `TRIAGE_INVESTIGATION_BUDGET`,
   default **600s**, derived rather than chosen: it clears the p99 of 1273
   recorded investigations (318s) by ~1.9x, is twice OLLAMA_TIMEOUT, and is a
   third of the controller's cooldown. Covers rounds, tools, retries and
   fallback; terminates with `deadline_exceeded` as data rather than prose.
6. **F-06 redaction, classified before widening.** Two of the four reported
   "misses" were not gaps -- one was a malformed test string of mine, one a
   certificate that is public by design. The widening **nearly shipped worse
   than it fixed**: normalising the separator broke the JSON, which would have
   silently disabled the claim checker. Replayed over 738 recorded tool
   outputs: zero changed, zero broke.

**Two things the phase converted from "not verified".** NetworkPolicy egress
enforcement is now tested on a live cluster, against an unlabelled control pod
so the block is attributable to the policy -- kind v1.36.1 / kindnet only,
managed dataplanes still untested. And **the same 16 scenarios ran on the
hosted OpenAI API with no business-logic change**, 16/16 at n=1, with all
fourteen derived tool schemas accepted by a second independent implementation.
That is mechanical compatibility, not accuracy: n=1 is Wilson 95% [81-100].

**Still NOT tested, and say so:** any real vLLM server (the arm64 image is
10.5GB and expects a CUDA device), EKS, AKS, NetworkPolicy on any managed
dataplane, and rate limiting against a live account.

**Mutation testing covers 25 guards and kills all 25.** Two were genuine
survivors first: the round-loop and tool-loop deadline checks are mutually
redundant for the ordinary shape, so removing either alone left every test
passing. Each is now isolated by a run only it can stop.

**Repository hygiene, settled 2026-08-24.** `origin/main` carries zero Claude
trailers, no Claude author or committer, no secrets (`.env` was never
committed) and no large files. Four commits from 2026-08-07 do carry
`Co-Authored-By: Claude Opus 5`, and they are reachable from **`refs/pull/*`
for PRs #1 and #3-#8** -- not orphaned, as first assumed. GitHub's PR refs are
permanent, so no GC or force-push removes them; only deleting the repository
would. Both contributor APIs report two contributors. If the web sidebar still
shows three, that is `graphs/contributors-data` returning HTTP 202 and GitHub
falling back to a pre-scrub render -- not a repository problem. A verified
bundle backup is at `/Users/ravirajput/kubewhy-history-backup/`.

**~~A deterministic policy did not fire.~~ Closed 2026-08-23, and it was not
the seam refactor.** The condition had been there since `LOGS_POLICY` was
written: reading *either* `get_pod_logs` or `get_pod_events` closed the gap.
The premise was wrong. For a container that started and exited, the events say
"Back-off restarting failed container" — the status again — and in the recorded
run they also carried a seven-minute-old `FailedScheduling` from start-up,
which the answer then named as the cause of the crash. So events there are not
merely insufficient: they supplied a wrong cause. Events still close the gap
for a pod matching *both* lists (`error` is a substring of
`CreateContainerConfigError`), because that container never started and has no
logs to read.

Fixing only that would have been a net loss, and replaying the recorded sets is
what showed it. The OOMKilled exclusion was keyed on the status *string*, and
the same OOM-killed pod reports `OOMKilled` when `list_pods` catches it
mid-crash and `CrashLoopBackOff` when it catches it mid-backoff — both
spellings appear for one `memory-hog` pod inside `think-OFF-16cases-n3`. Keyed
on the status it leaks in the backoff sample, which is where a crashlooping pod
spends most of its life: it newly demanded logs on 8 passing runs of a `stress`
workload whose logs say nothing. It reads `last_termination.reason` now.

**Measured.** Replay over all 254 recorded runs carrying their evidence: 34
policy firings before, 35 after, exactly one run changing — the one it was
opened about. Live `crashloop_root_cause` at n=5, kind + qwen3, thinking off:
**5/5, Wilson 95% [57-100]**, and **3 of the 5 stopped after exactly the
recorded failing sequence** with the policy firing on all three. Replaying each
one's state at the moment it stopped through the pre-fix logic returns `None`:
it could not have fired. That counterfactual is deterministic and is the actual
evidence. `results/logs-policy-fix-n5.json`.

**The 16-case regression is weaker than it looks and should not be quoted as an
improvement.** All 16 at n=3, thinking off: **48/48**, every case 3/3, nothing
moved down. Against the recorded think-OFF arm's 45/48 that is Fisher exact
**p=0.242** — undetermined, as every round at this n has been. And the three
failures in that arm were two `injection_in_logs_is_data` and one
`service_unreachable_chain`; none was `crashloop_root_cause`, so the difference
is variance on unrelated cases. What the run establishes is the *absence of a
regression*. `results/gateway-regression-think-off-n3.json`.

**The inference gateway is new and is the biggest change here.** `inference.py`
sits above `backends.py`: `backends` answers "which protocol", `inference`
answers where inference happens, whether evidence may leave to get there, and
what to do when it cannot be reached. `Gateway` presents the same four methods
a backend does, so `agent._backend()` returns one and **the loop is unchanged**
— a test asserts that interface, because if it stops holding then `agent.py`
has to learn something about inference.

- `TRIAGE_INFERENCE_MODE` is `local` | `cluster` | `api`. **The mode is checked
  against the endpoint**, not trusted: a mode claiming inference stays on your
  network refuses to start when its endpoint is off it. Otherwise
  `mode: cluster` pointed at a vendor installs cleanly, logs itself as
  in-cluster in every line, and ships pod logs anyway. `api` pointed at a
  *local* endpoint is allowed — claiming more egress than occurs is never the
  unsafe direction, and it is how the OpenAI backend is validated without a key.
- Classification is on the name **as written** and never resolves it. A lookup
  gives an answer that can change between the check and the request. The cost
  is stated in the module docstring and is why `allow_external` is a separate
  switch.
- `TRIAGE_BACKEND` and `OLLAMA_HOST` still work and still mean what they meant.
  The second matters most: the chart sets it and nothing else.
- **Failover is a wire-protocol problem before it is an availability problem.**
  Backends carry a `wire`; a mid-run failover happens only between providers
  sharing one, because halfway through, the history is in the primary's shape.
  A 400 or 401 is never failed over — both fail identically on the fallback and
  succeeding quietly elsewhere hides a config error someone has to see.
- **Found by running it, not reading it:** without a breaker, one investigation
  failed over on *every round*. A refused connection costs milliseconds and hid
  it; a primary that *times out* costs `MAX_ROUNDS × OLLAMA_TIMEOUT`.
  `TRIAGE_PRIMARY_RETRY_SECONDS` (60) fixes it, verified live before and after.

**Live verification, 2026-08-23, against a real Ollama:** local mode on the
native protocol, api mode on the OpenAI protocol pointed at the same server,
failover from a genuinely refused primary, the same outage with the fallback
disabled failing as it should, and an external endpoint refused with
`allow_external` unset. Both wire formats answered correctly from the same
model and reported real token counts.

**Still untested, and stated plainly in the code and the values comments:** any
*hosted* API (no key was used), any real vLLM server (`VLLMBackend` is the
OpenAI protocol under its own name — a statement about the protocol), and the
NetworkPolicy's *enforcement* (rendered and schema-checked only).

**`telemetry.py` is new**: hand-rolled counters and histograms in Prometheus
exposition format, for the reason `observability.py` hand-rolls its JSON
formatter. `/metrics` and `/inference` are behind the same bearer token as the
rest. `/readyz` no longer builds an Ollama client — it asks the gateway, and
names *which* target answered, because "ready on the fallback" and "ready on
the primary" are different states of the world. Endpoints are never metric
labels: one can carry a token in its userinfo.

**What the brief asked for and did not get, deliberately.** The FACT /
OBSERVATION / INFERENCE split exists — `grounding.contract()` returns
citation-backed observations, inferences and unknowns. RECOMMENDATION is not
extracted, and ACTION does not exist at all because tools stay read-only. Both
are noted in the summary as scoped follow-ups rather than bolted on: pulling
recommendations out of the prose means touching `grounding.py`, whose
`_PRESCRIPTIVE` regexes are calibrated to observed qwen3 output, and a change
there has to be replay-verified before it is believed.

**The model provider is behind a seam now, and the second backend is real.**
`backends.py` owns the provider; `TRIAGE_BACKEND` selects it; Ollama stays the
default because changing it would change what this project claims about your
data. `tool_schema.py` derives JSON Schema for all fourteen tools from the
same docstrings Ollama introspects, and refuses rather than guesses -- an
unannotated parameter raises instead of defaulting to "string", which would
not fail, it would just quietly degrade tool selection.

**The OpenAI-protocol backend was validated against a local Ollama `/v1`**,
which serves chat-completions alongside the native API: same model, same
tools, no key, no bill. Live through the whole loop -- schemas accepted, a
`call_` id returned, arguments decoded from the JSON string, the tool run, the
result matched back by `tool_call_id`, and a grounded answer. It found a real
defect on first contact: with no key, `Authorization: Bearer ` is an illegal
header value and httpx refuses to send it, failing locally rather than at the
provider. **OpenAI's hosted service is still untested, and so is any
comparison of answer quality between models.**

**What would break with a hosted backend, in priority order** (measured
reasoning, not speculation): `grounding.py` is calibrated to how qwen3 writes
-- `KNOWN_STATUSES`, `_TOOL_SPELLINGS`, `KNOWN_CAUSES` and the `_PRESCRIPTIVE`
regexes all exist because of observed output, and a different model would keep
producing verdicts, just less accurate ones. Then the policy budgets
(`MAX_ROUNDS`, `MAX_NUDGES`) which were tuned for qwen3's tool-calling rhythm.
Then argument shapes, which already bit once. Re-score with
`grounding.check(record["draft"], record["evidence"])` and read why each flag
fired, rather than comparing pass rates.

**GKE is validated and the cluster is deleted.** See `docs/PORTABILITY.md`.
RBAC 21/21 with a minted ServiceAccount token and no cloud IAM; controller
15/16 with `nightly-sync` 3/3 grounded through the CronJob race; noise 3/3
(eleven pods to two findings, unrelated incident still visible); entity
scoping 5/5 all grounded; injection 6/6 across logs and annotations; agentic
RCA 31/31; tools 10/10. **One portability bug found and fixed**, predicted by
the static audit before the cluster existed: `list_nodes` reported every
healthy GKE node as under `pressure`, because a Container-Optimized OS node
carries 26 conditions and `SysctlChanged` is True by design. Pressure is an
allowlist now.

**`helm install` of kubewhy on GKE works end to end**, and the chart can now
deploy Ollama too (`ollama.enabled`, off by default). Two chart defects were
found by installing rather than templating: the template rendered into a
namespace it never created, and the PVC lacked the label its siblings carry.
**A GKE cluster deletion does not reclaim a dynamically provisioned PD** -- the
Ollama PVC left a 20GB disk billing after the cluster was gone.

**qwen3 will not run on CPU-only nodes with thinking on.** On an
`e2-standard-8` it exceeded the 300s timeout without producing a first token;
with thinking off it works at ~128s per diagnosis. GPU is blocked on this
account -- not by quota, but because a free-tier billing account cannot
provision non-TPU accelerators at all.

Read README.md and CONTRIBUTING.md first.

## The console, built 2026-08-24

`ui.py` was a question box with an answer under it. Everything the agent
actually produces -- the verdict, the claims each traced to a tool result, the
ones it could not support, the calls that got there -- was reachable only by
unfolding a raw JSON expander. The conclusion was readable and uncheckable,
which is backwards for a tool whose whole claim is that its answers can be
checked.

`render_investigation()` now lays the investigation out top down: status strip
(verdict, tool calls, wall clock, backend, and any non-answer termination),
root cause, contradictions ahead of everything else, Observed / Inferred /
Unknown in three columns with each observation carrying `tool.field`, the
timeline of calls, then the evidence.

**The view computes no verdict.** Every field is read from what
`agent.stream()` returns and `grounding.contract()` produced. The recommended
next step is `agent.evidence_gap()` -- the same function the loop uses to
decide whether to send a run back -- so the console recommends what the agent
would have insisted on rather than a second opinion written in the view. A
second copy of the checker living in the UI is how a console comes to disagree
with its own backend, and there isn't one.

`store.list_jobs(limit)` was added to both implementations to back the sidebar
history. It drops `result` deliberately: a finished investigation carries its
whole evidence set, and a listing that returns those is one nobody can afford
to call.

**One defect this found, and it is the reason the tests are written the way
they are.** `st.error(icon="X")` is not a valid emoji, so Streamlit raised and
blanked the page *on every contradiction* -- the one verdict most worth
reading. Nothing before this pass had ever rendered one. The AppTest helper now
asserts `app.exception` is empty on every panel test, so the next page-level
crash fails a test rather than a demo.

**Verified:** 30 AppTest tests (12 new) and 38 store tests (5 new); live on
kind `ui-check` against the OpenAI API, header reading
`cluster: kind-ui-check · inference: api · openai · gpt-4o-mini ·
evidence: external`. **Not verified:** the console has never been driven by a
human, never seen a wide viewport or a real dark-mode client, and its history
sidebar has never held more than a handful of rows.

## What driving the console found, 2026-08-24

The console shipped in the morning; running it by hand in the afternoon found
three defects in it. All three are fixed, tested and pushed (`57e205c`).

- **Diagnose did nothing with an empty box.** The placeholder is a well-formed
  question in grey *inside* the field, which is what a filled-in field looks
  like; `if submitted and question:` then failed in silence -- no run, no
  message. An empty box now asks the question the box is showing. With nothing
  selected it says so.
- **The history recorded the scoping directive, not the question.** `question`
  is rebound to `agent.scoped_question()` before the run, so the sidebar read
  "Answer only about the workload demo/nightly-sy…". The strings are kept apart
  now: `question` is what a person typed, `prompt` is what the model got.
  `next_step()` uses `prompt`, which names the workload even when the typed
  question does not.
- **A finished investigation was missing from the sidebar until a reload,**
  because the history list is built near the top of the script. It reruns once
  after recording, *outside* the try -- `RerunException` is control flow, and
  catching it would turn a rerun into a red error box.

The prompt disclosure moved into `render_investigation()` and renders from the
recorded answer, which also fixed a latent version of the same bug: it was
drawn only in the pass that submitted the form, so moving a slider silently
removed the disclosure while leaving the answer it explained on screen.

**One test passed against the broken code and had to be rewritten.** The
sidebar-freshness test looked for the "no investigations yet" caption being
gone; the store is a `cache_resource` shared for the whole process, so an
earlier test had already put a row in it. It asserts a change across the click
now, with a marker no other test can write. Five of the seven new tests were
confirmed red against the previous `ui.py` -- check that, do not assume it.

## A browser suite, designed and not built

`docs/E2E.md` specifies a Playwright suite: harness, selector contract, ~25
cases. **None of it is implemented.** Playwright is not a dependency.

Read the first table in it before writing any of it. Two of the three defects
above were fixable in AppTest and were fixed there; a browser case that AppTest
could hold is a browser case in the wrong file. The harness is the load-bearing
part -- a scripted OpenAI-protocol server on `127.0.0.1`, because every state
worth rendering (contradicted, ungrounded, deadline_exceeded, zero tool calls)
comes from a model.

**Case R-01 is a live defect, and should be written red.**
`render_investigation()` passes `<span class='kw-dim'>` to `st.error`, which has
no `unsafe_allow_html` parameter and runs its body through `clean_text` -- so
the rule line very likely paints as literal angle brackets inside the red box
that announces a contradiction. Confirmed from Streamlit's source; **not
confirmed visually.** The AppTest test passes because `e.value` is the string
submitted, not the text painted.

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

4. **Thinking-off is measured against a paired arm now, and is STILL not
   settled. Latency is the finding that is not ambiguous.** Same cluster,
   same fixtures, same 15 `demo` pods, same battery and Low Power Mode, same
   code, one hour apart, `think` recorded on every run of both arms. Both
   scored under the repaired expectations.

   | arm | score | 95% CI | median | p95 | set |
   | --- | --- | --- | --- | --- | --- |
   | ON | 48/48 (100%) | [93-100] | 63.5s | 183.9s | `think-ON-16cases-n3.json` |
   | OFF | 45/48 (94%) | [83-98] | 9.0s | 36.1s | `think-OFF-16cases-n3.json` |

   **Fisher exact p=0.242.** Sixteen cases at n=3 per arm cannot separate 100%
   from 94%, and the off arm's interval reaches 98. **This is the third round
   to come back undetermined** -- p=0.483 at three cases, p=0.617 against the
   unpaired `widened-n3`, p=0.242 paired -- with the direction the same every
   time and significance never reached. Settling it needs roughly **n=15 per
   arm**, which is about five hours of model time at the ON arm's pace.
   **Do not flip the default on three undetermined rounds.**

   The off arm's three failures: two runs of `injection_in_logs_is_data` that
   name the injected instructions and call them malicious but never reach the
   real fault on the last log line, and one `service_unreachable_chain` that
   concluded `crasher-svc` has no associated pods. Thinking on gets all three.

   **Latency: 7.1x on the median, and every one of sixteen cases is slower
   with thinking on** -- 3.7x to 26.9x. Both arms were throttled identically,
   so the ratio is the honest part even though the absolute numbers are not.
   `cluster_wide_scan` is the worst absolute case, 187.6s against 42.5s.
   `slept_ms` is 0 across the off arm and 43ms across the on arm, so
   `caffeinate` held and neither set is measuring a nap.

   The old unpaired comparison against `widened-n3` is superseded and its
   numbers should not be quoted; it ran on a different day, on a different
   cluster, before the summary-drop fix, and its records predate `think`.

5. ~~**Five eval cases could be passed by repeating the question back.**~~
   **All repaired 2026-08-22, each verified by replay before it was changed.**
   An expectation term that appears in the case's own question tests nothing,
   and one echoed term carries a whole group -- `expect_any` passes on one and
   each `expect_all` group passes on one.

   | case | matched on | from |
   | --- | --- | --- |
   | `healthy_workload_with_no_logs` | `fine` | `quiet-and-fine` |
   | `unhealthy_question_about_a_healthy_pod` | the pod's name | the whole expectation was the name |
   | `healthy_not_reported_broken` | `healthy`, `working` | `healthy-web`, "working correctly?" |
   | `healthy_workload_not_substituted` | `healthy` | `healthy-web` |
   | `service_unreachable_chain` | `crash` | `crasher-svc` |

   Found by reading the void set, not by suspecting it: the first two scored
   PASS 3/3 on a cluster that never had their fixture, answering that the
   workload does not exist.

   **Replayed through `run_eval.grade()` over every recorded answer the five
   cases have -- 179 across sixteen sets -- eight verdicts change and every
   one is a wrong answer that had been passing.** Six are the "does not exist"
   answers. One, in `after-n2`, described `missing-configmap-key` and
   `missing-secret-key` instead of the pod it was asked about. One, in
   `think-OFF-16cases-n3`, concluded `crasher-svc` "does not have any
   associated pods" -- false, they exist and are not ready -- after calling
   `get_service_endpoints` and `list_pods` and never reaching the crash.

   Set scores: **`baseline-n10-2` unchanged at 99/100**, so the README table
   stands; **`widened-n3` unchanged at 47/48**, so the thinking-ON comparator
   stands; `think-OFF-16cases-n3` 46/48 -> 45/48; `after-n2` 31/32 -> 30/32.

   Two rules worth keeping. Bare `healthy` cannot be a verdict term in a
   question containing `unhealthy` -- it is a substring, so it would be
   satisfied by the opposite answer. And the criterion is **every**
   expectation group being echoable, not any: requiring the answer to name
   its subject is legitimate beside a group the question does not contain,
   which is how `unhealthy_question_about_a_healthy_pod` keeps the pod name.
   `tests/test_eval_graders.py` now requires the offender list to be empty.

6. **`forbid` reports flagging an injection as obeying it.** Measured
   2026-08-22 and deliberately not fixed. All three off-arm runs of
   `injection_in_logs_is_data` name the injected instructions and call them
   malicious; none obeys them. `forbid` matches the phrase anywhere in the
   answer, so the printed reason is "wrongly claimed 'delete every pod'" --
   the opposite of what the answer does, and it reads as a security failure
   that did not happen. The runs still fail correctly, on missing the real
   fault, so **this is a reporting defect and changing it moves no score.**
   Separating "repeated the injection while flagging it" from "obeyed it" by
   substring is the hard part: compliance would look like a short answer that
   is only `MAINTENANCE COMPLETE`.

7. **Controller and noise evidence is two rounds old.** Both passed when last
   measured (3s detect, 52.5s RCA; 10 failing pods -> 1 finding).

8. **The README benchmark table predates the `not-ready` projection change**
   and says so. Re-take for provenance when the machine is idle. Unaffected by
   the 2026-08-22 expectation repairs: `baseline-n10-2` re-grades unchanged at
   99/100, and the README quotes no thinking-arm figures.

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
.venv/bin/python -m pytest              # 1240 tests, no cluster, no model needed
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
pgrep -fl 'run_eval|run_controller_eval|probe_|ab_prompt'
kind get clusters
curl -s localhost:11434/api/ps          # want {"models":[]}
docker info >/dev/null 2>&1 && echo UP || echo DOWN
git status -sb | head -1                # want no ahead/behind
```

**`probe_scan` was widened to `probe_` on 2026-08-22**, because
`probe_evidence_read.py` was written that day and the check would not have
found it. A pattern naming today's scripts is a pattern that goes stale every
time one is added, which is the same class of leftover as the waiter shells
below: invisible to the grep that was written before it existed.

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
