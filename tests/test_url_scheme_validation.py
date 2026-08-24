"""SP-10: contract for security.validation.validate_http_url.

beagle-spotless-phase2, work package SP-10 (B310). Four call sites passed a
URL to urllib.request.urlopen without checking its scheme. urlopen is not an
HTTP client — it dispatches on the scheme — so a URL that arrives as
``file:///etc/shadow`` is a local-file read wearing the shape of an HTTP
request, and ``ftp://`` is an outbound FTP fetch.

Three of the four URLs were literals or built from loopback constants, but the
fourth (the proxy's upstream) inherits its scheme from configuration, which is
where a misconfiguration or an injected value would actually enter.
"""

from __future__ import annotations

import pytest

from beagle.security.validation import validate_http_url


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:9999/v1/models",
        "https://ollama.com/api/tags",
        "HTTP://example.com/x",
        "HTTPS://example.com/x",
        "http://user:pw@example.com:8080/path?q=1#f",
    ],
)
def test_http_and_https_are_accepted(url: str) -> None:
    assert validate_http_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/shadow",
        "file://localhost/etc/passwd",
        "ftp://example.com/x",
        "gopher://example.com/x",
        "data:text/plain;base64,aGk=",
        "jar:file:///tmp/x.jar!/y",
    ],
)
def test_non_http_schemes_are_refused(url: str) -> None:
    with pytest.raises(ValueError, match="scheme"):
        validate_http_url(url)


def test_scheme_check_is_case_insensitive() -> None:
    """A scheme is case-insensitive per RFC 3986; FILE:// must not slip through."""
    with pytest.raises(ValueError, match="scheme"):
        validate_http_url("FILE:///etc/shadow")


@pytest.mark.parametrize("url", ["http://", "https://", "/just/a/path", "example.com/x"])
def test_urls_without_a_host_are_refused(url: str) -> None:
    with pytest.raises(ValueError):
        validate_http_url(url)


def test_error_names_the_offending_scheme() -> None:
    with pytest.raises(ValueError) as excinfo:
        validate_http_url("ftp://example.com/x")
    assert "ftp" in str(excinfo.value)


def test_returns_the_url_unchanged_for_in_place_wrapping() -> None:
    """Call sites wrap an existing argument, so the value must pass through intact."""
    url = "https://example.com/a/b?c=d"
    assert validate_http_url(url) is url
