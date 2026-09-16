"""
Prefetch A/B criterion. Written 2026-09-16 BEFORE any arm of the A/B ran.

The variable is TRIAGE_PREFETCH_TARGET=on (commit 6f9700a): the variant is
handed scan_cluster(workload=) and describe_pod of its example pod before round
one. Two questions, and the second is the reason this file exists:

1. Latency. Read off each record's `timing` (the loop's own clocks, which start
   before the prefetch and charge it to tool_ms), never `seconds`.
2. Accuracy. Defect 53 measured the model stopping at a partial answer and
   guessing. So the outcome is not the case grader, for a reason specific to
   this variable: `expect_tools` counts prefetched calls, so the variant arm can
   never fail "never called describe_pod" and the grader is not comparable
   across arms. The criterion reads the answer.

`right`  -- the answer names the cause, with the specific evidence that makes it
            that cause rather than a neighbour of it.
`looked` -- the model itself called the tool that holds the cause, when that
            tool is not one of the two prefetched reads. None where the cause is
            in describe_pod, since the variant was handed it.

Per case, the cause and where it lives, verified by calling every tool by hand
on kind-kubewhy-prefetch before this was written:

  oomkill_root_cause            describe_pod: OOMKilled/137, limit 64Mi
  image_never_pulled_by_policy  describe_pod: "not present with pull policy of Never"
  healthy_not_reported_broken   scan + describe_pod: Running, ready
  crashloop_root_cause          get_pod_logs: "could not connect to db:5432"
  never_ready_readiness_probe   get_pod_events: "Readiness probe failed ... connection refused"
  poststart_hook_not_the_app    get_pod_events: FailedPostStartHook
  unschedulable_unbound_pvc     describe_pod: claim archive-data, class fast-ssd-nonexistent

Two of those hold a trap for the variant. never-ready's describe_pod carries the
probe config, `/healthz:8080`, so an answer can name the probe and port without
ever seeing that it fails -- `right` requires "connection refused", which is
only in the events. session-cache's describe_pod carries `Error/137`, which
invites an OOM story -- `wrong` is what the contradiction checker finds.

Revised before any arm ran, after scoring every recorded answer to these seven
cases and reading each disagreement with the grader:
- never-ready: "not listening on 8080" was accepted, and the recorded answer
  saying it also claimed the pod restarts. Now the measured phrase only.
- poststart: `wrong` was a word list, and it failed two answers that DENY an
  OOM kill ("the OOM killer is unlikely to be the cause") -- defect 52's error,
  repeated. It is now grounding.check() on the record's own draft and evidence,
  which after defect 52 separates the assertion from both denials.
- oomkill: "OOMKill the process" matched no term. Added `oomkill`.
- crashloop: "database" plus "connect" admitted a guess from a truncated log.
  Now the port, or "database" with "refused".
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import grounding  # noqa: E402

IMAGE = re.compile(r'"image":\s*"([^"]+)"|"images":\s*\[([^\]]*)\]')


def _model_called(record):
    """Tools the model asked for, excluding what it was handed.

    The trace puts prefetched entries first, and the record keeps their count.
    """
    return (record.get("tools") or [])[record.get("prefetched") or 0:]


def _masked(record):
    text = (record.get("answer") or "").lower()
    found = set()
    for item in record.get("evidence") or []:
        for a, b in IMAGE.findall(str(item.get("result", ""))):
            found.update(x.strip().strip('"') for x in (a or b).split(",") if x.strip())
    for image in sorted(found, key=len, reverse=True):
        text = text.replace(image.lower(), "<image>")
    return text


def _contradicted(record):
    """Rules today's checker fires on this run's draft against its evidence."""
    if not (record.get("draft") and record.get("evidence")):
        return []
    found = grounding.check(record["draft"], record["evidence"]).get("contradictions", [])
    return sorted({c.get("rule") for c in found})


def _any(text, terms):
    return any(t in text for t in terms)


def judge(record):
    case = record["case"]
    text = _masked(record)
    called = _model_called(record)
    out = {"right": False, "looked": None, "wrong": [],
           "contradicted": _contradicted(record)}

    if case == "oomkill_root_cause":
        out["right"] = (_any(text, ["oomkill", "out of memory", "oom kill", "oom-kill", "oom killer"])
                        and _any(text, ["64mi", "memory limit"]))
    elif case == "image_never_pulled_by_policy":
        out["right"] = _any(text, ["pull policy", "pullpolicy", "imagepullpolicy", "policy of never",
                                   "policy is never", "policy is set to never", "policy: never",
                                   "set to never"])
        out["wrong"] = [t for t in ["invalid", "malformed", "pull secret", "imagepullsecret",
                                    "credential", "does not exist in the registry", "unreachable"]
                        if t in text]
    elif case == "healthy_not_reported_broken":
        out["right"] = _any(text, ["healthy", "running normally", "no issue", "not broken",
                                   "functioning", "no problem"])
        out["wrong"] = [t for t in ["crashloop", "oomkilled", "is not healthy", "unhealthy",
                                    "not ready", "is failing"] if t in text]
        out["right"] = out["right"] and not out["wrong"]
    elif case == "crashloop_root_cause":
        out["right"] = "5432" in text or ("database" in text and "refused" in text)
        out["looked"] = "get_pod_logs" in called
    elif case == "never_ready_readiness_probe":
        # The failure itself, which only the events carry. "Not listening on
        # 8080" is readable off the probe config describe_pod hands over.
        out["right"] = "readiness" in text and "connection refused" in text
        out["looked"] = "get_pod_events" in called
    elif case == "poststart_hook_not_the_app":
        out["right"] = _any(text, ["poststart", "post-start", "post start", "lifecycle hook"])
        out["looked"] = "get_pod_events" in called
        out["wrong"] = out["contradicted"]
        out["right"] = out["right"] and not out["wrong"]
    elif case == "unschedulable_unbound_pvc":
        out["right"] = "archive-data" in text and "fast-ssd-nonexistent" in text
        claims = set(re.findall(r"\b[a-z0-9-]*(?:pvc|claim|data)[a-z0-9-]*\b", text))
        out["wrong"] = sorted(c for c in claims
                              if c not in {"archive-data", "persistentvolumeclaim",
                                           "persistentvolumeclaims", "claim", "claims", "data",
                                           "pvc", "pvcs", "volumeclaim", "volume_claims"})
    else:
        raise ValueError(f"no criterion for {case}")
    return out
