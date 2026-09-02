"""
Claims that disagree with the evidence, rather than claims the evidence cannot
support.

`grounding.check()` asks one question well: does this value appear in what the
tools returned? It cannot ask the other one. Measured 2026-08-23 during the
adversarial validation phase:

    evidence : last_termination.reason = "OOMKilled"
    answer   : "The pod is in CrashLoopBackOff, which means the container
                exited with an application error."
    verdict  : grounded, zero unverified claims

Every value in that sentence was measured. `CrashLoopBackOff` is in the
evidence, so the claim traced, so the answer got the badge a reader trusts
most -- while telling them the opposite of what the kubelet recorded. The
kernel killed that container for exceeding a limit; it did not choose to exit.

**This is a separate stage on purpose.** The checker in grounding.py is
calibrated against observed model output and its behaviour is load-bearing for
every recorded eval set. Teaching it a second question would mean re-deriving
that calibration. So this runs alongside it, over the same scoped evidence,
and contributes its own claim status.

**Four statuses, and the third is the one that did not exist before:**

    observed      the value is in the evidence            (SUPPORTED)
    contradicted  the evidence says something else        (CONTRADICTED)
    unverified    nothing in the evidence settles it      (INSUFFICIENT)
    inferred      the clause marked itself as reasoning   (INFERENCE)

**Rules are structured, not linguistic.** Each one reads a typed fact out of
the tool JSON -- a termination reason, a readiness fraction, a replica count --
and fires only when an *unhedged* clause about that same entity asserts
something the fact excludes. No rule tries to decide whether two sentences
mean the same thing; that is not something a regex can be, and the failure
mode of trying is flagging correct answers for wording, which is the fastest
way to make a signal worth ignoring.

**What this deliberately does NOT do.** It does not flag ambiguous causal
reasoning. "The crash is probably a bad config" over OOMKilled evidence is
hedged reasoning and stays an inference. It does not flag an answer for
omitting something. And it does not fire when the evidence is silent -- an
absent fact produces no finding, because "the tools did not say" is what
`unverified` already means.
"""

import json
import re

import grounding

# Reasons a container terminated that the kernel or the platform imposed,
# rather than the process choosing its own exit. An answer attributing one of
# these to the application is not describing what happened.
_IMPOSED_TERMINATIONS = {"oomkilled", "evicted", "deadlineexceeded"}

# Phrases that put the blame inside the process. Deliberately narrow: each one
# asserts an application-level cause outright. "error" alone is absent -- it
# appears in "error code", in "OOMKilled error", and in half of all correct
# prose about a failure.
_APPLICATION_CAUSE = (
    "application error",
    "application-level error",
    "application level error",
    "application-level issue",
    "application level issue",
    "application issue",
    "application bug",
    "bug in the application",
    "unhandled exception",
    "error in the code",
    "code error",
    "application crash",
    "application failure",
)

# Phrases asserting the container ran out of memory.
#
# The kill family is entered as the STEM, because the tuple used to carry
# "oom killed" and "oom-killed" and nothing else in that shape -- so an answer
# blaming "the OOM killer" said the same thing in the commonest English
# spelling of it and was not recognised. Measured on
# scoping_quiet_workload_beside_loud_one at n=5, 2026-08-28: all five answers
# named OOM as the cause, two said "oomkilled" and were caught, and three said
# "OOM killer" / "OOM kills" and came back `grounded` -- so the case scored
# 3/5 while every one of its answers was wrong. A pattern that misses a
# spelling does not report a smaller number; it reports the wrong one.
#
# "oom kill" subsumes killed, killer and kills; "oom-kill" does the same for
# the hyphenated forms. Both are still gated by _asserted, so "to avoid the
# OOM killer" and "this was not an OOM kill" stay out.
_MEMORY_CAUSE = (
    "out of memory",
    "out-of-memory",
    "oomkilled",
    "oom kill",
    "oom-kill",
    "oom termination",
    "memory limit exceeded",
    "exceeded its memory limit",
    "ran out of memory",
    "exhausted its memory",
)

