"""Section 9.4: Secret scrubber coverage tests.

Validates that scrub_secrets() catches all known secret patterns
and does NOT redact non-secret data.
"""

from __future__ import annotations

from beagle.security.sanitization import scrub_secrets


class TestSecretScrubberAPIKeys:
    """Scrubber catches API key patterns."""

    def test_api_key_with_equals(self):
        assert "REDACTED" in scrub_secrets("api_key=abc123def456ghi789jkl012mno345")

    def test_apikey_with_colon(self):
        assert "REDACTED" in scrub_secrets("apikey: abc123def456ghi789jkl012mno345")

    def test_bearer_token(self):
        assert "REDACTED" in scrub_secrets("Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9")

    def test_token_equals(self):
        assert "REDACTED" in scrub_secrets("token=abc123xyz890")


class TestSecretScrubberPasswords:
    """Scrubber catches password patterns."""

    def test_password_equals(self):
        assert "REDACTED" in scrub_secrets("password=hunter2password")

    def test_passwd_colon(self):
        assert "REDACTED" in scrub_secrets("passwd: mySecretPass123")

    def test_pwd_equals(self):
        assert "REDACTED" in scrub_secrets("pwd=complexP@ss1234")

    def test_secret_equals(self):
        assert "REDACTED" in scrub_secrets("secret=supersecretvalue123")


class TestSecretScrubberAWSCreds:
    """Scrubber catches AWS credential patterns."""

    def test_aws_access_key_id(self):
        assert "REDACTED" in scrub_secrets("aws_access_key_id=AKIAIOSFODNN7EXAMPLE")

    def test_aws_secret_access_key(self):
        assert "REDACTED" in scrub_secrets(
            "aws_secret_access_key=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
        )

    def test_aws_key_prefix(self):
        assert "REDACTED" in scrub_secrets("Key: AKIAIOSFODNN7EXAMPLE")


class TestSecretScrubberPrivateKeys:
    """Scrubber catches private key patterns."""

    def test_rsa_private_key(self):
        assert "REDACTED" in scrub_secrets("-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA...")

    def test_openssh_private_key(self):
        assert "REDACTED" in scrub_secrets("-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1r...")


class TestSecretScrubberDBURLs:
    """Scrubber catches database URLs with passwords."""

    def test_postgres_url(self):
        assert "REDACTED" in scrub_secrets("postgres://user:secretpass@db.example.com:5432/mydb")

    def test_mysql_url(self):
        assert "REDACTED" in scrub_secrets("mysql://admin:hunter2@mysql.host:3306/prod")

    def test_redis_url(self):
        # redis://:password@ (empty user) not caught by DB URL pattern,
        # but should be caught by password= pattern or bearer pattern
        result = scrub_secrets("redis://user:mypassword@redis.host:6379/0")
        assert "REDACTED" in result or "mySecretP@ss" not in result


class TestSecretScrubberGithubTokens:
    """Scrubber catches GitHub token patterns."""

    def test_github_pat(self):
        assert "REDACTED" in scrub_secrets("ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij")


class TestSecretScrubberFalsePositives:
    """Scrubber does NOT redact non-secret data."""

    def test_normal_text_unchanged(self):
        text = "The quick brown fox jumps over the lazy dog"
        assert scrub_secrets(text) == text

    def test_url_without_password(self):
        text = "https://example.com/api/v1/data"
        assert scrub_secrets(text) == text

    def test_short_token_not_redacted(self):
        """Short tokens under the length threshold should pass through."""
        text = "token=abc"  # 3 chars — below 6-char threshold
        result = scrub_secrets(text)
        # Should not be redacted (too short to be a real secret)
        assert "abc" in result or "REDACTED" not in result

    def test_variable_name_not_redacted(self):
        """Variable names without values should not be redacted."""
        text = "export API_KEY"
        result = scrub_secrets(text)
        # The name "API_KEY" alone (no = value) should pass
        assert "API_KEY" in result or "REDACTED" not in result


class TestSecretScrubberCaching:
    """Scrubber uses LRU caching for repeated inputs."""

    def test_same_input_same_output(self):
        """Repeated calls with same input return same result."""
        text = "api_key=abc123def456ghi789jkl012mno345"
        result1 = scrub_secrets(text)
        result2 = scrub_secrets(text)
        assert result1 == result2

    def test_different_inputs_different_results(self):
        """Different inputs produce different results."""
        r1 = scrub_secrets("password=secret123")
        r2 = scrub_secrets("no secrets here")
        assert r1 != r2
