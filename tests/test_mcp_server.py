"""
Tests for the MCP surface.

These check registration and schema derivation, which is where an MCP server
silently breaks: a tool that fails to register, or one whose schema loses its
arguments, is invisible until a client tries to call it.
"""

import asyncio

import mcp_server


def _tools():
    return asyncio.run(mcp_server.mcp.list_tools())


class TestRegistration:
    def test_exposes_every_agent_tool(self):
        """The MCP surface and the local agent must not drift apart."""
        import agent

        exposed = {t.name for t in _tools()}
        assert set(agent.TOOLS) <= exposed

    def test_no_mutating_tools_are_exposed(self):
        # The read-only guarantee is the whole security posture. An MCP client
        # is an untrusted caller; nothing here may change cluster state.
        forbidden = ("delete", "create", "patch", "update", "scale", "evict", "restart")
        offenders = [
            t.name for t in _tools() if any(word in t.name.lower() for word in forbidden)
        ]
        assert offenders == []

    def test_every_tool_has_a_description(self):
        missing = [t.name for t in _tools() if not (t.description or "").strip()]
        assert missing == []


class TestSchemas:
    def test_arguments_survive_schema_derivation(self):
        by_name = {t.name: t for t in _tools()}

        assert set(by_name["list_pods"].input_schema["properties"]) == {
            "namespace",
            "only_unhealthy",
            # Bounded like scan_cluster: a namespace is not a small number by
            # nature, and an unbounded list defeats the projection strategy.
            "limit",
            # Answers "is X healthy?" about X, rather than listing the
            # namespace and letting the model read a neighbour.
            "workload",
        }
        assert set(by_name["scan_cluster"].input_schema["properties"]) == {
            "only_unhealthy",
            "limit",
            # Narrowing, so a large cluster is navigable rather than truncated.
            "namespaces",
            # Reports one workload's state whether or not it is broken, which
            # is the only way to answer "is X healthy?" with "yes".
            "workload",
        }
        assert set(by_name["get_pod_logs"].input_schema["properties"]) == {
            "name",
            "namespace",
            "tail",
            # Multi-container pods need a choice; the API returns 400 without.
            "container",
        }

    def test_required_arguments_are_marked(self):
        by_name = {t.name: t for t in _tools()}
        # name has no default; namespace does.
        assert by_name["describe_pod"].input_schema.get("required") == ["name"]

    def test_zero_argument_tools_have_empty_schema(self):
        by_name = {t.name: t for t in _tools()}
        assert by_name["list_nodes"].input_schema.get("properties", {}) == {}


class TestVersionIsReported:
    """
    The server used to answer an empty string when asked what it was.

    Two files carry the number -- version.py and Chart.yaml -- because Helm
    cannot read Python. This is the check that keeps them from drifting, which
    is cheaper than adding a build step to generate one from the other.
    """

    def test_the_server_reports_a_version(self):
        import mcp_server
        from version import __version__

        assert __version__
        assert mcp_server.mcp.version == __version__

    def test_the_chart_agrees_with_the_package(self):
        import os
        import re
        from version import __version__

        chart = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "deploy", "chart", "Chart.yaml",
        )
        with open(chart) as fh:
            text = fh.read()

        app = re.search(r'^appVersion:\s*"?([^"\s]+)"?', text, re.M).group(1)
        ver = re.search(r'^version:\s*"?([^"\s]+)"?', text, re.M).group(1)

        assert app == __version__, f"Chart appVersion {app} != version.py {__version__}"
        assert ver == __version__, f"Chart version {ver} != version.py {__version__}"


class TestTheDocumentedPort:
    """
    The HTTP default is a published contract: README tells the reader
    `--http` serves "streamable HTTP on :8765", and an MCP client is
    configured against that number by hand.

    It cannot be reached by importing this module -- argparse is set up under
    `if __name__ == "__main__":`, which is why mutating the default survived
    and why no behavioural test can kill it. So this checks the two places
    the number is written still agree, in the same shape as
    `test_the_chart_agrees_with_the_package` above.
    """

    def test_the_readme_and_the_default_agree(self):
        import os
        import re

        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        source = open(os.path.join(root, "mcp_server.py"), encoding="utf-8").read()
        readme = open(os.path.join(root, "README.md"), encoding="utf-8").read()

        default = re.search(r'"--port",\s*type=int,\s*default=(\d+)', source)
        assert default, "the --port default is no longer where this test looks"

        documented = re.search(r"streamable HTTP on :(\d+)", readme)
        assert documented, "README no longer states the HTTP port"

        assert default.group(1) == documented.group(1)
