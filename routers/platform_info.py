import platform
import psutil
from datetime import datetime

def get_platform_info():
    """
    Returns static host identity: OS/platform string, hostname, the time the
    machine last booted, and how long it has been up.

    Use this to answer questions about what kind of machine this is, or how
    long it has been running since the last reboot.
    """

    os_details = platform.platform()

    hostname = platform.node()

    boot_time_timestamp = psutil.boot_time()
    boot_time = datetime.fromtimestamp(boot_time_timestamp)
    uptime = datetime.now() - boot_time
    
    platform_info = {
        "OS" : os_details,
        "Hostname" : hostname,
        "Boot_time" : boot_time.isoformat(timespec="seconds"),
        "Uptime" : str(uptime).split(".")[0]
    }
    
    return platform_info

if __name__ == "__main__":
    print(get_platform_info())