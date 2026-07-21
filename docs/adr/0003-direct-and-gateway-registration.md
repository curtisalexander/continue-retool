# ADR-0003: Support direct and gateway registration

- Status: Accepted
- Date: 2026-07-21

## Context

Directly registered tools have no discovery hop, but every schema occupies the
model context on every request. A gateway makes a large, rarely used tool tail
cheap at rest, at the cost of search/describe/call latency and broader routing
authority.

## Decision

Support both topologies. Register hot tools directly. Put a sufficiently large
long tail behind `gateway-mcp`, which owns its downstream processes and exposes
three discovery tools. Never register the same server directly and behind the
gateway in one workspace; the installer enforces the separation.

## Consequences

Operators choose latency versus resting schema cost without changing package
contents. Gateway startup degrades when an individual downstream is unavailable
and exposes the available catalog; doctor mode verifies the selected downstream
set when a healthy installation is required.
