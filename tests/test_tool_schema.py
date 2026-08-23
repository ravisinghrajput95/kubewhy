"""
Tests for the derived tool schemas.

The schemas are not the interesting part; the refusals are. A generator that
guesses produces a schema that works, describes the tool slightly wrongly, and
degrades tool selection without failing anything -- which is the hardest class
of defect to notice and the reason this module raises instead of defaulting.

No provider and no network: this is a pure function over the registry.
"""

import inspect

import pytest

import agent
import tool_schema


class TestTheWholeRegistryDerives:
    def test_every_tool_produces_a_schema(self):
        schemas = tool_schema.schemas_for(agent.TOOLS)
        assert len(schemas) == len(agent.TOOLS)

    def test_schema_names_are_the_registry_keys(self):
        # The model dispatches by the registry key. If a schema ever carried
        # __name__ instead and the two diverged, the model would call a name
        # the dispatcher does not know.
        for name, schema in zip(agent.TOOLS, tool_schema.schemas_for(agent.TOOLS)):
            assert schema["function"]["name"] == name

    def test_every_tool_carries_a_description(self):
        # Docstrings are the tool descriptions the model reads. An empty one
        # leaves it guessing when to call the tool.
        for schema in tool_schema.schemas_for(agent.TOOLS):
            description = schema["function"]["description"]
            assert description and len(description) > 20, schema["function"]["name"]

    def test_the_registry_stays_fully_annotated(self):
        # This is what lets the schemas be derived at all. If a new tool lands
        # with an unannotated parameter, this fails here rather than the
        # generator failing at whatever moment a second backend first runs.
        for name, func in agent.TOOLS.items():
            for parameter in inspect.signature(func).parameters.values():
                assert parameter.annotation is not inspect.Parameter.empty, \
                    f"{name}({parameter.name}) needs a type annotation"


class TestTypesAndDefaults:
    def test_a_known_tool_maps_types_correctly(self):
        schema = tool_schema.schema_for(agent.TOOLS["list_pods"], "list_pods")
        properties = schema["function"]["parameters"]["properties"]
        assert properties["namespace"]["type"] == "string"
        assert properties["only_unhealthy"]["type"] == "boolean"
        assert properties["limit"]["type"] == "integer"

    def test_defaults_are_carried_through(self):
        # The model reads a default as a hint about what the tool does when
        # left alone; dropping them has it passing values it did not need to.
        properties = tool_schema.schema_for(
            agent.TOOLS["get_pod_logs"], "get_pod_logs"
        )["function"]["parameters"]["properties"]
        assert properties["tail"]["default"] == 20
        assert properties["namespace"]["default"] == "default"

    def test_only_parameters_without_defaults_are_required(self):
        schema = tool_schema.schema_for(agent.TOOLS["get_pod_logs"], "get_pod_logs")
        assert schema["function"]["parameters"]["required"] == ["name"]

    def test_a_tool_with_no_parameters_has_no_required_key(self):
        # An empty `required` array is legal but noise, and some providers
        # treat its presence as meaningful.
        schema = tool_schema.schema_for(agent.TOOLS["list_nodes"], "list_nodes")
        parameters = schema["function"]["parameters"]
        assert parameters["properties"] == {}
        assert "required" not in parameters


class TestItRefusesRatherThanGuesses:
    """The whole point. Each of these would otherwise ship a plausible lie."""

    def test_an_unannotated_parameter_raises(self):
        def bad(name, namespace: str = "default"):
            """Does a thing."""

        with pytest.raises(TypeError) as caught:
            tool_schema.parameters_for(bad)
        assert "no type annotation" in str(caught.value)

    def test_an_unmappable_annotation_raises(self):
        def bad(when: complex = 0):
            """Does a thing."""

        with pytest.raises(TypeError) as caught:
            tool_schema.parameters_for(bad)
        assert "no JSON Schema equivalent" in str(caught.value)

    def test_a_variadic_signature_raises(self):
        def bad(*args: str):
            """Does a thing."""

        with pytest.raises(TypeError) as caught:
            tool_schema.parameters_for(bad)
        assert "variadic" in str(caught.value)


class TestDescriptionIsTheParagraphNotTheLine:
    def test_the_full_first_paragraph_survives(self):
        def tool(x: str = ""):
            """
            Use this when the pod is already gone.

            Not for a running pod: describe_pod is cheaper and says more.
            """

        description = tool_schema.schema_for(tool)["function"]["description"]
        assert description == "Use this when the pod is already gone."

    def test_a_wrapped_paragraph_is_joined_into_one_line(self):
        # CONTRIBUTING asks each docstring to say *when to use* the tool, and
        # that sentence is usually wrapped across lines. Sending it with the
        # newlines intact is untidy; truncating it at the first line would
        # quietly undo the prompt engineering.
        def tool(x: str = ""):
            """
            Use this when the service has endpoints
            but none of them are ready.

            More detail here.
            """

        description = tool_schema.schema_for(tool)["function"]["description"]
        assert description == (
            "Use this when the service has endpoints but none of them are ready."
        )
