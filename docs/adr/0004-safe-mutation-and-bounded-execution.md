# ADR-0004: Safe mutation and bounded execution

- Status: Accepted
- Date: 2026-07-21

## Context

Agent-generated inputs can be malformed or unexpectedly large. Directly
truncating a destination before encoding succeeds risks data loss. Unbounded
reads, searches, process output, concurrency, and retained job state can exhaust
memory or flood model context.

## Decision

Encode before mutation and replace ordinary files atomically through a flushed,
`fsync`ed sibling temporary file. Preserve mode bits, clean up failed temporary
files, and use bounded optimistic conflict detection. Bound bytes, lines,
entries, depth, matches, time, jobs, logs, and retained state. Drain subprocess
streams concurrently, preserve incremental decoder state, and kill process
groups on timeout and shutdown. Return expected failures as structured results.

## Consequences

Failed create/edit operations preserve existing bytes, and external changes are
not silently overwritten. Partial results explicitly report truncation. Atomic
replacement changes the inode and therefore does not preserve hard-link identity
or every form of special metadata; symlinks and ordinary permission bits require
explicit handling and tests.
