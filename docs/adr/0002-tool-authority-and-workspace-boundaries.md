# ADR-0002: Tool authority and workspace boundaries

- Status: Accepted
- Date: 2026-07-21

## Context

Continue can invoke tools without per-call approval. A lexical path prefix is
not a sufficient boundary because absolute paths, traversal, and symlinks can
escape it. Conversely, arbitrary shell commands cannot be safely reduced to a
set of path checks.

## Decision

Apply defense-in-depth path scoping for filesystem read, search, and edit
operations against the realpath of `MCP_WORKSPACE` plus explicit
`MCP_JAIL_EXTRA` roots. Keep scoping on by default, but do not describe it as a
sandbox or security boundary. Recommend Automatic only for scoped read-only
capabilities and Ask First for shell and file mutation.

## Consequences

Symlink escapes and adversarial path spellings are refused with structured
errors. Legitimate access outside configured roots requires an explicit extra
root, a deliberate scope override, or the approval-gated shell. Realpath checks
reduce mistakes but cannot provide complete containment or eliminate TOCTOU.
