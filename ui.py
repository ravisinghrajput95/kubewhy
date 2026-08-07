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

import streamlit as st

import agent
from routers.k8s_pods_info import (
    active_context,
    list_contexts,
    use_context,
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
def _scan(only_unhealthy, limit):
    return scan_cluster(only_unhealthy, limit)


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def _describe(name, namespace):
    return describe_pod(name, namespace)


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def _events(name, namespace):
    return get_pod_events(name, namespace)


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def _logs(name, namespace, tail):
    return get_pod_logs(name, namespace, tail)


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


st.set_page_config(page_title="local-triage-agent", page_icon="🩺", layout="wide")

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
    limit = st.slider("Max workloads", min_value=5, max_value=100, value=20, step=5)
    tail = st.slider("Log lines", min_value=10, max_value=100, value=40, step=10)

    if st.button("Refresh", width="stretch"):
        st.cache_data.clear()
    st.caption(f"Results cached {CACHE_TTL}s.")

st.title("local-triage-agent")

findings = _unwrap(_scan(only_unhealthy, limit), f"scan_cluster(only_unhealthy={only_unhealthy}, limit={limit})")

if findings:
    truncated = findings.pop("_truncated", None)

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
        pod = entry["example"]
        # Keys are "namespace/workload", or "namespace/workload:fault" when one
        # workload carries two faults at once.
        namespace = choice.split("/", 1)[0]

        if entry["pods"] > 1:
            st.caption(
                f"Showing `{pod}` — one of {entry['pods']} pods with this fault."
            )

        detail, events, logs = st.tabs(["Detail", "Events", "Logs"])

        with detail:
            data = _unwrap(_describe(pod, namespace), f"describe_pod({pod!r}, {namespace!r})")
            if data:
                st.json(data)

        with events:
            data = _unwrap(_events(pod, namespace), f"get_pod_events({pod!r}, {namespace!r})")
            if data:
                st.dataframe(data["events"], width="stretch", hide_index=True)

        with logs:
            data = _unwrap(_logs(pod, namespace, tail), f"get_pod_logs({pod!r}, {namespace!r}, tail={tail})")
            if data:
                st.caption(
                    f"Source: {data['source']}. Secrets are redacted by pattern "
                    "matching, which misses novel formats -- treat as sensitive."
                )
                st.code(data["logs"], language=None)

st.divider()
st.subheader("Ask")
st.caption(
    "Runs the whole agent loop against this cluster. Tens of seconds -- the "
    "tool chain appears as it happens rather than after."
)

question = st.text_input(
    "Question",
    placeholder="why is payments-api failing in staging?",
    label_visibility="collapsed",
)

if st.button("Diagnose", type="primary", disabled=not question):
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
    if confidence == "grounded":
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
