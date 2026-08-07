"""
Eval cases against the demo cluster.

Each case pins a fault that demo/broken-pods.yaml creates deliberately, so
the right answer is known in advance. Cases assert on substance -- the root
cause, not the wording -- because the model phrases things differently every
run.

    expect_any    at least one of these must appear (synonyms for one fact)
    expect_all    every group must be satisfied; a group is a list of synonyms
    forbid        must not appear; catches confident wrong answers
    expect_tools  tools the answer should have been built from
"""

CASES = [
    {
        "name": "oomkill_root_cause",
        "question": "Why does the memory-hog pod in the demo namespace keep restarting?",
        "expect_all": [
            ["oomkilled", "out of memory", "memory limit"],
            ["64mi", "64 mi", "64mb"],
        ],
        "expect_tools": ["describe_pod"],
    },
    {
        "name": "crashloop_root_cause",
        "question": "Why is the crasher pod in the demo namespace failing?",
        # The real cause is in the container logs, not the pod status. An
        # answer that stops at "CrashLoopBackOff" has not done the work.
        "expect_all": [["db:5432", "database", "connection refused"]],
        "expect_tools": ["get_pod_logs"],
    },
    {
        "name": "image_pull_failure",
        "question": "The bad-image pod in the demo namespace will not start. Why?",
        "expect_all": [
            ["image", "pull"],
            ["this-tag-does-not-exist", "does not exist", "doesn't exist", "not found"],
        ],
    },
    {
        "name": "service_unreachable_chain",
        "question": "The crasher-svc service in the demo namespace is unreachable. Why?",
        # Requires chaining service -> pods -> logs.
        "expect_all": [["endpoint", "no ready", "not ready", "crash"]],
        "expect_tools": ["get_service_endpoints"],
    },
    {
        "name": "service_selector_typo",
        "question": "Why does typo-svc in the demo namespace have no endpoints?",
        "expect_any": ["selector", "label", "matches no pods", "no pods"],
        "expect_tools": ["get_service_endpoints"],
    },
    {
        "name": "healthy_not_reported_broken",
        "question": "Is the healthy-web deployment in the demo namespace working correctly?",
        # A tool that calls everything broken is useless. This is the control.
        "expect_any": ["yes", "healthy", "working", "running", "2/2", "no issue"],
        "forbid": ["oomkilled", "crashloopbackoff", "imagepullbackoff"],
    },
    {
        "name": "cluster_wide_scan",
        "question": "Is anything broken anywhere in the cluster?",
        # Names no namespace, which is the whole trigger for scan_cluster. The
        # other cases all name one, so without this the tool is never exercised
        # by the suite at all -- a 21/21 score said nothing about it.
        "expect_tools": ["scan_cluster"],
        # Must find faults in more than the one workload it happens to look at
        # first. Deployment names rather than pod names: the pod suffix is a
        # fresh hash on every apply.
        "expect_all": [
            ["memory-hog"],
            ["crasher"],
            ["bad-image"],
        ],
    },
    {
        "name": "healthy_workload_not_substituted",
        "question": "What is the issue with the healthy-web deployment in demo?",
        # The trap: nothing is wrong with it, and the cluster is full of things
        # that are. Observed failure -- asked about a healthy workload, the
        # agent confidently described a different, broken one instead, which
        # reads as an answer and is worse than saying nothing.
        "expect_any": [
            "no issue",
            "healthy",
            "running",
            "working",
            "fine",
            "no problem",
            "not failing",
        ],
        "forbid": ["memory-hog", "crasher", "bad-image", "oomkilled", "imagepullbackoff"],
    },
    {
        "name": "host_not_cluster",
        "question": "How much memory is this host using?",
        # Must answer from host tools; reaching for pod tools means the two
        # surfaces have blurred.
        "expect_any": ["%", "percent"],
        "forbid_tools": ["list_pods", "describe_pod", "get_pod_logs"],
    },
]
