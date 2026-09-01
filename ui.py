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
import audit
import identity
import store
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


def _caller():
    """
    Who is looking at this page, or a refusal.

    Streamlit has no route layer, so this cannot be a middleware and cannot
    run before the connection is accepted -- it runs as the first thing the
    script does instead. That is why it is the *second* control: the first is
    that in a proxied deployment this app binds loopback and the Service
    targets the proxy, so an unauthenticated browser never reaches the script
    to be turned away by it. See identity.py.

    No peer address is passed because Streamlit does not expose one. The
    websocket's origin is not the request's, and inventing a peer from a
    header would be checking the thing against itself.
    """
    try:
        headers = dict(st.context.headers or {})
    except Exception:
        # Rendered outside a request -- AppTest, or a bare `python ui.py`.
        # Errors are data everywhere else in this project and a page that
        # dies deciding who is looking at it is worse than one that says.
        headers = {}
    return identity.require(headers)


def _exposure_warning():
    """
    The combination that is actually dangerous, or None.

    Unauthenticated is normal and correct on a laptop -- it is the documented
    default and the OS is the access control. Unauthenticated *and bound to
    every interface* is an unauthenticated cluster viewer on the LAN, and that
    is what earns a banner. Warning on the first alone would put a permanent
    red box on the ordinary case, which trains people to ignore it.
    """
    if identity.required():
        return None
    try:
        address = st.get_option("server.address")
    except Exception:
        return None
    if address in (None, "", "127.0.0.1", "::1", "localhost"):
        return None
    return (
        f"This console is bound to {address} with no authentication. It "
        "renders cluster state and pod logs, and anyone who can reach the "
        "port sees everything the ServiceAccount can read. Put an "
        "authenticating proxy in front and set TRIAGE_AUTH_MODE=proxy, or "
        "bind it back to 127.0.0.1."
    )


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

# --- the investigation, as an object -----------------------------------------
#
# The result is the primary thing on this page, not the conversation that
# produced it. An operator arriving at a finished investigation has to be able
# to answer, without reading prose: what was collected, what the evidence
# actually says, what was inferred from it, what is still unknown, whether any
# claim contradicts a measurement, how long it took, and which backend
# answered.
#
# Everything below is rendered from what agent.stream() already returns and
# grounding.contract() already computes. Nothing here re-derives a verdict; a
# second implementation of the checker living in the view is exactly how a UI
# comes to disagree with its own backend.

@st.cache_resource
def _history():
    """
    Where finished investigations are kept.

    store.build() rather than session_state: session state dies with the
    browser tab, and an operator who reloads after a fifteen-minute
    investigation should still find it. With TRIAGE_STATE_DB set it also
    survives a restart -- the same store the REST API's detached jobs use, so
    there is one place investigations live rather than two.
    """
    return store.build()


def _remember(question, answer):
    """Record a finished investigation. Never fatal: this is a convenience."""
    try:
        history = _history()
        job_id = store.new_job_id()
        history.create_job(job_id, question, at=store.now())
        history.update_job(job_id, "done", result=answer, at=store.now())
    except Exception:
        # A console that cannot write its own history is still a console.
        pass


def _recent(limit=12):
    try:
        return _history().list_jobs(limit=limit)
    except Exception:
        return []


