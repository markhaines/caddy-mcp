#!/usr/bin/env python3
"""
Caddy MCP Server
Provides Claude with tools to manage a Caddy reverse proxy via the Docker API.
Runs as a container on the same host as Caddy, using the local Docker socket.
"""

import asyncio
import hmac
import io
import json
import logging
import os
import shlex
import tarfile
import threading
import uuid
from datetime import datetime, timezone

import docker
import uvicorn
from mcp.server import Server
from mcp.server.sse import SseServerTransport
from mcp.types import TextContent, Tool
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Mount, Route

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("caddy-mcp")

# ---------------------------------------------------------------------------
# Configuration (via environment variables)
# ---------------------------------------------------------------------------
DOCKER_SOCKET = os.environ.get("DOCKER_SOCKET", "unix:///var/run/docker.sock")
CADDY_CONTAINER = os.environ.get("CADDY_CONTAINER", "caddy")
CADDY_CONTAINER_CONFIG = os.environ.get("CADDY_CONTAINER_CONFIG", "/etc/caddy/Caddyfile")
MCP_API_KEY = os.environ.get("MCP_API_KEY", "")
# Ceiling on every Docker API round-trip, in seconds. A write transaction is a
# sequence of these and holds the write lock throughout, so the maximum time it
# can occupy is an explicit property of this server rather than whatever the
# SDK happens to default to.
DOCKER_TIMEOUT = int(os.environ.get("DOCKER_TIMEOUT", "30"))
PORT = int(os.environ.get("PORT", "8000"))
# Path prefix this server is mounted under by an upstream reverse proxy.
# e.g. ROOT_PATH=/caddy means the server is reachable at https://host/caddy/sse.
# Used so the SSE endpoint emits `/caddy/messages/...` instead of `/messages/...`,
# which the client then POSTs back to via the same reverse proxy.
ROOT_PATH = os.environ.get("ROOT_PATH", "").rstrip("/")

# How many of our own timestamped backups to keep beside the live Caddyfile.
BACKUP_KEEP = int(os.environ.get("CADDY_BACKUP_KEEP", "10"))
# Backups this server writes are named
#     <config>.bak-YYYYmmddTHHMMSSZ-<8 lowercase hex>
# The timestamp sorts lexicographically (so `sort -r` is newest-first) and the
# random id makes two writes within the same second collision-proof — without
# it they would share a path and could overwrite each other's rollback source.
BACKUP_STAMP_FORMAT = "%Y%m%dT%H%M%SZ"
BACKUP_ID_CHARS = 8
_DIGIT = "[0-9]"
_HEX = "[0-9a-f]"
# SAFETY-CRITICAL: this glob feeds `rm`. It must match the shape above exactly
# and nothing else — in particular not the live Caddyfile itself and not the
# hand-made backups beside it (Caddyfile.backup, Caddyfile.bak-1778529190,
# Caddyfile.bak.20260617-113158, Caddyfile.bak-2026-08-15-095957,
# Caddyfile.bak.20260515-ipam). Keep it in lockstep with the name built in
# backup_config(); tests/test_server.py pins both.
BACKUP_SUFFIX_GLOB = (
    ".bak-" + _DIGIT * 8 + "T" + _DIGIT * 6 + "Z-" + _HEX * BACKUP_ID_CHARS
)
# What the config-type probe reports. `test -f` alone is not enough: it exits 1
# for ANY false predicate — path is a directory, a socket, a dangling symlink —
# and treating all of those as "absent" would write with no backup in exactly
# the cases that most need one. The probe evaluates -f, -L and -e separately so
# genuine absence is distinguishable from "there is something there, but it is
# not a regular file". Any other status means the probe itself failed.
PROBE_REGULAR_FILE = 0
PROBE_ABSENT = 1
PROBE_NOT_REGULAR = 3
PROBE_SYMLINK_TO_NON_REGULAR = 4

LOG_LINES_DEFAULT = 100
LOG_LINES_MIN = 1
LOG_LINES_MAX = 2000

server = Server("caddy-mcp")