# Phrases asserting a thing is not there. The evidence has to positively show
# it IS there for these to be a contradiction.
_ABSENCE = (
    "does not exist",
    "doesn't exist",
    "no such pod",
    "no such deployment",
    "no such service",
    "could not be found",
    "could not find",
    "was not found",
    "is not present",
    "does not have any associated pods",
    "has no associated pods",
    "no pods are associated",
    "no matching pods",
    "there are no pods",
    # Endpoint phrasings. Measured on 2026-08-25: qwen3 wrote "has no endpoints
    # because its selector ... matches no pods" and gpt-4o-mini wrote "has no
    # ready endpoints because the selector ... matches no pods at all". Neither
    # matched anything above, so the endpoint rule below -- which has existed
    # since the contradiction stage shipped -- could not fire in either
    # direction for the two most common ways of saying it.
    # A verb is required. The bare phrase "no endpoints" matched a heading
    # ("- **No Endpoints**:") and the antecedent of a conditional ("If no
    # endpoints, investigate the database deployment") -- neither of which
    # asserts anything, and both of which were scored as contradictions of a
    # correct answer. Two false positives in 907 replayed runs, against one
    # true finding, which is the wrong ratio for a rule that calls an answer
    # wrong.
    "has no endpoints",
    "have no endpoints",
    "has no ready endpoints",
    "no ready endpoints to",
    "matches no pods",
    "match no pods",
)

# Phrases asserting a workload is not ready or not running.
_NOT_READY = (
    "is not ready",
    "are not ready",
    "is unready",
    "not in a ready state",
    "never becomes ready",
    "never became ready",
    "failing its readiness probe",
    "readiness probe is failing",
)
_NOT_RUNNING = (
    "is not running",
    "are not running",
    "is down",
    "has crashed",
    "is crashing",
    "is failing",
    "is in a crashloop",
    "keeps restarting",
)


# A phrase that is being denied is not a phrase being asserted. Found by
# replaying the corpus: "This is not a resource exhaustion issue (no OOMKilled
# or memory limits reported)" contains "oomkilled" and asserts its absence.
# Matching on presence rather than on assertion made a correct sentence a
# contradiction, which is the exact failure this module's docstring warns
# against.
_NEGATORS = re.compile(
    r"\b(no|not|never|without|isn't|aren't|wasn't|weren't|none|nothing|"
    r"neither|nor|lacks|lacking|absent)\b|n't\b")

# Advice about a thing that has NOT happened is not a claim that it has.
# Found live on 2026-08-24 in the first 432 real runs this stage ever saw:
# "ensure the pod has sufficient resources (CPU/memory) to avoid OOMKilled"
# is a recommendation, and it was read as an assertion that the container was
# OOM-killed. The corpus replay could not have caught this -- no recorded
# answer had phrased it that way.
_PROSPECTIVE = re.compile(
    r"\b(avoid|avoiding|prevent|preventing|risk of|guard against|"
    r"protect against|in case of|so it does not|to stop|reduce the chance)\b")

# How far back to look. A negator further away than this is usually governing
# a different part of the sentence.
_NEGATION_WINDOW = 40


def _asserted(lowered, phrase):
    """
    Whether `phrase` is claimed rather than denied.

    Looks only at the text immediately before it, so a phrase that carries its
    own "not" -- "does not exist", "is not ready" -- is not mistaken for a
    denial of itself.
    """
    start = lowered.find(phrase)
    if start < 0:
        return False
    window = lowered[max(0, start - _NEGATION_WINDOW):start]
    if _NEGATORS.search(window) or _PROSPECTIVE.search(window):
        return False
    return True


# An identifier the answer set apart from the surrounding prose: quoted,
# backticked, or in markdown emphasis. Two characters minimum, so an
# apostrophe in ordinary English -- "the kubelet's" -- cannot pair with a
# later one and read as a name.
#
# The delimiter has to close with itself. Any-of-three let the entity's own
# closing backtick pair with the next apostrophe in the sentence, so "the pod
# `X` isn't running and does not exist" read as though something else were
# being called absent, and the rule went quiet on a claim it should have
# caught. Found by testing this function directly rather than in a run.
_MARKED_UP = re.compile(
    r"""(?P<quote>[`"'])[^`"']{2,}(?P=quote)|\*\*[^*]{2,}\*\*|\*[^*]{2,}\*"""
)


