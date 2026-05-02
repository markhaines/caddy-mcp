#!/usr/bin/env python3
"""
Caddy MCP Server
Provides Claude with tools to manage a Caddy reverse proxy via the Docker API.
Runs as a container on the same host as Caddy, using the local Docker socket.
"""

import io
import json
import logging
import os
import tarfile

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
PORT = int(os.environ.get("PORT", "8000"))

docker_client = docker.DockerClient(base_url=DOCKER_SOCKET)
server = Server("caddy-mcp")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_container():
    """Get the Caddy container, raising a clear error if not found."""
    return docker_client.containers.get(CADDY_CONTAINER)


def pack_tar(filename: str, content: bytes) -> io.BytesIO:
    """Wrap bytes in a tar archive suitable for docker put_archive."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo(name=filename)
        info.size = len(content)
        tar.addfile(info, io.BytesIO(content))
    buf.seek(0)
    return buf


# ---------------------------------------------------------------------------
# MCP tool definitions
# ---------------------------------------------------------------------------

@server.list_tools()
async def list_tools():
    return [
        Tool(
            name="caddy_read_config",
            description="Read the current Caddyfile from disk.",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="caddy_write_config",
            description=(
                "Write a new Caddyfile to disk. "
                "Always call caddy_validate first, then caddy_reload after writing."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "config": {
                        "type": "string",
                        "description": "The complete Caddyfile content to write.",
                    }
                },
                "required": ["config"],
            },
        ),
        Tool(
            name="caddy_validate",
            description=(
                "Validate a Caddyfile without applying it. "
                "Returns any syntax or configuration errors."
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
                        "description": "Number of log lines to return (default: 100).",
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

@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list:
    try:
        # -- Read Caddyfile --------------------------------------------------
        if name == "caddy_read_config":
            container = get_container()
            result = container.exec_run(["cat", CADDY_CONTAINER_CONFIG], demux=False)
            content = result.output.decode("utf-8", errors="replace")
            return [TextContent(type="text", text=content)]

        # -- Write Caddyfile -------------------------------------------------
        elif name == "caddy_write_config":
            config = arguments["config"]
            container = get_container()
            filename = os.path.basename(CADDY_CONTAINER_CONFIG)
            directory = os.path.dirname(CADDY_CONTAINER_CONFIG)
            tar = pack_tar(filename, config.encode("utf-8"))
            container.put_archive(directory, tar)
            log.info("Caddyfile written to %s in container", CADDY_CONTAINER_CONFIG)
            return [TextContent(type="text", text="✓ Caddyfile written. Call caddy_reload to apply.")]

        # -- Validate --------------------------------------------------------
        elif name == "caddy_validate":
            config = arguments["config"]
            container = get_container()
            # Copy config into the container at a temp path, validate, then clean up.
            tar = pack_tar("Caddyfile.validate", config.encode("utf-8"))
            container.put_archive("/tmp", tar)
            result = container.exec_run(
                ["caddy", "validate", "--config", "/tmp/Caddyfile.validate", "--adapter", "caddyfile"],
                demux=False,
            )
            container.exec_run(["rm", "-f", "/tmp/Caddyfile.validate"])
            output = result.output.decode("utf-8", errors="replace").strip()
            success = result.exit_code == 0
            prefix = "✓ Valid" if success else "✗ Invalid"
            return [TextContent(type="text", text=f"{prefix}\n{output}" if output else prefix)]

        # -- Reload ----------------------------------------------------------
        elif name == "caddy_reload":
            container = get_container()
            result = container.exec_run(
                [
                    "caddy", "reload",
                    "--config", CADDY_CONTAINER_CONFIG,
                    "--adapter", "caddyfile",
                ],
                demux=False,
            )
            output = result.output.decode("utf-8", errors="replace").strip()
            success = result.exit_code == 0
            prefix = "✓ Caddy reloaded successfully" if success else "✗ Reload failed"
            log.info("caddy reload: exit=%d", result.exit_code)
            return [TextContent(type="text", text=f"{prefix}\n{output}" if output else prefix)]

        # -- Logs ------------------------------------------------------------
        elif name == "caddy_get_logs":
            lines = int(arguments.get("lines", 100))
            container = get_container()
            logs = container.logs(tail=lines, timestamps=True).decode("utf-8", errors="replace")
            return [TextContent(type="text", text=logs or "(no logs)")]

        # -- Status ----------------------------------------------------------
        elif name == "caddy_status":
            container = get_container()
            container.reload()
            state = container.attrs.get("State", {})
            tags = container.image.tags
            info = {
                "name": container.name,
                "status": container.status,
                "id": container.short_id,
                "image": tags[0] if tags else "unknown",
                "running": state.get("Running", False),
                "started_at": state.get("StartedAt", ""),
                "restart_count": container.attrs.get("RestartCount", 0),
            }
            return [TextContent(type="text", text=json.dumps(info, indent=2))]

        else:
            return [TextContent(type="text", text=f"Unknown tool: {name}")]

    except docker.errors.NotFound:
        return [TextContent(type="text", text=f"Error: container '{CADDY_CONTAINER}' not found. Check CADDY_CONTAINER env var.")]
    except FileNotFoundError:
        return [TextContent(type="text", text=f"Error: Caddyfile not found at {CADDYFILE_PATH}. Check CADDYFILE_PATH env var.")]
    except Exception as exc:
        log.exception("Tool error in %s", name)
        return [TextContent(type="text", text=f"Error: {exc}")]


# ---------------------------------------------------------------------------
# HTTP app (SSE transport + optional API key auth)
# ---------------------------------------------------------------------------

class ApiKeyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if MCP_API_KEY and request.url.path != "/health":
            key = (
                request.headers.get("x-api-key")
                or request.query_params.get("api_key")
            )
            if key != MCP_API_KEY:
                return Response("Unauthorized", status_code=401)
        return await call_next(request)


def create_app() -> Starlette:
    sse = SseServerTransport("/messages/")

    async def handle_sse(request: Request):
        async with sse.connect_sse(
            request.scope, request.receive, request._send
        ) as streams:
            await server.run(
                streams[0],
                streams[1],
                server.create_initialization_options(),
            )

    async def health(_: Request):
        try:
            docker_client.ping()
            container = get_container()
            return Response(
                json.dumps({"status": "ok", "caddy": container.status}),
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
    log.info("Caddy container: %s", CADDY_CONTAINER)
    log.info("Caddy config   : %s", CADDY_CONTAINER_CONFIG)
    log.info("API key auth  : %s", "enabled" if MCP_API_KEY else "disabled")
    uvicorn.run(create_app(), host="0.0.0.0", port=PORT)
