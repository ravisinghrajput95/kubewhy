"""
Tests for secret redaction.

Pod logs are the most likely place for a live credential to appear, and this
tool reads them into a model context and prints them to a terminal. These
cases are the shapes that actually leak.
"""

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
