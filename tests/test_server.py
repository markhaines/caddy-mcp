"""Unit tests for the Caddy MCP server. No Docker daemon required."""

import io
import re
import tarfile

import pytest

import server
from stubs import StubContainer

CONFIG_PATH = server.CADDY_CONTAINER_CONFIG          # /etc/caddy/Caddyfile
CONFIG_DIR = "/etc/caddy"
LIVE_CONFIG = "example.com {\n\treverse_proxy 127.0.0.1:8080\n}\n"


@pytest.fixture
def container(monkeypatch):
    """A stub Caddy container wired in as the server's container."""
    stub = StubContainer(files={CONFIG_PATH: LIVE_CONFIG.encode("utf-8")})
    monkeypatch.setattr(server, "get_container", lambda: stub)
    return stub


def use(monkeypatch, stub):
    monkeypatch.setattr(server, "get_container", lambda: stub)
    return stub


def text_of(result):
    assert len(result) == 1
    return result[0].text


# ---------------------------------------------------------------------------
# pack_tar
# ---------------------------------------------------------------------------

def test_pack_tar_round_trip():
    """One member, named by basename only, byte-identical including the
    trailing newline and non-ASCII characters."""
    content = "site.example {\n\t# café ✓ naïve\n\treverse_proxy 10.0.0.1:80\n}\n".encode("utf-8")

    buf = server.pack_tar("Caddyfile", content)

    with tarfile.open(fileobj=io.BytesIO(buf.getvalue())) as tar:
        members = tar.getmembers()
        assert len(members) == 1
        member = members[0]
        # A full path here would write to the wrong location inside the container.
        assert member.name == "Caddyfile"
        assert "/" not in member.name
        assert member.size == len(content)
        assert tar.extractfile(member).read() == content


# ---------------------------------------------------------------------------
# TASK 1 regression: a failed `cat` must never be returned as config content
# ---------------------------------------------------------------------------

def test_read_config_surfaces_error_when_cat_fails(monkeypatch):
    stderr = "cat: /etc/caddy/Caddyfile: No such file or directory"
    use(monkeypatch, StubContainer(files={}))  # nothing at CONFIG_PATH

    out = text_of(server.run_tool("caddy_read_config", {}))

    assert out.startswith("Error"), out
    # The killer: the stderr string must not come back as if it were the config.
    assert out.strip() != stderr
    assert "No config content was returned" in out
    assert CONFIG_PATH in out
    assert server.CADDY_CONTAINER in out


def test_read_config_error_on_nonzero_exit_even_with_plausible_output(monkeypatch):
    """Exit code decides, not how config-shaped the output looks."""
    stub = use(monkeypatch, StubContainer(files={CONFIG_PATH: LIVE_CONFIG.encode()}))
    stub.fail["cat"] = (1, "example.com {\n\treverse_proxy 127.0.0.1:8080\n}\n")

    out = text_of(server.run_tool("caddy_read_config", {}))

    assert out.startswith("Error"), out


def test_read_config_returns_content_on_success(container):
    out = text_of(server.run_tool("caddy_read_config", {}))
    assert out == LIVE_CONFIG


# ---------------------------------------------------------------------------
# TASK 2: caddy_write_config as a guarded transaction
# ---------------------------------------------------------------------------

def test_write_refuses_when_validation_fails(monkeypatch):
    stub = use(monkeypatch, StubContainer(
        files={CONFIG_PATH: LIVE_CONFIG.encode()},
        validate_ok=False,
        validate_output="Caddyfile:3: unrecognized directive: revrse_proxy",
    ))

    out = text_of(server.run_tool("caddy_write_config", {"config": "broken {"}))

    assert out.startswith("Error"), out
    assert "unrecognized directive" in out
    # Nothing may have been written towards the live config directory.
    assert [c for c in stub.put_archive_calls if c["path"] == CONFIG_DIR] == []
    assert stub.files[CONFIG_PATH] == LIVE_CONFIG.encode()
    # And no backup was taken, because we never got that far.
    assert _backups(stub) == []