def _header_strip():
    """
    Cluster, inference and health, on one line above everything.

    Read from the same places the API reports them -- inference.gateway() for
    the mode and provider, and its probe() for health -- so the console cannot
    disagree with /inference and /readyz about what this deployment is doing.
    Where inference happens decides what leaves your network, so it belongs on
    screen rather than in a settings page nobody opens.

    Never raises: a header that takes the page down when the model is
    unreachable is a header that hides the one fact worth showing.
    """
    # The bound context if the operator picked one, else whatever kubectl
    # would use. Naming the cluster matters more here than anywhere else on the
    # page: every finding below is about one cluster and there is no other
    # indication of which.
    try:
        context = _ctx() or active_context() or "current-context"
    except Exception:
        context = _ctx() or "current-context"
    cells = [f"<span><span class='kw-k'>cluster</span><b>"
             f"{html.escape(context)}</b></span>"]
    try:
        import inference

        described = inference.gateway().config.describe()
        primary = described["primary"]
        label = (f"{primary['mode']} · {primary['provider']} · "
                 f"{primary['model']}")
        st.session_state["backend_label"] = label
        cells.append(f"<span><span class='kw-k'>inference</span><b>"
                     f"{html.escape(label)}</b></span>")
        egress = ("external" if primary["destination"] == "external"
                  else "on-network")
        tone = "kw-warn" if egress == "external" else "kw-ok"
        cells.append(f"<span><span class='kw-k'>evidence</span>"
                     f"<b class='{tone}'>{egress}</b></span>")
    except Exception as exc:
        cells.append(f"<span><span class='kw-k'>inference</span>"
                     f"<b class='kw-bad'>{type(exc).__name__}</b></span>")

    health = st.session_state.get("health")
    if health is not None:
        tone = "kw-ok" if health else "kw-bad"
        word = "ready" if health else "not ready"
        cells.append(f"<span><span class='kw-k'>health</span>"
                     f"<b class='{tone}'>{word}</b></span>")
    return "<div class='kw-hdr'>" + "".join(cells) + "</div>"


VERDICT_STYLE = {
    "grounded": ("Grounded", "ok",
                 "every figure traced to a tool result"),
    "partial": ("Partial", "warn",
                "some claims were not found in any tool result"),
    grounding.CONTRADICTED: ("Contradicted", "bad",
                             "a claim disagrees with a measurement"),
    grounding.INSUFFICIENT: ("Insufficient evidence", "muted",
                             "nothing here could be checked"),
    "ungrounded": ("Ungrounded", "bad",
                   "claims were made with no tool result behind them"),
}


def _chip(label, tone, title=""):
    return (
        f"<span class='kw-chip kw-{tone}' title='{html.escape(title)}'>"
        f"{html.escape(label)}</span>"
    )


def _cite(evidence):
    """One claim's provenance, as tool and field rather than 'the transcript'."""
    if not evidence:
        return ""
    bits = []
    for item in evidence[:3]:
        tool = item.get("tool") or "?"
        field = item.get("field")
        bits.append(f"{tool}.{field}" if field else tool)
    return " · ".join(bits)


def next_step(answer):
    """
    The next investigation step, or None.

    Deterministic and borrowed, not invented: agent.evidence_gap() is the same
    function the loop uses to decide whether to send a run back for evidence
    the status block provably does not contain. Reusing it here means the
    console recommends exactly what the agent would have insisted on, rather
    than a second opinion written in the view.

    A UI that invented its own suggestion would be the fake-AI-capability this
    project does not ship.
    """
    trace = answer.get("tool_calls") or []
    outputs = [item.get("result", "") for item in (answer.get("evidence") or [])]
    if not trace or not outputs:
        return None
    try:
        # The prompt rather than the typed question: evidence_gap prefers the
        # pod the question names, and the scoped prompt names it even when the
        # operator typed "why is this broken?".
        gap = agent.evidence_gap(
            trace, outputs, answer.get("prompt") or answer.get("question", ""))
    except Exception:
        return None
    if not gap:
        return None
    kind, pod, namespace, status = gap
    tool = "get_pod_logs" if kind == "logs" else "get_pod_events"
    return (
        f"`{tool}` on **{pod}** in `{namespace}` — it is {status}, and the "
        f"cause of that is not in the status block."
    )