def _absence_is_about(clause, phrase, name):
    """
    Whether `name` is the thing the absence phrase is talking about.

    Found live on 2026-08-24. This clause is the correct answer to the
    stuck-volume case:

        The pod `missing-configmap-volume` is stuck in ContainerCreating
        because the ConfigMap `nginx-conf` referenced in its volume
        configuration does not exist in the `config-faults` namespace.

    The pod is the only *labelled* entity -- `ConfigMap` is not one of the
    kinds the pattern knows -- so the rule paired "does not exist" with the
    pod, which very much does exist, and called a correct answer a
    contradiction.

    An absence claim attaches to the nearest named thing before it. If some
    other marked-up identifier sits between the entity and the phrase, that
    identifier is the subject and this entity is not.

    Markdown emphasis counts as marking it up, and leaving it out was a hole
    rather than an omission. The clause above is the one this guard was
    written for, and the model writes it `nginx-conf` on some runs and
    **nginx-conf** on others: replayed over the 1469 recorded runs carrying a
    draft, the backticked spelling was let through and the bold one fired --
    4 false contradictions on `stuck_volume_needs_events`, each of them
    against the correct answer to that case.

    Undelimited names are deliberately NOT covered. "the ConfigMap nginx-conf
    ... does not exist" is the same false positive, and the only test
    available there is the identifier SHAPE -- which would also swallow "the
    pod X in the config-faults namespace does not exist", where the absence
    really is about X. That trade buys a false negative on a true
    contradiction, which is the failure this rule exists to catch.
    """
    low = clause.lower()
    at = low.find(phrase)
    if at < 0:
        return False
    start = low.rfind(name, 0, at)
    if start < 0:
        return False
    between = clause[start + len(name):at]
    return not _MARKED_UP.search(between)


def _walk(node, seen):
    """Every (key, value) pair in a nested document, depth first."""
    if isinstance(node, dict):
        for key, value in node.items():
            seen.append((str(key).lower(), value))
            _walk(value, seen)
    elif isinstance(node, list):
        for item in node:
            _walk(item, seen)


