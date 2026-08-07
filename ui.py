"""
Browser UI over the read-only collectors.

The fourth surface on the same tools: agent.py answers questions, app.py serves
REST, mcp_server.py speaks MCP, and this renders the same projections in a
browser. It imports the collectors directly rather than calling the API, for
the same reason app.py does -- they are plain functions returning JSON-able
dicts, so there is nothing to adapt.

    pip install -r requirements-ui.txt
    streamlit run ui.py

Two Streamlit defaults are overridden in .streamlit/config.toml, and both
matter: it otherwise listens on every interface with no authentication, and
reports usage back to streamlit.io.

Read-only holds here exactly as everywhere else. Every function called below
is one the MCP server already exposes to untrusted clients; this surface adds
no capability, only a way to look at it.
"""

import datetime as dt

import streamlit as st

import agent
from routers.k8s_pods_info import (
    active_context,
    list_contexts,
    list_namespaces,
    use_context,
    workload_pods,
    describe_pod,
    get_pod_events,
    get_pod_logs,
    list_nodes,
    scan_cluster,
)

# Short enough that a page you are watching during an incident stays honest,
# long enough that moving a slider does not re-list every pod in the cluster.
# Streamlit reruns the whole script on every widget interaction, so without
# this the API server takes a request per keystroke.
CACHE_TTL = 10


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def _scan(only_unhealthy, limit, namespaces):
    return scan_cluster(only_unhealthy, limit, namespaces)


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def _namespaces():
    return list_namespaces()


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def _workload_pods(namespace, workload):
    return workload_pods(namespace, workload)


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def _describe(name, namespace):
    return describe_pod(name, namespace)


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def _events(name, namespace):
    return get_pod_events(name, namespace)


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def _logs(name, namespace, tail, container=""):
    return get_pod_logs(name, namespace, tail, container)


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def _nodes():
    return list_nodes()


def _unwrap(result, tool):
    """
    Render a collector's failure modes, or return its data.

    Collectors never raise -- they return {"error": ...} so the agent loop can
    survive a dead cluster. The UI has to honour that contract rather than
    letting a 403 render as an empty table, which would read as "nothing is
    wrong".
    """
    st.caption(f"`{tool}`")

    if not isinstance(result, dict):
        return result
    if "error" in result:
        st.error(result["error"])
        return None
    if "result" in result:
        st.info(result["result"])
        return None
    return result


st.set_page_config(page_title="kubewhy", page_icon="🩺", layout="wide")

with st.sidebar:
    st.subheader("Cluster")

    current = active_context()
    contexts = list_contexts()

    if contexts:
        chosen = st.selectbox(
            "Context",
            contexts,
            index=contexts.index(current) if current in contexts else 0,
            label_visibility="collapsed",
        )
        if chosen != current:
            # Rebinds the clients and drops every cached result, so nothing
            # from the previous cluster survives on screen.
            use_context(chosen)
            st.cache_data.clear()
            st.rerun()
    else:
        st.code(current, language=None)

    st.caption(
        f"Reading **{current}**. This is the cluster the client is bound to, "
        "not whatever `current-context` happens to say now. Nothing writes to it."
    )

    only_unhealthy = st.toggle(
        "Only unhealthy", value=True, help="Off lists every workload, which is slower and much longer."
    )

    # A cluster with a thousand workloads is unusable as one flat list, and
    # the backend filter is cheaper than fetching everything and hiding rows:
    # a single namespace is a namespaced query rather than a cluster-wide one.
    chosen_namespaces = st.multiselect(
        "Namespaces", _namespaces(), help="Empty scans the whole cluster."
    )
    search = st.text_input("Filter", placeholder="workload or pod name")

    limit = st.slider("Max workloads", min_value=5, max_value=500, value=20, step=5)
    tail = st.slider("Log lines", min_value=10, max_value=100, value=40, step=10)

    if st.button("Refresh", width="stretch"):
        st.cache_data.clear()
        st.session_state["read_at"] = dt.datetime.now()

    # "Cached 10s" told you the policy, not whether what you are looking at is
    # current, which is the only thing that matters during an incident.
    read_at = st.session_state.setdefault("read_at", dt.datetime.now())
    st.caption(
        f"Cluster read at {read_at:%H:%M:%S}. Results are reused for "
        f"{CACHE_TTL}s so moving a slider does not re-read the cluster; "
        "Refresh forces a new read."
    )

