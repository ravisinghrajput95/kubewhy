"""
Defect 50 correctness criterion. Written 2026-09-14 BEFORE any A/B run existed.

The case grader cannot be used as the outcome here: its expect_any term "never"
matches inside the fixture's image name, so any answer quoting the image passes it.
Every image string in the run's own evidence is masked before matching.

strict   -- the answer names the pull policy as the cause
lenient  -- strict, or names the kubelet reason ErrImageNeverPull
wrong    -- names a different member of the image-fault family, or the pull-secret near-miss
pod_read -- the run reached describe_pod or get_pod_events (the only tools carrying the message)
"""
import re

STRICT = ["pull policy", "pullpolicy", "imagepullpolicy", "policy of never",
          "policy is never", "policy is set to never", "policy: never", "set to never"]
LENIENT_EXTRA = ["errimageneverpull"]
WRONG = ["invalid", "malformed", "pull secret", "imagepullsecret", "credential",
         "typo", "does not exist in the registry", "unreachable"]
IMAGE = re.compile(r'"image":\s*"([^"]+)"|"images":\s*\[([^\]]*)\]')

def images(record):
    found = {"an-image-that-was-never-loaded:v3", "billing-api:3.2.1"}
    for item in record.get("evidence") or []:
        for a, b in IMAGE.findall(str(item.get("result", ""))):
            found.update(x.strip().strip('"') for x in (a or b).split(",") if x.strip())
    return found

def judge(record):
    text = (record.get("answer") or "").lower()
    for img in sorted(images(record), key=len, reverse=True):
        text = text.replace(img.lower(), "<image>")
    strict = any(t in text for t in STRICT)
    return {"strict": strict,
            "lenient": strict or any(t in text for t in LENIENT_EXTRA),
            "wrong": [t for t in WRONG if t in text],
            "pod_read": any(t in ("describe_pod", "get_pod_events") for t in record.get("tools") or [])}
