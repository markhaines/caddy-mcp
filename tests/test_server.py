"""Unit tests for the Caddy MCP server. No Docker daemon required."""

import asyncio
import datetime
import fnmatch
import io
import time
import re
import tarfile
import threading

import pytest
from starlette.testclient import TestClient

import server
from stubs import StubContainer, StubDockerClient

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
        re.escape(CONFIG_PATH) + r"\.bak-\d{8}T\d{6}Z-[0-9a-f]{8}", backups[0]
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

    prunes = [c for c in container.exec_calls if c[0] == "sh" and "tail -n +" in c[-1]]
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
# Round 2 — the rollback path (cleanup and restore are independent)
# ---------------------------------------------------------------------------

def test_rollback_still_runs_when_cleanup_raises(monkeypatch):
    """Blocking 1: a raising `rm` used to jump past restore_backup() while the
    response still claimed a rollback had been attempted."""
    stub = use(monkeypatch, StubContainer(files={CONFIG_PATH: LIVE_CONFIG.encode()}))
    stub.fail["mv"] = (1, "mv: can't rename: Read-only file system")
    real_exec_run = stub.exec_run

    def exec_run(cmd, demux=False):
        # Only the staged-file cleanup explodes; validation's /tmp rm is fine.
        if cmd[0] == "rm" and any(a.startswith(CONFIG_DIR) for a in cmd[1:]):
            raise RuntimeError("docker exec died during cleanup")
        return real_exec_run(cmd, demux=demux)

    monkeypatch.setattr(stub, "exec_run", exec_run)

    out = text_of(server.run_tool("caddy_write_config", {"config": "a.example {\n}\n"}))

    assert out.startswith("Error"), out
    assert "Read-only file system" in out            # the original failure
    assert "Cleanup FAILED" in out                   # cleanup outcome, honestly
    assert "Rollback: restored" in out               # ...and the restore STILL ran
    assert stub.files[CONFIG_PATH] == LIVE_CONFIG.encode()
    # The restore actually happened, it was not merely claimed.
    assert [c for c in stub.exec_calls if c[0] == "cp" and c[-1] == CONFIG_PATH]


def test_cleanup_and_rollback_outcomes_are_reported_separately(monkeypatch):
    """Blocking 2: a nonzero `rm` is surfaced, not discarded, and is clearly
    distinct from the rollback outcome."""
    stub = use(monkeypatch, StubContainer(files={CONFIG_PATH: LIVE_CONFIG.encode()}))
    stub.fail["mv"] = (1, "mv: I/O error")
    stub.fail["rm"] = (1, "rm: Permission denied")

    out = text_of(server.run_tool("caddy_write_config", {"config": "a.example {\n}\n"}))

    lines = out.splitlines()
    assert lines[0].startswith("Error: Write failed:")
    assert "I/O error" in lines[0]
    assert lines[1].startswith("Cleanup FAILED:")
    assert "Permission denied" in lines[1]
    assert lines[2].startswith("Rollback: restored")
    assert stub.files[CONFIG_PATH] == LIVE_CONFIG.encode()


def test_rollback_failure_is_never_reported_as_success(monkeypatch):
    stub = use(monkeypatch, StubContainer(files={CONFIG_PATH: LIVE_CONFIG.encode()}))
    stub.fail["mv"] = (1, "mv: I/O error")
    real_exec_run = stub.exec_run

    def exec_run(cmd, demux=False):
        if cmd[0] == "cp" and cmd[-1] == CONFIG_PATH:
            raise RuntimeError("docker exec died during rollback")
        return real_exec_run(cmd, demux=demux)

    monkeypatch.setattr(stub, "exec_run", exec_run)

    out = text_of(server.run_tool("caddy_write_config", {"config": "a.example {\n}\n"}))

    assert "Rollback FAILED" in out
    assert "MANUAL ACTION REQUIRED" in out
    assert "Rollback: restored" not in out
    assert "Cleanup: removed" in out          # cleanup did succeed, and says so


