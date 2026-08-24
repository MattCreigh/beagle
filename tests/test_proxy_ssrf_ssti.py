"""WP-4: SSRF guard in the Ollama proxy and SSTI autoescape in templates."""

from __future__ import annotations

from beagle.bridges.ollama_cloud_proxy import ProxyHandler, _normalize_model_name
from beagle.config import toml_template, yaml_template


class _FakeHandler(ProxyHandler):
    """Minimal ProxyHandler that records send_error calls."""

    def __init__(self):
        self._sent_error = None
        self._sent_status = None
        self._sent_headers = {}
        self._sent_body = b""
        self.path = "/v1/chat/completions"
        self.headers = {}
        self._upstream = "https://ollama.com"
        self._api_key = "test-key"
        self.rfile = None
        self.wfile = _FakeWriter()

    def send_error(self, code, message=None):
        self._sent_error = (code, message)

    def send_response(self, code):
        self._sent_status = code

    def send_header(self, k, v):
        self._sent_headers[k] = v

    def end_headers(self):
        pass


class _FakeWriter:
    def write(self, data):
        return len(data)


def test_proxy_rejects_absolute_url_path():
    """B8: an absolute-URL request target is rejected before any upstream call."""
    handler = _FakeHandler()
    handler.path = "http://evil.example.com/v1/chat/completions"
    handler._proxy("POST")
    assert handler._sent_error is not None
    assert handler._sent_error[0] == 400


def test_proxy_rejects_double_slash_path():
    """B8: a //-prefixed path is rejected."""
    handler = _FakeHandler()
    handler.path = "//evil.example.com/v1/chat/completions"
    handler._proxy("POST")
    assert handler._sent_error is not None
    assert handler._sent_error[0] == 400


def test_proxy_accepts_normal_path(monkeypatch):
    """B8: a normal single-slash path is accepted (no early rejection)."""
    from urllib.error import URLError

    import beagle.bridges.ollama_cloud_proxy as proxy

    def _fake_urlopen(*args, **kwargs):
        raise URLError("no network in test")

    monkeypatch.setattr(proxy, "urlopen", _fake_urlopen)

    handler = _FakeHandler()
    handler.path = "/v1/models"
    handler.headers = {"Content-Length": "0"}
    handler._proxy("GET")
    # No 400 rejection; the request proceeds to the upstream call path.
    assert handler._sent_error is None


def test_toml_template_autoescape_enabled():
    """B9: the TOML template Environment enables autoescape."""
    env = toml_template._build_environment()
    assert env.autoescape is not None
    # A template that injects HTML must be escaped.
    rendered = env.from_string("{{ value }}").render(value="<script>alert(1)</script>")
    assert "&lt;script&gt;" in rendered


def test_yaml_template_autoescape_enabled():
    """B9: the YAML template Environment enables autoescape."""
    env = yaml_template._build_environment()
    assert env.autoescape is not None
    rendered = env.from_string("{{ value }}").render(value="<b>hi</b>")
    assert "&lt;b&gt;" in rendered


def test_normalize_model_name_date_tag():
    """M6: date-tagged model names normalize to the canonical key."""
    assert _normalize_model_name("deepseek-v4-flash:0731-cloud") == "deepseek-v4-flash:cloud"
    assert _normalize_model_name("gemma4:31b-cloud") == "gemma4:31b-cloud"
    assert _normalize_model_name("minimax-m3:cloud") == "minimax-m3:cloud"
