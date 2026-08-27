"""
Who is asking.

kubewhy is built for one SRE team against one cluster, and that decision is
what this module encodes. Authorization lives in `deploy/rbac.yaml`: the
ClusterRole is the boundary, and everyone who gets past this module is
entitled to everything it grants. There is no per-user authorization model and
this is not the place one would go -- the tools take a `namespace` argument as
a filter the caller picks, not as a boundary, and making it a boundary would
mean impersonation and a very different threat model. See SECURITY.md.

So the job here is narrow: establish *that* the caller authenticated, and
recover *who* they are so the audit trail can name them.

## Why the identity arrives in a header

Streamlit has no route layer to hang an authenticator on. Anything this
process checks runs after the connection is accepted and the websocket is up,
which makes app-level auth a thing a bug can undo. The enforcement is
therefore structural: an authenticating reverse proxy is the only listener the
Service targets, and the app binds loopback behind it. Nothing unauthenticated
can reach the app to be let in by mistake.

That is the same argument this project makes about NetworkPolicy -- a property
the dataplane enforces beats one this process promises.

## What this module still does, given that

Fails closed on the misconfiguration. `TRIAGE_AUTH_MODE=proxy` is the operator
saying "I am behind a proxy"; if the identity header is then absent, the
request is refused rather than served anonymously. Two independent controls,
because the one that matters is the one nobody remembered to check: someone
restoring `--server.address=0.0.0.0` for a debugging session and leaving it.

## What this is not

Header trust is only as good as the guarantee that nothing else can reach the
backend. In `proxy` mode a caller who can open a socket to the app directly
can spell any identity they like. The loopback bind is what prevents that, and
`require()` takes the peer address where a surface knows it so the refusal
does not depend on the bind alone. Stated plainly because a security control
whose limits are undocumented is a liability.
"""

import logging
import os
import re

log = logging.getLogger("triage.identity")

# The operator's declaration of what is in front of this process.
#
#   none  -- nothing is. The default, and correct on loopback: this is how the
#            tool runs on a laptop, where the OS is the access control.
#   proxy -- an authenticating reverse proxy is, and its assertion is trusted.
MODE_NONE = "none"
MODE_PROXY = "proxy"
MODES = (MODE_NONE, MODE_PROXY)

# oauth2-proxy spells the identity two different ways depending on how it is
# deployed, and picking one would break the other. As a reverse proxy with
# --pass-user-headers it sets the X-Forwarded family; in the nginx auth_request
# shape with --set-xauthrequest it sets X-Auth-Request. Both are read, in
# preference order, so the chart and an existing ingress-level deployment can
# both work without the operator having to know which family they got.
#
# Overridable because "put your own header here" is a five-minute integration
# with a proxy nobody here has heard of, and hard-coding is a support burden.
_EMAIL_HEADERS = ("x-forwarded-email", "x-auth-request-email")
_USER_HEADERS = (
    "x-forwarded-preferred-username",
    "x-auth-request-preferred-username",
    "x-forwarded-user",
    "x-auth-request-user",
)
_GROUP_HEADERS = ("x-forwarded-groups", "x-auth-request-groups")

# A principal name reaches a log line, and item 3 of the production work is an
# audit trail. A newline in an identity is a forged audit record, so control
# characters are stripped rather than escaped -- there is no legitimate one in
# an email address or a username, and a stripped name is still recognisable
# where an escaped one is noise.
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

# Long enough for a real corporate email, short enough that a header cannot be
# used to write a megabyte into every audit record.
_MAX_LEN = 320


class Unauthenticated(Exception):
    """
    No identity, where one was required.

    Carries the reason so the surface can log why without re-deriving it, and
    so the two cases stay distinguishable: a header that never arrived is a
    proxy that is missing or misconfigured, while a request from off-loopback
    is someone reaching the backend directly. Those need different responses
    from whoever is on call.
    """

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class Principal:
    """
    The authenticated caller, or the absence of one.

    Deliberately not a dict: this ends up in log records and on the page, and
    a dict invites callers to add fields to it that then differ per surface.
    """

    __slots__ = ("name", "email", "groups", "source")

    def __init__(self, name="", email="", groups=(), source="anonymous"):
        self.name = name
        self.email = email
        self.groups = tuple(groups)
        self.source = source

    @property
    def authenticated(self):
        return self.source != "anonymous"

    def label(self):
        """
        What to show a human and what to write in an audit record.

        Email first: it is the field that identifies a person across systems,
        which is what an audit trail is for. The username is a fallback
        because some providers do not release an email claim at all.
        """
        return self.email or self.name or "anonymous"

    # No __eq__ here, deliberately. Nothing compares two Principals -- every
    # comparison in this project is on label(), which is a string -- and
    # defining __eq__ without __hash__ sets __hash__ to None, which silently
    # makes the class unhashable. That is a trap for the next person who keys
    # a dict or a set on a principal, which is exactly what per-caller rate
    # limiting would reach for. Found by mutation testing: six mutants inside
    # the __eq__ that used to be here all survived, because nothing exercised
    # it at all.

    def __repr__(self):
        return f"Principal({self.label()!r}, source={self.source!r})"


ANONYMOUS = Principal()


def mode():
    """
    The configured mode, defaulting to `none`.

    An unrecognised value is a configuration error and is *not* silently
    treated as `none`: a typo in the one setting that turns authentication on
    must not be the thing that turns it off. It raises at import-time use,
    which on every surface here is startup.
    """
    raw = (os.getenv("TRIAGE_AUTH_MODE") or MODE_NONE).strip().lower()
    if raw not in MODES:
        raise ValueError(
            f"TRIAGE_AUTH_MODE={raw!r} is not one of {', '.join(MODES)}. "
            "Refusing to guess: a typo here would disable authentication."
        )
    return raw


