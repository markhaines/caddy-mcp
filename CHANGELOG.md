# Changelog: caddy-mcp

All notable changes to this project. Semver, tagged `vMAJOR.MINOR.PATCH`.

Reconstructed from git history on 2026-09-08.

## Unreleased

### Fixed
- **The "Caddyfile not found" handler crashed instead of reporting.** The config variable
  was renamed to `CADDY_CONTAINER_CONFIG` everywhere except the `FileNotFoundError` branch,
  which still referenced `CADDYFILE_PATH`. That branch raised `NameError` from inside the
  `except`, so the message written to explain the problem was itself the thing that broke,
  and only at the moment it was needed. Found by lint and typecheck the first time they ran.
- `create_app()` was annotated `-> Starlette` but returns the `NormalizeMcpPath` ASGI
  wrapper. Annotation corrected to `ASGIApp`.

### Added
- 13 tests: `pack_tar` round-tripping (including the header size that `put_archive`
  silently truncates on), the `/mcp` path rewrite, and the API-key middleware in both
  directions.
- Lint (`ruff`), typecheck (`mypy`) and the tests, all running in CI on every push and
  pull request.
- A real README (this is a public repo and had a one-line stub), an MIT `LICENSE`, and this
  changelog. That completes Dev tier 3.
- A Security section, with tests pinning both behaviours it describes:
  - an empty `MCP_API_KEY` disables authentication entirely, on a server whose whole job is
    rewriting the reverse proxy's configuration
  - the key is also accepted as an `?api_key=` query parameter, which puts it in access
    logs and browser history

### Changed
- The Docker client is created on first use rather than at import. It used to be a
  module-level `docker.DockerClient(...)`, which connects immediately, so `import server`
  raised `DockerException` anywhere without a running daemon. Nothing about packing a tar
  or checking an API key needs Docker, and this is what made any of it testable.

## 2026-09-08

### Fixed
- Pinned `mcp==1.27.0`. It was `>=1.0.0`, and a rebuild resolved to 2.2.0, which removes
  the low-level decorator API this server is built on. The container crash-looped on
  `AttributeError: 'Server' object has no attribute 'list_tools'`.

## Earlier

### Added
- MCP server for managing a Caddy reverse proxy through the Docker API: read, write,
  validate and reload the Caddyfile, plus container logs and status.
- Streamable HTTP and legacy SSE transports, optional API-key auth, and support for being
  mounted under a reverse-proxy path prefix.