def _ready_fraction(value):
    """
    True/False/None for a readiness value in any shape a tool reports it.

    `list_pods` says "0/1", `describe_pod` says a boolean per container, and a
    Deployment reports ready and desired as separate integers. All three mean
    the same thing and none of them is spelled the same way.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        match = re.fullmatch(r"\s*(\d+)\s*/\s*(\d+)\s*", value)
        if match:
            ready, total = int(match.group(1)), int(match.group(2))
            return total > 0 and ready == total
    return None


def facts(entries):
    """
    The typed facts the scoped evidence establishes.

    Only facts a rule below consumes. A fact that is absent is absent -- there
    is no default, because a default would let a rule fire on evidence that
    never spoke.
    """
    found = {}
    for entry in entries:
        try:
            data = json.loads(entry["text"])
        except (TypeError, ValueError):
            continue
        if not isinstance(data, (dict, list)):
            continue

        pairs = []
        _walk(data, pairs)

        for key, value in pairs:
            if key == "last_termination" and isinstance(value, dict):
                reason = str(value.get("reason", "")).lower()
                if reason:
                    found.setdefault("termination_reason", reason)
                if isinstance(value.get("exit_code"), int):
                    found.setdefault("exit_code", value["exit_code"])
            elif key == "ready":
                state = _ready_fraction(value)
                if state is not None:
                    # False wins: one unready container makes the pod unready,
                    # and a pod document may carry several.
                    found["ready"] = found.get("ready", True) and state
            elif key == "status" and isinstance(value, str):
                found.setdefault("status", value.lower())
            elif key == "limits" and isinstance(value, dict):
                for unit, amount in value.items():
                    found.setdefault(f"limit_{str(unit).lower()}", str(amount))
            elif key in ("restarts", "restart_count") and isinstance(value, int):
                found.setdefault("restarts", value)
            elif key in ("ready_endpoints", "not_ready_endpoints") and isinstance(
                    value, list):
                found["endpoints_total"] = found.get(
                    "endpoints_total", 0) + len(value)
                # Separately, because "no ready endpoints" and "no endpoints"
                # are different claims against different measurements. A
                # Service whose only endpoint is not ready satisfies the first
                # and contradicts the second, and collapsing them scored nine
                # correct answers as contradictions -- every one of them saying
                # "crasher-svc has no ready endpoints", which is exactly what
                # ready_endpoints: [] means.
                if key == "ready_endpoints":
                    found["ready_endpoints_total"] = found.get(
                        "ready_endpoints_total", 0) + len(value)
    return found


def _entity_present(entries, name):
    """
    Whether the tools positively reported this entity as existing.

    Not merely "the name appears": an event reading `configmap "nginx-conf"
    not found` contains the name and is the tools reporting the opposite.
    Presence means the name keys a document or is the subject of one.
    """
    lowered = name.lower()
    for entry in entries:
        try:
            data = json.loads(entry["text"])
        except (TypeError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        if "error" in data:
            continue
        text = entry["text"].lower()
        if f'"{lowered}"' not in text and lowered not in text:
            continue
        # The evidence itself saying it is missing is not presence.
        if re.search(rf"{re.escape(lowered)}\W{{0,4}}(not found|does not exist)",
                     text):
            continue
        if str(data.get("pod", "")).lower() == lowered:
            return True
        for key, value in data.items():
            if str(key).lower() == lowered and isinstance(value, dict):
                return True
            if isinstance(value, dict) and str(
                    value.get("example", "")).lower() == lowered:
                return True
    return False


# Why the measured value settles the question, per rule. Each is a fact about
# what Kubernetes writes into that field, not a statement about any particular
# answer, and none of them names a cause.
#
# This exists because of what a re-ask WITHOUT it produced. Told only that
# "oomkilled" conflicted with `last_termination.reason = error`, qwen3 kept
# the claim and talked its way past the measurement: "the reason field shows
# Error, which is a generic placeholder in Kubernetes and does not specify the
# exact cause ... and does not contradict the exit code 137". That is a false
# statement about Kubernetes, invented to protect a conclusion. Measured at
# n=5 on scoping_quiet_workload_beside_loud_one, 2026-08-28: the re-ask fired
# on 4 of 5 runs and every one came back contradicted anyway.
#
# A conflict a model can dismiss as a technicality is one it will dismiss. So
# the re-ask says what the field would have read if the claim were true.
WHY_IT_SETTLES = {
    "termination_reason_vs_memory_cause": (
        "the kubelet writes reason OOMKilled when the kernel's OOM killer "
        "terminates a container, so a reason of Error means the kill came "
        "from somewhere else. Exit code 137 is SIGKILL and says only that the "
        "container was killed, never by whom"
    ),
    "imposed_termination_vs_application_cause": (
        "that reason is recorded when something outside the container ended "
        "it, so the process did not choose its own exit"
    ),
    "ready_vs_claimed_not_ready": (
        "the container's ready field is the kubelet's own record of the "
        "readiness probe result"
    ),
    "claimed_absent_but_measured_present": (
        "the object came back in a tool result, so it exists"
    ),
    "service_has_endpoints_vs_claimed_none": (
        "get_service_endpoints lists the pods behind the Service, so a "
        "non-empty list means the selector matches something"
    ),
}


def _finding(rule, asserted, measured, clause, entries, field=None):
    return {
        "rule": rule,
        "claim": asserted,
        "measured": measured,
        # Absent for a rule with no honest one-line mechanism; callers must
        # treat it as optional rather than assume every rule explains itself.
        "because": WHY_IT_SETTLES.get(rule),
        "clause": clause.strip()[:220],
        "evidence": [
            {"id": (e["source"] or {}).get("id"),
             "tool": (e["source"] or {}).get("tool"),
             "field": field}
            for e in entries[:1]
        ],
    }


def check(answer, tool_outputs):
    """Contradictions only. See scan() for both halves."""
    return scan(answer, tool_outputs)[0]


def confirmations(answer, tool_outputs):
    """
    Absence claims the evidence positively CONFIRMS.

    The mirror of the endpoint contradiction rule, and it exists because the
    two halves of one measurement were being treated differently. Asked about
    `typo-svc`, both qwen3 and gpt-4o-mini answered correctly -- the selector
    matches no pods -- and both were scored `insufficient_evidence`, meaning
    "nothing here could be checked". But `get_service_endpoints` had returned
    `ready_endpoints: []` and `not_ready_endpoints: []`, which is exactly the
    measurement that settles the claim. The evidence was there; the contract
    had no way to say "supported" about an absence, only "contradicted".

    A confirmation requires the tool to have been CALLED: `endpoints_total` is
    set to 0 when both lists are read and empty, and absent when no service
    document was read at all. Confirming an absence from silence would be the
    unfalsifiable tick this checker exists to prevent.
    """
    return scan(answer, tool_outputs)[1]


def scan(answer, tool_outputs):
    """
    (contradictions, confirmations) for an answer against its evidence.

    One pass, two outputs, deliberately. Scoping is grounding's own, and a
    second implementation of it here is how two stages come to disagree about
    which entity a clause is about.

    Returns a list of findings, empty when nothing disagrees. Scoping is
    grounding's own, so the two stages cannot disagree about which entity a
    clause is talking about -- a contradiction found against the wrong pod's
    evidence would be the wrong-entity failure this project has spent months
    removing, reintroduced by its own safety net.
    """
    text = (answer or "").strip()
    if not text or not tool_outputs:
        return [], []

    evidence = (
        tool_outputs
        if isinstance(tool_outputs[0], dict) and "result" in tool_outputs[0]
        else grounding.records(tool_outputs)
    )
    everything = [{"text": r["result"], "source": r} for r in evidence]
    index = grounding._entity_index(evidence)

    findings = []
    supported = []
    for clause in grounding._claims(answer):
        lowered = clause.lower()

        # Reasoning is not a contradiction, however wrong it turns out to be.
        # The system prompt asks the model to mark inference; punishing a
        # clause for carrying that mark would undo the labelling this project
        # grades for.
        if any(word in lowered for word in grounding.HEDGES):
            continue

        entries = grounding._scope(clause, index, everything)
        if not entries:
            continue
        known = facts(entries)

        # --- termination reason vs claimed cause -------------------------
        reason = known.get("termination_reason")
        if reason in _IMPOSED_TERMINATIONS:
            hit = next((p for p in _APPLICATION_CAUSE
                            if p in lowered and _asserted(lowered, p)), None)
            if hit:
                findings.append(_finding(
                    "imposed_termination_vs_application_cause", hit,
                    f"last_termination.reason = {reason}", clause, entries,
                    "last_termination.reason"))
        elif reason and reason not in _IMPOSED_TERMINATIONS:
            # The mirror: the container chose its exit and the answer blames
            # the kernel. Only when no OOM appears anywhere in scope, so a pod
            # whose evidence genuinely mentions both is left alone.
            scope_text = " ".join(e["text"] for e in entries).lower()
            if "oomkilled" not in scope_text:
                hit = next((p for p in _MEMORY_CAUSE
                                if p in lowered and _asserted(lowered, p)), None)
                if hit:
                    findings.append(_finding(
                        "termination_reason_vs_memory_cause", hit,
                        f"last_termination.reason = {reason}", clause, entries,
                        "last_termination.reason"))

        # --- readiness ----------------------------------------------------
        if known.get("ready") is True:
            hit = next((p for p in _NOT_READY
                        if p in lowered and _asserted(lowered, p)), None)
            if hit:
                findings.append(_finding(
                    "ready_vs_claimed_not_ready", hit,
                    "ready reported true by the tools", clause, entries,
                    "ready"))
            hit = next((p for p in _NOT_RUNNING
                        if p in lowered and _asserted(lowered, p)), None)
            if hit and known.get("status", "").startswith("running"):
                findings.append(_finding(
                    "running_vs_claimed_failing", hit,
                    f"status = {known['status']}, ready = true",
                    clause, entries, "status"))

        # --- existence ----------------------------------------------------
        hit = next((p for p in _ABSENCE
                    if p in lowered and _asserted(lowered, p)), None)

        # A Service the tools reported endpoints for is a Service with pods
        # behind it, whatever the answer says. This is the structured form of
        # the existence check and it catches what the name-based one below
        # cannot: the recorded failure wrote "`crasher-svc` service ... does
        # not have any associated pods" -- name before kind, which the
        # labelled-entity pattern reads as "namespace does". The endpoint
        # count needs no name at all.
        #
        # Recorded in results/think-OFF-16cases-n3.json: get_service_endpoints
        # returned ready_endpoints ["10.244.0.12"] and the answer was scored
        # insufficient_evidence, which reads as "nothing to check" rather than
        # "this is false".
        # Which measurement settles this claim is decided by the claim. A
        # phrase naming readiness is about ready_endpoints; one naming
        # endpoints or pods in general is about all of them.
        ready_scoped = hit is not None and "ready" in hit
        # Named for the operator, who is told to go and read it: `ui._cite`
        # prints this beside the finding as `tool.field`. So it names fields
        # of the tool result, never `endpoints_total`, which is a counter
        # this module derives and nobody can look up.
        field = ("ready_endpoints" if ready_scoped
                 else "ready_endpoints, not_ready_endpoints")
        count = known.get("ready_endpoints_total" if ready_scoped
                          else "endpoints_total")

        if hit and count is not None and count > 0:
            findings.append(_finding(
                "service_has_endpoints_vs_claimed_none", hit,
                f"get_service_endpoints reported {count} "
                f"{'ready ' if ready_scoped else ''}endpoint(s)",
                clause, entries, field))
        elif hit and count == 0:
            # The same predicate, the same fact, the other direction. `== 0`
            # rather than `not known.get(...)`: the key is absent when no
            # service document was read, and confirming an absence from a tool
            # that was never called is exactly the unfalsifiable tick this
            # module exists to prevent.
            supported.append(_finding(
                "service_endpoints_confirmed_empty", hit,
                f"get_service_endpoints reported 0 "
                f"{'ready ' if ready_scoped else ''}endpoint(s)",
                clause, entries, field))

        if hit:
            for kind, name in grounding._LABELLED_ENTITY.findall(clause):
                name = name.strip(".,;:").lower()
                if not name or name in grounding._ENTITY_KINDS:
                    continue
                if not any(ch.isdigit() or ch == "-" for ch in name):
                    continue
                if not _absence_is_about(clause, hit, name):
                    continue
                if _entity_present(entries, name):
                    findings.append(_finding(
                        "claimed_absent_but_measured_present", hit,
                        f"{kind.lower()} {name} appears in the tool results",
                        clause, entries, name))
                    break

        # --- numeric resource values --------------------------------------
        # Only a value explicitly presented as a limit or a request. The first
        # version matched any number adjacent to "cpu" or "memory", and the
        # corpus replay showed what that costs: the `stress` fixture logs
        # "dispatching hogs: 0 cpu, 0 io, 1 vm, 0 hdd", and six correct
        # answers quoting that line were scored as claiming a CPU limit of 0.
        # Six false positives, zero true ones.
        for unit in ("memory", "cpu"):
            measured = known.get(f"limit_{unit}")
            if not measured:
                continue
            stated = re.search(
                rf"{unit}\s+(?:limit|request)\s+(?:of\s+|is\s+|at\s+)?"
                rf"([\d.]+\s*(?:Mi|Gi|Ki|m|M|G)?)"
                rf"|([\d.]+\s*(?:Mi|Gi|Ki|m|M|G)?)\s+{unit}\s+(?:limit|request)",
                clause, re.IGNORECASE)
            if not stated:
                continue
            value = (stated.group(1) or stated.group(2) or "").strip()
            if value and value.replace(" ", "").lower() != measured.replace(
                    " ", "").lower():
                findings.append(_finding(
                    "resource_limit_disagrees", value,
                    f"limits.{unit} = {measured}", clause, entries,
                    f"limits.{unit}"))

    # One finding per rule per claim: repeating a contradiction should not
    # repeat the alarm, exactly as grounding's flag() does for unverified.
    unique, seen = [], set()
    for finding in findings:
        key = (finding["rule"], finding["claim"], finding["measured"])
        if key not in seen:
            seen.add(key)
            unique.append(finding)

    seen_ok, unique_ok = set(), []
    for finding in supported:
        key = (finding["rule"], finding["claim"], finding["measured"])
        if key not in seen_ok:
            seen_ok.add(key)
            unique_ok.append(finding)

    # A clause cannot be both. If anything in this answer is contradicted the
    # confirmations are dropped, because the verdict is CONTRADICTED either way
    # and a confirmed absence sitting beside a contradiction reads as partial
    # support for an answer that is wrong.
    return unique, ([] if unique else unique_ok)