# Docker clients are per-thread; see get_docker_client().
_thread_local = threading.local()
# Serialises the write transaction so two callers cannot interleave
# backup/stage/rename against the same file.
#
# INVARIANT: this is process-local. It serialises writers inside ONE process,
# which is what the current single-container deployment has. It cannot see a
# second worker process, a second replica of this container, or anyone editing
# the Caddyfile by hand. Running more than one writer against the same
# Caddyfile would need an inter-process lock (a lock file on the shared mount,
# or an advisory flock) held from the backup through to commit or rollback —
# not implemented here, because nothing today needs it.
_write_lock = threading.Lock()


class ToolError(Exception):
    """A failure that should be reported back to the caller as an MCP error."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_docker_client():
    """Return this thread's Docker client, created on first use.

    Per-thread, not shared. docker-py's APIClient subclasses requests.Session,
    and Requests gives no thread-safety guarantee for concurrent use of one
    Session — its connection pool and cookie jar are the usual hazards. Since
    tool dispatch and /health moved onto worker threads, a single shared client
    would be used concurrently by reads, validation, logs, status, reload and
    the healthcheck (_write_lock serialises writes against writes, nothing
    more). A client per thread sidesteps that without a global Docker lock,
    which would undo the offload and could park /health behind a long write.

    Thread-local storage also settles the initialisation race: each thread
    fills only its own slot, so concurrent first use cannot build several
    clients and throw all but one away.

    The count stays bounded — asyncio.to_thread uses the default executor,
    capped around min(32, cpu + 4) threads.

    Still lazy, and it must stay that way: constructing a client at import time
    turns an unreachable or unauthorised daemon into a crash loop (this one
    reached RestartCount 6609) instead of a tool call that returns an error.
    """
    client = getattr(_thread_local, "client", None)
    if client is None:
        client = docker.DockerClient(base_url=DOCKER_SOCKET, timeout=DOCKER_TIMEOUT)
        _thread_local.client = client
    return client


def get_container():
    """Get the Caddy container, raising a clear error if not found."""
    return get_docker_client().containers.get(CADDY_CONTAINER)


def pack_tar(filename: str, content: bytes) -> io.BytesIO:
    """Wrap bytes in a tar archive suitable for docker put_archive."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo(name=filename)
        info.size = len(content)
        tar.addfile(info, io.BytesIO(content))
    buf.seek(0)
    return buf


def exec_in_container(container, cmd: list) -> tuple:
    """Run a command in the Caddy container.

    Returns (exit_code, output). The caller MUST look at the exit code before
    using the output: with demux=False docker merges stderr into stdout, so a
    failed command's output is an error message, never file content.
    A missing exit code is normalised to -1 (i.e. "not success").
    """
    result = container.exec_run(cmd, demux=False)
    output = (result.output or b"").decode("utf-8", errors="replace")
    exit_code = -1 if result.exit_code is None else int(result.exit_code)
    return exit_code, output


def put_archive_checked(container, path: str, tar, what: str) -> None:
    """put_archive, checking the return value. Raises ToolError on failure."""
    ok = container.put_archive(path, tar)
    if not ok:
        raise ToolError(
            f"Docker rejected the upload while {what}: put_archive({path!r}) "
            f"returned {ok!r} for container '{CADDY_CONTAINER}'."
        )


def require_str_arg(arguments: dict, key: str) -> str:
    """Fetch a required string argument with a message that names it."""
    if not isinstance(arguments, dict) or key not in arguments:
        raise ToolError(
            f"Missing required argument '{key}'. Pass it as a string in the "
            f"tool's arguments object."
        )
    value = arguments[key]
    if not isinstance(value, str):
        raise ToolError(
            f"Argument '{key}' must be a string, got {type(value).__name__}."
        )
    return value


def read_config(container) -> str:
    """Read the live Caddyfile out of the container.

    Raises ToolError on a non-zero exit so a `cat: ... No such file` message can
    never be handed back to the caller as if it were the config — a caller that
    then edited and wrote it back would replace the whole live config.
    """
    exit_code, output = exec_in_container(container, ["cat", CADDY_CONTAINER_CONFIG])
    if exit_code != 0:
        raise ToolError(
            f"Could not read the Caddyfile at {CADDY_CONTAINER_CONFIG} in "
            f"container '{CADDY_CONTAINER}' (cat exited {exit_code}): "
            f"{output.strip() or '(no output)'}. "
            f"No config content was returned — do NOT write a config based on "
            f"this response. Check CADDY_CONTAINER_CONFIG and that the "
            f"container has the config bind-mounted."
        )
    return output