def test_no_backup_is_reported_as_not_attempted(monkeypatch):
    """With no pre-existing file there is nothing to restore — and the message
    must say that, not imply a restore happened."""
    stub = use(monkeypatch, StubContainer(files={}))
    stub.fail["mv"] = (1, "mv: I/O error")

    out = text_of(server.run_tool("caddy_write_config", {"config": "a.example {\n}\n"}))

    assert "Rollback: not attempted" in out
    assert "Rollback: restored" not in out


# ---------------------------------------------------------------------------
# Round 2 — backup naming and the prune glob (they move in lockstep)
# ---------------------------------------------------------------------------

# Real filenames sitting beside the live Caddyfile on the host. The prune glob
# feeds `rm`, so matching any of these would delete someone's backup — and
# matching the first would delete production.
REAL_NEIGHBOURS = [
    "Caddyfile",                       # THE LIVE CONFIG
    "Caddyfile.backup",
    "Caddyfile.bak-1778529190",
    "Caddyfile.bak.20260617-113158",
    "Caddyfile.bak-2026-08-15-095957",
    "Caddyfile.bak.20260515-ipam",
]


def prune_pattern():
    """The exact pattern prune_backups() hands to the shell."""
    return f"{CONFIG_PATH}{server.BACKUP_SUFFIX_GLOB}"


@pytest.mark.parametrize("name", REAL_NEIGHBOURS)
def test_prune_glob_does_not_match_real_neighbouring_files(name):
    assert not fnmatch.fnmatchcase(f"{CONFIG_DIR}/{name}", prune_pattern()), name


def test_prune_glob_matches_the_names_we_generate(container):
    """Generated names must stay inside the glob, or pruning silently stops."""
    server.run_tool("caddy_write_config", {"config": "a.example {\n}\n"})
    server.run_tool("caddy_write_config", {"config": "b.example {\n}\n"})

    backups = _backups(container)
    assert len(backups) == 2
    for backup in backups:
        assert fnmatch.fnmatchcase(backup, prune_pattern()), backup


def test_prune_glob_rejects_near_misses():
    near_misses = [
        f"{CONFIG_PATH}.bak-20260818T131500Z",            # the old, id-less form
        f"{CONFIG_PATH}.bak-20260818T131500Z-",           # empty id
        f"{CONFIG_PATH}.bak-20260818T131500Z-abcdefg",    # id too short
        f"{CONFIG_PATH}.bak-20260818T131500Z-abcdefghi",  # id too long
        f"{CONFIG_PATH}.bak-20260818T131500Z-ABCDEF12",   # not lowercase hex
        f"{CONFIG_PATH}.bak-20260818T131500Z-abcdefgh",   # 'g'/'h' are not hex
        f"{CONFIG_PATH}.bak-2026081T131500Z-abcdef12",    # short timestamp
    ]
    for name in near_misses:
        assert not fnmatch.fnmatchcase(name, prune_pattern()), name


def test_backup_names_do_not_collide_within_one_second(container, monkeypatch):
    """One-second stamps alone let two writes share a backup path and clobber
    each other's rollback source."""
    frozen = datetime.datetime(2026, 8, 18, 13, 15, 0, tzinfo=datetime.timezone.utc)

    class FrozenClock:
        @staticmethod
        def now(tz=None):
            return frozen if tz is None else frozen.astimezone(tz)

    monkeypatch.setattr(server, "datetime", FrozenClock)

    server.run_tool("caddy_write_config", {"config": "a.example {\n}\n"})
    server.run_tool("caddy_write_config", {"config": "b.example {\n}\n"})

    backups = _backups(container)
    assert len(backups) == 2, backups
    assert len(set(backups)) == 2
    stamps = {b.rsplit("-", 1)[0] for b in backups}
    assert len(stamps) == 1, "the test did not actually freeze the clock"
    # The first backup still holds the config that existed before write one.
    assert LIVE_CONFIG.encode() in [container.files[b] for b in backups]