st.title("kubewhy")

namespaces = ",".join(chosen_namespaces)
findings = _unwrap(
    _scan(only_unhealthy, limit, namespaces),
    f"scan_cluster(only_unhealthy={only_unhealthy}, limit={limit}"
    + (f", namespaces={namespaces!r}" if namespaces else "")
    + ")",
)

if findings:
    truncated = findings.pop("_truncated", None)

    if search:
        # Client-side, and only over what the scan already returned: the
        # server has no name index to query, so a "search" that claimed to
        # cover the whole cluster would be lying about its scope.
        needle = search.lower()
        findings = {
            key: entry
            for key, entry in findings.items()
            if needle in key.lower() or needle in entry["example"].lower()
        }
        if not findings:
            st.info(f"nothing matching {search!r} in the {limit} workloads scanned")

    st.dataframe(
        [
            {
                "workload": key,
                "status": entry["status"],
                # Grouped by fault, so a rollout showing ErrImagePull on one
                # replica and ImagePullBackOff on another is one row.
                "fault": entry.get("fault", "-"),
                "pods": entry["pods"],
                "example pod": entry["example"],
            }
            for key, entry in findings.items()
        ],
        width="stretch",
        hide_index=True,
    )

    if truncated:
        st.warning(truncated)

    st.divider()
    st.subheader("Why")
    st.caption(
        "The scan reports where, never why. Pick a workload to read the "
        "termination reason, events and logs behind it."
    )

    choice = st.selectbox("Workload", list(findings), label_visibility="collapsed")
    if choice:
        entry = findings[choice]
        # Keys are "namespace/workload", or "namespace/workload:fault" when one
        # workload carries two faults at once.
        namespace, _, rest = choice.partition("/")
        workload_name = rest.split(":", 1)[0]

        # A Deployment is its replicas. Showing only the example pod and
        # calling it the workload's story is wrong when three replicas fail
        # for different reasons.
        replicas = _workload_pods(namespace, workload_name)
        if isinstance(replicas, list) and len(replicas) > 1:
            labels = {
                f"{p['pod']}  —  {p['status']}{'' if p['ready'] else '  (not ready)'}": p
                for p in replicas
            }
            picked = st.radio(
                f"{len(replicas)} pods in this workload",
                list(labels),
                horizontal=False,
            )
            selected = labels[picked]
            pod = selected["pod"]
            containers = selected["containers"]
        else:
            pod = entry["example"]
            containers = next(
                (p["containers"] for p in replicas if p["pod"] == pod), []
            ) if isinstance(replicas, list) else []

        # Hand the selection to the Ask panel below, which otherwise has no
        # idea what "the selected workload" refers to.
        st.session_state["subject"] = {
            "namespace": namespace,
            "workload": f"{namespace}/{workload_name}",
            "pod": pod,
        }

        detail, events, logs = st.tabs(["Detail", "Events", "Logs"])

        with detail:
            data = _unwrap(_describe(pod, namespace), f"describe_pod({pod!r}, {namespace!r})")
            if data:
                st.json(data)

        with events:
            healthy_now = next(
                (p["ready"] and p["status"] == "Running" for p in replicas if p["pod"] == pod),
                False,
            ) if isinstance(replicas, list) else False
            if healthy_now:
                # Events are history. A pod that waited on a taint keeps that
                # warning for life, and without saying so the page presents a
                # resolved problem as a current one.
                st.info(
                    f"`{pod}` is Running and ready **now**. Any warnings below "
                    "already happened — check their age before acting."
                )

            data = _unwrap(_events(pod, namespace), f"get_pod_events({pod!r}, {namespace!r})")
            if data:
                st.dataframe(data["events"], width="stretch", hide_index=True)

        with logs:
            # Sidecars mean "the pod's logs" is ambiguous; picking silently
            # would show the proxy while the app is what broke.
            container = ""
            if len(containers) > 1:
                container = st.radio(
                    "Container", containers, horizontal=True, key=f"c-{pod}"
                )

            data = _unwrap(
                _logs(pod, namespace, tail, container),
                f"get_pod_logs({pod!r}, {namespace!r}, tail={tail}"
                + (f", container={container!r}" if container else "")
                + ")",
            )
            if data:
                st.caption(
                    f"Source: {data['source']}"
                    + (f", container `{data['container']}`" if data.get("container") else "")
                    + ". Secrets are redacted by pattern matching, which misses "
                    "novel formats -- treat as sensitive."
                )
                st.code(data["logs"], language=None)

