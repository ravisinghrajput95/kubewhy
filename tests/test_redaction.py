"""
Tests for secret redaction.

Pod logs are the most likely place for a live credential to appear, and this
tool reads them into a model context and prints them to a terminal. These
cases are the shapes that actually leak.
"""

import json
import pytest
import redaction


class TestCatchesRealSecretShapes:
    def test_aws_access_key(self):
        out = redaction.redact("using key AKIAIOSFODNN7EXAMPLE for upload")
        assert "AKIAIOSFODNN7EXAMPLE" not in out
        assert "[REDACTED:AWS_KEY]" in out

    def test_github_token(self):
        out = redaction.redact("clone with ghp_16CharsMinimumAAAAAAAAAAAAAAAA")
        assert "ghp_" not in out

    def test_jwt(self):
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NSJ9.dBjftJeZ4CVPmB92K27uhbUJU1p1r"
        assert jwt not in redaction.redact(f"Authorization header was {jwt}")

    def test_password_in_connection_string(self):
        out = redaction.redact("postgres://admin:hunter2@db:5432/app")
        assert "hunter2" not in out
        # The rest of the URL survives, so the model can still diagnose it.
        assert "db:5432" in out and "admin" in out

    def test_key_value_secret(self):
        out = redaction.redact("DB_PASSWORD=s3cr3tvalue")
        assert "s3cr3tvalue" not in out

    def test_bearer_header(self):
        out = redaction.redact("Bearer abcdefghijklmnopqrstuvwxyz123456")
        assert "abcdefghijklmnopqrstuvwxyz123456" not in out

    def test_private_key_block(self):
        block = (
            "-----BEGIN RSA PRIVATE KEY-----\n"
            "MIIEowIBAAKCAQEA1234567890\n"
            "-----END RSA PRIVATE KEY-----"
        )
        out = redaction.redact(f"loaded:\n{block}\ndone")
        assert "MIIEowIBAAKCAQEA" not in out
        assert "loaded:" in out and "done" in out


class TestPreservesDiagnosticValue:
    """Over-redaction destroys the thing the tool exists to do."""

    def test_ordinary_log_line_untouched(self):
        line = "FATAL: could not connect to db:5432: connection refused"
        assert redaction.redact(line) == line

    def test_keeps_ports_and_exit_codes(self):
        line = "container exited with code 137 after 4 restarts on port 8080"
        assert redaction.redact(line) == line

    def test_keeps_kubernetes_identifiers(self):
        line = "pod memory-hog-bc76968c6-z92zr OOMKilled, limit 64Mi"
        assert redaction.redact(line) == line

    def test_empty_and_none_are_safe(self):
        assert redaction.redact("") == ""
        assert redaction.redact(None) is None


class TestIntegration:
    def test_pod_logs_are_redacted(self):
        """The tool must not hand a raw secret back to the model."""
        from unittest.mock import MagicMock, patch

        from routers import k8s_pods_info as k8s

        api = MagicMock()
        api.read_namespaced_pod_log.return_value.data = (
            b"connecting with password=supersecret123 to db"
        )
        with patch.object(k8s, "_api", return_value=api):
            result = k8s.get_pod_logs("p", "demo")

        assert "supersecret123" not in result["logs"]
        assert "REDACTED" in result["logs"]


