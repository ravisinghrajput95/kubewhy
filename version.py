"""
One place the version is written down.

The MCP server was reporting an empty version string to every client, because
nothing in the Python source knew what version this is -- the number lived
only in the Helm chart, which the code cannot read. A client asking "what am I
talking to?" got "kubewhy" and a blank.

Kept deliberately dependency-free and import-cheap: this is imported by the
MCP server at startup and by a test that asserts it has not drifted from
`deploy/chart/Chart.yaml`. Two files carrying the same number is not ideal,
but Helm cannot read Python and the alternative -- generating one from the
other at build time -- adds a build step to a project that currently has none.
The test is the cheaper guarantee.

Bump this and `Chart.yaml` (both `version` and `appVersion`) together when
tagging a release; the tag is what publishes the image and chart to GHCR.
"""

__version__ = "0.2.0"
