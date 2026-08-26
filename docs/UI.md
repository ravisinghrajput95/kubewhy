# kubewhy — the operator console

`ui.py`, a single Streamlit page. It is an **AI-assisted Kubernetes
investigation console**, not a chatbot with Kubernetes branding, and the
difference shows up in one design rule:

> The view computes no verdict.

Every field on the page is read from what `agent.stream()` returned and
`grounding.contract()` produced. There is no second implementation of the
checker in the view, because a second implementation is how a console comes to
disagree with its own backend and neither one is obviously wrong.

## What is on the page

**Header strip** — cluster, inference mode, provider, model, and where evidence
goes (`on-network` or `external`). In api mode that last word is the difference
between a local tool and one that ships pod logs to a third party, so it is on
screen rather than behind a settings page.

**Scan** — `scan_cluster(only_unhealthy=True, limit=20)` printed above its own
table. The call that produced the table is shown so the reader can reproduce it.

**Why** — pick a workload, read its describe/events/logs directly. The scan
says *where*; this is *why*.

**Ask** — runs the whole agent loop. The tool chain streams as it happens, with
elapsed time in the label text, not only in a spinner. (A browser stops painting
animation frames when a tab is hidden, at which point a spinner freezes into a
static arc and a working investigation looks identical to a hung one. Measured:
with the tab backgrounded, `requestAnimationFrame` fired zero times in 1.2s
while the animation still reported `playState: "running"`.)

**The investigation panel**, top down:

| section | what it shows |
|---|---|
| status strip | verdict, tool-call count, wall clock, backend, and any non-answer termination |
| root cause | the answer, with contradictions rendered *before* it |
| what was sent | the scoped prompt, when the question was rewritten |
| what the evidence says | Observed / Inferred / Unknown, three columns, each observation carrying `tool.field` |
| timeline | every call with the arguments actually executed, and its scope action |
| evidence | the raw tool results the answer was built from |

## Investigation identity

The selected workload must be the investigated workload, all the way through.
This is enforced rather than displayed:

```
selection  →  agent.scoped_target(workload, namespace, pod)  →  stream(target=…)
```

The loop uses the target it is handed and does not re-derive one. That matters
because it used to: `targeting.target_of()` recovered the target by parsing the
prompt `scoped_question()` had just written, and that prompt is full of English
that parses as a name. `(for example pod nightly-sync-abc)` yielded a workload
called `example`; with that phrase removed it yielded `other`, from "Do not
report on any **other workload**". `enforce()` then rewrote every tool call to
the phantom — including calls the model had already got right — and the run died
on `no workload named example exists in this cluster`, identically on two
different models.

Parsing survives only for surfaces that genuinely have nothing but a sentence:
the CLI, Slack, MCP.

Every event carries a `run_id` and every answer carries its `target`, so
`selected == requested == tool == evidence == RCA` is a comparison a caller can
make rather than an assumption it has to trust.
`tests/test_investigation_identity.py` makes it, on two workloads in different
namespaces.

## The selection cannot move on its own

The scan is rebuilt on a TTL and its option list genuinely changes underneath
you — `only_unhealthy` hides a workload the moment it recovers, and a CronJob's
workload leaves the scan every time its pods complete. Two defects came from
that, both fixed:

- an unkeyed selectbox is **positional**, so a re-ordered scan moved the target;
- when the selected workload *left* the list, the index fell back to 0 and the
  target silently became an unrelated workload. Measured:
  `demo/nightly-sync → demo/bad-image`, no warning.

The selection is keyed and re-anchored by value; a workload that leaves the scan
stays selected, with a warning naming it. Moving the target is the operator's
decision.

## What the console deliberately does not do

- **No autonomous remediation.** It reads. Every tool is read-only.
- **No invented suggestions.** The "recommended next step" is
  `agent.evidence_gap()` — the same function the loop uses to decide whether to
  send a run back — so the console recommends what the agent would have
  insisted on, not a second opinion written in the view.
- **No client-side logic.** Streamlit renders server-side; the browser holds no
  Kubernetes client and no provider credential. `tests/test_ui_security.py`
  pins that: no `fetch(`, no provider host, no credential identifier anywhere in
  `ui.py`, and a configured API key appears nowhere on the rendered page.

## Testing

| file | what it covers |
|---|---|
| `tests/test_ui.py` | rendering, failure modes, the panel per verdict, the form |
| `tests/test_investigation_identity.py` | target integrity end to end, entity scoping, run isolation |
| `tests/test_ui_security.py` | credentials, client-side calls, redaction, evidence wiring |

`streamlit.testing.v1.AppTest` asserts the element tree — the string the script
*submitted*, never the text the browser *painted*. Appearance defects are
structurally invisible to it; `docs/E2E.md` designs the browser suite that would
see them and says which of them are worth having.

## Validation status

| | |
|---|---|
| **Functional UI validation** | **PROVEN** — rendering, failure modes, per-verdict panels, form behaviour, investigation identity, security boundary |
| **Automated browser-paint validation** | **NOT TESTED** — no browser E2E suite exists |

The screenshots and the GIF in `docs/images/` are real recordings against a live
cluster and serve as *qualitative* visual evidence. They are not automated
validation and must not be cited as such.

## Failure states the console renders

Each is a state the loop can genuinely produce, and each has a test:

| State | What the operator sees |
|---|---|
| `contradicted` | the contradiction rendered as an error **before** the answer, with the rule and the measurement |
| `insufficient_evidence` | the verdict named, with whatever evidence was collected still readable |
| `deadline_exceeded` | a bad chip in the status strip; nothing invented to fill the gap |
| `max_rounds` | likewise |
| zero tool calls | a warning that nothing was measured |
| backend unreachable | a red error, the form still usable, no blank page |
| cluster unreachable | the error shown, and no table implying a healthy cluster |
| an unrecognised verdict | rendered as-is rather than silently mapped onto a known one |

## Limitations

- **Authentication, but no authorization.** `ui.auth.enabled=true` puts an OIDC
  proxy in front and binds the console to loopback behind it; everyone who signs
  in sees everything the ServiceAccount can read, and there is no per-user
  model. Without it there is no authentication at all — loopback-pinned for that
  reason, and the chart requires `ui.exposureAcknowledged=true` to expose one
  in-cluster. ClusterIP only either way.
- **One replica.** Investigation history lives in this process's store.
- **Appearance is untested.** See the table above.
- **Streamlit's generated class names are not an API.** The console emits its own
  (`kw-strip`, `kw-claim`, `kw-hdr`) precisely so tests and any future browser
  suite have a stable anchor.
