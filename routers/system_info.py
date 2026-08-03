import psutil

def get_system_info():
    """
    Returns overall host utilisation right now: cpu percent, memory percent,
    root disk percent, and the logged-in user.

    This is the best first call when asked whether the machine is healthy,
    busy, or under pressure. It reports totals only -- to find out which
    process is responsible, follow up with the top cpu or top memory tools.
    """

    cpu_usage = psutil.cpu_percent(interval=1)
    
    memory = psutil.virtual_memory()
    memory_usage = memory.percent
    
    disk = psutil.disk_usage('/')
    disk_usage = disk.percent
    
    users = psutil.users()
    current_user = users[0].name if users else None
    
    system_info = {
        "cpu" : cpu_usage,
        "memory" : memory_usage,
        "disk" : disk_usage,
        "user" : current_user
    }
    
    return system_info

if __name__ == "__main__":
    print(get_system_info())