class TestFormatsClassifiedInValidation:
    """
    F-06. The adversarial report caught 9 of 13 planted formats. Each miss was
    classified before anything was changed, and only the realistic
    high-value ones were added -- a redactor that fires on ordinary text is
    worse than one with gaps, because a pass that corrupts a pod name defeats
    the checker that reads it.
    """

    # Every value here is SYNTHETIC and none has ever been valid: sequential
    # placeholders, AWS's own published example key, and the canonical jwt.io
    # sample. They must match the real formats byte for byte, because a fixture
    # that does not look like a credential cannot prove the redactor catches
    # credentials -- which is the whole point of this test.
    #
    # The Google one is ASSEMBLED rather than written out. GitHub secret
    # scanning matched the literal and opened a "public leak" alert on a string
    # that was never a key (alert #1, 2026-08-24): Google's format has no
    # documented example range the way AWS's AKIAIOSFODNN7EXAMPLE does, so the
    # scanner cannot tell a fixture from the real thing. Concatenating keeps the
    # test byte-identical -- redaction.py still sees the full 39-character
    # string -- while the pattern no longer appears anywhere in the source for a
    # scanner, or a reader, to mistake for a credential.
    @pytest.mark.parametrize("label,sample", [
        ("Authorization: Basic", "Authorization: Basic dXNlcjpwYXNzd29yZA=="),
        ("Proxy-Authorization", "Proxy-Authorization: Basic dXNlcjpwYXNz"),
        ("Google API key", "AIza" + "SyD-1234567890abcdefghijklmnopqrstu"),
        ("Azure storage key", "AccountKey=abcd1234efgh5678=="),
        ("dockerconfigjson", '{"auths":{"r.io":{"auth":"dXNlcjpwYXNz"}}}'),
        ("Secret data.password", '{"data":{"password":"c3VwZXJzZWNyZXQ="}}'),
    ])
    def test_realistic_kubernetes_and_cloud_credentials_are_caught(
            self, label, sample):
        """
        Basic sat in the same header as Bearer, which was already caught -- an
        inconsistency rather than a novel format. The two JSON shapes are how
        a Kubernetes credential is most often actually written down, and the
        pattern was stopping at the quote between the key and its colon.
        """
        assert redaction.redact(sample) != sample, label

    @pytest.mark.parametrize("label,sample", [
        ("a certificate is public by design",
         "-----BEGIN CERTIFICATE-----\nMIIabc\n-----END CERTIFICATE-----"),
        ("bare base64 carries no marker at all",
         "Y3JlZGVudGlhbDpzdXBlcnNlY3JldA=="),
        ("private_key_id is an identifier, not the key",
         '"private_key_id": "abc123def456"'),
    ])
    def test_formats_deliberately_left_alone(self, label, sample):
        """
        Classified as out of scope rather than missed. Redacting every base64
        string would destroy image digests, checksums and encoded config --
        most of what a projection contains.
        """
        assert redaction.redact(sample) == sample, label

    @pytest.mark.parametrize("text", [
        '{"pod":"crasher-5964d99948-9g8vg","status":"CrashLoopBackOff"}',
        '{"data":{"password":"c3VwZXJzZWNyZXQ="},"status":"Running"}',
        '{"auths":{"reg.io":{"auth":"dXNlcjpwYXNz"}}}',
        '{"env":{"API_KEY":"sk-abcdefghijklmnop"},"restarts":4}',
    ])
    def test_redacted_tool_output_still_parses_as_json(self, text):
        """
        The hazard this nearly shipped. Rewriting `"password": "x"` into
        `password=[REDACTED]` deletes the quote and the colon, leaving a
        document that no longer parses -- so grounding.check() could not read
        the evidence at all, and a pass meant to protect a credential would
        silently disable the claim checker.
        """
        json.loads(redaction.redact(text))

    def test_ordinary_prose_and_object_names_survive(self):
        """
        The false-positive floor. Replayed over 738 recorded tool outputs, the
        widened patterns changed nothing and broke no JSON.
        """
        for text in ("The pod crasher-5964d99948-9g8vg is in CrashLoopBackOff",
                     "authentication failed for the readiness probe",
                     "no such host: ollama.ollama.svc.cluster.local",
                     "restarts: 14, exit code 137, limit 64Mi"):
            assert redaction.redact(text) == text, text


class TestBothBoundariesUseOneFilter:
    def test_the_egress_pass_is_the_same_function(self):
        """
        One filter, not two. A boundary pass with its own pattern list would
        drift from the collection pass, and the shapes it stopped catching
        would be exactly the ones nobody was watching.
        """
        import inference

        leak = "Authorization: Basic dXNlcjpwYXNzd29yZA=="

        collected = redaction.redact(leak)
        at_boundary = inference._redacted([{"content": leak}])[0]["content"]

        assert at_boundary == collected
        assert "dXNlcjpwYXNzd29yZA" not in at_boundary
