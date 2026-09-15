"""
Outcome criterion for the describe_pod scheduling change. Written 2026-09-15
BEFORE either arm ran: the before arm on 938f06a (describe_pod reports nothing
about scheduling), the after arm on the change.

The case graders are recorded and not used as the outcome, for two reasons:
- unschedulable_node_affinity's second expect_all group accepts "node", which
  almost any answer contains, and its first accepts "pending", which is in the
  question -- so it passes nearly any answer that reached a grounding verdict.
- both cases demand get_pod_events. After the change describe_pod carries the
  scheduler's own message, so a correct answer built from describe_pod alone
  fails that expectation -- the defect 51 shape. Measuring correctness with it
  would score the fix as a regression.

The workload name `gpu-scoring` is masked before matching, because it hands
the model "GPU" whatever the tools say (defect 50's lesson, on a pod name).

unschedulable_node_affinity   (cause: nodeSelector accelerator=nvidia-a100 matches no node)
    primary     names_selector -- names a node selector or node affinity mismatch.
                Bare "node" and "label" do not count.
    secondary   names_value    -- "nvidia-a100" or "accelerator": the required label
                itself, which no tool reported before the change
                gpu_cause      -- attributes it to a missing GPU resource or hardware;
                read by hand before counting, since "no node has the nvidia-a100
                accelerator label" is correct and also mentions a GPU

unschedulable_unbound_pvc     (cause: PVC archive-data names StorageClass fast-ssd-nonexistent)
    primary     names_pvc      -- names the claim as unbound or pending. Bare
                "volume" does not count: every volumeMount contains it.
    secondary   names_class    -- the StorageClass, by name or as the missing object
"""

import re

# Revision 1, committed before either arm ran, after checking the criterion
# against the three recorded 2026-09-12 runs: two of them scored primary True
# while their cause was "the node lacks a GPU", because they mention selectors
# only to DENY one exists -- "The pod does not have explicit node selectors or
# taints preventing scheduling". That sentence is false, and it is the gap this
# change is about: describe_pod never reported nodeSelector, so its absence from
# the projection was read as its absence from the pod. primary now requires a
# selector term AND no denial that the pod has a selector.
DENIES_SELECTOR = re.compile(
    r"(?:does not|doesn't|do not|don't|not)\s+(?:have|include|specify|define|set)"
    r"\s+(?:any\s+|explicit\s+|a\s+)?(?:node[ -]?selectors?|node affinity|affinity)"
    r"|\bno\s+(?:explicit\s+)?(?:node[ -]?selectors?|node affinity)"
    r"|\bno\s+taints?\s+or\s+node[ -]?selectors?")

SELECTOR = ["nodeselector", "node selector", "node-selector", "node affinity",
            "node-affinity", "nodeaffinity", "affinity", "selector", "nvidia-a100",
            "accelerator"]
VALUE = ["nvidia-a100", "accelerator"]
GPU_CAUSE = ["nvidia.com/gpu", "gpu request", "gpu resource", "gpu hardware", "no gpu",
             "lacks gpu", "lack of gpu", "gpu capacity", "gpu-enabled", "gpu support"]
PVC = ["persistentvolumeclaim", "pvc", "volume claim", "unbound"]

# Revision 2, committed after both arms above and BEFORE a third, PVC-only arm
# runs on the claim-naming follow-up. Read from the after arm by hand: every run
# named "unbound PVC" (primary 5/5) and none named the claim or its StorageClass
# -- neither did the before arm, since no pod-level tool carried them -- but the
# after arm stopped at describe_pod and guessed the claim's name, twice as the
# kube-root-ca.crt ConfigMap. primary cannot see that, so these are added and are
# the outcome for the third arm:
#   names_claim       the claim's real name, archive-data
#   wrong_claim_name  a claim name that is not it, or "likely named" guessing
#   names_class_value the missing StorageClass by name, fast-ssd-nonexistent
#   says_pvc_missing  the claim itself called missing -- it exists; its class does not
#   scan_references   whether the one tool carrying the StorageClass was called
WRONG_CLAIM = re.compile(r"likely named|kube-root-ca|archive-pvc|volumeclaimtemplates")
PVC_MISSING = re.compile(
    r"(?:pvc|persistentvolumeclaim|claim)[^.]{0,40}"
    r"(?:does not exist|doesn't exist|not been created|not created|is missing)")
CLASS = ["fast-ssd-nonexistent", "storageclass", "storage class"]


def judge(record):
    text = (record.get("answer") or "").lower().replace("gpu-scoring", "<workload>")
    tools = record.get("tools") or []
    out = {"case_grader": record.get("passed"),
           "pod_read": "describe_pod" in tools or "get_pod_events" in tools,
           "events_read": "get_pod_events" in tools}
    if record["case"] == "unschedulable_node_affinity":
        out["denies_selector"] = bool(DENIES_SELECTOR.search(text))
        out["primary"] = any(t in text for t in SELECTOR) and not out["denies_selector"]
        out["names_value"] = any(t in text for t in VALUE)
        out["gpu_cause"] = any(t in text for t in GPU_CAUSE)
    elif record["case"] == "unschedulable_unbound_pvc":
        out["primary"] = any(t in text for t in PVC)
        out["names_class"] = any(t in text for t in CLASS)
        out["names_claim"] = "archive-data" in text
        out["wrong_claim_name"] = bool(WRONG_CLAIM.search(text))
        out["names_class_value"] = "fast-ssd-nonexistent" in text
        out["says_pvc_missing"] = bool(PVC_MISSING.search(text))
        out["scan_references"] = "scan_references" in tools
    return out
