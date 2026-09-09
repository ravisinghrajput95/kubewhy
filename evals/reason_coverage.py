"""
Which Kubernetes failure reasons the eval corpus can even present.

The 29 cases in `cases.py` were written here, against fixtures also written
here, to check prompts also written here. That closes a loop: the suite can
only find failures somebody already imagined, and a number computed from it
says how well the agent does on the faults its author thought of.

This breaks one link of that loop by taking the *list of faults* from
Kubernetes rather than from this project. The reasons below are transcribed
from the kubelet's own source, so the denominator is not an opinion:

  pkg/kubelet/images/types.go   image pull failures, as waiting reasons
  pkg/kubelet/events/event.go   the event reasons the kubelet emits
  pkg/kubelet/kuberuntime/      container waiting and terminated reasons

**Two modes, and the difference between them is the whole point.**

`--named` (the default) greps the cases and the fixtures. It is cheap, needs
no cluster, and it **undercounts**: a fault the fixtures produce but never
write down reads as missing. `bad-image` runs `nginx:this-tag-does-not-exist`,
which the kubelet reports as `ErrImagePull` before it settles into
`ImagePullBackOff` -- produced on every run, named nowhere, and counted absent.
So the named figure is a lower bound on the corpus and an upper bound on
nothing.

`--cluster CONTEXT` reads the reasons a live cluster actually reports, after
the fixtures are applied. That is the measurement; the grep is the estimate.
The context is required rather than defaulted, because this machine has
carried a kind cluster belonging to another project and reading it would
produce a number about somebody else's workloads.

**Neither mode measures accuracy.** They measure which failure modes the
corpus can put in front of the agent at all. Whether the answers are right is
`evals/run_eval.py`'s question. Coverage is a prerequisite for an accuracy
claim, not a substitute: an agent cannot be shown to diagnose `InvalidImageName`
correctly by a suite that never produces one.

Run:  python evals/reason_coverage.py
      python evals/reason_coverage.py --cluster kind-kubewhy-corpus
"""

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Transcribed verbatim. "waiting" and "terminated" are what appear in a
# container's status block, which is what describe_pod returns and what the
# agent reads; "event" reasons appear only in the Event stream, which is why
# get_pod_events exists at all.
REASONS = {
    # -- container status: waiting -------------------------------------
    "ImagePullBackOff": "waiting",
    "ErrImagePull": "waiting",
    "ImageInspectError": "waiting",
    "ErrImageNeverPull": "waiting",
    "InvalidImageName": "waiting",
    "CrashLoopBackOff": "waiting",
    "CreateContainerConfigError": "waiting",
    "CreateContainerError": "waiting",
    "RunContainerError": "waiting",
    "ContainerCreating": "waiting",
    "PodInitializing": "waiting",
    "StartError": "waiting",
    # -- container status: terminated ----------------------------------
    "OOMKilled": "terminated",
    "Error": "terminated",
    "Completed": "terminated",
    "ContainerCannotRun": "terminated",
    "DeadlineExceeded": "terminated",
    "Evicted": "terminated",
    # -- kubelet events -------------------------------------------------
    "FailedMount": "event",
    "FailedAttachVolume": "event",
    "FailedMapVolume": "event",
    "VolumeResizeFailed": "event",
    "FileSystemResizeFailed": "event",
    "FailedCreatePodSandBox": "event",
    "NetworkNotReady": "event",
    "SandboxChanged": "event",
    "Unhealthy": "event",
    "ProbeWarning": "event",
    "FailedPostStartHook": "event",
    "FailedPreStopHook": "event",
    "FailedSync": "event",
    "FailedKillPod": "event",
    "ExceededGracePeriod": "event",
    "NodeNotReady": "event",
    "Preempting": "event",
    "BackOff": "event",
    "Killing": "event",
    "FreeDiskSpaceFailed": "event",
    "InvalidDiskCapacity": "event",
    "ImageGCFailed": "event",
    "ContainerGCFailed": "event",
    "FailedValidation": "event",
    # -- scheduler, not the kubelet, but the commonest question there is
    "FailedScheduling": "event",
}


def _corpus_text():
    """Everything the eval corpus says: the cases and the fixtures."""
    parts = []
    for name in ("evals/cases.py",):
        parts.append(open(os.path.join(ROOT, name), encoding="utf-8").read())
    demo = os.path.join(ROOT, "demo")
    for name in sorted(os.listdir(demo)):
        if name.endswith(".yaml"):
            parts.append(open(os.path.join(demo, name), encoding="utf-8").read())
    return "\n".join(parts)


def coverage():
    text = _corpus_text().lower()
    covered, missing = {}, {}
    for reason, kind in REASONS.items():
        # Word-ish match: the corpus writes these in prose and in expectations,
        # sometimes lowercased or spaced, so compare on the squashed form too.
        needle = reason.lower()
        spaced = re.sub(r"(?<!^)(?=[A-Z])", " ", reason).lower()
        hit = needle in text or spaced in text
        (covered if hit else missing)[reason] = kind
    return covered, missing


def from_cluster(context):
    """The reasons a live cluster is actually reporting, right now."""
    from kubernetes import client, config

    config.load_kube_config(context=context)
    api = client.CoreV1Api()
    seen = set()

    for pod in api.list_pod_for_all_namespaces().items:
        for status in (pod.status.container_statuses or []) + (
                pod.status.init_container_statuses or []):
            state = status.state
            for part in (state.waiting, state.terminated):
                if part is not None and part.reason:
                    seen.add(part.reason)
            last = status.last_state
            if last and last.terminated and last.terminated.reason:
                seen.add(last.terminated.reason)
        if pod.status.reason:
            seen.add(pod.status.reason)

    for event in api.list_event_for_all_namespaces().items:
        if event.reason:
            seen.add(event.reason)
    return seen


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if "--cluster" in argv:
        context = argv[argv.index("--cluster") + 1]
        print(f"reading cluster context: {context}\n")
        live = from_cluster(context)
        known = {r for r in REASONS if r in live}
        unknown = sorted(live - set(REASONS))
        print(f"reasons this cluster is reporting: {len(live)}")
        print(f"  of the {len(REASONS)} enumerated: {len(known)} "
              f"({len(known) / len(REASONS):.0%})")
        print(f"  present: {', '.join(sorted(known))}")
        if unknown:
            print(f"\n  reported but NOT in the enumeration ({len(unknown)}): "
                  f"{', '.join(unknown)}")
            print("  -- the list here is from one kubelet version; a reason "
                  "this cluster emits and it does not name belongs in it.")
        return 0

    covered, missing = coverage()
    total = len(REASONS)
    print(f"Kubernetes failure reasons: {total}")
    print(f"  NAMED in the corpus:      {len(covered)} ({len(covered) / total:.0%})")
    print(f"  not named:                {len(missing)}")
    print("  (named, not produced -- see the module docstring; ErrImagePull is")
    print("   produced by bad-image on every run and appears in neither list)")
    print()
    for kind in ("waiting", "terminated", "event"):
        gaps = sorted(r for r, k in missing.items() if k == kind)
        have = sorted(r for r, k in covered.items() if k == kind)
        print(f"{kind}: {len(have)} covered, {len(gaps)} not")
        if gaps:
            print(f"    missing: {', '.join(gaps)}")
    print()
    print("Coverage is not accuracy. A reason present here only means the")
    print("corpus can put that fault in front of the agent; run_eval.py is")
    print("what says whether the answer was right.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
