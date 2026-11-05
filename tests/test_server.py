"""Tests for the parts of the server that do not need a Docker daemon.

Everything here is reachable only because the Docker client is created lazily; it
used to be built at import time, so `import server` raised before any test ran.

The API-key middleware gets the most attention: it is the only thing standing between
the internet-facing proxy and a tool that rewrites the reverse proxy's config.
"""
import asyncio
import contextlib
import importlib
import json
import os
import sys
import tarfile
from pathlib import Path

import docker
import pytest
from docker.models.containers import ExecResult
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


class FakeContainer:
    """Stands in for a docker-py Container. Records calls; no daemon needed."""

    def __init__(self, exec_results=None, put_archive_error=None):
        self.exec_results = exec_results or {}
        self.put_archive_error = put_archive_error
        self.put_archive_calls = []
        self.exec_calls = []

    def exec_run(self, cmd, **_kwargs):
        self.exec_calls.append(cmd)
        return self.exec_results.get(cmd[0], ExecResult(0, b""))

    def put_archive(self, path, data):
        self.put_archive_calls.append(path)
        if self.put_archive_error is not None:
            raise self.put_archive_error
        return True


def _call(name, arguments=None):
    return asyncio.run(srv.call_tool(name, arguments or {}))[0].text


def test_a_failed_cat_is_reported_as_an_error_not_returned_as_the_config(monkeypatch):
    """exec_run does not raise when cat fails: it returns a non-zero exit code.

    Returning that output as the config hands the caller an error message to edit
    and write back over the live Caddyfile.
    """
    cat_error = b"cat: can't open '/etc/caddy/Caddyfile': No such file or directory\n"
    fake = FakeContainer(exec_results={"cat": ExecResult(1, cat_error)})
    monkeypatch.setattr(srv, "get_container", lambda: fake)

    text = _call("caddy_read_config")

    assert text.startswith("Error:")
    assert "CADDY_CONTAINER_CONFIG" in text
    assert srv.CADDY_CONTAINER_CONFIG in text


def test_a_successful_cat_returns_the_config_unchanged(monkeypatch):
    config = b"example.com {\n\trespond \"hi\"\n}\n"
    fake = FakeContainer(exec_results={"cat": ExecResult(0, config)})
    monkeypatch.setattr(srv, "get_container", lambda: fake)

    assert _call("caddy_read_config") == config.decode()


def _missing_path():
    return docker.errors.NotFound("Could not find the file /etc/caddy in container caddy")


def test_a_missing_config_directory_on_write_names_the_path_not_the_container(monkeypatch):
    """Docker answers put_archive to a missing directory with a 404 (NotFound)."""
    fake = FakeContainer(put_archive_error=_missing_path())
    monkeypatch.setattr(srv, "get_container", lambda: fake)

    text = _call("caddy_write_config", {"config": "example.com {\n}\n"})

    assert text.startswith("Error:")
    assert "CADDY_CONTAINER_CONFIG" in text
    assert os.path.dirname(srv.CADDY_CONTAINER_CONFIG) in text
    assert "CADDY_CONTAINER env var" not in text


def test_a_missing_tmp_on_validate_names_tmp_not_the_container(monkeypatch):
    fake = FakeContainer(put_archive_error=_missing_path())
    monkeypatch.setattr(srv, "get_container", lambda: fake)

    text = _call("caddy_validate", {"config": "example.com {\n}\n"})

    assert text.startswith("Error:")
    assert "/tmp" in text
    assert "CADDY_CONTAINER env var" not in text


@pytest.mark.parametrize("tool", ["caddy_write_config", "caddy_validate"])
def test_container_gone_at_put_archive_is_not_reported_as_a_missing_directory(monkeypatch, tool):
    """Docker also answers put_archive with a 404 when the container itself is gone.

    That happens if the container is removed or recreated between get_container()
    and put_archive. It must not send the user to check the config path.
    """
    fake = FakeContainer(put_archive_error=docker.errors.NotFound("No such container: abc123"))
    monkeypatch.setattr(srv, "get_container", lambda: fake)

    text = _call(tool, {"config": "x"})

    assert text == (
        f"Error: container '{srv.CADDY_CONTAINER}' not found. Check CADDY_CONTAINER env var."
    ), text


def test_a_missing_container_still_reports_container_not_found(monkeypatch):
    class FakeContainers:
        def get(self, _name):
            raise docker.errors.NotFound("No such container: caddy")

    class FakeClient:
        containers = FakeContainers()

    monkeypatch.setattr(srv, "get_docker_client", lambda: FakeClient())

    for tool, args in [
        ("caddy_read_config", {}),
        ("caddy_write_config", {"config": "x"}),
        ("caddy_validate", {"config": "x"}),
    ]:
        text = _call(tool, args)
        assert text == (
            f"Error: container '{srv.CADDY_CONTAINER}' not found. Check CADDY_CONTAINER env var."
        ), tool


# --- legacy SSE endpoint -------------------------------------------------------------


def test_sse_session_ending_does_not_raise_or_return_500(monkeypatch):
    """Starlette calls whatever a Route endpoint returns as the ASGI response.

    handle_sse returned None, so every legacy-SSE session that ended raised
    `TypeError: 'NoneType' object is not callable` and answered 500.
    """
    class FakeSseTransport:
        def __init__(self, endpoint):
            self.endpoint = endpoint

        @contextlib.asynccontextmanager
        async def connect_sse(self, scope, receive, send):
            yield (None, None)

        async def handle_post_message(self, scope, receive, send):
            raise AssertionError("not used")

    async def fake_run(*_args, **_kwargs):
        return None

    monkeypatch.setattr(srv, "SseServerTransport", FakeSseTransport)
    monkeypatch.setattr(srv.server, "run", fake_run)

    # raise_server_exceptions defaults to True, so the TypeError would surface here.
    with TestClient(srv.create_app()) as client:
        r = client.get("/sse")
    assert r.status_code == 200


# --- README matches the tools ---------------------------------------------------------


def test_readme_tools_table_matches_what_caddy_status_returns(monkeypatch):
    """caddy_status returns `started_at`, not an uptime, so the README must not say uptime."""
    class FakeImage:
        tags = ["caddy:2"]

    class StatusContainer:
        name = "caddy"
        status = "running"
        short_id = "abc123"
        image = FakeImage()
        attrs = {"State": {"Running": True, "StartedAt": "2026-11-05T00:00:00Z"}, "RestartCount": 0}

        def reload(self):
            pass

    monkeypatch.setattr(srv, "get_container", lambda: StatusContainer())
    info = json.loads(_call("caddy_status"))
    assert "uptime" not in info and "started_at" in info

    readme = Path(__file__).resolve().parent.parent / "README.md"
    row = next(line for line in readme.read_text().splitlines() if line.startswith("| `caddy_status`"))
    assert "uptime" not in row.lower()
    assert "start time" in row.lower()
