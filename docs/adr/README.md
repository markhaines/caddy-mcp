# Architecture Decision Records

One numbered markdown file per decision. **Append-only:** an accepted ADR is never edited to
change what it says. To reverse one, write a new ADR, set its `Supersedes:` line, and flip
the old one's status to `Superseded by ADR-00NN`. The trail is the point, and a rewritten
ADR is a trail that lies.

Copy `0000-template.md` to start a new one. Format and conventions: the `dev-standards`
skill, section "Recording decisions".

## Why this exists

Tests stop the code regressing. They do nothing to stop a fresh session re-litigating a
decision already made, or replacing something deliberate with the off-the-shelf thing it was
deliberately not. Read the index below before proposing a change to how this repo is built.

## When one is owed

Any time options were weighed, any implicit decision that constrains what can be built
later, and any reversal of an earlier ADR. Not every commit, and not what is already obvious
from reading the code. The test is whether it would drift if nobody wrote it down.

## Index

| ADR | Title | Status |
|---|---|---|
| [0001](0001-validate-before-reload.md) | Validate before reload, pinned by a test | Accepted |

## Candidates, not yet written

Decisions already made and visible in this repo's docs or code, but not yet recorded here.
Write one when it next comes up, not in a batch.

- Why the server talks to the Caddy container over the Docker API rather than the host filesystem
- Why an empty MCP_API_KEY disables auth entirely rather than defaulting to a generated key
- Why ROOT_PATH handling carries an explicit redirect fix
