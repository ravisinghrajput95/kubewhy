import psutil
import time


def get_top_cpu_processes(limit: int = 5):
    """
    Returns the processes currently using the most CPU, keyed by pid, with
    name, cpu_percent, memory_percent and username.

    Use this to identify what is driving high CPU. Takes roughly one second
    to sample. Args: limit -- how many processes to return, default 5.
    """

    # Initialize CPU counters
    for proc in psutil.process_iter():
        try:
            proc.cpu_percent(None)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    time.sleep(1)

    processes = []

    for proc in psutil.process_iter(
        ["pid", "name", "memory_percent", "username"]
    ):
        try:
            info = proc.info

            if info["name"] == "System Idle Process":
                continue

            info["cpu_percent"] = proc.cpu_percent(None)
            processes.append(info)

        except (
            psutil.NoSuchProcess,
            psutil.AccessDenied,
            psutil.ZombieProcess,
        ):
            continue

    processes.sort(key=lambda p: p["cpu_percent"], reverse=True)

    return {
        str(p["pid"]): {
            "name": p["name"],
            "cpu_percent": p["cpu_percent"],
            "memory_percent": round(p["memory_percent"], 2),
            "username": p["username"],
        }
        for p in processes[:limit]
    }


if __name__ == "__main__":
    print(get_top_cpu_processes())