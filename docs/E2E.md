# Browser tests for the console — a design

**Status: design only. Nothing here is implemented.** Playwright is not a
dependency of this repo yet, and no test in `tests/` drives a browser. This
document specifies the harness, the selector contract and the cases; it does
not describe anything that runs today.

## What this suite is for

`tests/test_ui.py` has 37 tests and they are good ones. They assert the element
tree the Streamlit script *produced*. A browser suite asserts what a person can
*see and do*, and those are not the same claim.

The distinction is not theoretical. From 2026-08-24, three defects in this
console and the layer that could see each:

| Defect | AppTest | Browser |
|---|---|---|
| `st.error(icon="✕")` raised and blanked the page on every contradiction | **caught it** — once a test rendered a contradiction at all | would also catch it |
| Diagnose did nothing with an empty box (`and question` failing silently) | **could** catch it, and now does | found it |
| `<span class='kw-dim'>` inside `st.error` renders as literal angle-bracket text | **cannot** catch it | only layer that can |

Read that table before writing a browser test. Two of the three are *not*
arguments for Playwright — the first was a per-verdict coverage gap, and the
second was a gap in driving the form. Both were fixed in AppTest, which is
faster, hermetic and already in CI. A browser case that AppTest could hold is a
browser case in the wrong file.

The third is the shape that justifies the suite. `st.error` takes no
`unsafe_allow_html` parameter — its body goes through `clean_text` and is
rendered as escaped markdown — so the `<span>` this console passes it reaches
the reader as visible source code inside the red box that announces a
contradiction. The AppTest assertion passes because `element.value` is the
string that was *submitted*, not the text that was *painted*. No assertion over
the element tree can distinguish those two, ever.

*(That defect is confirmed from Streamlit's API surface, not yet confirmed
visually. Confirming it is case R-01 below, and would be this suite's first
finding.)*

So: **a case belongs here only if the answer depends on the browser having
parsed, laid out, painted or animated something, or on a person having
interacted with it over time.** Everything else belongs in AppTest.

## The harness

Three fixtures, in dependency order. The first is the one that makes the rest
possible.

### 1. A scripted inference server — `tests/e2e/fake_model.py`

The blocker for browser-testing this console is that the interesting states are
produced by a model. `contradicted`, `ungrounded`, `deadline_exceeded` and
`0 tool calls` are exactly the states that never appear in a demo and exactly
the ones whose rendering has been wrong.

A small OpenAI-protocol HTTP server, returning a canned sequence of tool calls
and a final answer per scenario, removes the model from the test entirely:

```
TRIAGE_INFERENCE_MODE=api
TRIAGE_INFERENCE_PROVIDER=openai
TRIAGE_INFERENCE_ENDPOINT=http://127.0.0.1:{port}/v1
```

`127.0.0.1` classifies as internal, so no external-egress flag is needed and
the test cannot accidentally exercise the real API. Scenarios are named
fixtures (`oom_grounded`, `contradicted`, `no_tools`, `slow_then_answer`,
`backend_500`), selected per test by a header or a path prefix.

This is the single highest-value piece of the design. Without it every browser
case is a live-model case: slow, priced, and non-deterministic in exactly the
field the assertion reads.

### 2. A cluster

Two tiers, and the choice is per case:

- **kind + `demo/broken-pods.yaml` + `demo/config-faults.yaml`** for cases that
  need real collector output. Created once per session, not per test.
- **no cluster at all** for the rendering cases, which reach the panel through
  a pre-seeded investigation rather than through a scan.

A hard constraint learned the expensive way: **assertions must not depend on a
pod's status string.** `demo/memory-hog` reads `OOMKilled` in one scan and
`CrashLoopBackOff` in the next, minutes apart, on the same pod. Assert on
structure (a row exists, a column is populated, the fault class is `crash`),
never on which of those two words appears.

### 3. The server under test

Start `streamlit run ui.py` on an ephemeral port with `--server.headless true`,
poll `/_stcore/health` until `ok`, yield the base URL, terminate on teardown.
Session-scoped. Never port 8501 — a developer's own console is usually there.

## The selector contract

Streamlit's generated class names are not an API. Two stable hooks exist:

1. **Streamlit's own `data-testid`** — `stMarkdown`, `stButton`, `stTextInput`,
   `stExpander`, `stDataFrame`, `stAlert`, `stSidebar`. Stable across patch
   releases, not across majors.
2. **This console's own classes**, which it already emits and which the AppTest
   suite already keys on: `.kw-hdr`, `.kw-strip`, `.kw-chip`, `.kw-claim` with
   `.kw-ok` / `.kw-warn` / `.kw-bad`, `.kw-next`, `.kw-dim`.

The second set is the better anchor and it exists because the panel was built
from classed divs rather than bare markdown. Prefer it.

**One change to `ui.py` this design requires:** the widgets a test drives need
explicit `key=` arguments (`key="question"`, `key="diagnose"`, `key="context"`,
`key="only_unhealthy"`), which Streamlit surfaces as a `.st-key-{key}` class on
the wrapper. Today those widgets have no keys and can only be located
positionally — and positional lookup is what made the AppTest widget indices
fragile (`text_input[0]` is Question, `[1]` is Filter, discovered by trial).
This is a four-line change and it is a prerequisite, not an optimisation.

## The cases

Grouped by the property under test. Each names the fixture it needs.

### R — rendering integrity (no cluster; seeded answer)

- **R-01 A contradiction's rule line is text, not markup.** Assert the red
  alert's `innerText` contains `rule: imposed_termination_vs_application_cause`
  and does *not* contain `<span` or `kw-dim`. *This is the known defect above
  and should be written first, red.*