st.divider()
st.subheader("Ask")
st.caption(
    "Runs the whole agent loop against this cluster. Tens of seconds -- the "
    "tool chain appears as it happens rather than after."
)

# What is selected above is context the model never had. "What is wrong with
# the selected workload?" has no referent on its own, so the model scanned the
# whole cluster and answered about everything -- the selection was on screen
# and nowhere else.
subject = st.session_state.get("subject")

# A form, so Enter in the box submits. A question typed and then apparently
# ignored until you find the button is the kind of thing that makes a tool feel
# broken.
with st.form("ask", clear_on_submit=False):
    question = st.text_input(
        "Question",
        placeholder=(
            f"why is {subject['workload']} failing?" if subject
            else "why is payments-api failing in staging?"
        ),
        label_visibility="collapsed",
    )
    scoped = (
        st.checkbox(
            f"About the selected workload ({subject['workload']})", value=True
        )
        if subject
        else False
    )
    submitted = st.form_submit_button("Diagnose", type="primary")

if submitted and question:
    if scoped:
        # Context, not an instruction. Prepending "Regarding demo/backup:"
        # hijacked questions that were deliberately broad -- "diagnose all
        # cluster resources" came back as a report on demo/backup alone. Say
        # what is on screen and let the question decide its own scope.
        question = (
            f"Context: the user is looking at workload {subject['workload']} "
            f"in namespace {subject['namespace']} (for example pod "
            f"{subject['pod']}). If the question below does not name a "
            f"workload and is not about the cluster as a whole, it refers to "
            f"that one.\n\nQuestion: {question}"
        )
    steps = st.status("Thinking...", expanded=True)
    try:
        # stream() rather than ask(): the chain is the point, and a diagnosis
        # runs long enough that a caller shown only the final answer is
        # watching a spinner for a minute.
        for event in agent.stream(question):
            if event["type"] == "tool_call":
                steps.write(f"`{event['name']}({event['arguments']})`")
            elif event["type"] == "tool_result":
                steps.write(
                    f"&nbsp;&nbsp;↳ {event['duration_ms']:.0f} ms, "
                    f"{len(event['result'])} chars",
                    unsafe_allow_html=True,
                )
            elif event["type"] == "answer":
                # Held in session_state so that moving a slider afterwards
                # re-renders the answer instead of re-running the model.
                st.session_state["answer"] = event
        result = st.session_state.get("answer")
        steps.update(
            label=f"{len(result['tool_calls'])} tool calls" if result else "No answer",
            state="complete",
            expanded=False,
        )
    except Exception as exc:
        # Most often Ollama is not running. The loop itself contains tool
        # failures, so anything reaching here is the model backend.
        steps.update(label="Failed", state="error")
        st.error(f"{type(exc).__name__}: {exc}")

answer = st.session_state.get("answer")
if answer:
    st.markdown(answer["answer"])

    # Confidence is not a footnote: an answer shown without it is the failure
    # grounding.py exists to prevent.
    confidence = answer["confidence"]
    if not answer["tool_calls"]:
        # "grounded" here means only that a reply with no figures contradicted
        # nothing. Calling a clarifying question "grounded" implies it was
        # checked against the cluster, and nothing was.
        st.warning("no tools were called — nothing here was measured")
    elif confidence == "grounded":
        st.success("grounded — every figure traced to a tool result")
    elif confidence == "partial":
        st.warning(
            "partial — not found in any tool result: "
            + ", ".join(answer["unverified"])
        )
    else:
        st.error("ungrounded — the model answered having called no tools")

with st.expander("Nodes"):
    # Worth a click before blaming a workload: pods that are Pending or being
    # evicted are usually a node problem wearing a workload's name.
    data = _unwrap(_nodes(), "list_nodes()")
    if data:
        st.dataframe(
            [
                {
                    "node": name,
                    "ready": node["ready"],
                    "pressure": ", ".join(node["pressure"]) if node["pressure"] else "-",
                    "cpu": node["allocatable_cpu"],
                    "memory": node["allocatable_memory"],
                    "unschedulable": node["unschedulable"],
                }
                for name, node in data.items()
            ],
            width="stretch",
            hide_index=True,
        )
