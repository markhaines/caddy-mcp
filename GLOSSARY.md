# Glossary

| Term | Here it means |
|---|---|
| **Caddyfile** | The config file *inside* the running Caddy container, at `CADDY_CONTAINER_CONFIG`. Not a file on the host. |
| **Validate** | Caddy's own config check, run against the same binary that will load it. The gate before reload. |
| **Reload** | Applying the current config to the running proxy with no restart. |
| **`ROOT_PATH`** | The path prefix when this server is itself mounted behind a reverse proxy. |
| **`MCP_API_KEY`** | The shared secret. Empty means no authentication at all, with no warning. |
