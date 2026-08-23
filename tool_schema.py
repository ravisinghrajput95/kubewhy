"""
JSON Schema for the tool registry, derived rather than hand-written.

Ollama takes the Python callables in `agent.TOOLS` straight from the registry
and builds the schema itself by introspecting name, signature and docstring.
Every other provider wants explicit JSON Schema. Hand-maintaining fourteen of
those alongside the functions is how they drift: the docstrings here are
prompt engineering, not documentation -- they are the text the model reads
when deciding which tool to call -- and a schema whose description has fallen
a refactor behind changes tool selection without changing a single test.

So this derives them from the same source Ollama uses. One definition, two
consumers.

**It refuses rather than guesses.** A parameter with no annotation raises,
because the alternative is defaulting it to "string" and silently telling the
model that `only_unhealthy` is text. That kind of error does not fail; it
produces a slightly worse agent, which is the hardest thing to notice. All
fourteen tools are fully annotated today and a test pins that.

**The description is the whole first paragraph of the docstring, not a
summary.** CONTRIBUTING requires each docstring to say *when to use* the tool
rather than what it returns, and that sentence is the thing doing the work.
Truncating it to a line would quietly undo the prompt engineering.

Not wired to any provider yet, and deliberately so -- there is no second
backend to consume it. It exists because it is the part of adding one that
can be written and verified without an API key.
"""

import inspect

# The JSON Schema type for each Python annotation this registry uses. Explicit
# rather than a lookup on __name__, so an unfamiliar annotation raises here
# instead of arriving at the provider as something unintended.
_TYPES = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
}


def _description(func):
    """
    The docstring's first paragraph, whitespace-normalised.

    Paragraph rather than first line: these docstrings open with a sentence
    about when to reach for the tool and then qualify it, and the qualifier is
    often the part that stops the model calling it in the wrong situation.
    """
    doc = inspect.getdoc(func) or ""
    paragraph = doc.split("\n\n", 1)[0]
    return " ".join(paragraph.split())


def parameters_for(func):
    """The `parameters` object: properties, types, defaults and required."""
    signature = inspect.signature(func)
    properties = {}
    required = []

    for name, parameter in signature.parameters.items():
        # *args / **kwargs cannot be expressed and no tool here uses them.
        if parameter.kind in (parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD):
            raise TypeError(
                f"{func.__name__}({name}) is variadic; a tool must have an "
                "enumerable signature for a schema to describe it"
            )

        if parameter.annotation is inspect.Parameter.empty:
            raise TypeError(
                f"{func.__name__}({name}) has no type annotation. Refusing to "
                "guess: defaulting it to string would tell the model the wrong "
                "thing about the argument, which degrades tool use without "
                "failing anything."
            )

        try:
            json_type = _TYPES[parameter.annotation]
        except (KeyError, TypeError):
            raise TypeError(
                f"{func.__name__}({name}) is annotated {parameter.annotation!r}, "
                f"which has no JSON Schema equivalent here. Add one to _TYPES "
                f"deliberately rather than letting it fall through."
            ) from None

        prop = {"type": json_type}
        if parameter.default is inspect.Parameter.empty:
            # No default means the model must supply it.
            required.append(name)
        else:
            # Carried through: the model reads defaults as a hint about what
            # the tool does when left alone, and omitting them has it passing
            # values it did not need to.
            prop["default"] = parameter.default
        properties[name] = prop

    schema = {"type": "object", "properties": properties}
    # Only when non-empty: an empty `required` array is legal but noise, and
    # some providers treat its presence as meaningful.
    if required:
        schema["required"] = required
    return schema


def schema_for(func, name=None):
    """One tool, in the shape every OpenAI-compatible API expects."""
    return {
        "type": "function",
        "function": {
            # The registry key, not __name__, because the model dispatches by
            # the key. They match today and a test pins that -- but if they
            # ever diverge, the key is the one that has to win.
            "name": name or func.__name__,
            "description": _description(func),
            "parameters": parameters_for(func),
        },
    }


def schemas_for(registry):
    """The whole registry, in registry order."""
    return [schema_for(func, name) for name, func in registry.items()]