def render_investigation(answer):
    """The whole investigation, top down: conclusion first, then its basis."""
    confidence = answer.get("confidence", "ungrounded")
    label, tone, why = VERDICT_STYLE.get(
        confidence, (confidence, "muted", ""))
    timing = answer.get("timing") or {}
    trace = answer.get("tool_calls") or []
    rca = answer.get("rca") or {}

    # --- status strip: the four numbers an operator reads first -------------
    strip = [
        _chip(label, tone, why),
        _chip(f"{len(trace)} tool calls", "muted", "evidence collected"),
        _chip(f"{timing.get('wall_ms', 0) / 1000:.1f}s", "muted",
              "wall clock for the whole investigation"),
    ]
    if answer.get("termination"):
        strip.append(_chip(answer["termination"].replace("_", " "), "bad",
                           "the investigation did not finish on its own terms"))
    backend = st.session_state.get("backend_label")
    if backend:
        strip.append(_chip(backend, "muted", "inference backend that answered"))
    st.markdown(
        "<div class='kw-strip'>" + "".join(strip) + "</div>",
        unsafe_allow_html=True,
    )

    # --- what was actually asked --------------------------------------------
    #
    # Rendered from the answer rather than at submit time, so it survives every
    # rerun the page does afterwards. It used to be drawn only in the pass that
    # submitted the form, which meant moving a slider silently removed the
    # disclosure while leaving on screen the answer it explained.
    prompt = answer.get("prompt")
    if prompt and prompt != (answer.get("question") or ""):
        st.caption(
            "Scoped to the selected workload — untick the box above to ask "
            "about the cluster as a whole."
        )
        with st.expander("what was actually sent to the model"):
            # st.code renders one long line with horizontal overflow, so the
            # scoping directive scrolled off the right edge -- including the
            # sentence that matters most, "Do not report on any other
            # workload". A disclosure panel that hides the disclosure is worse
            # than no panel, because it looks like the whole prompt. Reported
            # from a real GKE session on 2026-08-22.
            st.markdown(
                f"<pre style='white-space:pre-wrap;word-break:break-word;"
                f"font-size:0.85em;margin:0'>{html.escape(prompt)}</pre>",
                unsafe_allow_html=True,
            )

    # --- root cause ---------------------------------------------------------
    st.markdown("#### Root cause")
    if not trace:
        st.warning("no tools were called — nothing here was measured")
    st.markdown(answer["answer"])

    if confidence == grounding.CONTRADICTED:
        # Ahead of everything else it could say. "The tools did not say" and
        # "the tools said otherwise" are different, and only the second means
        # the answer is wrong.
        for item in answer.get("contradictions", []):
            # No HTML here, and that is not a style preference. st.error takes
            # no unsafe_allow_html and escapes its body, so a <span> reaches
            # the reader as visible angle brackets -- in the red box that
            # announces the one verdict most worth reading. Confirmed in a
            # browser; the element tree cannot see it, because AppTest reads
            # the string that was submitted rather than the text that was
            # painted. Backticks are markdown, which st.error does render, and
            # a rule name is an identifier anyway.
            st.error(
                f"**Contradicted** — claimed *{item['claim']}*, but "
                f"{item['measured']}  \n"
                f"rule: `{item.get('rule','')}`",
                icon=":material/error:",
            )
    elif confidence == "grounded" and not answer.get("checked"):
        st.info("nothing to verify — this answer makes no measurable claim")

    step = next_step(answer)
    if step:
        st.markdown(
            f"<div class='kw-next'><b>Recommended next step</b><br>{step}</div>",
            unsafe_allow_html=True,
        )

    # --- what the evidence says --------------------------------------------
    st.markdown("#### What the evidence says")
    observations = rca.get("observations") or []
    inferences = rca.get("inferences") or []
    unknowns = rca.get("unknowns") or []
    corrections = answer.get("rewrites") or []

    cols = st.columns(3)
    with cols[0]:
        st.markdown(f"**Observed** · {len(observations)}")
        st.caption("traced to a tool result")
        for claim in observations[:12]:
            st.markdown(
                f"<div class='kw-claim kw-ok'>{html.escape(str(claim['claim']))}"
                f"<span class='kw-dim'>{html.escape(_cite(claim.get('evidence')))}"
                f"</span></div>", unsafe_allow_html=True)
        if not observations:
            st.caption("— none —")
    with cols[1]:
        st.markdown(f"**Inferred** · {len(inferences)}")
        st.caption("reasoning the run marked as such")
        for claim in inferences[:12]:
            st.markdown(
                f"<div class='kw-claim kw-warn'>"
                f"{html.escape(str(claim['claim']))}</div>",
                unsafe_allow_html=True)
        if not inferences:
            st.caption("— none —")
    with cols[2]:
        st.markdown(f"**Unknown** · {len(unknowns)}")
        st.caption("stated, and not supported")
        for claim in unknowns[:12]:
            st.markdown(
                f"<div class='kw-claim kw-bad'>{html.escape(str(claim))}</div>",
                unsafe_allow_html=True)
        if not unknowns:
            st.caption("— none —")

    if corrections:
        # verify() rewrites a fabricated value at the point it appears, so the
        # reader skimming for a number finds the measured one. Saying so is the
        # difference between a corrected answer and an edited one.
        st.markdown("**Corrected in the text above**")
        for edit in corrections:
            st.markdown(
                f"- `{edit.get('claim')}` → `{edit.get('observed')}` "
                f"({edit.get('action')})")

    # --- timeline -----------------------------------------------------------
    st.markdown("#### Timeline")
    if trace:
        rows = []
        for i, call in enumerate(trace, 1):
            args = call.get("arguments") or {}
            rows.append({
                "#": i,
                "tool": call["name"],
                "arguments": ", ".join(f"{k}={v}" for k, v in args.items()) or "—",
                "prefetched": "yes" if call.get("prefetched") else "",
                "scope": (call.get("scope") or {}).get("action", ""),
            })
        st.dataframe(rows, hide_index=True, width="stretch")
    else:
        st.caption("no tool calls")

    rounds = timing.get("rounds")
    if rounds:
        st.caption(
            f"{rounds} model rounds · model {timing.get('model_ms', 0)/1000:.1f}s"
            f" · tools {timing.get('tool_ms', 0)/1000:.1f}s"
            f" · re-asks: {answer.get('nudges', 0)} named-tool,"
            f" {answer.get('policies', 0)} evidence,"
            f" {answer.get('coverage', 0)} coverage"
        )

    # --- the evidence itself ------------------------------------------------
    evidence = answer.get("evidence") or []
    with st.expander(f"Evidence · {len(evidence)} tool results"):
        if not evidence:
            st.caption("evidence was not retained for this run")
        for item in evidence:
            st.markdown(
                f"<span class='kw-dim'>{item.get('id')} · "
                f"{html.escape(str(item.get('tool')))}</span>",
                unsafe_allow_html=True)
            st.code(item.get("result", ""), language="json")


