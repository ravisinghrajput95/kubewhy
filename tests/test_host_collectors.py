"""
Tests for the host collectors.

These run against the real machine rather than mocks -- they assert shape and
invariants, not specific values, so they hold on any host.
"""

from unittest.mock import patch

from routers.platform_info import get_platform_info
from routers.process_info import get_processes
from routers.system_info import get_system_info
from routers.top_memory import get_top_memory_processes


class TestPlatformInfo:
    def test_returns_json_safe_types(self):
        """FastAPI renders timedelta as a bare float, which reads badly."""
        info = get_platform_info()

        assert isinstance(info["Boot_time"], str)
        assert isinstance(info["Uptime"], str)

    def test_uptime_has_no_microseconds(self):
        assert "." not in get_platform_info()["Uptime"]


class TestSystemInfo:
    def test_percentages_in_range(self):
        info = get_system_info()

        for key in ("cpu", "memory", "disk"):
            assert 0 <= info[key] <= 100, f"{key} out of range"

    def test_no_logged_in_user_is_none_not_crash(self):
        # Headless hosts and containers have an empty user list; indexing it
        # unguarded raises IndexError.
        with patch("routers.system_info.psutil.users", return_value=[]):
            assert get_system_info()["user"] is None


class TestProcessInfo:
    def test_groups_pids_by_name(self):
        processes = get_processes()

        assert processes
        assert all(isinstance(pids, list) for pids in processes.values())

    def test_name_filter_narrows_results(self):
        everything = get_processes()
        assert everything, "expected at least one running process"

        target = next(iter(everything))
        filtered = get_processes(name_filter=target)

        assert target in filtered
        assert len(filtered) <= len(everything)

    def test_name_filter_is_case_insensitive(self):
        target = next(iter(get_processes()))
        assert get_processes(name_filter=target.upper())

    def test_unmatched_filter_returns_empty(self):
        assert get_processes(name_filter="zzz-no-such-process-zzz") == {}


class TestTopMemory:
    def test_respects_limit(self):
        assert len(get_top_memory_processes(limit=3)) <= 3

    def test_sorted_descending(self):
        values = [p["memory_percent"] for p in get_top_memory_processes(limit=5).values()]
        assert values == sorted(values, reverse=True)
