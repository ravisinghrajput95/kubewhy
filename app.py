from fastapi import FastAPI
from pydantic import BaseModel

from agent import ask
from routers.platform_info import get_platform_info
from routers.system_info import get_system_info
from routers.process_info import get_processes
from routers.top_cpu import get_top_cpu_processes
from routers.top_memory import get_top_memory_processes
from routers.k8s_pods_info import (
    list_pods,
    describe_pod,
    get_pod_events,
    get_pod_logs,
)

app = FastAPI(title="System stats")

@app.get("/platform")
def platform():
    return get_platform_info()

@app.get("/system")
def system():
    return get_system_info()

@app.get("/processes")
def process():
    return get_processes()

@app.get("/cpu")
def top_cpu():
    return get_top_cpu_processes()

@app.get("/memory")
def top_memory():
    return get_top_memory_processes()

@app.get("/pods")
def pods(namespace: str = "default", only_unhealthy: bool = False):
    return list_pods(namespace, only_unhealthy)

@app.get("/pods/{name}")
def pod_detail(name: str, namespace: str = "default"):
    return describe_pod(name, namespace)

@app.get("/pods/{name}/events")
def pod_events(name: str, namespace: str = "default", limit: int = 10):
    return get_pod_events(name, namespace, limit)

@app.get("/pods/{name}/logs")
def pod_logs(name: str, namespace: str = "default", tail: int = 20):
    return get_pod_logs(name, namespace, tail)

class Question(BaseModel):
    question: str

@app.post("/ask")
def ask_agent(body: Question):
    """Answer a plain-English question about this host via the local model."""
    return ask(body.question)