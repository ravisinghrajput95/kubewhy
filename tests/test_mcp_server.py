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

        assert set(by_name["list_pods"].inputSchema["properties"]) == {
            "namespace",
            "only_unhealthy",
        }
        assert set(by_name["get_pod_logs"].inputSchema["properties"]) == {
            "name",
            "namespace",
            "tail",
        }

    def test_required_arguments_are_marked(self):
        by_name = {t.name: t for t in _tools()}
        # name has no default; namespace does.
        assert by_name["describe_pod"].inputSchema.get("required") == ["name"]

    def test_zero_argument_tools_have_empty_schema(self):
        by_name = {t.name: t for t in _tools()}
        assert by_name["list_nodes"].inputSchema.get("properties", {}) == {}
