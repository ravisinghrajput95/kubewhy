"""
Runs the eval cases against a live demo cluster and a live model.

This tests the agent, not the code: whether the model actually reaches the
right root cause. It needs a cluster and Ollama running, which is why it lives
outside tests/ and never runs in CI.

    kind create cluster --name triage-demo
    kubectl apply -f demo/broken-pods.yaml
    python evals/run_eval.py
    python evals/run_eval.py --model llama3.2 --repeat 3

Because the model is non-deterministic, --repeat runs each case several times
and reports a pass rate. A single failure is noise; a low rate is a finding.
"""

import argparse
import datetime as dt
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# This directory too, so `from cases import CASES` resolves when something
# other than the shell imports this file -- the grader has tests now, and they
# do not run from evals/.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import agent  # noqa: E402
import routers.k8s_pods_info as k8s  # noqa: E402
from cases import CASES  # noqa: E402
from host_state import low_power_mode  # noqa: E402
from ollama_state import resident  # noqa: E402


def _satisfied(group, text):
    return any(term.lower() in text for term in group)


# Transport failures that mean the provider never answered. Matched on the
# recorded answer text, because that is what the record keeps -- the exception
# object is long gone by the time anyone reads results/.
#
# Deliberately narrow, and anchored on the "ERROR: " prefix this harness itself
# writes. A wrong ANSWER that happens to contain the word "disconnected" is a
# real failure and must stay scored; only a run that produced no answer at all
# is void. The three phrases are httpx's, from a provider that accepted the
# connection and then went away mid-request.
_PROVIDER_GONE = (
    "server disconnected",
    "connection reset",
    "remoteprotocolerror",
    "failed to connect to ollama",
    "connection refused",
    "read timed out",
)


def provider_failed(result):
    """
    Why this run should be VOID rather than scored, or None.

    A run is void when the harness's own error path produced the answer AND no
    tool was ever called. Both halves matter: the prefix says this text came
    from `except Exception` rather than from a model, and the empty trace says
    the run did not get far enough to have done anything worth grading. A run
    that called four tools and then lost the connection on its last round has
    still demonstrated most of what the case measures, and voiding it would
    quietly shrink n for a run that mostly happened.
    """
    answer = str(result.get("answer") or "")
    if not answer.startswith("ERROR: "):
        return None
    if result.get("tool_calls"):
        return None
    lowered = answer.lower()
    for phrase in _PROVIDER_GONE:
        if phrase in lowered:
            return answer[7:][:80]
    return None


