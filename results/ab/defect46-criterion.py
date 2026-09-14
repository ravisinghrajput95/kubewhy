"""
Defect 46 outcome criterion. Written 2026-09-14 BEFORE any defect 46 A/B run existed.

Variable: SYSTEM_PROMPT's "137 is SIGKILL: it says ..." (control) against
"An exit code above 128 is a signal: it says ..." (variant). Nothing else differs.

job_killed_by_its_own_deadline
    primary   the case's own grader. None of its expect_any or forbid terms occurs
              in either sentence, so it favours neither arm.
    secondary states_137 -- "137" anywhere in the answer. The Job's pods are
              deleted, so no tool result for this run can carry it.

scoping_quiet_workload_beside_loud_one
    The case's own grader is NOT used as the outcome: its expect_any holds "137"
    and "sigkill", which are the control sentence's own words, so the control arm
    can pass by echoing its prompt and the variant arm cannot.
    primary   names_probe -- "liveness" or "probe" in the answer. The real cause is a
              liveness probe against a port nothing listens on.
    secondary mentions_oom -- "oom" or "out of memory" anywhere; read by hand before
              counting, because "not OOMKilled" is the correct sentence.
              case_grader -- recorded for continuity, never compared across arms.
"""


def judge(record):
    text = (record.get("answer") or "").lower()
    out = {"case_grader": record.get("passed")}
    if record["case"] == "job_killed_by_its_own_deadline":
        out["primary"] = record.get("passed")
        out["states_137"] = "137" in text
    else:
        out["primary"] = "liveness" in text or "probe" in text
        out["mentions_oom"] = "oom" in text or "out of memory" in text
    return out
