# caddy-mcp

Dev tier: 3 (public)

**Let an AI assistant read, edit, validate and reload your Caddy reverse proxy — safely,
over MCP.**

Caddy's config lives inside a container, which makes it exactly the kind of thing you end
up SSHing in to poke at by hand. This is a small [MCP](https://modelcontextprotocol.io)
server that exposes it as a handful of tools instead, so Claude (or any MCP client) can
read the Caddyfile, propose a change, **validate it before applying**, and reload.

- **Validate before reload.** `caddy_validate` runs Caddy's own config check, so a broken
  Caddyfile is caught before it takes the proxy down rather than after.
- **Talks to the container, not the host.** Reads and writes the Caddyfile inside the
  running Caddy container over the Docker API, so it works wherever Caddy actually lives.
- **Logs on tap.** `caddy_get_logs` pulls recent container logs without a shell.
- **Runs behind a reverse proxy.** Supports being mounted under a path prefix, with the
  redirect trap that usually breaks that already handled (see below).

## Tools

| Tool | What it does |
|---|---|
| `caddy_read_config` | Read the current Caddyfile from the container |
| `caddy_write_config` | Write a new Caddyfile into the container |
| `caddy_validate` | Run Caddy's own config validation |
| `caddy_reload` | Reload Caddy with the current config |
| `caddy_get_logs` | Recent container logs |
| `caddy_status` | Container status, uptime and restart count |

## Install

```sh
git clone https://github.com/markhaines/caddy-mcp
cd caddy-mcp
MCP_API_KEY=$(openssl rand -hex 32) docker compose up -d
```

Then point your MCP client at `http://<host>:8811/mcp`, sending the key as an `x-api-key`
header.

## Configuration

| Variable | Default | What it is |
|---|---|---|
| `DOCKER_SOCKET` | `unix:///var/run/docker.sock` | How to reach Docker. Can be a `tcp://` endpoint for a remote host. |
| `CADDY_CONTAINER` | `caddy` | Name of the Caddy container |
| `CADDY_CONTAINER_CONFIG` | `/etc/caddy/Caddyfile` | Caddyfile path *inside* that container |
| `MCP_API_KEY` | *(empty)* | Shared secret. **See the warning below.** |
| `PORT` | `8000` | Listen port inside the container |
| `ROOT_PATH` | *(empty)* | Path prefix when mounted behind a reverse proxy, e.g. `/caddy` |

## Security

⚠️ **An empty `MCP_API_KEY` disables authentication completely.** There is no warning and
no safe default: the server starts, serves every tool, and anyone who can reach the port
can rewrite your reverse proxy's configuration. Always set it. This is pinned by a test so
it cannot change silently, but it is still the sharpest edge here.

⚠️ **The key is also accepted as an `?api_key=` query parameter.** That is supported
because some MCP clients cannot set headers, but a key in a URL ends up in the reverse
proxy's access log, in browser history, and in any `Referer` header. Prefer the `x-api-key`
header, and do not enable query-parameter auth on anything internet-facing without knowing
where your logs go.

The key comparison is a plain `!=` rather than a constant-time compare.

## Behind a reverse proxy

Set `ROOT_PATH=/caddy` and the SSE endpoint emits URLs carrying the prefix, so clients POST
back through the same proxy.

One trap is handled in-process: Starlette answers a bare `/mcp` with a 307 to `/mcp/`, and
that `Location` is root-relative. Behind a prefix, a client following it lands on
`https://host/mcp/` and escapes the prefix entirely. `NormalizeMcpPath` rewrites the path
inside the ASGI scope instead, so both `/mcp` and `/mcp/` work under any prefix with no
redirect on the wire.

## Development

```sh
pip install -r requirements-dev.txt
pytest -q          # tests, no Docker daemon needed
ruff check .       # lint
mypy .             # typecheck
```

CI runs all three on every push and pull request.

The Docker client is created on first use rather than at import, specifically so the module
can be imported without a running daemon. It used to connect at import time, which made the
whole file untestable: `import server` raised before any test could run.

## Version pinning

`mcp` is pinned to `1.27.0`. Version 2.x removes the low-level `@server.list_tools()` /
`@server.call_tool()` decorator API this server is built on; an unpinned rebuild on
2026-09-08 resolved to 2.2.0 and crash-looped on
`AttributeError: 'Server' object has no attribute 'list_tools'`. Migrating to the 2.x API
is a separate job.

## Releases

Semver, tagged `vMAJOR.MINOR.PATCH`. See [CHANGELOG.md](CHANGELOG.md).

## Licence

[MIT](LICENSE).
