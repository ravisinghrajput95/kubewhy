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

import html
import grounding
import datetime as dt
import time

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


def _ctx():
    """
    This session's cluster, and the first argument to everything cached below.

    st.cache_data is shared by every session in the process, so without the
    context in the key two sessions on two clusters read each other's results.
    Passing it explicitly is what keeps the cache honest; the collectors do
    not take it, and do not need to.
    """
    return st.session_state.get("context") or ""


def _bind(context):
    """
    Point this thread's collectors at the session's cluster.

    Streamlit reruns the script on a fresh thread and runs a cache miss on
    whichever thread asked, so the binding has to be re-asserted rather than
    set once when the selector changed.
    """
    use_context(context or None)


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def _scan(context, only_unhealthy, limit, namespaces, workload=""):
    _bind(context)
    return scan_cluster(only_unhealthy, limit, namespaces, workload)


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def _namespaces(context):
    _bind(context)
    return list_namespaces()


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def _workload_pods(context, namespace, workload):
    _bind(context)
    return workload_pods(namespace, workload)


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def _describe(context, name, namespace):
    _bind(context)
    return describe_pod(name, namespace)


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def _events(context, name, namespace):
    _bind(context)
    return get_pod_events(name, namespace)


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def _logs(context, name, namespace, tail, container=""):
    _bind(context)
    return get_pod_logs(name, namespace, tail, container)


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def _nodes(context):
    _bind(context)
    return list_nodes()


def progress_label(tool, calls, elapsed):
    """
    What the status bar says while a diagnosis runs.

    Progress has to be TEXT, not only the spinner beside it. That spinner is a
    CSS animation, and a browser stops painting frames whenever the tab is
    hidden, occluded or busy -- measured in Chrome with the tab backgrounded,
    requestAnimationFrame fired zero times in 1.2 seconds while the animation
    still reported playState "running". It freezes mid-rotation into a static
    arc, so a run that is working looks exactly like one that has hung. That
    matters here more than in most apps, because a diagnosis legitimately takes
    minutes and this model backend has stalled for as long as 1013 seconds.

    Text cannot be paused by a compositor. Whenever the reader looks back, the
    label says which tool is running, how many have run, and for how long --
    all of which a spinner never said even when it was spinning.
    """
    return f"{tool} — tool {calls}, {elapsed:.0f}s elapsed"


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


# One definition, used for the browser tab and for the heading on the page.
# They were set in two places and drifted into two different marks, which
# reads as two different products in the same window.
PAGE_ICON = "🩺"

st.set_page_config(page_title="kubewhy", page_icon=PAGE_ICON, layout="wide")

# Streamlit reserves about 6rem above the first element, which on a wide
# dashboard is a screenful of nothing before the scan table. Pulled in to leave
# room for the toolbar and no more.
#
# Selected by data-testid rather than the st-emotion-cache-* classes beside
# them: those are content hashes and change whenever Streamlit rebuilds its
# stylesheet, so a rule written against one silently stops applying on upgrade.
# The bare .block-container fallback covers older builds that predate the
# testid.
st.markdown(
    """
    <style>
      [data-testid="stMainBlockContainer"], .block-container {
        padding-top: 2.2rem;
      }
      [data-testid="stSidebarUserContent"] {
        padding-top: 0.5rem;
      }
      /* Streamlit puts padding on the heading itself as well as the container,
         so trimming only the container leaves the sidebar sitting lower than
         the page title beside it. */
      [data-testid="stSidebarUserContent"] > div > div:first-child h3 {
        padding-top: 0;
      }
    </style>
    """,
    unsafe_allow_html=True,
)

# Before anything reads the cluster. The context lives in session state and is
# re-asserted on every rerun, because it is scoped to the caller now and
# Streamlit gives each rerun a fresh thread -- a selection made last rerun is
# not still in effect on this one.
_bind(_ctx())

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
            # Only this session moves. Another browser session on another
            # cluster keeps reading its own, which a process-wide switch used
            # to break underneath it.
            #
            # No cache clear either: the context is part of every cache key,
            # so the new cluster cannot serve the old one's entries, and
            # clearing would throw away a concurrent session's results.
            st.session_state.context = chosen
            _bind(chosen)
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
        "Namespaces", _namespaces(_ctx()), help="Empty scans the whole cluster."
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