def grade(case, result):
    """
    Return (passed, [reasons it failed], [notes]).

    `forbid` is read against whether the question was answered, for the same
    reason `tools_named_but_not_called` is in the controller eval: one string
    match cannot tell *answered about the wrong thing* from *answered, then
    mentioned another thing*, and only the case knows which of those it is
    looking at.

    Measured on `healthy_workload_not_substituted`, the case that motivated
    `forbid` in the first place. Four failures were recorded with their answer
    text -- 2 of 30 in a replay probe, 2 of 20 live -- and all four read like
    this:

        The healthy-web deployment in demo is running normally with 2 ready
        replicas. No issues detected. Other deployments in demo (like
        bad-image, memory-hog) are unhealthy and may require investigation.

    That is the correct answer with a true aside, and it was being scored as
    the substitution the case exists to catch. Nothing in the run was wrong;
    the grader was. A substitution failing to deliver the verdict still fails,
    because `answered` is false there.

    The 2026-08-17 baseline's 2 failures on this case are unverified: they
    carry the same two reasons, `memory-hog` and `bad-image`, and predate the
    run that keeps answer text, so there is nothing left to read.

    Notes are printed and recorded, never scored -- an aside is worth seeing,
    since one that grew into a diagnosis of the neighbour would be a real
    failure and this is the only place it would show up first.
    """
    answer = result["answer"].lower()
    called = [c["name"] for c in result["tool_calls"]]
    failures = []
    notes = []

    # An adversarial case must prove its payload reached the model, or it is
    # not testing anything.
    #
    # `injection_in_annotations_is_data` asserted the agent resists an
    # injection delivered through pod annotations, and passed 3/3 for weeks.
    # No projection carries annotations -- verified live on 2026-08-23 across
    # all seven Kubernetes tools against a pod whose annotations did contain
    # the injection -- so the payload never entered the context. The defence
    # was real and the test was vacuous, which are different things, and
    # nothing in the harness could tell them apart.
    #
    # A case declaring `payload` now fails when that text is absent from the
    # evidence the run actually collected. Failing rather than skipping is
    # deliberate: a silent skip is how the first one hid.
    payload = case.get("payload")
    if payload:
        collected = " ".join(
            str(item.get("result", "")) for item in result.get("evidence", [])
        ).lower()
        if payload.lower() not in collected:
            failures.append(
                f"payload {payload!r} never reached the model -- this run "
                f"tested nothing"
            )

    wanted = "expect_any" in case or "expect_all" in case
    missing = []

    if "expect_any" in case and not _satisfied(case["expect_any"], answer):
        missing.append(f"none of {case['expect_any']} in answer")

    for group in case.get("expect_all", []):
        if not _satisfied(group, answer):
            missing.append(f"missing {group}")

    failures += missing

    # Answered means the case's own positive expectations were all met. A case
    # that declares none has nothing to condition on, so its forbid list stays
    # unconditional rather than silently becoming advisory.
    answered = wanted and not missing

    for term in case.get("forbid", []):
        if term.lower() not in answer:
            continue
        if answered:
            notes.append(f"named {term!r} alongside the answer")
        else:
            failures.append(f"wrongly claimed {term!r}")

    for tool in case.get("expect_tools", []):
        if tool not in called:
            failures.append(f"never called {tool}")

    for tool in case.get("forbid_tools", []):
        if tool in called:
            failures.append(f"should not have called {tool}")

    # A correct root cause wrapped around an invented figure passed every case
    # in this suite until 2026-08-19, because nothing here read the grounding
    # verdict. Observed live the day before, all three inside otherwise
    # correct diagnoses: 512Mi for a container measured at 64Mi, "503 Service
    # Unavailable" for a connection that was refused, and "exit 137 means the
    # OOM killer" for a pod with no memory limit at all.
    #
    # Opt-in per case rather than suite-wide, so the ten original cases keep
    # measuring what they measured and stay comparable to every published
    # number. The unverified claims are recorded as a note either way, because
    # a fabrication next to a right answer is worth seeing even where it is
    # not being scored.
    unverified = result.get("unverified") or []
    if unverified:
        if case.get("require_grounded"):
            failures.append(f"unverified claims: {unverified}")
        else:
            notes.append(f"stated {unverified} with no measurement behind it")

    # The verdict the checker reached, scored against the verdict this scenario
    # is supposed to produce. `require_grounded` above is a boolean and can only
    # say "no unverified claims"; it cannot express "this scenario has no
    # answer in the cluster, so insufficient_evidence is the CORRECT outcome and
    # a confident root cause is the failure". Those cases are the point of the
    # insufficient-evidence category and there was no way to score them.
    expected = case.get("expected_grounding")
    if expected:
        got = result.get("confidence")
        if got not in expected:
            failures.append(f"grounding verdict {got!r}, expected one of {expected}")

    # Contradictions are RECORDED, not scored, and that is a measured decision
    # rather than a soft one. Replaying this rule as a hard failure over the 793
    # recorded runs that kept both answer and evidence flipped 4 previously
    # passing runs. Two were read in full:
    #
    #   TRUE  - "the workload does not exist in the cluster" for crasher-svc,
    #           while the evidence carried not_ready_endpoints ["10.0.0.13"].
    #           The pod exists and is merely unready. The old grader passed it.
    #   FALSE - a correct database-connection diagnosis citing exit code 1,
    #           failed because the word "oomkilled" appeared later in the answer
    #           as a possibility being ruled out.
    #
    # One in two of the runs it would newly fail was a false positive, and a
    # grader that cries wolf on correct work is one people learn to ignore.
    # `contradictions` is reported as its own metric instead -- which is what
    # the phase brief asks for anyway: every metric independently, no collapsing
    # into a single score.
    if result.get("contradictions"):
        notes.append("contradicted: " + "; ".join(
            f"{c.get('claim')} vs {c.get('measured')}"
            for c in result["contradictions"][:3]))

    return not failures, failures, notes


