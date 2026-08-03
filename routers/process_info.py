import psutil


def get_processes(name_filter: str = ""):
    """
    Returns running processes grouped by name, as {process_name: [pids]}.

    Use this to check whether a specific program is running and how many
    copies of it exist. Args: name_filter -- case-insensitive substring; when
    given, only matching process names are returned. Always pass a filter if
    you are looking for something specific, since the unfiltered list is long.
    """
    processes = {}
    needle = name_filter.lower()

    for proc in psutil.process_iter(['pid', 'name']):
        try:
            p_name = proc.info['name']
            p_pid = proc.info['pid']

            # Handle processes with no name
            if p_name is None:
                p_name = "Unknown"

            if needle and needle not in p_name.lower():
                continue

            if p_name not in processes:
                processes[p_name] = []

            processes[p_name].append(p_pid)

        except (
            psutil.NoSuchProcess,
            psutil.AccessDenied,
            psutil.ZombieProcess,
        ):
            continue

    return processes


if __name__ == "__main__":
    print(get_processes())