- **R-02 A contradiction does not blank the page.** With the contradicted
  scenario, assert the alert is visible **and** that `#### Timeline` still
  renders below it. The regression that shipped killed everything downstream;
  asserting the alert alone would not have caught it.
- **R-03 Cluster-controlled text cannot inject markup.** Seed a claim, a pod
  name and a log line containing `<img src=x onerror=...>` and
  `</div><script>`. Assert no extra element was created, `window.__xss` is
  undefined, and the payload appears as visible text. This is not paranoia:
  `demo/adversarial.yaml` exists because cluster content reaches the model, and
  the model's words reach `unsafe_allow_html`. The observation/inference/unknown
  paths call `html.escape`; `.kw-next` interpolates `{step}` without it, which
  is safe only for as long as its inputs stay Kubernetes-shaped names.
- **R-04 The page never scrolls horizontally.** Assert
  `documentElement.scrollWidth <= clientWidth` at 1280, 1440 and 1920, with a
  200-char claim and a 40-argument tool call on screen. The prompt-disclosure
  panel already had this bug once: the sentence "Do not report on any other
  workload" scrolled off the right edge, which is the sentence that matters.
- **R-05 The disclosure shows its own last line.** Not "the expander exists" —
  open it and assert the final sentence of the scoped prompt is within the
  viewport box. R-04's bug passed an existence assertion.
- **R-06 Long values wrap or scroll inside their container**, not out of it:
  the evidence `<pre>`, the timeline table, the claim columns.

### T — theme (no cluster; seeded answer)

- **T-01 Both schemes paint.** Under `color_scheme="dark"` and `"light"`,
  assert every `.kw-chip` and `.kw-claim` has a computed colour that differs
  from its computed background. The console defines `:root` tokens and a
  `prefers-color-scheme` block; nothing has ever checked the resolved values.
- **T-02 The verdict tones stay distinguishable** — `.kw-ok`, `.kw-warn` and
  `.kw-bad` resolve to three different colours in both schemes. A palette that
  collapses under one scheme turns the verdict into decoration.
- **T-03 `EVIDENCE external` is visually distinct** from the rest of the header
  in both schemes. It is the one word on the page that says pod logs leave the
  network.

### I — interaction over time (scripted model; no cluster needed)

- **I-01 Diagnose with an empty box runs**, and the caption names the question
  that ran. *(Now held by AppTest too; keep the browser copy only because the
  grey-placeholder illusion is a rendering property.)*
- **I-02 A second click during a run does not start a second investigation.**
  With `slow_then_answer`, click Diagnose twice and assert the fake model
  received exactly one request. Untested today, at any layer.
- **I-03 Progress appears before the answer does.** With a scripted 3-tool
  chain and a delay between calls, assert the status label text changes at
  least twice before the panel appears. The comment in `ui.py` about spinners
  freezing when a tab is backgrounded is a real measurement; this asserts the
  textual progress that replaced it.
- **I-04 Enter in the question box submits** — the form exists for this.
- **I-05 The keyboard reaches everything.** Tab order covers question,
  checkbox, Diagnose; focus is visible at each stop.

### S — state and sessions (scripted model + kind)

- **S-01 History appears without a reload** (regression: it did not, until the
  rerun fix).
- **S-02 The history label is the typed question**, not the scoping directive
  (regression: it was the directive).
- **S-03 Clicking a history row restores that investigation** — panel contents
  match the row clicked, including verdict and tool count.
- **S-04 Reload loses the panel but not the history.** `session_state` dies
  with the tab; the store does not. After F5 the answer is gone from the main
  column and present in the sidebar. This is the behaviour `_history()` was
  written for and nothing has ever exercised it.
- **S-05 Two tabs share one store.** Investigate in tab A, rerun tab B, assert
  A's investigation is in B's sidebar. The store is a `cache_resource`, process
  wide — this asserts the design intent rather than an accident.
- **S-06 "New investigation" clears the answer and keeps the subject.**

### F — failure modes (scripted model)

- **F-01 Backend unreachable** → a red error, the form still usable, no blank
  page. Scenario `backend_500`.
- **F-02 Cluster unreachable** → the error is shown and no table implies a
  healthy cluster. Point `KUBECONFIG` at a file with no `current-context` —
  which is not hypothetical, it happened during this session's own testing.
- **F-03 `deadline_exceeded`** shows in the status strip as a bad chip.
- **F-04 An answer with zero tool calls** shows the warning rather than a green
  verdict.

### P — performance, as a budget rather than a benchmark

- **P-01 A 200-workload scan paints in under 5s** and the table is scrollable
  rather than 200 rows of DOM. One case, generous threshold, guarding a cliff.

## What must not be tested here

Verdict computation, grounding, contradiction rules, store semantics, redaction,
endpoint classification, the agent loop. All of these have fast hermetic tests
and a browser adds nothing but minutes and flake. If a browser case fails and
the cause is in one of those, the fix is a unit test plus a deletion here.

## Runtime and CI

Browser cases cost 2-5s each plus fixture setup; the kind tier costs ~3 minutes
once. Target: **R, T, I, F on every PR** (no cluster, scripted model, well under
two minutes), **S and P nightly** (kind tier). A suite that makes the PR loop
slow gets skipped, and a skipped suite protects nothing.

## What I would validate first

1. That R-01 fails. A design whose first case passes on known-broken code is
   measuring the wrong thing — the freshness test in `tests/test_ui.py` did
   exactly that this session and had to be rewritten.
2. That the scripted model server actually receives the request. An eval case
   passed 3/3 once while its payload never reached the model; the same trap is
   open here, and I-02 depends on request counting being real.
3. That `.st-key-*` appears in the DOM on this Streamlit version before writing
   selectors against it.