def validate_config(container, config: str) -> tuple:
    """Validate a config's exact bytes inside the container.

    Returns (is_valid, validator_output). Staged under a unique name so
    concurrent calls cannot clobber each other, and cleaned up in a finally.
    """
    staged = f"/tmp/Caddyfile.validate-{uuid.uuid4().hex}"
    put_archive_checked(
        container,
        "/tmp",
        pack_tar(os.path.basename(staged), config.encode("utf-8")),
        f"staging a config for validation at {staged}",
    )
    try:
        exit_code, output = exec_in_container(
            container,
            ["caddy", "validate", "--config", staged, "--adapter", "caddyfile"],
        )
        return exit_code == 0, output.strip()
    finally:
        # Best-effort, and deliberately swallowed: a cleanup problem must not
        # replace the validation result on the way out of this finally.
        try:
            cleanup_code, cleanup_output = exec_in_container(container, ["rm", "-f", staged])
            if cleanup_code != 0:
                log.warning(
                    "Failed to remove validation temp file %s (exit %d): %s",
                    staged, cleanup_code, cleanup_output.strip(),
                )
        except Exception as cleanup_exc:
            log.warning("Failed to remove validation temp file %s: %s", staged, cleanup_exc)


def config_probe_script() -> str:
    """Shell that classifies the config path into one PROBE_* status.

    -f is checked first (it follows symlinks, so a symlink to a regular file is
    a regular file), then -L for a symlink whose target is missing or is not a
    regular file, then -e for anything else that exists. Only the final branch
    is genuine absence.
    """
    path = shlex.quote(CADDY_CONTAINER_CONFIG)
    return (
        f"if [ -f {path} ]; then exit {PROBE_REGULAR_FILE}; "
        f"elif [ -L {path} ]; then exit {PROBE_SYMLINK_TO_NON_REGULAR}; "
        f"elif [ -e {path} ]; then exit {PROBE_NOT_REGULAR}; "
        f"else exit {PROBE_ABSENT}; fi"
    )


def backup_config(container) -> str:
    """Copy the live Caddyfile aside. Returns the backup path, or "" if there
    is genuinely no file there yet.

    Refuses the write for anything that is neither a regular file nor a clean
    absence: writing unprotected is only acceptable when we have established
    that there is nothing to protect.
    """
    probe_code, probe_output = exec_in_container(
        container, ["sh", "-c", config_probe_script()]
    )
    if probe_code == PROBE_ABSENT:
        log.warning(
            "No existing file at %s to back up before writing", CADDY_CONTAINER_CONFIG
        )
        return ""
    if probe_code != PROBE_REGULAR_FILE:
        reasons = {
            PROBE_NOT_REGULAR: (
                "something exists at that path but it is not a regular file "
                "(a directory, socket or device)"
            ),
            # -f already failed, so the target is missing, or it exists and
            # is not a regular file. The check cannot tell those apart, so the
            # message does not pretend to.
            PROBE_SYMLINK_TO_NON_REGULAR: (
                "that path is a symlink whose target is missing or is not a "
                "regular file"
            ),
        }
        reason = reasons.get(
            probe_code,
            f"the check itself failed (probe exited {probe_code}"
            + (f": {probe_output.strip()}" if probe_output.strip() else "")
            + ")",
        )
        raise ToolError(
            f"Refusing to write: {reason}, so {CADDY_CONTAINER_CONFIG} in "
            f"container '{CADDY_CONTAINER}' cannot be backed up. Not writing "
            f"without a backup. The live config is unchanged."
        )

    stamp = datetime.now(timezone.utc).strftime(BACKUP_STAMP_FORMAT)
    backup = (
        f"{CADDY_CONTAINER_CONFIG}.bak-{stamp}-{uuid.uuid4().hex[:BACKUP_ID_CHARS]}"
    )
    exit_code, output = exec_in_container(
        container, ["cp", "-p", CADDY_CONTAINER_CONFIG, backup]
    )
    if exit_code != 0:
        raise ToolError(
            f"Refusing to write: could not back up {CADDY_CONTAINER_CONFIG} to "
            f"{backup} (cp exited {exit_code}): {output.strip() or '(no output)'}. "
            f"The live config is unchanged."
        )
    log.info("Backed up %s to %s", CADDY_CONTAINER_CONFIG, backup)
    return backup