def test_write_validates_the_exact_bytes_it_commits(container):
    new_config = "new.example {\n\trespond \"ok\"\n}\n"

    server.run_tool("caddy_write_config", {"config": new_config})

    staged_for_validation = [
        c for c in container.put_archive_calls if c["path"] == "/tmp"
    ]
    assert len(staged_for_validation) == 1
    assert _tar_bytes(staged_for_validation[0]["data"]) == new_config.encode("utf-8")
    assert container.files[CONFIG_PATH] == new_config.encode("utf-8")


def test_write_backs_up_then_replaces_atomically(container):
    new_config = "new.example {\n\trespond \"ok\"\n}\n"

    out = text_of(server.run_tool("caddy_write_config", {"config": new_config}))

    assert out.startswith("✓"), out
    backups = _backups(container)
    assert len(backups) == 1
    assert re.fullmatch(
        re.escape(CONFIG_PATH) + r"\.bak-\d{8}T\d{6}Z", backups[0]
    ), backups[0]
    assert container.files[backups[0]] == LIVE_CONFIG.encode()
    assert container.files[CONFIG_PATH] == new_config.encode("utf-8")
    assert backups[0] in out

    # Staged under a unique temp name in the target directory, renamed over the
    # target, and never left behind.
    staged = [c for c in container.put_archive_calls if c["path"] == CONFIG_DIR]
    assert len(staged) == 1
    moves = [c for c in container.exec_calls if c[0] == "mv"]
    assert len(moves) == 1
    temp_path = moves[0][-2]
    assert temp_path.startswith(f"{CONFIG_DIR}/.Caddyfile.tmp-")
    assert temp_path != CONFIG_PATH
    assert moves[0][-1] == CONFIG_PATH
    assert temp_path not in container.files


def test_write_prunes_old_backups(container):
    server.run_tool("caddy_write_config", {"config": "a.example {\n}\n"})

    prunes = [c for c in container.exec_calls if c[0] == "sh"]
    assert len(prunes) == 1
    script = prunes[0][-1]
    assert f"tail -n +{server.BACKUP_KEEP + 1}" in script
    # Only our own timestamped scheme is ever matched for deletion.
    assert server.BACKUP_SUFFIX_GLOB in script


def test_write_rolls_back_when_put_archive_returns_false(monkeypatch):
    """put_archive returns a bool; a False here used to be reported as success."""
    stub = use(monkeypatch, StubContainer(
        files={CONFIG_PATH: LIVE_CONFIG.encode()},
        put_archive_fail_paths={CONFIG_DIR},
    ))

    out = text_of(server.run_tool("caddy_write_config", {"config": "a.example {\n}\n"}))

    assert out.startswith("Error"), out
    assert "put_archive" in out
    assert "Rollback: restored" in out
    assert stub.files[CONFIG_PATH] == LIVE_CONFIG.encode()


def test_write_fails_cleanly_when_staging_for_validation_is_rejected(monkeypatch):
    """Failing before the backup: nothing to roll back, live config untouched."""
    stub = use(monkeypatch, StubContainer(
        files={CONFIG_PATH: LIVE_CONFIG.encode()},
        put_archive_ok=False,
    ))

    out = text_of(server.run_tool("caddy_write_config", {"config": "a.example {\n}\n"}))

    assert out.startswith("Error"), out
    assert stub.files[CONFIG_PATH] == LIVE_CONFIG.encode()
    assert _backups(stub) == []


def test_write_rolls_back_when_the_rename_fails(monkeypatch):
    stub = use(monkeypatch, StubContainer(files={CONFIG_PATH: LIVE_CONFIG.encode()}))
    stub.fail["mv"] = (1, "mv: can't rename: Read-only file system")

    out = text_of(server.run_tool("caddy_write_config", {"config": "a.example {\n}\n"}))

    assert out.startswith("Error"), out
    assert "Read-only file system" in out          # the original failure
    assert "Rollback: restored" in out             # and the rollback outcome
    assert stub.files[CONFIG_PATH] == LIVE_CONFIG.encode()


def test_write_refuses_when_the_backup_cannot_be_taken(monkeypatch):
    stub = use(monkeypatch, StubContainer(files={CONFIG_PATH: LIVE_CONFIG.encode()}))
    stub.fail["cp"] = (1, "cp: can't create: Permission denied")

    out = text_of(server.run_tool("caddy_write_config", {"config": "a.example {\n}\n"}))

    assert out.startswith("Error"), out
    assert "back up" in out
    assert [c for c in stub.put_archive_calls if c["path"] == CONFIG_DIR] == []
    assert stub.files[CONFIG_PATH] == LIVE_CONFIG.encode()


