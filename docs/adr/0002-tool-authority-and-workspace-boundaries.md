# ADR-0002: Tool authority and workspace boundaries

- Status: Accepted
- Date: 2026-07-21

## Context

Continue can invoke tools without per-call approval. A lexical path prefix is
not a sufficient boundary because absolute paths, traversal, and symlinks can
escape it. Conversely, arbitrary shell commands cannot be safely reduced to a
set of path checks.

## Decision

Jail filesystem read, search, and edit operations to the realpath of
`MCP_WORKSPACE` plus explicit `MCP_JAIL_EXTRA` roots. Keep the jail on by
default. Confine notes independently to repository-local storage. Recommend
Automatic only for confined/read-only capabilities and Ask First for shell and
file mutation. Treat gateway authority as the union of its downstreams.

## Consequences

Symlink escapes and adversarial path spellings are refused with structured
errors. Legitimate access outside configured roots requires an explicit extra
root, a deliberate jail override, or the approval-gated shell. Deployers must
not infer that the gateway itself is a sandbox.
