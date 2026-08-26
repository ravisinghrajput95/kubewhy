"""
The authentication boundary for the console and the API.

Weighted deliberately towards the refusals. A test suite for an authenticator
that mostly proves valid identities are accepted is proving the easy half; the
half worth having is that the absent, the empty and the off-path request are
all refused.
"""

import pytest

import identity


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """No inherited configuration -- every test states its own posture."""
    for name in (
        "TRIAGE_AUTH_MODE",
        "TRIAGE_AUTH_EMAIL_HEADER",
        "TRIAGE_AUTH_USER_HEADER",
        "TRIAGE_AUTH_GROUPS_HEADER",
    ):
        monkeypatch.delenv(name, raising=False)


def proxy(monkeypatch):
    monkeypatch.setenv("TRIAGE_AUTH_MODE", "proxy")


# --- mode ------------------------------------------------------------------


def test_mode_defaults_to_none():
    assert identity.mode() == identity.MODE_NONE
    assert identity.required() is False


def test_mode_proxy(monkeypatch):
    proxy(monkeypatch)
    assert identity.mode() == identity.MODE_PROXY
    assert identity.required() is True


def test_mode_is_case_and_space_insensitive(monkeypatch):
    monkeypatch.setenv("TRIAGE_AUTH_MODE", "  PROXY ")
    assert identity.mode() == identity.MODE_PROXY


def test_unknown_mode_raises_rather_than_defaulting_to_none(monkeypatch):
    """
    A typo in the setting that enables authentication must not disable it.

    The tempting implementation -- treat anything unrecognised as `none` --
    turns `TRIAGE_AUTH_MODE=proxxy` into an open console with no error.
    """
    monkeypatch.setenv("TRIAGE_AUTH_MODE", "proxxy")
    with pytest.raises(ValueError, match="proxxy"):
        identity.mode()


# --- resolve ---------------------------------------------------------------


def test_no_headers_is_anonymous():
    who = identity.resolve({})
    assert who is identity.ANONYMOUS
    assert who.authenticated is False
    assert who.label() == "anonymous"


def test_email_header():
    who = identity.resolve({"X-Forwarded-Email": "sre@example.com"})
    assert who.authenticated
    assert who.email == "sre@example.com"
    assert who.source == "proxy"
    assert who.label() == "sre@example.com"


def test_auth_request_family_is_read_too():
    """oauth2-proxy spells it differently in its auth_request shape."""
    who = identity.resolve({"X-Auth-Request-Email": "sre@example.com"})
    assert who.email == "sre@example.com"


def test_header_lookup_is_case_insensitive():
    """HTTP header names are, and not every mapping handed here is."""
    who = identity.resolve({"x-FORWARDED-eMaIl": "sre@example.com"})
    assert who.email == "sre@example.com"


def test_username_alone_authenticates():
    """Some providers release no email claim at all."""
    who = identity.resolve({"X-Forwarded-User": "ravi"})
    assert who.authenticated
    assert who.name == "ravi"
    assert who.label() == "ravi"


def test_empty_email_header_does_not_authenticate_as_empty_string():
    """
    oauth2-proxy forwards an empty X-Forwarded-Email when the provider
    released no email claim. Present-but-empty is not an identity.
    """
    who = identity.resolve({"X-Forwarded-Email": ""})
    assert who.authenticated is False


def test_empty_email_falls_through_to_the_username():
    who = identity.resolve({"X-Forwarded-Email": "  ", "X-Forwarded-User": "ravi"})
    assert who.name == "ravi"
    assert who.label() == "ravi"


def test_email_is_preferred_over_username_in_the_label():
    who = identity.resolve(
        {"X-Forwarded-Email": "sre@example.com", "X-Forwarded-User": "ravi"}
    )
    assert who.label() == "sre@example.com"
    assert who.name == "ravi"


def test_groups_are_split():
    who = identity.resolve(
        {"X-Forwarded-Email": "sre@example.com",
         "X-Forwarded-Groups": "platform, sre ,"}
    )
    assert who.groups == ("platform", "sre")


def test_groups_without_an_identity_do_not_authenticate():
    assert identity.resolve({"X-Forwarded-Groups": "platform"}).authenticated is False


def test_control_characters_are_stripped_from_an_identity():
    """
    A principal name reaches a log line and, next, an audit record. A newline
    in it is a forged record.
    """
    who = identity.resolve(
        {"X-Forwarded-Email": "sre@example.com\nlevel=INFO forged=true"}
    )
    assert "\n" not in who.email
    assert who.email == "sre@example.comlevel=INFO forged=true"