def required():
    """Whether a request without an identity should be refused."""
    return mode() == MODE_PROXY


def _clean(value):
    """One header value, made safe to log and to render."""
    if not value:
        return ""
    return _CONTROL.sub("", str(value)).strip()[:_MAX_LEN]


def _configured(env, fallback):
    """
    Header names for one field: the operator's, else the known ones.

    Comma-separated so a deployment straddling two proxies can name both, and
    lower-cased because HTTP header names are case-insensitive and the
    mappings this is handed are not all case-insensitive dicts.
    """
    raw = (os.getenv(env) or "").strip()
    if not raw:
        return fallback
    return tuple(part.strip().lower() for part in raw.split(",") if part.strip())


def _first(headers, names):
    """
    The first of `names` present and non-empty.

    Non-empty matters and is not pedantry: oauth2-proxy forwards an empty
    X-Forwarded-Email when the provider released no email claim. Treating
    present-but-empty as an identity would authenticate a caller as "".
    """
    for name in names:
        value = _clean(headers.get(name))
        if value:
            return value
    return ""


def resolve(headers):
    """
    The principal a request's headers assert, or ANONYMOUS.

    Asserting is all a header can do. Whether that assertion is worth anything
    is a property of the deployment -- see the module docstring -- and is
    decided by `require()`, not here.
    """
    lowered = {str(key).lower(): value for key, value in dict(headers or {}).items()}

    email = _first(lowered, _configured("TRIAGE_AUTH_EMAIL_HEADER", _EMAIL_HEADERS))
    user = _first(lowered, _configured("TRIAGE_AUTH_USER_HEADER", _USER_HEADERS))
    raw_groups = _first(lowered, _configured("TRIAGE_AUTH_GROUPS_HEADER", _GROUP_HEADERS))

    if not (email or user):
        return ANONYMOUS

    groups = tuple(part.strip() for part in raw_groups.split(",") if part.strip())
    return Principal(name=user, email=email, groups=groups, source="proxy")


def _is_loopback(peer):
    """
    Whether a peer address is this machine talking to itself.

    Compared as a string rather than parsed: the addresses a sidecar produces
    are a tiny closed set, and `ipaddress` would accept forms -- integer IPv4,
    the ::ffff:127.0.0.1 mapping -- that need thinking about rather than
    accepting. The one mapped form that genuinely occurs is listed explicitly.

    This project has been bitten once by two parsers disagreeing on one string
    at a security boundary, and the lesson taken was to narrow what is
    accepted rather than to normalise more cleverly.
    """
    return str(peer or "").strip().lower() in {
        "127.0.0.1",
        "::1",
        "::ffff:127.0.0.1",
        "localhost",
    }


def require(headers, peer=None):
    """
    The principal, or refuse.

    `peer` is the address the request arrived from, where the surface knows it
    -- FastAPI does, Streamlit does not. Passing it makes the loopback bind
    something this process verifies rather than something it assumes, which
    matters because the bind is the only reason the header can be trusted.
    Omitting it is not a hole so much as a weaker position: the bind still
    holds, there is just nothing here checking that it does.

    A measured caveat, because it decides whether the check works at all.
    uvicorn 0.51.0 rewrites the peer address from X-Forwarded-For by default,
    trusting it from 127.0.0.1 -- which is precisely the sidecar. Measured
    against a live server: with the default flags and `X-Forwarded-For:
    203.0.113.9`, `request.client.host` reads 203.0.113.9 rather than
    127.0.0.1, so this check would refuse every legitimate proxied request
    while still catching a direct one. With --no-proxy-headers the same request
    reads 127.0.0.1.

    So anyone putting a proxy in front of app.py must run uvicorn with
    --no-proxy-headers. That is documentation rather than something enforced
    here, and the gap is worth naming: the Helm chart ships the controller and
    the console, not the API, so there is no template to pin the flag in. A
    caller that cannot make the guarantee should pass peer=None rather than a
    rewritten address -- weaker, but honest, where passing a rewritten one
    refuses valid traffic and protects nothing.
    """
    if not required():
        return resolve(headers)

    if peer is not None and not _is_loopback(peer):
        # Not "invalid credentials". A request that reached the app from off
        # the pod's loopback means the proxy is not the only listener, and no
        # header this request carries is worth reading.
        raise Unauthenticated(
            f"request from {peer} did not arrive over loopback: in "
            "TRIAGE_AUTH_MODE=proxy the app must bind loopback with the "
            "authenticating proxy in front of it. Either something reached "
            "the app around the proxy, or the server is rewriting the peer "
            "address from X-Forwarded-For -- uvicorn does that by default "
            "and needs --no-proxy-headers here"
        )

    principal = resolve(headers)
    if not principal.authenticated:
        raise Unauthenticated(
            "TRIAGE_AUTH_MODE=proxy but the request carried no identity "
            "header: the proxy is missing, or is not configured to pass one"
        )
    return principal


def startup_warning():
    """
    What to say at startup about the posture, or None if there is nothing.

    Returned rather than logged so a surface can also render it -- the console
    running unauthenticated is worth a banner on the page, not just a line in
    a log nobody has open.
    """
    if mode() == MODE_NONE:
        return (
            "TRIAGE_AUTH_MODE is 'none': this surface is unauthenticated and "
            "renders cluster state and pod logs. Bind it to loopback, or put "
            "an authenticating proxy in front and set TRIAGE_AUTH_MODE=proxy."
        )
    return None