def populated(pods):
    """
    Whether list_pods() came back with pods in it.

    Its shape is a dict keyed by pod name -- {"crasher-abc": {...}} -- and an
    error is the same dict type carrying an "error" key. The first version of
    the caller tested `not isinstance(pods, dict)`, which reads every real
    answer as an empty cluster, and its test passed because the test handed it
    a list. Shared with preflight() so the two cannot drift apart again.
    """
    if isinstance(pods, dict) and pods.get("error"):
        return False
    return bool(pods)


def fixtures_present(cases):
    """
    Cases whose fixture file has not been applied to this cluster.

    A case declares `needs`, and until now nothing read it. On 2026-08-22 a
    sixteen-case set ran against a cluster carrying only demo/broken-pods.yaml
    and reported 36/48 for the thinking-off arm. Six of those cases were
    unreachable: four failed because the workload they ask about does not
    exist, and -- worse -- two PASSED, because a question of the form "is this
    healthy?" is satisfied by an answer about a pod that is not there at all.
    A missing fixture is therefore not a visible failure. It is a silent one
    in both directions.

    Checked by namespace: every namespace the manifest declares must hold at
    least one pod. That catches the case this is built for, a file never
    applied, without asserting a pod-by-pod inventory that would go stale
    every time a fixture gains a workload.
    """
    import yaml

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    missing = {}
    checked = {}
    for case in cases:
        needs = case.get("needs")
        if not needs:
            continue
        path = os.path.join(root, needs)
        try:
            with open(path) as handle:
                docs = [d for d in yaml.safe_load_all(handle) if d]
        except OSError as exc:
            missing.setdefault(needs, set()).add(f"unreadable: {exc}")
            continue

        for namespace in {
            (doc.get("metadata") or {}).get("namespace") for doc in docs
        } - {None}:
            if namespace not in checked:
                checked[namespace] = populated(k8s.list_pods(namespace=namespace))
            if not checked[namespace]:
                missing.setdefault(needs, set()).add(namespace)

    return {needs: sorted(where) for needs, where in missing.items()}


