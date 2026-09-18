# ADR-0001: Validate before reload, pinned by a test

- **Status:** Accepted
- **Date:** 2026-09-18
- **Supersedes:** none

## Context

This server hands an AI assistant write access to the config of the reverse proxy that
fronts everything. The failure mode is not a bad edit, which is expected and recoverable.
It is a bad edit that gets **applied**, because a Caddyfile that fails to parse takes the
proxy down, and the proxy is how you would reach the thing you use to fix it.

## Decision

`caddy_validate` runs Caddy's own config check, and validation is expected before reload
rather than offered alongside it. A broken Caddyfile is caught before it takes the proxy
down rather than after.

## Consequences

Two calls where one would do, on every change. That is the point: the write and the apply
are separate steps with a gate between them, so an assistant cannot do both in one
uninterrupted motion.

Validation uses Caddy's own checker inside the running container, so it is testing the exact
binary and version that will load the config, not an approximation.

## Options rejected

- **Reload and roll back on failure.** The rollback path needs the proxy, which is the thing
  that is down.
- **Validate implicitly inside reload.** Removes the caller's chance to see the error and
  fix it before anything is applied.
