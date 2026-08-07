"""
Watches a cluster and diagnoses failures without being asked.

The agent answers questions well, but to get value from it someone has to
already know something is wrong, be at a terminal, and know what to ask --
and anyone who satisfies all three would be faster typing `kubectl describe`.
This inverts that: the controller notices a pod going unhealthy, runs the
diagnosis itself, and delivers the root cause somewhere people already look.

Inference latency stops mattering here. Nobody is waiting on it.

    python controller.py                       # stdout, all namespaces
    TRIAGE_SINK=slack SLACK_WEBHOOK_URL=... python controller.py

The hard part is not the watching, it is not becoming noise. During a real
incident dozens of pods fail at once, and a tool that posts dozens of messages
gets muted and then uninstalled. Three mechanisms prevent that: findings are
grouped by owning workload rather than pod, each workload has a cooldown, and
there is a global hourly ceiling.
"""

import logging
import os
import queue
import signal
import threading
import time

from kubernetes import client, config, watch

import agent
import observability
import sinks
from routers.k8s_pods_info import FAULT_CLASS, _pod_status, workload_of

observability.configure()
log = logging.getLogger("triage.controller")

# Statuses worth waking someone for. Deliberately excludes transient states
# like ContainerCreating and PodInitializing, which resolve on their own.
WATCHED = {
    "CrashLoopBackOff",
    "OOMKilled",
    "ImagePullBackOff",
    "ErrImagePull",
    "CreateContainerConfigError",
    "Error",
    "Evicted",
}

NAMESPACES = [n for n in os.getenv("TRIAGE_NAMESPACES", "").split(",") if n]
COOLDOWN = int(os.getenv("TRIAGE_COOLDOWN", "1800"))
MAX_PER_HOUR = int(os.getenv("TRIAGE_MAX_PER_HOUR", "12"))
# A pod already broken when the controller starts is usually a known problem.
# Diagnosing every one of them on boot is the fastest way to get muted.
SKIP_EXISTING = os.getenv("TRIAGE_SKIP_EXISTING", "true").lower() == "true"


class Budget:
    """Cooldown per workload plus a global hourly ceiling."""

    def __init__(self, cooldown=COOLDOWN, max_per_hour=MAX_PER_HOUR):
        self.cooldown = cooldown
        self.max_per_hour = max_per_hour
        self._last_seen = {}
        self._recent = []
        self._lock = threading.Lock()

    def allow(self, key, now=None):
        # Not `now or time.monotonic()`: a caller passing now=0 means time
        # zero, not "unset", and the falsy check silently substituted the
        # real clock -- which made every subsequent comparison nonsense.
        now = time.monotonic() if now is None else now
        with self._lock:
            self._recent = [t for t in self._recent if now - t < 3600]

            if len(self._recent) >= self.max_per_hour:
                log.warning("rate_limited", extra={"key": key})
                return False

            last = self._last_seen.get(key)
            if last is not None and now - last < self.cooldown:
                return False

            self._last_seen[key] = now
            self._recent.append(now)
            return True


