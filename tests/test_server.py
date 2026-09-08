"""Tests for the parts of the server that do not need a Docker daemon.

Everything here is reachable only because the Docker client is created lazily; it
used to be built at import time, so `import server` raised before any test ran.

The API-key middleware gets the most attention: it is the only thing standing between
the internet-facing proxy and a tool that rewrites the reverse proxy's config.
"""
import importlib
import os
import sys
import tarfile

import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server as srv  # noqa: E402

# --- pack_tar ------------------------------------------------------------------------


def test_pack_tar_round_trips_content():
    content = b"example.com {\n\trespond \"hi\"\n}\n"
    buf = srv.pack_tar("Caddyfile", content)
    with tarfile.open(fileobj=buf) as tar:
        names = tar.getnames()
        assert names == ["Caddyfile"]
        assert tar.extractfile("Caddyfile").read() == content


def test_pack_tar_sets_the_size_so_docker_accepts_it():
    """put_archive silently truncates a member whose header size is wrong."""
    content = b"x" * 4096
    with tarfile.open(fileobj=srv.pack_tar("Caddyfile", content)) as tar:
        assert tar.getmember("Caddyfile").size == len(content)


def test_pack_tar_is_rewound_ready_to_read():
    assert srv.pack_tar("Caddyfile", b"abc").tell() == 0


def test_pack_tar_handles_empty_content():
    with tarfile.open(fileobj=srv.pack_tar("Caddyfile", b"")) as tar:
        assert tar.extractfile("Caddyfile").read() == b""


# --- NormalizeMcpPath ----------------------------------------------------------------


def _echo_app():
    async def echo(request):
        return PlainTextResponse(request.scope["path"])

    return Starlette(routes=[Route("/mcp/", echo), Route("/other", echo)])


def test_normalize_rewrites_bare_mcp_to_the_trailing_slash_form():
    """Serving /mcp in-process instead of 307-ing to /mcp/.

    Starlette's redirect Location is root-relative, so behind a proxy that mounts
    this server under a prefix the client would follow it out of the prefix
    entirely.
    """
    client = TestClient(srv.NormalizeMcpPath(_echo_app()))
    r = client.get("/mcp", follow_redirects=False)
    assert r.status_code == 200, "should be served directly, not redirected"
    assert r.text == "/mcp/"


def test_normalize_leaves_other_paths_alone():
    client = TestClient(srv.NormalizeMcpPath(_echo_app()))
    assert client.get("/other").text == "/other"


# --- ApiKeyMiddleware ----------------------------------------------------------------


def _client_with_key(key):
    """Rebuild the module with MCP_API_KEY set, since it is read at import."""
    os.environ["MCP_API_KEY"] = key
    mod = importlib.reload(srv)
    app = Starlette(routes=[
        Route("/health", lambda r: PlainTextResponse("ok")),
        Route("/tools", lambda r: PlainTextResponse("secret")),
    ])
    app.add_middleware(mod.ApiKeyMiddleware)
    return TestClient(app)


@pytest.fixture(autouse=True)
def _restore_env():
    before = os.environ.get("MCP_API_KEY")
    yield
    if before is None:
        os.environ.pop("MCP_API_KEY", None)
    else:
        os.environ["MCP_API_KEY"] = before
    importlib.reload(srv)


def test_no_key_is_rejected():
    assert _client_with_key("s3cret").get("/tools").status_code == 401


def test_a_wrong_key_is_rejected():
    c = _client_with_key("s3cret")
    assert c.get("/tools", headers={"x-api-key": "nope"}).status_code == 401


def test_the_right_key_in_a_header_is_accepted():
    c = _client_with_key("s3cret")
    r = c.get("/tools", headers={"x-api-key": "s3cret"})
    assert r.status_code == 200 and r.text == "secret"


def test_health_is_reachable_without_a_key():
    """The health endpoint has to answer an unauthenticated prober."""
    assert _client_with_key("s3cret").get("/health").status_code == 200


def test_an_empty_configured_key_disables_auth_entirely():
    """Deploying with MCP_API_KEY unset leaves the server WIDE OPEN.

    Pinned so it cannot change by accident, and called out in the README: this is a
    tool that rewrites the reverse proxy's configuration.
    """
    assert _client_with_key("").get("/tools").status_code == 200


def test_the_key_is_also_accepted_in_a_query_string():
    """Pinned, but see "Known limitations" in the README.

    A key in the URL ends up in the proxy's access log and in browser history, where
    a header would not. It is supported because some MCP clients cannot set headers.
    """
    c = _client_with_key("s3cret")
    assert c.get("/tools?api_key=s3cret").status_code == 200


# --- error handling ------------------------------------------------------------------


def test_a_missing_caddyfile_reports_an_error_instead_of_crashing_the_handler(monkeypatch):
    """Regression: the FileNotFoundError branch referenced CADDYFILE_PATH.

    The config variable was renamed to CADDY_CONTAINER_CONFIG everywhere else, so
    this one line raised NameError from inside the except block. The handler written
    to explain the problem was the thing that broke, and only at the moment it was
    needed.
    """
    import asyncio

    def _boom(*_a, **_k):
        raise FileNotFoundError("no Caddyfile")

    monkeypatch.setattr(srv, "get_container", _boom)
    result = asyncio.run(srv.call_tool("caddy_read_config", {}))
    text = result[0].text
    assert "Error" in text
    assert srv.CADDY_CONTAINER_CONFIG in text