def preflight(namespace="demo"):
    """
    Which cluster this is about to measure, and whether the fixtures are on it.

    Both halves were learned the same way. A run started against the kind
    cluster on 2026-08-17 and, one case in, `current-context` moved to a GKE
    cluster somebody else had just created; every tool call after that
    answered honestly about an empty cluster, and the model was left saying
    "the namespace demo does not exist". Unattended, that is 100 runs and 90
    minutes producing a score of zero that looks exactly like the agent having
    catastrophically regressed.

    The context is ambient state that anything on the machine can change --
    `kind create`, `gcloud container clusters get-credentials`, another
    terminal -- so an eval that reads it without recording it cannot say what
    it measured. Returns the context name to store beside the results.
    """
    context = k8s.active_context()
    pods = k8s.list_pods(namespace=namespace)

    if isinstance(pods, dict) and pods.get("error"):
        print(
            f"cluster {context!r} cannot answer for namespace {namespace!r}: "
            f"{pods['error']}\n"
            "Point --context at the cluster holding demo/broken-pods.yaml.",
            file=sys.stderr,
        )
        return None, context

    return len(pods), context


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default=agent.MODEL)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--case", help="run only this case by name")
    parser.add_argument("--json", help="write per-run results to this file")
    parser.add_argument(
        "--context",
        help="kubeconfig context to measure against; defaults to current-context, "
             "which anything on the machine is free to change mid-run",
    )
    args = parser.parse_args()

    cases = [c for c in CASES if not args.case or c["name"] == args.case]
    if not cases:
        print(f"no case named {args.case!r}", file=sys.stderr)
        return 2

    if args.context:
        k8s.use_context(args.context)

    pods, context = preflight()
    if pods is None:
        return 2

    # Before any model time is spent. A case whose fixture is absent does not
    # measure the agent, and it does not announce itself either -- see
    # fixtures_present. Refuse rather than warn: the whole value of the number
    # at the end is that every case in it was reachable.
    absent = fixtures_present(cases)
    if absent:
        for needs, where in absent.items():
            print(f"{needs} has not been applied: no pods in "
                  f"{', '.join(where)}", file=sys.stderr)
        print("kubectl apply -f " + " -f ".join(sorted(absent)), file=sys.stderr)
        return 2

    print(
        f"model={args.model}  cases={len(cases)}  repeat={args.repeat}  "
        f"context={context}  demo pods={pods}  "
        f"think={'on' if agent.THINK else 'off'}\n"
    )

    total_passes = total_runs = voids = 0
    ungrounded = 0
    # The metric that matters: a right answer resting entirely on measurement.
    # `score` alone cannot separate that from a right answer carrying an
    # invented figure, and those are different products.
    clean = 0
    records = []
    # Accumulated across rounds rather than printed as we go: repeat-major
    # ordering means no case is finished until the final round.
    stats = {
        c["name"]: {"passes": 0, "runs": 0, "elapsed": 0.0, "reasons": []}
        for c in cases
    }

    # Repeat-major, not case-major, and the ordering is the whole point.
    # An interrupted run is the normal outcome here rather than the
    # exception: a full set takes over an hour, and the defect this file
    # exists to measure is one that hangs for thousands of seconds. What an
    # interruption leaves behind is therefore a design decision.
    #
    # Case-major leaves a few cases complete and the rest never run, which is
    # not a sample of the suite -- it is a complete measurement of whichever
    # cases happened to be first. Measured on 2026-08-15: the machine shut
    # down 61 runs in and four of the ten cases had never run once, so the
    # suite-wide number was unusable while the early cases were oversampled.
    # Repeat-major would have left six rounds of all ten.
    for round_index in range(args.repeat):
        for case in cases:
            passes = 0
            elapsed = 0.0
            reasons = []
            was_resident = resident(args.model)
            # Wall clock and machine load at the moment the run began. Without
            # these a stall can only be described, never attributed: the first
            # set of numbers showed slow runs arriving in adjacent pairs on a
            # model that was resident throughout, which rules out the loader
            # and leaves contention as the obvious next suspect -- and nothing
            # recorded said what else the machine was doing at the time.
            began_at = dt.datetime.now().isoformat(timespec="seconds")
            load_before = os.getloadavg()[0]
            started = time.time()
            try:
                result = agent.ask(case["question"], model=args.model,
                                   evidence=True)
            except ConnectionError as exc:
                # Infrastructure, not the model. Scoring this as a failed case
                # would report a plausible-looking low score for what is
                # really "nothing ran".
                print(f"\nollama is unreachable: {exc}", file=sys.stderr)
                print("start it with `ollama serve`", file=sys.stderr)
                return 2
            except Exception as exc:
                result = {"answer": f"ERROR: {exc}", "tool_calls": [], "confidence": "ungrounded"}
            elapsed += time.time() - started

            # A run the provider never answered is VOID, not failed, and this
            # is the second time that distinction has had to be made in this
            # repo -- probe_evidence_read.py voids a capture that came back
            # empty for the same reason. Scoring "nothing ran" as a wrong
            # answer reports a plausible-looking low number for an outage.
            #
            # The ConnectionError branch above already aborts the suite when
            # the provider is down at the start. It is deliberately not
            # widened to cover this: a transport error mid-run is usually one
            # blip in an unattended run of hundreds, and aborting six hours of
            # work over it loses far more than it protects. Voiding the run
            # keeps the rest.
            #
            # Measured 2026-08-30: `oomkill_root_cause` recorded 2 passes and
            # one `passed: False` whose answer was the string "ERROR: Server
            # disconnected without sending a response" with no tool calls at
            # all. That scored 2/3 for a case that was really 2/2 and a blip.
            # httpx raises RemoteProtocolError there, which is not a
            # ConnectionError, so it fell past the guard written to catch
            # exactly this.
            void_reason = provider_failed(result)
            if void_reason:
                voids += 1
                print(f"  r{round_index + 1:<3} {case['name']:32} "
                      f"VOID  {void_reason}", flush=True)
                records.append({
                    "case": case["name"], "model": args.model,
                    "void": True, "void_reason": void_reason,
                    "answer": result.get("answer", ""),
                    "seconds": round(elapsed, 1),
                    "started_at": began_at,
                })
                if args.json:
                    with open(args.json, "w") as fh:
                        json.dump(records, fh, indent=1)
                continue

            ok, why, notes = grade(case, result)
            passes += ok
            reasons += why
            if result.get("confidence") != "grounded":
                ungrounded += 1
            if ok and not (result.get("unverified") or []):
                clean += 1

            records.append({
                "case": case["name"],
                "model": args.model,
                # Which cluster this run asked about -- the context the client
                # is bound to, not whatever current-context says now, since
                # the two stop agreeing the moment anything else touches the
                # kubeconfig. On the record rather than in a header because
                # the record is the unit anyone reads a year later, and it is
                # a cached lookup.
                "context": k8s.active_context(),
                # Which arm this is. TRIAGE_THINK is ambient state read at
                # import time, exactly like current-context was before the
                # 2026-08-17 mix-up, and a set that does not record it cannot
                # say afterwards which arm it measured. The thinking-off
                # comparison has one set of three cases per arm and rests
                # entirely on remembering which shell exported what.
                "think": agent.THINK,
                "passed": bool(ok),
                "seconds": round(time.time() - started, 1),
                "confidence": result.get("confidence"),
                # Whether the weights were already loaded when this run began.
                # A stall on a run that had to load them is a loader problem,
                # not a model one, and the two have been indistinguishable in
                # every latency figure this project has published so far.
                "model_resident": was_resident,
                "started_at": began_at,
                # 1-minute load average, sampled before and after. A run that
                # is slow because sixteen other things were running is not
                # telling you anything about the agent.
                "load_before": round(load_before, 2),
                "load_after": round(os.getloadavg()[0], 2),
                # Whether macOS was throttling the machine. Measured
                # 2026-08-18: a set's median run time doubled mid-run with
                # every second attributed to model_ms, tools at 0.03s,
                # slept_ms zero and load at 0.63 -- the battery had drained
                # and Low Power Mode had switched itself on. Low Power Mode
                # throttles the GPU, so a local model halves in speed while
                # every timer stays honest and the host looks idle. See
                # host_state.low_power_mode.
                "low_power_mode": low_power_mode(),
                # What was flagged, not just that something was: without this
                # a rise in the unverified count cannot be told apart from the
                # checker getting stricter.
                "unverified": result.get("unverified", []),
                "tools": [c["name"] for c in result.get("tool_calls", [])],
                # The arguments too, because for several tools the name is not
                # the behaviour. scan_cluster() and scan_cluster(workload='x')
                # return different things -- the second reports one workload
                # whether or not it is broken -- and the 2026-08-17 baseline
                # recorded only the name, so the runs that answered about the
                # right workload could not be told from the ones that asked
                # the right question. Same for namespace on every pod tool.
                "arguments": [
                    c.get("arguments", {}) for c in result.get("tool_calls", [])
                ],
                # What the tools returned, not only what they were asked. This
                # is the record's only irreplaceable field: everything else
                # here can be recomputed from a re-run, and this cannot,
                # because the cluster has moved on and the model answers
                # differently the second time.
                #
                # It exists because of a question that could not be answered
                # on 2026-08-21. grounding.py was changed to stop treating a
                # hedged status as a fabrication, and the obvious check --
                # re-score the existing sets under both versions and compare
                # -- was impossible, since no set held the tool output the
                # checker reads. The comparison had to be run against
                # probe_scan_summary.py's records instead, which cover one
                # case. With this field any past set can be re-scored offline,
                # for free, against any future checker.
                #
                # grounding.records() shape, so a replay is
                # grounding.check(record["answer"], record["evidence"])
                # verbatim -- same ids, same ordering, prefetched entries in
                # the same places the live check saw them.
                "evidence": result.get("evidence", []),
                # The answer as the checker read it. `answer` below is the
                # published text, after unsupported values were rewritten and
                # the audit markers added, so it is the right field to read
                # and the wrong one to re-check: replaying check() against it
                # scored 3 of 5 live runs differently on 2026-08-21, since the
                # audit footer contributes digits of its own.
                "draft": result.get("draft"),
                # The answer itself, not just the strings it was missing. A
                # failure recorded as ["missing ['memory-hog']"] cannot say
                # whether the model looked and found nothing, described a
                # different workload, or wrote a plan -- and those want three
                # different fixes. Re-running to find out costs another hour
                # and gets a different answer, this being a model.
                "answer": result.get("answer", ""),
                # How many times the run was sent back for naming a tool it
                # never called. A run that reached get_pod_logs on its own and
                # one that had to be told are the same trace without this.
                "nudges": result.get("nudges", 0),
                # How many times a deterministic policy sent the run back for
                # evidence the status block provably does not contain. Without
                # it, a run that collected events on its own cannot be told
                # from one that had to be made to -- and the first 32-run set
                # after the policy shipped could not say whether it had ever
                # fired.
                "policies": result.get("policies", 0),
                # How many times the run was sent back for leaving workloads
                # out of its own summary. Beside nudges and policies for the
                # same reason they are there: a run that listed everything
                # first time reads identically to one that was asked twice.
                "coverage": result.get("coverage", 0),
                # How many times the run was sent back because a claim in its
                # answer contradicted a value in its own evidence. The three
                # above are about what the run did; this one is about what it
                # concluded. Recorded for the reason `policies` is: the first
                # n=5 set after the re-ask shipped had no field for it, so the
                # records could not say whether the thing being measured had
                # fired at all.
                "reconciles": result.get("reconciles", 0),
                "failures": why,
                # Not failures, and not noise either: an answer that names a
                # broken neighbour beside a correct verdict is one edit away
                # from the substitution this suite is watching for, and a run
                # that only prints its failures cannot show that drift.
                "notes": notes,
                # Where the wall clock went, split model against tools, with
                # the per-round breakdown. `seconds` above can only say a run
                # took 2217s; this says whether one round hung or every round
                # was slow, which is the distinction that killed the last two
                # hypotheses for lack of evidence.
                "timing": result.get("timing"),
                # --- added for the evaluation baseline ----------------------
                # Every metric the report states independently has to come out
                # of the record, not out of a re-run. Collapsing these into one
                # number is what the phase brief forbids, and a record that
                # cannot separate "wrong" from "correct but unsupported" forces
                # exactly that.
                #
                # The grounding contract as the checker produced it: observed /
                # inferred / unknown / contradicted, each with citations.
                "rca": result.get("rca"),
                # Claims the checker found a measurement AGAINST. Distinct from
                # `unverified`, which is silence. Only one of the two means the
                # answer is wrong.
                "contradictions": result.get("contradictions", []),
                # Why the run stopped. `deadline_exceeded` and `max_rounds` are
                # different operational failures and neither is a wrong answer.
                "termination": result.get("termination"),
                "budget_s": result.get("budget_s"),
                # Which investigation produced this record, and what it was
                # about -- so a scoped run can be checked for having stayed on
                # its target rather than assumed to have.
                "run_id": result.get("run_id"),
                "target": result.get("target"),
            })

            # Written after every run, not at the end. A full set is over an
            # hour, and the defect this file exists to measure is a run that
            # hangs for 1013s -- so the interrupted run is the likely one, and
            # holding sixty results in memory until then loses exactly the
            # evidence that was worth collecting. ab_prompt.py already does
            # this; run_eval.py should not be the one that throws it away.
            if args.json:
                with open(args.json, "w") as fh:
                    json.dump(records, fh, indent=1)

            total_passes += passes
            total_runs += 1
            entry = stats[case["name"]]
            entry["passes"] += passes
            entry["runs"] += 1
            entry["elapsed"] += elapsed
            entry["reasons"] += reasons

            # One line per run rather than one per case: with repeat-major
            # there is nothing to summarise until the end, and an hour of
            # silence is indistinguishable from a hang -- which is the exact
            # failure this suite is trying to characterise.
            # A nap is printed next to the run it happened in, because the
            # alternative is what happened for two months: a run reported as
            # 725s against a 62s median, with nothing to say the host had been
            # suspended for 548s of it. See agent._timing.
            slept = (result.get("timing") or {}).get("slept_ms") or 0.0
            # flush, because stdout is block-buffered the moment this is
            # redirected to a file -- which is how a long run is always
            # started. The per-run line exists so an hour of silence can be
            # told from the hang this suite measures, and a 4KB buffer gives
            # exactly that silence back.
            print(
                f"  r{round_index + 1:<3} {case['name']:32} "
                f"{'PASS' if passes else 'FAIL':5} {elapsed:6.1f}s"
                + (f"  [host asleep {slept / 1000:.0f}s]" if slept > 1000 else ""),
                flush=True,
            )
            for note in notes:
                # `~` rather than `-`, matching the controller eval: a reader
                # scanning a hundred lines needs to see at a glance that this
                # one did not count against the score.
                print(f"       ~ {note}", flush=True)

    print()
    for case in cases:
        entry = stats[case["name"]]
        if not entry["runs"]:
            continue
        mark = (
            "PASS" if entry["passes"] == entry["runs"]
            else ("FLAKY" if entry["passes"] else "FAIL")
        )
        print(
            f"{mark:6} {case['name']:32} {entry['passes']}/{entry['runs']}  "
            f"{entry['elapsed'] / entry['runs']:5.1f}s"
        )
        for reason in dict.fromkeys(entry["reasons"]):
            print(f"         - {reason}")

    if args.json:
        with open(args.json, "w") as fh:
            json.dump(records, fh, indent=1)
        print(f"\nwrote {len(records)} runs to {args.json}")

    rate = total_passes / total_runs * 100 if total_runs else 0
    print(f"\nscore: {total_passes}/{total_runs} ({rate:.0f}%)")
    if voids:
        # Loud, and separate from the score. A run the provider never answered
        # is excluded from the denominator, so the reader has to be told how
        # many there were -- a 60-run suite reporting 40 is a different claim
        # from one reporting 60, and silently shrinking n is how a sample
        # stops meaning what its interval says.
        print(f"VOID: {voids} run(s) excluded -- the provider did not answer. "
              f"n is {total_runs}, not {total_runs + voids}.")
    print(
        f"fully grounded and correct: {clean}/{total_runs} "
        f"({clean / total_runs * 100:.0f}%)" if total_runs else ""
    )
    print(f"answers not fully grounded: {ungrounded}/{total_runs}")

    # Non-zero exit on a clear regression, so this can gate a release even
    # though it is too slow and too non-deterministic for CI.
    return 0 if rate >= 70 else 1


if __name__ == "__main__":
    raise SystemExit(main())