class Controller:
    def __init__(self, sink=None, budget=None, model=None):
        self.sink = sink or sinks.build()
        self.budget = budget or Budget()
        self.model = model or agent.MODEL
        # Bounded: if diagnosis falls behind a failure storm, drop new work
        # rather than growing a queue that delivers stale findings later.
        self.work = queue.Queue(maxsize=32)
        self.stopping = threading.Event()
        # Bounded by the number of pods present at startup, and never added
        # to after that window closes.
        self.preexisting_uids = set()

    # -- detection ----------------------------------------------------------

    def interesting(self, pod):
        """Whether this pod state is worth a diagnosis."""
        if pod.metadata.deletion_timestamp:
            return None

        status = _pod_status(pod)
        if status not in WATCHED:
            return None

        # A pod that restarted once an hour ago and is running now is not a
        # problem. Require it to be currently not-ready.
        statuses = pod.status.container_statuses or []
        if statuses and all(cs.ready for cs in statuses):
            return None

        return status

    def enqueue(self, pod, status):
        scope = workload_of(pod) or pod.metadata.name
        fault = FAULT_CLASS.get(status, status)
        key = f"{pod.metadata.namespace}/{scope}/{fault}"
        if not self.budget.allow(key):
            return False

        try:
            self.work.put_nowait((pod, status))
            return True
        except queue.Full:
            log.warning("queue_full_dropping", extra={"key": key})
            return False

    # -- diagnosis ----------------------------------------------------------

    def diagnose(self, pod, status):
        namespace = pod.metadata.namespace
        name = pod.metadata.name
        workload = workload_of(pod)

        question = (
            f"Pod {name} in namespace {namespace} is {status}. "
            "Find the root cause and say what should change."
        )

        started = time.monotonic()
        try:
            result = agent.ask(question, model=self.model)
        except Exception as exc:
            log.error(
                "diagnosis_failed",
                extra={"pod": name, "namespace": namespace, "error": str(exc)},
            )
            return None

        finding = {
            "pod": name,
            "namespace": namespace,
            "workload": workload,
            "status": status,
            "replicas": self.count_affected(namespace, workload, status),
            "diagnosis": result["answer"],
            "confidence": result["confidence"],
            "unverified": result["unverified"],
            "tool_calls": [c["name"] for c in result["tool_calls"]],
        }

        log.info(
            "diagnosed",
            extra={
                "pod": name,
                "namespace": namespace,
                "status": status,
                "confidence": finding["confidence"],
                "duration_ms": round((time.monotonic() - started) * 1000),
            },
        )
        return finding

    def count_affected(self, namespace, workload, status):
        """How many pods of this workload share the fault, for the summary."""
        if not workload:
            return 1
        try:
            pods = client.CoreV1Api().list_namespaced_pod(namespace, _request_timeout=15)
        except Exception:
            return 1
        return sum(
            1
            for p in pods.items
            if workload_of(p) == workload and _pod_status(p) == status
        ) or 1

    # -- loops --------------------------------------------------------------

    def worker(self):
        """
        Single worker on purpose.

        Diagnosis is GPU-bound against one Ollama instance, so concurrency
        would only make every diagnosis slower.
        """
        while not self.stopping.is_set():
            try:
                pod, status = self.work.get(timeout=1)
            except queue.Empty:
                continue

            try:
                finding = self.diagnose(pod, status)
                if finding:
                    self.sink.send(finding)
            except Exception:
                log.exception("worker_error")
            finally:
                self.work.task_done()

    def watch_once(self, api, timeout=300):
        """One watch session. Returns when the stream closes; caller reloops."""
        stream = watch.Watch()
        kwargs = {"timeout_seconds": timeout}

        if len(NAMESPACES) == 1:
            method = api.list_namespaced_pod
            kwargs["namespace"] = NAMESPACES[0]
        else:
            method = api.list_pod_for_all_namespaces

        for event in stream.stream(method, **kwargs):
            if self.stopping.is_set():
                stream.stop()
                return

            pod = event["object"]
            if NAMESPACES and pod.metadata.namespace not in NAMESPACES:
                continue

            # On startup the watch replays every existing pod as ADDED, and
            # diagnosing that backlog is the fastest way to get muted. Record
            # those uids once and suppress them for the process lifetime.
            #
            # Only recorded during the startup window: adding on every event
            # grew the set forever, which on a cluster with Job churn is a
            # slow leak for no benefit -- after the window the membership test
            # changed nothing.
            if self.in_startup_window():
                self.preexisting_uids.add(pod.metadata.uid)
                if SKIP_EXISTING:
                    continue
            elif self.preexisting_uids and SKIP_EXISTING:
                if pod.metadata.uid in self.preexisting_uids:
                    continue

            status = self.interesting(pod)
            if status and self.enqueue(pod, status):
                log.info(
                    "queued",
                    extra={
                        "pod": pod.metadata.name,
                        "namespace": pod.metadata.namespace,
                        "status": status,
                    },
                )

    def in_startup_window(self, window=10):
        """Whether the watch is still replaying pods that predate us."""
        return time.monotonic() - self.start_time < window

    def run(self):
        self.start_time = time.monotonic()
        config.load_incluster_config() if os.getenv(
            "KUBERNETES_SERVICE_HOST"
        ) else config.load_kube_config()
        api = client.CoreV1Api()

        thread = threading.Thread(target=self.worker, daemon=True)
        thread.start()

        log.info(
            "controller_started",
            extra={
                "namespaces": NAMESPACES or ["*"],
                "cooldown_s": self.budget.cooldown,
                "max_per_hour": self.budget.max_per_hour,
                "model": self.model,
                "sink": type(self.sink).__name__,
            },
        )

        while not self.stopping.is_set():
            try:
                self.watch_once(api)
            except Exception as exc:
                # Watches expire and API servers restart; that is normal.
                log.warning("watch_restarting", extra={"error": str(exc)})
                time.sleep(2)

        thread.join(timeout=5)

    def stop(self, *_):
        log.info("controller_stopping")
        self.stopping.set()


if __name__ == "__main__":
    controller = Controller()
    signal.signal(signal.SIGTERM, controller.stop)
    signal.signal(signal.SIGINT, controller.stop)
    controller.run()