st.set_page_config(page_title="kubewhy", page_icon=PAGE_ICON, layout="wide")

# Before the page renders anything. set_page_config has to come first because
# Streamlit requires it to, and nothing between the two reads the cluster.
try:
    WHO = _caller()
except identity.Unauthenticated as _refused:
    # No icon= argument. st.error(icon="X") is not a valid emoji, Streamlit
    # raises on it, and the raise blanks the whole page -- which is how this
    # console once rendered nothing at all on every contradiction. A refusal
    # that blanks the page cannot tell anyone why they were refused.
    st.error(
        "**Not signed in.**\n\n"
        f"{_refused.reason}\n\n"
        "This console renders cluster state and pod logs, so it refuses "
        "rather than serving them to a caller it cannot name."
    )
    st.stop()

# Re-asserted on every rerun, not set once. Streamlit runs the script on a
# fresh thread each time and a ContextVar belongs to the thread that set it --
# the same reason _bind() re-asserts the cluster context below.
audit.actor(WHO if WHO.authenticated else "anonymous",
            surface="console",
            auth=WHO.source)

_EXPOSED = _exposure_warning()
if _EXPOSED:
    st.warning(_EXPOSED)

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

      /* --- console chrome ---------------------------------------------------
         Tokens rather than literals, and both themes defined, because
         Streamlit renders in whichever the viewer chose and a colour that only
         exists in one of them is invisible in the other. */
      :root {
        --kw-ok:#1a7f4b; --kw-warn:#8a6d0b; --kw-bad:#b3261e; --kw-muted:#5a6472;
        --kw-line:#d5dce4; --kw-sunk:#f1f4f8; --kw-dim:#6b7683;
      }
      @media (prefers-color-scheme: dark) {
        :root {
          --kw-ok:#63bc85; --kw-warn:#d4b93f; --kw-bad:#f2705f; --kw-muted:#939eaa;
          --kw-line:#2a323d; --kw-sunk:#171d26; --kw-dim:#818c99;
        }
      }
      .kw-strip { display:flex; flex-wrap:wrap; gap:.4rem; margin:.1rem 0 1rem; }
      .kw-chip {
        font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:11px;
        letter-spacing:.04em; padding:3px 9px; border:1px solid currentColor;
        border-radius:2px; white-space:nowrap;
      }
      .kw-ok{color:var(--kw-ok)} .kw-warn{color:var(--kw-warn)}
      .kw-bad{color:var(--kw-bad)} .kw-muted{color:var(--kw-muted)}
      .kw-claim {
        border-left:3px solid currentColor; padding:2px 0 2px 9px;
        margin:3px 0; font-size:13.5px; display:flex; justify-content:space-between;
        gap:.6rem; align-items:baseline;
      }
      .kw-dim { color:var(--kw-dim); font-size:11px;
                font-family:ui-monospace,SFMono-Regular,Menlo,monospace; }
      .kw-next {
        border-left:3px solid var(--kw-muted); background:var(--kw-sunk);
        padding:.55rem .8rem; margin:.9rem 0; font-size:13.5px;
      }
      .kw-hdr {
        display:flex; gap:1.6rem; flex-wrap:wrap; align-items:baseline;
        border-bottom:1px solid var(--kw-line); padding-bottom:.5rem;
        margin-bottom:.9rem; font-size:12px;
      }
      .kw-hdr b { font-weight:600; }
      .kw-hdr .kw-k { color:var(--kw-dim); text-transform:uppercase;
                      letter-spacing:.1em; font-size:10px; margin-right:.35rem; }
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
    # Who is looking, above everything else. On a shared console the first
    # question about an investigation in the history is who ran it, and a page
    # that cannot answer "who am I signed in as" cannot answer that either.
    if WHO.authenticated:
        st.caption(f"Signed in as **{WHO.label()}**")

    # Investigations first, cluster second. The investigation is the primary
    # object on this page; the cluster browser is how you pick a subject for
    # one.
    st.subheader("Investigations")
    if st.button("New investigation", width="stretch"):
        # Only the result is cleared. The subject and the context stay, because
        # "ask another question about this pod" is the common next action and
        # making the operator re-select it would be the console forgetting what
        # they were doing.
        st.session_state.pop("answer", None)
        st.rerun()

    _rows = _recent()
    if _rows:
        for _job in _rows:
            _q = (_job.get("question") or "").strip().replace("\n", " ")
            _label = (_q[:46] + "…") if len(_q) > 46 else _q
            _when = _job.get("created_at")
            _stamp = (dt.datetime.fromtimestamp(_when).strftime("%H:%M")
                      if _when else "")
            if st.button(f"{_stamp}  {_label or 'investigation'}",
                         key=f"hist-{_job['id']}", width="stretch"):
                _stored = _history().get_job(_job["id"])
                if _stored and _stored.get("result"):
                    st.session_state["answer"] = _stored["result"]
                    st.rerun()
    else:
        st.caption("no investigations yet")

    st.divider()
    st.subheader("Cluster")

    current = active_context()
    contexts = list_contexts()

    # What this session asked for, falling back to what the client reports.
    # Not active_context() alone: a context that is in the kubeconfig but
    # cannot be built -- a missing cert file, a malformed user -- reports as
    # "unavailable" for as long as the process lives, by design. Comparing the
    # picker against that meant choosing such a context bound it, reran the
    # script, read "unavailable" again, found it still different and reran
    # again. The console spun instead of rendering, and the picker snapped
    # back to the first entry on every pass.
    bound = _ctx() or current

    if contexts:
        chosen = st.selectbox(
            "Context",
            contexts,
            index=contexts.index(bound) if bound in contexts else 0,
            label_visibility="collapsed",
        )
        if chosen != bound:
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

