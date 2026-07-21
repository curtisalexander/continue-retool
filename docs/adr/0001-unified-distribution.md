# ADR-0001: One distribution with selective activation

- Status: Accepted
- Date: 2026-07-21

## Context

Independent per-server projects duplicated lockfiles, environments, releases,
installer syncs, and safety helpers. Keeping an unused server off disk did not
provide a meaningful security boundary; whether Continue starts it determines
whether it receives tool authority.

## Decision

Ship every server in one `continue-mcp` Python distribution with one lockfile
and virtual environment. Preserve separate console entry points and operating
system processes. Keep `--only`, but define it as direct-registration or gateway-
downstream selection rather than package selection.

## Consequences

Shared safety behavior has one implementation and installation performs one
dependency sync. Releases are coordinated by construction. All code is present
in the environment, while unregistered servers remain inert. Adding a server
requires updating the package, installer, tests, audit, documentation, and the
chosen registration topology.