# ---------------------------------------------------------------------------
# Round 2 — backup absence must not be inferred from an arbitrary failure
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "probe_status,expected_reason",
    [
        (server.PROBE_NOT_REGULAR, "not a regular file"),
        (server.PROBE_DANGLING_SYMLINK, "symlink to a missing target"),
        (127, "the check itself failed"),
        (2, "the check itself failed"),
    ],
)
def test_write_refuses_when_the_path_is_not_a_backable_regular_file(
    monkeypatch, probe_status, expected_reason
):
    """`test -f` exits 1 for any false predicate, so "not a regular file" must
    not be read as "absent" and waved through as an unprotected write."""
    stub = use(monkeypatch, StubContainer(
        files={CONFIG_PATH: LIVE_CONFIG.encode()}, probe_status=probe_status
    ))

    out = text_of(server.run_tool("caddy_write_config", {"config": "a.example {\n}\n"}))

    assert out.startswith("Error"), out
    assert expected_reason in out
    assert "Not writing without a backup" in out
    assert [c for c in stub.put_archive_calls if c["path"] == CONFIG_DIR] == []
    assert stub.files[CONFIG_PATH] == LIVE_CONFIG.encode()
    assert _backups(stub) == []


def test_probe_script_classifies_before_it_trusts_absence():
    """The probe must ask -f, then -L, then -e — in that order — so only the
    final branch means absence."""
    script = server.config_probe_script()
    assert f"[ -f " in script and "-L" in script and "-e" in script
    assert script.index("-f") < script.index("-L") < script.index("-e")
    assert f"exit {server.PROBE_ABSENT}; fi" in script
    # Statuses must stay distinct or the classification collapses.
    assert len({
        server.PROBE_REGULAR_FILE, server.PROBE_ABSENT,
        server.PROBE_NOT_REGULAR, server.PROBE_DANGLING_SYMLINK,
    }) == 4


def test_write_proceeds_without_a_backup_when_there_is_genuinely_no_config(monkeypatch):
    stub = use(monkeypatch, StubContainer(files={}))

    out = text_of(server.run_tool("caddy_write_config", {"config": "a.example {\n}\n"}))

    assert out.startswith("✓"), out
    assert "Backup: none" in out
    assert stub.files[CONFIG_PATH] == b"a.example {\n}\n"


# ---------------------------------------------------------------------------
# Round 2 — API key middleware
# ---------------------------------------------------------------------------

API_KEY = "s3cret-test-key"


@pytest.fixture
def client(monkeypatch, container):
    monkeypatch.setattr(server, "MCP_API_KEY", API_KEY)
    monkeypatch.setattr(server, "get_docker_client", lambda: StubDockerClient(container))
    return TestClient(server.create_app(), raise_server_exceptions=False)


def test_api_key_matches_rejects_non_ascii_without_raising(monkeypatch):
    """hmac.compare_digest refuses non-ASCII str arguments; this must be a
    non-match, not a TypeError."""
    monkeypatch.setattr(server, "MCP_API_KEY", API_KEY)

    with pytest.raises(TypeError):
        __import__("hmac").compare_digest("café", API_KEY)   # the old comparison

    assert server.api_key_matches("café") is False
    assert server.api_key_matches(None) is False
    assert server.api_key_matches("") is False
    assert server.api_key_matches(API_KEY) is True


def test_non_ascii_header_is_401_not_500(client):
    response = client.get("/anything", headers={"x-api-key": "café".encode("utf-8")})
    assert response.status_code == 401


def test_query_string_credential_is_rejected(client):
    """The api_key query param was removed: it leaks into proxy access logs."""
    assert client.get(f"/anything?api_key={API_KEY}").status_code == 401


def test_missing_header_is_401(client):
    assert client.get("/anything").status_code == 401


def test_wrong_header_is_401(client):
    assert client.get("/anything", headers={"x-api-key": "nope"}).status_code == 401


def test_valid_header_passes_the_middleware(client):
    # 404 rather than 401: authentication passed and routing took over.
    assert client.get("/anything", headers={"x-api-key": API_KEY}).status_code == 404