st.markdown(_header_strip(), unsafe_allow_html=True)

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

    # Keyed, and re-anchored by *value* rather than by position. The options
    # are rebuilt from every scan, and this cluster's CronJob workloads come
    # and go between scans -- so an unkeyed selectbox silently moved the
    # investigation target to whatever had taken index 0. The target of an
    # investigation must never change because a background read finished.
    _options = list(findings)
    _previous = st.session_state.get("workload_choice")
    # A workload can leave the scan while you are looking at it: `only_unhealthy`
    # hides it the moment it recovers, and a CronJob's workload disappears every
    # time its pods complete. Falling back to index 0 then retargets the
    # investigation to an unrelated workload without saying a word -- measured,
    # demo/nightly-sync -> demo/bad-image, and the next Diagnose would have
    # investigated a workload nobody chose. Keep it selected and say what
    # happened; moving the target is the user's decision to make.
    _vanished = bool(_previous) and _previous not in _options
    if _vanished:
        _options = [_previous] + _options
    choice = st.selectbox(
        "Workload", _options, label_visibility="collapsed",
        index=_options.index(_previous) if _previous in _options else 0,
        key="workload_choice",
    )
    if _vanished and choice == _previous:
        st.warning(
            f"**{_previous}** is no longer in the scan — it may have recovered, "
            "or its pods may have completed. It is still the selected target; "
            "pick another workload to move on."
        )
    if choice and choice in findings:
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