# The same mark as the browser tab, from the same constant. An emoji rather
# than artwork of my own: it costs no asset, fetches nothing from a CDN --
# which this page cannot afford, rendering cluster state on a claim that
# nothing leaves your network -- and it cannot fall out of step with the tab.
st.markdown(
    f"""
    <div style="display:flex;align-items:center;gap:.6rem;margin-bottom:.25rem;">
      <span style="font-size:2.5rem;line-height:1;" role="img"
            aria-label="kubewhy">{PAGE_ICON}</span>
      <h1 style="margin:0;padding:0;line-height:1;">kubewhy</h1>
    </div>
    """,
    unsafe_allow_html=True,
)

namespaces = ",".join(chosen_namespaces)
findings = _unwrap(
    _scan(_ctx(), only_unhealthy, limit, namespaces),
    f"scan_cluster(only_unhealthy={only_unhealthy}, limit={limit}"
    + (f", namespaces={namespaces!r}" if namespaces else "")
    + ")",
)

if findings:
    truncated = findings.pop("_truncated", None)

    if search:
        # Filter what is already on the page first -- free, and the common
        # case, since you are usually narrowing a list you can see.
        needle = search.lower()
        findings = {
            key: entry
            for key, entry in findings.items()
            if needle in key.lower() or needle in entry["example"].lower()
        }

        if not findings:
            # Nothing on the page, which used to end here with "nothing
            # matching X in the 20 workloads scanned". Honest about its scope
            # and useless as an answer: the workload may well exist, just
            # outside the limit or healthy and therefore never scanned. Ask
            # the server, which can answer about one workload by name whether
            # or not anything is wrong with it -- the only way to distinguish
            # "not here" from "not in the cluster".
            findings = _unwrap(
                _scan(_ctx(), False, limit, namespaces, search),
                f"scan_cluster(workload={search!r})",
            ) or {}
            findings.pop("_truncated", None)

            if findings:
                st.caption(
                    f"Not in the {limit} workloads scanned — found by asking "
                    f"the cluster for {search!r} directly."
                )
            # Nothing found needs no message here: scan_cluster answers
            # "no workload named X exists in this cluster", which _unwrap has
            # already rendered and which is more precise than anything this
            # branch could add.

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
        replicas = _workload_pods(_ctx(), namespace, workload_name)
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
            data = _unwrap(_describe(_ctx(), pod, namespace), f"describe_pod({pod!r}, {namespace!r})")
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

            data = _unwrap(_events(_ctx(), pod, namespace), f"get_pod_events({pod!r}, {namespace!r})")
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
                _logs(_ctx(), pod, namespace, tail, container),
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
            f"About the selected workload ({subject['workload']})",
            value=True,
            help=(
                "On, the question is answered only about the selected "
                "workload. Turn it off to ask about the cluster as a whole -- "
                "but then a vague question has nothing to anchor to, and the "
                "agent will ask you which workload you mean."
            ),
        )
        if subject
        else False
    )
    submitted = st.form_submit_button("Diagnose", type="primary")

