# caddy-mcp

Manage a Caddy reverse proxy by talking to it. Read the Caddyfile, validate a change, write it and reload, all from an MCP client instead of an SSH session.

[Install](#install) · [Quick start](#quick-start) · [Configuration](#configuration) · [Safety](#safety-notes)

---

## What it does

Adding a site to Caddy normally means SSHing to the proxy host, editing a file, hoping the syntax is right, and reloading. `caddy-mcp` puts that loop behind six MCP tools, so an assistant like Claude can read the current config, check a proposed change parses, apply it, and pull the container logs when something breaks.

It talks to Caddy through the Docker API rather than needing a shell on the box, so the proxy container itself stays untouched. Point it at a Docker socket (or a socket-proxy) and it does the rest.

## Features

- Reads the live Caddyfile straight from inside the running container, so what you see is what Caddy is actually serving
- Validates a proposed config before it goes anywhere near disk
- Writes and reloads as separate steps, so you can check the parse result first
- Container logs on demand when a reload fails, which is where Caddy prints the exact line number
- Status check covering container state, restart count and start time
- Ships with a `skill/` directory that teaches Claude the safe change workflow and a set of Caddyfile patterns
- Optional API key auth, so the endpoint can sit behind a reverse proxy without being open

## Install

**Requirements:** Docker, a running Caddy container, and network access to a Docker socket or socket-proxy.

```bash
git clone https://github.com/markhaines/caddy-mcp.git
cd caddy-mcp
echo "MCP_API_KEY=$(openssl rand -hex 32)" > .env
docker compose up -d
```

The server listens on port 8000 in the container. The compose file publishes it on 8811.

<details>
<summary>Running against a socket-proxy instead of the raw Docker socket</summary>

Mounting `/var/run/docker.sock` directly gives this container full control of the Docker daemon. A socket-proxy narrows that to the calls the server actually makes:

```yaml
environment:
  - DOCKER_SOCKET=tcp://your-socket-proxy:2375
```

The proxy needs `CONTAINERS`, `EXEC`, `POST`, `INFO`, `VERSION` and `IMAGES` enabled. `IMAGES` is easy to miss: the status tool reads image tags, which hits `/images/{id}/json` and returns 403 without it.
</details>

## Quick start

Connect your MCP client to the SSE endpoint:

```
http://your-host:8811/sse
```

Then ask for the current config:

```
> What sites are configured in Caddy right now?
```

The assistant calls `caddy_read_config` and comes back with the Caddyfile. Adding a site follows the same path: read, validate, write, reload.

Check the server is up without a key:

```bash
curl http://your-host:8811/health
```

```json
{"status": "ok", "caddy": "running"}
```

## Tools

| Tool | What it does |
|------|-------------|
| `caddy_read_config` | Read the current Caddyfile from inside the Caddy container |
| `caddy_validate` | Parse a proposed config without applying it |
| `caddy_write_config` | Replace the Caddyfile with new content |
| `caddy_reload` | Reload Caddy using the config currently on disk |
| `caddy_get_logs` | Fetch recent container logs |
| `caddy_status` | Container state, restart count and start time |

## Configuration

All configuration is environment variables.

| Variable | Default | What it does |
|----------|---------|--------------|
| `DOCKER_SOCKET` | `unix:///var/run/docker.sock` | How to reach Docker. Use `tcp://host:2375` for a socket-proxy. |
| `CADDY_CONTAINER` | `caddy` | Name of the Caddy container to manage. |
| `CADDY_CONTAINER_CONFIG` | `/etc/caddy/Caddyfile` | Path to the Caddyfile *inside* that container. |
| `MCP_API_KEY` | *(empty)* | Bearer token required on every request. Empty means auth is off. |
| `PORT` | `8000` | Port the server listens on inside the container. |
| `ROOT_PATH` | *(empty)* | Path prefix when mounted behind a reverse proxy, e.g. `/caddy`. |

### Behind a reverse proxy

Set `ROOT_PATH` to the prefix the proxy strips. With `ROOT_PATH=/caddy` and a Caddy block like:

```
handle_path /caddy/* {
    reverse_proxy caddy-mcp:8000
}
```

the server advertises its message endpoint as `/caddy/messages/...`, which the client posts back through the same proxy. Without it, the client posts to a path the proxy doesn't route and the session hangs after connecting.

## Safety notes

This server can replace the configuration of a proxy that may be fronting live sites. Worth knowing before you point it at production:

- `caddy_write_config` replaces the **entire** Caddyfile. It is not an append. Always read the current config first and submit the full file with your change folded in.
- If Caddy runs with `--watch`, a written config can be picked up without an explicit `caddy_reload`, so a bad write applies immediately.
- Leaving `MCP_API_KEY` empty disables authentication completely. If the port is reachable beyond localhost, set a key.
- The Docker permissions this server needs include `exec` and archive upload against the Caddy container. Prefer a scoped socket-proxy over mounting the raw socket.

## Deploying an update

Two different commands, two very different outcomes. The distinction matters because the compose file pins `image:` as well as `build:`.

```bash
docker compose up -d          # reuses the existing local image
docker compose up -d --build  # rebuilds from the Dockerfile
```

A plain `up -d` recreates the containers but **reuses the `caddy-mcp:latest` image already on the host**. Changes to the Dockerfile or `requirements.txt` do not take effect. That makes it the safe option for config-only changes.

Rebuilding with `--build` is the deliberate one: it is where a new base image, new dependency pins and a non-root user all take effect at once. Tag the current image as a rollback first:

```bash
docker tag caddy-mcp:latest caddy-mcp:rollback
docker compose up -d --build
```

If the rebuild misbehaves, point `image:` at `caddy-mcp:rollback` and `up -d` again.

A few things worth knowing:

- Run compose commands from the directory holding the `.env` file. `MCP_API_KEY` uses the required-variable form, so `down`, `restart`, `logs` and `ps` all refuse to run without it. For a CI syntax check, `MCP_API_KEY=dummy docker compose config` is enough.
- The socket-proxy is pinned by digest, which also pins away its security updates. Bump it deliberately rather than expecting a `pull` to do it.
- `docker compose pull` will try to fetch the locally-built `caddy-mcp:latest` and fail. Use `--ignore-buildable` if you script a pull step.

## The Claude skill

`skill/SKILL.md` is a Claude skill that describes the change workflow and a library of Caddyfile patterns (reverse proxy, internal TLS, basic auth, redirects) plus fixes for the usual 502 and reload failures. Copy it into your skills directory to have Claude follow the safe ordering by default.

## License

No license file yet. Treat as all rights reserved until one is added.
