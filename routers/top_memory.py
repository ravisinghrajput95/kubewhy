import heapq
import psutil


def get_top_memory_processes(limit: int = 5):
    """
    Returns the processes currently using the most memory, keyed by pid, with
    name, memory_percent and username.

    Use this to identify what is consuming RAM.
    Args: limit -- how many processes to return, default 5.
    """
    processes = []

    for proc in psutil.process_iter(
        ["pid", "name", "memory_percent", "username"]
    ):
        try:
            info = proc.info

            if info["name"] == "System Idle Process":
                continue

            processes.append(info)

        except (
            psutil.NoSuchProcess,
            psutil.AccessDenied,
            psutil.ZombieProcess,
        ):
            continue

    top_processes = heapq.nlargest(
        limit,
        processes,
        key=lambda p: p.get("memory_percent") or 0.0,
    )

    return {
        str(p["pid"]): {
            "name": p["name"],
            "memory_percent": round(p.get("memory_percent") or 0.0, 2),
            "username": p["username"],
        }
        for p in top_processes
    }

if __name__ == "__main__":
    print(get_top_memory_processes())