def prune_backups(container) -> None:
    """Keep only the newest BACKUP_KEEP backups written by this server.

    Best-effort: a failure here is logged, never surfaced as a write failure.
    The names sort lexicographically by timestamp, so `sort -r` is newest-first.
    """
    pattern = f"{CADDY_CONTAINER_CONFIG}{BACKUP_SUFFIX_GLOB}"
    script = (
        f"ls -1d {pattern} 2>/dev/null | sort -r | tail -n +{BACKUP_KEEP + 1} "
        f'| while read -r f; do rm -f "$f"; done'
    )
    exit_code, output = exec_in_container(container, ["sh", "-c", script])
    if exit_code != 0:
        log.warning("Backup pruning failed (exit %d): %s", exit_code, output.strip())


def restore_backup(container, backup: str) -> str:
    """Put a backup back over the live config. Returns a human-readable outcome."""
    if not backup:
        return (
            "Rollback: not attempted — no backup existed (there was no file at "
            f"{CADDY_CONTAINER_CONFIG} beforehand), so there was nothing to restore."
        )
    exit_code, output = exec_in_container(
        container, ["cp", "-p", backup, CADDY_CONTAINER_CONFIG]
    )
    if exit_code != 0:
        log.error("Rollback failed from %s (exit %d)", backup, exit_code)
        return (
            f"Rollback FAILED: could not restore {backup} over "
            f"{CADDY_CONTAINER_CONFIG} (cp exited {exit_code}): "
            f"{output.strip() or '(no output)'}. MANUAL ACTION REQUIRED — the "
            f"live config may be in an unknown state."
        )
    log.info("Rolled %s back from %s", CADDY_CONTAINER_CONFIG, backup)
    return f"Rollback: restored {CADDY_CONTAINER_CONFIG} from {backup}."


def cleanup_staged(container, staged_path: str) -> str:
    """Remove a staged temp file, reporting the outcome. Never raises.

    Cleanup is independent of rollback: a stale temp file is untidy, a
    Caddyfile left broken is an outage, so nothing here may stop a restore.
    """
    try:
        exit_code, output = exec_in_container(container, ["rm", "-f", staged_path])
    except Exception as exc:
        log.warning("Could not remove staged file %s: %s", staged_path, exc)
        return (
            f"Cleanup FAILED: removing the staged file {staged_path} raised "
            f"{exc!r}. A stale temp file may remain in the Caddy config directory."
        )
    if exit_code != 0:
        log.warning(
            "Could not remove staged file %s (rm exited %d): %s",
            staged_path, exit_code, output.strip(),
        )
        return (
            f"Cleanup FAILED: could not remove the staged file {staged_path} "
            f"(rm exited {exit_code}): {output.strip() or '(no output)'}. A stale "
            f"temp file may remain in the Caddy config directory."
        )
    return f"Cleanup: removed the staged file {staged_path}."


def rollback_to_backup(container, backup: str) -> str:
    """restore_backup() that cannot raise, so the reported outcome is always
    the outcome that actually happened."""
    try:
        return restore_backup(container, backup)
    except Exception as exc:
        log.error("Rollback from %s raised: %s", backup or "(no backup)", exc)
        return (
            f"Rollback FAILED: restoring {backup or '(no backup)'} raised {exc!r}. "
            f"MANUAL ACTION REQUIRED — the live config may be in an unknown state"
            + (f"; the backup is at {backup}." if backup else ".")
        )


def write_config(container, config: str) -> str:
    """Serialised entry point for the write transaction.

    Two writes interleaving would race on the backup/stage/rename sequence and
    could restore the wrong content, so the whole transaction is taken under a
    lock.
    """
    with _write_lock:
        return write_config_locked(container, config)