def test_an_identity_is_length_capped():
    who = identity.resolve({"X-Forwarded-Email": "a" * 5000})
    assert len(who.email) == 320


def test_a_custom_header_can_be_named(monkeypatch):
    monkeypatch.setenv("TRIAGE_AUTH_EMAIL_HEADER", "X-Goog-Authenticated-User-Email")
    who = identity.resolve({"X-Goog-Authenticated-User-Email": "sre@example.com"})
    assert who.email == "sre@example.com"


def test_naming_a_custom_header_replaces_the_defaults(monkeypatch):
    """
    Otherwise pointing the deployment at one proxy's header would leave the
    other proxy's header still accepted, which is a bypass rather than a
    convenience.
    """
    monkeypatch.setenv("TRIAGE_AUTH_EMAIL_HEADER", "X-Goog-Authenticated-User-Email")
    assert identity.resolve({"X-Forwarded-Email": "sre@example.com"}).authenticated is False


# --- require ---------------------------------------------------------------


def test_require_in_none_mode_allows_anonymous():
    """The laptop case: loopback, no proxy, and the OS is the access control."""
    assert identity.require({}).authenticated is False


def test_require_in_proxy_mode_refuses_a_request_with_no_identity(monkeypatch):
    proxy(monkeypatch)
    with pytest.raises(identity.Unauthenticated) as caught:
        identity.require({})
    assert "no identity header" in caught.value.reason


def test_require_in_proxy_mode_refuses_an_empty_identity(monkeypatch):
    proxy(monkeypatch)
    with pytest.raises(identity.Unauthenticated):
        identity.require({"X-Forwarded-Email": ""})


def test_require_in_proxy_mode_accepts_a_proxied_identity(monkeypatch):
    proxy(monkeypatch)
    who = identity.require({"X-Forwarded-Email": "sre@example.com"})
    assert who.label() == "sre@example.com"


@pytest.mark.parametrize("peer", ["127.0.0.1", "::1", "::ffff:127.0.0.1", "localhost"])
def test_require_accepts_the_loopback_peers_a_sidecar_produces(monkeypatch, peer):
    proxy(monkeypatch)
    who = identity.require({"X-Forwarded-Email": "sre@example.com"}, peer=peer)
    assert who.authenticated


@pytest.mark.parametrize("peer", ["10.244.0.7", "192.168.1.4", "::ffff:10.0.0.1"])
def test_a_valid_header_from_off_loopback_is_still_refused(monkeypatch, peer):
    """
    The one that matters. In proxy mode the header is trusted *because* only
    the sidecar can reach the app; a request that arrived from anywhere else
    has proved that premise false, and its headers are worth nothing however
    well-formed they are.
    """
    proxy(monkeypatch)
    with pytest.raises(identity.Unauthenticated) as caught:
        identity.require({"X-Forwarded-Email": "attacker@example.com"}, peer=peer)
    assert "bypassed the authenticating proxy" in caught.value.reason


def test_off_loopback_is_not_checked_when_no_proxy_is_claimed(monkeypatch):
    """
    `none` means nothing is in front and the operator has said so. Refusing
    off-loopback requests there would break every laptop that binds 0.0.0.0
    on purpose, and would be enforcing a policy nobody asked for.
    """
    assert identity.require({}, peer="10.244.0.7").authenticated is False


def test_the_two_refusals_are_distinguishable(monkeypatch):
    """
    A missing header is a misconfigured proxy; an off-loopback request is
    someone reaching the backend directly. Different pages, different night.
    """
    proxy(monkeypatch)
    with pytest.raises(identity.Unauthenticated) as missing:
        identity.require({}, peer="127.0.0.1")
    with pytest.raises(identity.Unauthenticated) as bypass:
        identity.require({"X-Forwarded-Email": "a@b.c"}, peer="10.0.0.1")
    assert missing.value.reason != bypass.value.reason


# --- posture ---------------------------------------------------------------


def test_startup_warning_in_none_mode_names_what_is_exposed():
    warning = identity.startup_warning()
    assert warning and "pod logs" in warning


def test_no_startup_warning_in_proxy_mode(monkeypatch):
    proxy(monkeypatch)
    assert identity.startup_warning() is None


def test_principal_repr_does_not_leak_groups_into_a_log_line():
    """repr lands in logs; it should be short and name the person."""
    who = identity.Principal(email="sre@example.com", groups=("a",) * 50, source="proxy")
    assert repr(who) == "Principal('sre@example.com', source='proxy')"
