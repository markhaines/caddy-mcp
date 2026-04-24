---
name: caddy
description: >
  Manage the Caddy reverse proxy. Use this skill whenever the user mentions Caddy,
  reverse proxy, routing, SSL certificates, domain configuration, site hosting,
  proxy rules, or asks to add, update, remove, or troubleshoot any web service
  accessible via the homelab. Also use when the user wants to check Caddy logs,
  diagnose a 502/503 error, or inspect what domains are currently configured.
  Trigger on any mention of "Caddyfile", "caddy reload", or specific domain names
  that might be managed by the proxy.
---

# Caddy Management Skill

You have direct MCP access to the Caddy reverse proxy on docker-proxy1 via the
`caddy-mcp` tools. Use them — don't ask the user to run commands manually.

## Tools Available

| Tool | What it does |
|------|-------------|
| `caddy_read_config` | Read the full current Caddyfile from disk |
| `caddy_write_config` | Write a new Caddyfile (validate first, reload after) |
| `caddy_validate` | Validate a config without applying it |
| `caddy_reload` | Reload Caddy with the config currently on disk |
| `caddy_get_logs` | Fetch recent container logs (pass `lines` to control count) |
| `caddy_status` | Check container status, uptime, restart count |

## Standard Workflow for Any Config Change

Always follow this order — skipping steps risks downtime:

1. `caddy_read_config` — read the full existing config first
2. Prepare the updated config, making only the changes asked for and preserving everything else
3. `caddy_validate` — validate the new config before touching disk
4. If valid: `caddy_write_config` — write the new config
5. `caddy_reload` — apply it
6. If the reload fails: `caddy_get_logs` immediately to diagnose

Never skip validation. Never overwrite the full config without reading it first.

## Server Details

- Host: docker-proxy1 (192.168.10.41)
- Caddyfile on host: `/docker/caddy/caddyfile`
- Caddy container name: `caddy`
- Caddy config path inside container: `/etc/caddy/Caddyfile`

If you're unsure the container is running before making changes, call `caddy_status` first.

## Caddyfile Patterns

### Basic reverse proxy
```
service.domain.com {
    reverse_proxy 192.168.10.x:PORT
}
```

### With automatic HTTPS (Let's Encrypt)
```
# Caddy handles TLS automatically for public domains — just use the domain name.
service.example.com {
    reverse_proxy backend-host:8080
}
```

### Internal/local only (no TLS, or self-signed)
```
service.local {
    tls internal
    reverse_proxy 192.168.10.x:PORT
}
```

### With headers
```
service.domain.com {
    reverse_proxy backend:port {
        header_up Host {upstream_hostport}
        header_up X-Real-IP {remote_host}
    }
}
```

### Basic auth
```
service.domain.com {
    basicauth {
        # Generate hash: caddy hash-password --plaintext yourpassword
        username HASHED_PASSWORD_HERE
    }
    reverse_proxy backend:port
}
```

### Redirect
```
old.domain.com {
    redir https://new.domain.com{uri} permanent
}
```

## Common Issues

**502 Bad Gateway**: The upstream service is down or the IP/port is wrong. Check the backend is running.

**Reload fails**: Run `caddy_get_logs` immediately — Caddy logs the exact parse error with line number.

**TLS not working**: Public domains need port 80/443 open and DNS pointed at the server. Internal domains should use `tls internal`.

**Config looks correct but changes not appearing**: Confirm `caddy_reload` returned success. If it did, hard-refresh the browser (Caddy changes are instant).