if submitted and question:
    asked = question
    if scoped:
        # Shared with the CLI, the REST API and the controller: see
        # agent.scoped_question for why this is directive rather than a hint.
        question = agent.scoped_question(
            question, subject["workload"], subject["namespace"], subject["pod"]
        )
        # Say so, and show the rewrite. The checkbox defaults to on, so a
        # question about a namespace or about some other workload gets silently
        # turned into a question about the selected one, and the model is told
        # in as many words not to answer anything else. The user then reads
        # their own sentence next to an answer that ignores it. Every other
        # panel here names the tool behind it; this one was rewriting the input
        # and not showing it.
        st.caption(
            f"Scoped to **{subject['workload']}** — untick the box above to "
            "ask about the cluster as a whole."
        )
        with st.expander("what was actually sent to the model"):
            # st.code renders one long line with horizontal overflow, so the
            # scoping directive scrolled off the right edge -- including the
            # sentence that matters most, "Do not report on any other
            # workload". A disclosure panel that hides the disclosure is
            # worse than no panel, because it looks like the whole prompt.
            # Reported from a real GKE session on 2026-08-22: the user could
            # see "Answer only about the workload adversarial/annotation-inj"
            # and nothing after it.
            st.markdown(
                f"<pre style='white-space:pre-wrap;word-break:break-word;"
                f"font-size:0.85em;margin:0'>{html.escape(question)}</pre>",
                unsafe_allow_html=True,
            )
    # A new diagnosis invalidates the last one, and it has to be dropped
    # *before* the model is called rather than when the new answer arrives.
    # Otherwise the previous workload's answer stays on screen for the minute
    # this takes -- and if the run fails, or ends without an answer event, it
    # stays there for good, rendered underneath whichever workload is selected
    # now. An answer about a different workload, shown as though it were about
    # this one, is exactly the substitution scan_cluster(workload=...) and
    # scoped_question exist to prevent; it should not come back in through the
    # UI's own state.
    st.session_state.pop("answer", None)

    steps = st.status("Thinking...", expanded=True)
    result = None
    started = time.monotonic()
    calls = 0
    try:
        # stream() rather than ask(): the chain is the point, and a diagnosis
        # runs long enough that a caller shown only the final answer is
        # watching a spinner for a minute.
        for event in agent.stream(question):
            if event["type"] == "tool_call":
                calls += 1
                # Progress in the LABEL, not only in the spinner beside it.
                # That spinner is a CSS animation, and a browser stops painting
                # frames whenever the tab is hidden, occluded or busy -- at
                # which point it freezes mid-rotation into a static arc that
                # reads as a chevron, and a diagnosis that is working looks
                # identical to one that has hung. Measured: with the tab
                # backgrounded, requestAnimationFrame fired zero times in 1.2s
                # while the animation still reported playState "running".
                #
                # Text cannot be paused by a compositor. Whenever the reader
                # next looks, the label states what is happening and how long
                # it has taken, which is also more than a spinner ever said.
                steps.update(
                    label=progress_label(event["name"], calls, time.monotonic() - started)
                )
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
                result = event
        # This run's answer, not whatever is in session_state: reading it back
        # out meant a run that produced nothing was labelled with the previous
        # run's tool count.
        took = time.monotonic() - started
        steps.update(
            label=(
                f"{len(result['tool_calls'])} tool calls, {took:.0f}s"
                if result
                else f"No answer after {took:.0f}s"
            ),
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
    elif confidence == "grounded" and not answer.get("checked"):
        # Tools ran, but the answer asserts nothing checkable -- "I could not
        # identify any failing pods" has no figure and no status name in it, so
        # the checker finds nothing to contradict and reports grounded. Green
        # there says the cluster confirmed this, when what happened is that
        # there was nothing to confirm.
        st.info("nothing to verify — this answer makes no measurable claim")
    elif confidence == "grounded":
        st.success("grounded — every figure traced to a tool result")
    elif confidence == grounding.CONTRADICTED:
        # Ahead of partial in the branch order for the same reason it is ahead
        # in check(): "the tools did not say" and "the tools said otherwise"
        # are different, and only the second means the answer is wrong.
        st.error(
            "contradicted — the evidence says otherwise:\n\n"
            + "\n".join(
                f"- claimed *{c['claim']}*, but {c['measured']}"
                for c in answer.get("contradictions", [])
            )
        )
    elif confidence == "partial":
        st.warning(
            "partial — not found in any tool result: "
            + ", ".join(answer["unverified"])
        )
    elif confidence == grounding.INSUFFICIENT:
        st.info("insufficient evidence — nothing here could be checked")
    else:
        st.error("ungrounded — the model answered having called no tools")

with st.expander("Nodes"):
    # Worth a click before blaming a workload: pods that are Pending or being
    # evicted are usually a node problem wearing a workload's name.
    data = _unwrap(_nodes(_ctx()), "list_nodes()")
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