if submitted and not question and subject:
    # The placeholder is a well-formed question about the selected workload,
    # rendered in grey inside the box -- which is what a filled-in field looks
    # like. Clicking Diagnose then did nothing at all: no run, no message, the
    # `and question` guard failing in silence. Reported as "the button seems not
    # responding", and that is exactly what it was.
    #
    # Asking the question the box is already showing is the least surprising
    # thing this can do, and the caption below says which question ran.
    question = (f"why is {subject['workload']} failing?")

if submitted and not question:
    st.warning("Type a question — the grey text is a placeholder, and there is "
               "no workload selected to ask about.")

if submitted and question:
    asked = question
    # None means "no selection was made, infer the target from the words" --
    # the CLI and Slack path. Bound here so that an unscoped run cannot reach
    # the stream() call with the name unset.
    target = None
    if scoped:
        # Shared with the CLI, the REST API and the controller: see
        # agent.scoped_question for why this is directive rather than a hint.
        question = agent.scoped_question(
            question, subject["workload"], subject["namespace"], subject["pod"]
        )
        # The same selection, as data. Without this the loop re-derived the
        # target by parsing the prompt just built above -- and that prompt says
        # "(pod: x)" and "any other workload", both of which parse as workload
        # names. Passing it means the target cannot be anything but the one on
        # screen.
        target = agent.scoped_target(
            subject["workload"], subject["namespace"], subject["pod"]
        )
        # Say so, and show the rewrite. The checkbox defaults to on, so a
        # question about a namespace or about some other workload gets silently
        # turned into a question about the selected one, and the model is told
        # in as many words not to answer anything else. The user then reads
        # their own sentence next to an answer that ignores it. Every other
        # panel here names the tool behind it; this one was rewriting the input
        # and not showing it.
        # Disclosed by render_investigation() below, from the recorded
        # prompt, so that it is still there on the next rerun.
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
        for event in agent.stream(question, target=target):
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
                # Two different strings, and conflating them put the
                # scoping directive in the history list where the operator's
                # own sentence belonged. `question` is what the model was
                # handed; `asked` is what a person typed and what they will
                # scan the sidebar for.
                event["question"] = asked
                event["prompt"] = question
                st.session_state["answer"] = event
                _remember(asked, event)
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

    # The sidebar's history list is built near the top of the script, which is
    # to say before this run existed. Without a rerun, the investigation you
    # just finished is missing from "Investigations" until something else
    # happens to redraw the page -- which looked like history was not being
    # kept at all. Outside the try: RerunException is a control-flow signal,
    # and catching it here would turn a rerun into a red error box.
    #
    # The model is not called again -- `submitted` is False on the rerun and
    # the answer is already in session_state. The live tool log is replaced by
    # the panel's own timeline, which holds the same chain.
    if result is not None:
        st.rerun()

answer = st.session_state.get("answer")
if answer:
    render_investigation(answer)

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
