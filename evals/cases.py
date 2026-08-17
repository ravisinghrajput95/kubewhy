"""
Eval cases against the demo cluster.

Each case pins a fault that demo/broken-pods.yaml creates deliberately, so
the right answer is known in advance. Cases assert on substance -- the root
cause, not the wording -- because the model phrases things differently every
run.

    expect_any    at least one of these must appear (synonyms for one fact)
    expect_all    every group must be satisfied; a group is a list of synonyms
    forbid        catches a confident wrong answer: fails the run when the
                  expectations above went unmet, and is recorded as a note
                  when they were met, because a term appearing next to a
                  correct answer is an aside and not the wrong answer
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
        #
        # The forbidden names are conditional on the verdict, which is the
        # difference between the failure above and "healthy-web is running
        # normally; bad-image and memory-hog are unhealthy". All four failures
        # recorded with their answer text were the second shape. See grade().
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
        "name": "inference_is_marked",
        # The gap this closes: every other case checks whether the answer is
        # right, and none check whether a claim the tools could not support is
        # labelled as one. That is what the prompt's "never state an inference
        # as if you measured it" was added for, so without this the suite
        # cannot see the thing it was measuring -- a before/after over the
        # whole suite came back a coin flip for exactly that reason.
        #
        # The question is deliberately unanswerable from measurement. Whether
        # a pod will OOM again is a prediction; the tools show that it did,
        # never that it will. An answer stating the future flatly is the
        # failure this is looking for.
        "question": (
            "Will the memory-hog pod in the demo namespace be OOMKilled again "
            "in the next hour?"
        ),
        "expect_any": [
            "likely",
            "probably",
            "worth checking",
            "may ",
            "might",
            "appears",
            "suggests",
            "expect",
            "cannot predict",
            "can't predict",
            "cannot know",
            "no way to know",
            "unless",
            "if the",
        ],
        # It still has to rest on the measurement rather than answer from
        # nothing: the pod's actual history is what any honest prediction is
        # reasoning from.
        "expect_tools": ["describe_pod"],
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
