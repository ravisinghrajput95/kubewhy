# Architecture

Three entry points share one set of tools. The tools are plain Python
functions returning JSON-able dicts, which is what lets them serve as REST
handlers, Ollama tools and MCP tools with no adapter in between.

```mermaid
flowchart TB
    subgraph clients [Entry points]
        CLI["agent.py<br/>CLI"]
        API["app.py<br/>FastAPI"]
        MCP["mcp_server.py<br/>MCP stdio/HTTP"]
    end

    subgraph loop [Agent loop]
        ASK["ask()<br/>bounded by MAX_ROUNDS"]
        GRD["grounding.check()<br/>verifies claims"]
    end

    subgraph tools [Tools · routers/]
        HOST["Host<br/>psutil"]
        K8S["Kubernetes<br/>client-go API"]
        RED["redaction.redact()"]
    end

    OLL["Ollama<br/>local model"]
    CLUSTER[("Kubernetes<br/>API server")]

    CLI --> ASK
    API --> ASK
    API -.direct, no model.-> tools
    MCP ==>|"tools exposed<br/>to any MCP client"| tools

    ASK <-->|"tool_calls / results"| OLL
    ASK --> tools
    ASK --> GRD

    K8S --> RED
    HOST --> tools
    K8S -->|"read-only<br/>15s timeout"| CLUSTER

    classDef sec fill:#7f1d1d,stroke:#ef4444,color:#fff
    class RED sec
```

The MCP path deliberately bypasses the loop: an MCP client brings its own
model, so it wants the tools, not another agent.

## A diagnosis, step by step

This is the real chain from the service-unreachable eval case. Note the model
never sees the cluster; it only sees projections, and each call is chosen from
what the previous one returned.

```mermaid
sequenceDiagram
    autonumber
    actor U as User
    participant A as ask()
    participant M as Ollama (qwen3)
    participant K as Kubernetes API

    U->>A: "crasher-svc is unreachable. Why?"
    A->>M: question + 12 tool schemas

    M-->>A: call get_service_endpoints
    A->>K: EndpointSlice for crasher-svc
    K-->>A: 0 ready, 1 not-ready
    A->>M: {"diagnosis": "pods match but none ready"}

    Note over M: needs to know which pod
    M-->>A: call list_pods(only_unhealthy)
    A->>K: list pods
    K-->>A: crasher CrashLoopBackOff
    A->>M: 91 tokens, not 7,560

    M-->>A: call describe_pod
    A->>K: read pod
    K-->>A: exit code 1, 6 restarts
    A->>M: limits + termination reason

    Note over M: status is the symptom,<br/>not the cause
    M-->>A: call get_pod_logs
    A->>K: logs (previous container)
    K-->>A: raw log text
    Note over A: redact() strips secrets
    A->>M: "could not connect to db:5432"

    M-->>A: final answer
    A->>A: grounding.check(answer, outputs)
    A-->>U: root cause + confidence + trace
```

## Why projection is the load-bearing decision

The model has a fixed context budget. Everything else follows from that.

| Payload | Tokens |
| --- | --- |
| Raw `list_namespaced_pod`, 5 pods | ~7,560 |
| `list_pods(only_unhealthy=True)` | ~91 |

At ~1,500 tokens per raw pod, a 50-pod namespace exceeds qwen3's entire 40k
window in a single call — before the model has reasoned about anything. Every
tool therefore returns only the fields a diagnosis depends on. A test asserts
a token ceiling so this cannot silently regress.

The cost is real: projections discard information, so a fault that hinges on a
field we dropped is invisible. The fields were chosen from the failure modes
in `demo/`, which is a sample of convenience, not a survey.

## Trust boundaries

```mermaid
flowchart LR
    subgraph machine [Your machine]
        direction TB
        AGENT[agent + tools]
        OLLAMA[Ollama]
    end

    CLUSTER[("Cluster<br/>API server")]
    CLOUD(["Third-party<br/>LLM APIs"])

    CLUSTER -->|"reads: pods, logs,<br/>events, nodes"| AGENT
    AGENT <-->|"prompts + tool results"| OLLAMA
    AGENT -.->|"never"| CLOUD

    classDef never stroke-dasharray:5 5,fill:#374151,color:#9ca3af
    class CLOUD never
```

Inference is local, so prompts and cluster data never reach a third party.
That is the guarantee. What it is *not*: pod logs still enter this process,
this terminal and this machine's memory. `redaction.py` strips common secret
shapes first, and `deploy/rbac.yaml` limits what the agent can read at all,
but "local" is not the same as "safe to point at prod".