def test_write_reports_missing_config_argument(container):
    out = text_of(server.run_tool("caddy_write_config", {}))

    assert "Missing required argument 'config'" in out
    assert container.put_archive_calls == []


def test_validate_reports_missing_config_argument(container):
    assert "Missing required argument 'config'" in text_of(
        server.run_tool("caddy_validate", {})
    )


# ---------------------------------------------------------------------------
# TASK 5: validate staging, log clamping, status
# ---------------------------------------------------------------------------

def test_validate_uses_a_unique_path_and_cleans_up(container):
    server.run_tool("caddy_validate", {"config": "a {\n}\n"})
    server.run_tool("caddy_validate", {"config": "b {\n}\n"})

    staged = [c["data"] for c in container.put_archive_calls if c["path"] == "/tmp"]
    names = [_tar_name(d) for d in staged]
    assert len(set(names)) == 2, names
    removed = [c[-1] for c in container.exec_calls if c[0] == "rm"]
    assert sorted(removed) == sorted(f"/tmp/{n}" for n in names)
    assert not [p for p in container.files if p.startswith("/tmp/")]


def test_validate_cleans_up_even_when_the_validator_call_raises(monkeypatch):
    stub = use(monkeypatch, StubContainer(files={CONFIG_PATH: LIVE_CONFIG.encode()}))
    real_exec_run = stub.exec_run

    def exploding_exec_run(cmd, demux=False):
        if cmd[0] == "caddy":
            raise RuntimeError("docker exec died")
        return real_exec_run(cmd, demux=demux)

    monkeypatch.setattr(stub, "exec_run", exploding_exec_run)

    out = text_of(server.run_tool("caddy_validate", {"config": "a {\n}\n"}))

    assert out.startswith("Error"), out
    assert [c for c in stub.exec_calls if c[0] == "rm"], "temp file was not cleaned up"


def test_validate_reports_invalid(monkeypatch):
    stub = use(monkeypatch, StubContainer(
        validate_ok=False, validate_output="Caddyfile:1: bad"
    ))
    out = text_of(server.run_tool("caddy_validate", {"config": "x"}))
    assert out.startswith("✗ Invalid")
    assert "Caddyfile:1: bad" in out


def test_reload_reports_success(container):
    assert text_of(server.run_tool("caddy_reload", {})).startswith("✓")


@pytest.mark.parametrize(
    "requested,expected",
    [
        (None, server.LOG_LINES_DEFAULT),
        (50, 50),
        (0, server.LOG_LINES_MIN),
        (-5, server.LOG_LINES_MIN),
        (10**9, server.LOG_LINES_MAX),
    ],
)
def test_get_logs_clamps_lines(container, requested, expected):
    arguments = {} if requested is None else {"lines": requested}

    server.run_tool("caddy_get_logs", arguments)

    assert container.logs_calls[-1]["tail"] == expected


def test_get_logs_rejects_a_non_integer_lines(container):
    out = text_of(server.run_tool("caddy_get_logs", {"lines": "many"}))
    assert "'lines' must be an integer" in out


def test_status_does_not_touch_container_image(container):
    """container.image is None here; reading `.tags` off it would raise."""
    out = text_of(server.run_tool("caddy_status", {}))
    assert '"image": "caddy:2-alpine"' in out
    assert '"running": true' in out


def test_status_falls_back_when_the_image_is_unknown(monkeypatch):
    use(monkeypatch, StubContainer(attrs={"State": {"Running": True}}))
    assert '"image": "unknown"' in text_of(server.run_tool("caddy_status", {}))


def test_unknown_tool(container):
    assert text_of(server.run_tool("nope", {})) == "Unknown tool: nope"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _backups(stub):
    return [p for p in stub.files if p.startswith(f"{CONFIG_PATH}.bak-")]


def _tar_bytes(raw):
    with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
        member = tar.getmembers()[0]
        return tar.extractfile(member).read()


def _tar_name(raw):
    with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
        return tar.getmembers()[0].name