def write_config_locked(container, config: str) -> str:
    """Validate, back up, then atomically replace the live Caddyfile.

    Order matters: the exact bytes that will be committed are validated first,
    so validation cannot be bypassed by validating one value and writing
    another. The new file is staged under a unique temp name in the same
    directory and renamed over the target, so `--watch` can never observe a
    truncated config. Any failure after the backup exists triggers a restore.
    """
    directory = os.path.dirname(CADDY_CONTAINER_CONFIG) or "/"
    filename = os.path.basename(CADDY_CONTAINER_CONFIG)

    is_valid, validator_output = validate_config(container, config)
    if not is_valid:
        raise ToolError(
            "Refusing to write: the supplied config failed `caddy validate`. "
            "Nothing was changed and the live config is untouched.\n"
            f"{validator_output or '(no validator output)'}"
        )

    backup = backup_config(container)
    staged_name = f".{filename}.tmp-{uuid.uuid4().hex}"
    staged_path = f"{directory.rstrip('/')}/{staged_name}"

    try:
        put_archive_checked(
            container,
            directory,
            pack_tar(staged_name, config.encode("utf-8")),
            f"staging the new config at {staged_path}",
        )
        exit_code, output = exec_in_container(
            container, ["mv", "-f", staged_path, CADDY_CONTAINER_CONFIG]
        )
        if exit_code != 0:
            raise ToolError(
                f"Could not move the staged config {staged_path} over "
                f"{CADDY_CONTAINER_CONFIG} (mv exited {exit_code}): "
                f"{output.strip() or '(no output)'}."
            )
    except Exception as exc:
        # Three separate outcomes, reported separately and truthfully: the
        # failure itself, whether the staged file was cleaned up, and whether
        # the live config was actually restored. Neither helper raises, so a
        # cleanup problem can never skip the rollback (or be mistaken for one).
        cleanup_outcome = cleanup_staged(container, staged_path)
        rollback_outcome = rollback_to_backup(container, backup)
        raise ToolError(
            f"Write failed: {exc}\n{cleanup_outcome}\n{rollback_outcome}"
        ) from exc

    log.info(
        "Caddyfile written to %s in container (%d bytes, backup %s)",
        CADDY_CONTAINER_CONFIG, len(config.encode("utf-8")), backup or "none",
    )
    prune_backups(container)

    lines = [
        f"✓ Caddyfile written to {CADDY_CONTAINER_CONFIG} "
        f"({len(config.encode('utf-8'))} bytes). Validated before writing.",
        f"Backup: {backup}" if backup else "Backup: none (no previous file existed).",
        "If Caddy runs with --watch the new config is already being picked up; "
        "otherwise call caddy_reload to apply it.",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# MCP tool definitions
# ---------------------------------------------------------------------------

@server.list_tools()
async def list_tools():
    return [
        Tool(
            name="caddy_read_config",
            description=(
                "Read the complete current Caddyfile from disk. This is the "
                "required first step before any caddy_write_config call."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="caddy_write_config",
            description=(
                "DESTRUCTIVE — REPLACES THE ENTIRE CADDYFILE with the content you "
                "supply. This is a whole-file overwrite, not an append or a merge: "
                "every site block you leave out is deleted and those sites go "
                "offline. You MUST call caddy_read_config first and send back the "
                "complete file with your change applied. The exact bytes you send "
                "are validated before anything is committed and the write is "
                "refused if validation fails; the previous file is backed up first "
                "and restored automatically if the write fails partway. If Caddy "
                "runs with --watch the write applies itself, otherwise call "
                "caddy_reload afterwards."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "config": {
                        "type": "string",
                        "description": (
                            "The COMPLETE Caddyfile content. Whatever you pass "
                            "becomes the entire file; anything omitted is lost."
                        ),
                    }
                },
                "required": ["config"],
            },
        ),
        Tool(
            name="caddy_validate",
            description=(
                "Validate a Caddyfile without applying it. "
                "Returns any syntax or configuration errors. "
                "caddy_write_config validates its own input as well, so this is "
                "for checking a draft, not a prerequisite you can satisfy on its "
                "behalf."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "config": {
                        "type": "string",
                        "description": "The Caddyfile content to validate.",
                    }
                },
                "required": ["config"],
            },
        ),
        Tool(
            name="caddy_reload",
            description=(
                "Reload Caddy with the Caddyfile currently on disk. "
                "Call this after caddy_write_config to apply changes."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="caddy_get_logs",
            description="Get recent Caddy container logs.",
            inputSchema={
                "type": "object",
                "properties": {
                    "lines": {
                        "type": "integer",
                        "description": "Number of log lines to return.",
                        "default": LOG_LINES_DEFAULT,
                        "minimum": LOG_LINES_MIN,
                        "maximum": LOG_LINES_MAX,
                    }
                },
            },
        ),
        Tool(
            name="caddy_status",
            description="Get the current status and basic info for the Caddy container.",
            inputSchema={"type": "object", "properties": {}},
        ),
    ]


# ---------------------------------------------------------------------------
# MCP tool implementations
# ---------------------------------------------------------------------------

def tool_read_config(arguments: dict) -> str:
    return read_config(get_container())


def tool_write_config(arguments: dict) -> str:
    config = require_str_arg(arguments, "config")
    return write_config(get_container(), config)


def tool_validate(arguments: dict) -> str:
    config = require_str_arg(arguments, "config")
    is_valid, output = validate_config(get_container(), config)
    prefix = "✓ Valid" if is_valid else "✗ Invalid"
    return f"{prefix}\n{output}" if output else prefix


def tool_reload(arguments: dict) -> str:
    exit_code, output = exec_in_container(
        get_container(),
        ["caddy", "reload", "--config", CADDY_CONTAINER_CONFIG, "--adapter", "caddyfile"],
    )
    output = output.strip()
    prefix = "✓ Caddy reloaded successfully" if exit_code == 0 else "✗ Reload failed"
    log.info("caddy reload: exit=%d", exit_code)
    return f"{prefix}\n{output}" if output else prefix


def tool_get_logs(arguments: dict) -> str:
    raw = arguments.get("lines", LOG_LINES_DEFAULT)
    try:
        lines = int(raw)
    except (TypeError, ValueError):
        raise ToolError(f"Argument 'lines' must be an integer, got {raw!r}.")
    lines = max(LOG_LINES_MIN, min(lines, LOG_LINES_MAX))
    logs = get_container().logs(tail=lines, timestamps=True).decode("utf-8", errors="replace")
    return logs or "(no logs)"


def tool_status(arguments: dict) -> str:
    container = get_container()
    container.reload()
    attrs = container.attrs or {}
    state = attrs.get("State", {})
    info = {
        "name": container.name,
        "status": container.status,
        "id": container.short_id,
        # Read the image off the container's own attrs: container.image is None
        # when the image ID is missing from attrs, which turns `.tags` into an
        # AttributeError.
        "image": attrs.get("Config", {}).get("Image") or "unknown",
        "running": state.get("Running", False),
        "started_at": state.get("StartedAt", ""),
        "restart_count": attrs.get("RestartCount", 0),
    }
    return json.dumps(info, indent=2)


TOOL_HANDLERS = {
    "caddy_read_config": tool_read_config,
    "caddy_write_config": tool_write_config,
    "caddy_validate": tool_validate,
    "caddy_reload": tool_reload,
    "caddy_get_logs": tool_get_logs,
    "caddy_status": tool_status,
}


def run_tool(name: str, arguments: dict) -> list:
    """Dispatch a tool call and turn every failure into a readable MCP error."""
    handler = TOOL_HANDLERS.get(name)
    if handler is None:
        return [TextContent(type="text", text=f"Unknown tool: {name}")]

    try:
        return [TextContent(type="text", text=handler(arguments or {}))]
    except ToolError as exc:
        log.warning("Tool %s failed: %s", name, exc)
        return [TextContent(type="text", text=f"Error: {exc}")]
    except docker.errors.NotFound:
        return [TextContent(
            type="text",
            text=(
                f"Error: container '{CADDY_CONTAINER}' not found. "
                f"Check the CADDY_CONTAINER env var."
            ),
        )]
    except FileNotFoundError as exc:
        return [TextContent(
            type="text",
            text=(
                f"Error: a required path was not found ({exc}). The Docker socket "
                f"is {DOCKER_SOCKET} (env DOCKER_SOCKET) and the Caddyfile is "
                f"expected at {CADDY_CONTAINER_CONFIG} inside container "
                f"'{CADDY_CONTAINER}' (env CADDY_CONTAINER_CONFIG)."
            ),
        )]
    except Exception as exc:
        log.exception("Tool error in %s", name)
        return [TextContent(type="text", text=f"Error: {exc}")]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list:
    # run_tool is entirely blocking — every tool makes synchronous Docker
    # round-trips (exec, put_archive, logs, inspect). Running it inline would
    # hold the sole event-loop thread for the whole call, so a slow or hung
    # Docker request would stall SSE keepalives, other in-flight tool calls and
    # /health, making the server look dead. Offload all of them; _write_lock
    # (redundant while everything shared one thread) is what serialises the
    # write transaction now that real threads are involved.
    return await asyncio.to_thread(run_tool, name, arguments)


# ---------------------------------------------------------------------------
# HTTP app (SSE transport + optional API key auth)
# ---------------------------------------------------------------------------

def api_key_matches(provided) -> bool:
    """Constant-time credential check that cannot raise.

    hmac.compare_digest refuses str arguments containing non-ASCII characters
    (TypeError), so a header like `x-api-key: café` would otherwise surface as
    a 500 with a stack trace. Compare encoded bytes instead, and treat anything
    that will not encode — or is missing entirely — as simply not a match.
    """
    try:
        return hmac.compare_digest(
            (provided or "").encode("utf-8"), MCP_API_KEY.encode("utf-8")
        )
    except (AttributeError, TypeError, UnicodeError):
        return False


class ApiKeyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if MCP_API_KEY and request.url.path != "/health":
            # Header only: a key in the query string ends up in reverse-proxy
            # access logs, browser history and referrers.
            if not api_key_matches(request.headers.get("x-api-key")):
                return Response("Unauthorized", status_code=401)
        return await call_next(request)


def create_app() -> Starlette:
    # If the server sits behind a reverse proxy at ROOT_PATH, the SSE transport
    # must emit endpoint URLs that include the prefix so the client POSTs back
    # via the same proxy. The Mount path stays at /messages/ because the proxy
    # strips ROOT_PATH before the request reaches us.
    sse = SseServerTransport(f"{ROOT_PATH}/messages/")

    async def handle_sse(request: Request):
        async with sse.connect_sse(
            request.scope, request.receive, request._send
        ) as streams:
            await server.run(
                streams[0],
                streams[1],
                server.create_initialization_options(),
            )

    def probe_docker() -> str:
        get_docker_client().ping()
        return get_container().status

    async def health(_: Request):
        try:
            # Blocking Docker calls, so off the loop for the same reason as the
            # tools: the endpoint that reports whether this container is
            # healthy must stay answerable while a write is in flight.
            caddy_status = await asyncio.to_thread(probe_docker)
            return Response(
                json.dumps({"status": "ok", "caddy": caddy_status}),
                media_type="application/json",
            )
        except Exception as e:
            return Response(
                json.dumps({"status": "error", "detail": str(e)}),
                status_code=503,
                media_type="application/json",
            )

    app = Starlette(
        routes=[
            Route("/sse", endpoint=handle_sse),
            Mount("/messages/", app=sse.handle_post_message),
            Route("/health", endpoint=health),
        ]
    )

    if MCP_API_KEY:
        app.add_middleware(ApiKeyMiddleware)

    return app


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    log.info("Starting Caddy MCP server on port %d", PORT)
    log.info("Docker socket : %s", DOCKER_SOCKET)
    log.info("Docker timeout: %ds", DOCKER_TIMEOUT)
    log.info("Caddy container: %s", CADDY_CONTAINER)
    log.info("Caddy config   : %s", CADDY_CONTAINER_CONFIG)
    log.info("API key auth  : %s", "enabled" if MCP_API_KEY else "disabled")
    log.info("Root path     : %s", ROOT_PATH or "(none)")
    log.info("Backups kept  : %d", BACKUP_KEEP)
    uvicorn.run(create_app(), host="0.0.0.0", port=PORT)