def test_health_is_exempt_from_auth(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


# ---------------------------------------------------------------------------
# Round 3 — the event loop must stay responsive, writes must stay serialised
# ---------------------------------------------------------------------------

async def wait_for(flag, timeout=2.0):
    """Wait for a threading.Event without blocking the event loop.

    If the tool dispatch were running inline on the loop, this coroutine would
    not get a turn until the whole blocking call had finished — which is
    exactly what the tests below detect.
    """
    deadline = time.monotonic() + timeout
    while not flag.is_set():
        if time.monotonic() > deadline:
            return False
        await asyncio.sleep(0.005)
    return True


def test_a_slow_write_does_not_stall_the_event_loop(monkeypatch):
    """MAJOR 1: run_tool is fully blocking, so dispatching it inline would hold
    the sole event-loop thread for the entire Docker round-trip."""
    stub = use(monkeypatch, StubContainer(
        files={CONFIG_PATH: LIVE_CONFIG.encode()},
        block_on="cp",              # block during the backup step, mid-transaction
    ))
    finished = []

    async def scenario():
        write = asyncio.create_task(
            server.call_tool("caddy_write_config", {"config": "a.example {\n}\n"})
        )
        write.add_done_callback(lambda _: finished.append(True))

        assert await wait_for(stub.started), "the write never reached the backup step"

        # The loop is alive while the write is parked inside Docker: another
        # tool call runs to completion, and a plain coroutine gets its turns.
        ticks = 0
        for _ in range(5):
            await asyncio.sleep(0)
            ticks += 1
        status = text_of(await server.call_tool("caddy_status", {}))

        assert ticks == 5
        assert '"status": "running"' in status
        assert not finished, "the write completed before the concurrent call — it blocked the loop"

        stub.release.set()
        return text_of(await write)

    result = asyncio.run(scenario())
    assert result.startswith("✓"), result
    assert stub.files[CONFIG_PATH] == b"a.example {\n}\n"


async def call_health(app):
    """Drive the /health route directly on the current event loop."""
    route = [r for r in app.routes if getattr(r, "path", "") == "/health"][0]
    sent = []

    async def send(message):
        sent.append(message)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    await asyncio.wait_for(
        route.handle(
            {"type": "http", "method": "GET", "path": "/health", "headers": [],
             "query_string": b"", "app": app, "root_path": ""},
            receive, send,
        ),
        timeout=2.0,
    )
    return [m for m in sent if m["type"] == "http.response.start"][0]["status"]


def test_health_stays_answerable_while_a_write_is_in_flight(monkeypatch, container):
    """The endpoint that decides whether this container is healthy must not be
    parked behind a slow write."""
    container.block_on = "cp"
    monkeypatch.setattr(server, "MCP_API_KEY", "")
    monkeypatch.setattr(server, "get_docker_client", lambda: StubDockerClient(container))
    app = server.create_app()

    async def scenario():
        write = asyncio.create_task(
            server.call_tool("caddy_write_config", {"config": "a.example {\n}\n"})
        )
        assert await wait_for(container.started), "the write never reached the backup step"
        assert not write.done(), "the write finished before health ran — it blocked the loop"

        status = await call_health(app)

        assert not write.done(), "health only answered once the write had finished"
        container.release.set()
        await write
        return status

    assert asyncio.run(scenario()) == 200


def test_a_slow_health_probe_does_not_stall_the_event_loop(monkeypatch, container):
    """/health makes blocking Docker calls of its own; those must be offloaded
    too, or a hung daemon takes the whole loop down with it."""
    monkeypatch.setattr(server, "MCP_API_KEY", "")
    started, release = threading.Event(), threading.Event()
    stub_client = StubDockerClient(container)

    def slow_ping():
        started.set()
        release.wait(timeout=2.0)
        return True

    stub_client.ping = slow_ping
    monkeypatch.setattr(server, "get_docker_client", lambda: stub_client)
    app = server.create_app()

    async def scenario():
        probe = asyncio.create_task(call_health(app))
        assert await wait_for(started), "health never reached the docker ping"

        ticks = 0
        for _ in range(5):
            await asyncio.sleep(0)
            ticks += 1

        assert ticks == 5
        assert not probe.done(), "health completed inline — it held the event loop"
        release.set()
        return await probe

    assert asyncio.run(scenario()) == 200


def test_two_concurrent_writes_do_not_interleave(monkeypatch):
    """The transactions must be serialised, not merely both eventually done:
    an interleaved backup/stage/rename can restore the wrong content."""
    stub = use(monkeypatch, StubContainer(files={CONFIG_PATH: LIVE_CONFIG.encode()}))
    original_run = stub._run

    def slow_run(cmd):
        if cmd[0] == "cp":          # widen the window the lock has to cover
            time.sleep(0.02)
        return original_run(cmd)

    monkeypatch.setattr(stub, "_run", slow_run)

    async def scenario():
        return await asyncio.gather(
            server.call_tool("caddy_write_config", {"config": "first {\n}\n"}),
            server.call_tool("caddy_write_config", {"config": "second {\n}\n"}),
        )

    results = asyncio.run(scenario())
    assert all(text_of(r).startswith("✓") for r in results)

    # Both writes really did run on different threads...
    threads = {thread for thread, _ in stub.call_log}
    assert len(threads) == 2, f"expected two worker threads, saw {len(threads)}"

    # ...and their call spans in the ordered log do not overlap.
    spans = {}
    for index, (thread, _) in enumerate(stub.call_log):
        first, last = spans.get(thread, (index, index))
        spans[thread] = (min(first, index), max(last, index))
    (a_first, a_last), (b_first, b_last) = sorted(spans.values())
    assert a_last < b_first, (
        f"transactions interleaved: {a_first}-{a_last} overlaps {b_first}-{b_last}"
    )

    # One backup per write, and the survivor is one of the two configs.
    assert len(_backups(stub)) == 2
    assert stub.files[CONFIG_PATH] in (b"first {\n}\n", b"second {\n}\n")


def test_write_lock_is_held_across_the_whole_transaction(monkeypatch):
    """A second writer cannot start while the first holds the lock."""
    stub = use(monkeypatch, StubContainer(
        files={CONFIG_PATH: LIVE_CONFIG.encode()},
        block_on="cp",
    ))

    async def scenario():
        first = asyncio.create_task(
            server.call_tool("caddy_write_config", {"config": "first {\n}\n"})
        )
        assert await wait_for(stub.started)
        calls_before = len(stub.call_log)

        second = asyncio.create_task(
            server.call_tool("caddy_write_config", {"config": "second {\n}\n"})
        )
        for _ in range(20):                 # give it every chance to barge in
            await asyncio.sleep(0.005)
        assert len(stub.call_log) == calls_before, (
            "a second write started while the first held the lock"
        )

        stub.release.set()
        return text_of(await first), text_of(await second)

    first_result, second_result = asyncio.run(scenario())
    assert first_result.startswith("✓")
    assert second_result.startswith("✓")


def test_docker_timeout_is_explicit_and_configurable(monkeypatch):
    """The write transaction's maximum duration must not rest on an SDK
    default, since the lock is held across the whole sequence."""
    assert server.DOCKER_TIMEOUT == 30            # documented default

    captured = {}

    class FakeDocker:
        @staticmethod
        def DockerClient(**kwargs):
            captured.update(kwargs)
            return "client"

    monkeypatch.setattr(server, "_docker_client", None)
    monkeypatch.setattr(server, "docker", FakeDocker)
    monkeypatch.setattr(server, "DOCKER_TIMEOUT", 7)

    assert server.get_docker_client() == "client"
    assert captured["timeout"] == 7
    assert captured["base_url"] == server.DOCKER_SOCKET
    # Cached, not rebuilt per call.
    assert server.get_docker_client() == "client"

    monkeypatch.setattr(server, "_docker_client", None)


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
