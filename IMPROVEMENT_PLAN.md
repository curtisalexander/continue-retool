# Repository improvement plan

This plan turns the July 2026 repository review into small, testable changes.
Safety work comes first: a tool should fail without damaging existing data,
crossing its configured path boundary, exhausting memory, or leaving runaway
processes behind.

## Design principles

- **Fail without data loss.** Encode and validate before mutation; replace files
  atomically; preserve the previous file when any step fails.
- **Resolve, contain, then act.** Normalize paths, resolve symlinks, verify the
  real target is in an allowed root, and minimize time between checking and use.
- **Bound model-facing and internal work.** Cap bytes, entries, matches,
  recursion depth, subprocess time, concurrent jobs, and retained artifacts.
- **Return actionable structured errors.** Expected filesystem and subprocess
  failures must not escape as protocol exceptions or masquerade as empty success.
- **Keep selective activation.** The toolkit may install as one distribution,
  but users choose which servers Continue registers or places behind the gateway;
  unselected server code never starts or receives tool authority.
- **Test claims, not implementation details.** Every safety or setup promise in
  the documentation should have an end-to-end regression test.

## Reference: Pi coding agent

Pi is a useful behavioral reference, especially for Unicode matching, path
variants, per-file mutation serialization, and matching multiple edits against
one original snapshot. It is not the ceiling for write durability: its current
local edit/write operations call Node's `writeFile` directly rather than using
atomic replacement.

For this toolkit, an atomic local write means:

1. Encode the entire new content before touching the destination.
2. Create a uniquely named sibling file in the destination directory.
3. Write, flush, and `fsync` that file; preserve the destination's mode when it
   exists.
4. Replace the destination with `os.replace`, which is atomic on the same
   filesystem.
5. Best-effort `fsync` the parent directory on platforms that support it.
6. Remove the sibling file on every pre-replacement failure.

A sibling temporary file is intentional: a system temporary directory may be a
different filesystem, where rename is not atomic. This changes the inode and can
affect hard links or special metadata, so the implementation must explicitly
handle symlinks, preserve ordinary permission bits, and document those limits.

## Phase 1 — safe editing

- [x] Replace direct truncating writes in edit-mcp with the atomic write
  sequence above.
- [x] Return structured write/encoding errors and prove the original bytes
  survive each failure.
- [x] Fix fuzzy `replace_all` so replacements target only matches found in the
  original normalized snapshot.
- [x] Fix the edit/fs Unicode-path implementation drift (notably U+202F in
  macOS screenshot names).
- [x] Add tests for permissions, symlinks, replacement failure, legacy encoding,
  replacement text containing the search text, and unchanged-line preservation.
- [x] Add bounded optimistic conflict detection so an external edit made after
  the initial read is never silently overwritten.

Acceptance: no failed edit/create operation truncates or partially changes an
existing file; fuzzy replacement cannot re-edit text it just inserted.

## Phase 2 — safe installation and recovery

- [x] Add an installation manifest with created-file hashes and previous-file
  backups.
- [x] Make installer writes atomic and quote/serialize paths safely.
- [x] On uninstall, remove only unchanged installer-owned files and restore
  pre-existing files instead of deleting their backups.
- [x] Make `--check` parse and launch the installed configuration, including its
  exact command, arguments, environment, and workspace.
- [x] Add install/reinstall/check/uninstall tests, including paths with spaces,
  `#`, quotes, and Windows-style drive syntax.

Acceptance: install followed by uninstall restores the exact initial project;
the doctor fails whenever the configuration Continue would launch is broken.

## Phase 3 — path and workload bounds

- [x] Confine notes storage after realpath resolution; reject absolute,
  traversal, and escaping-symlink directory configurations.
- [x] Make notes writes atomic and cap the note index as well as note reads and
  searches.
- [x] Enforce byte caps as bytes rather than characters.
- [x] Cap fs recursion depth and handle inaccessible entries without protocol
  exceptions.
- [x] Avoid scanning an entire large file merely to return a small page, or make
  exact total-line counting explicit and bounded.
- [x] Audit every environment-controlled numeric limit for sane minimums and
  maximums.

Acceptance: adversarial paths remain inside configured roots and adversarial
sizes/depths produce bounded, explicit partial results.

## Phase 4 — subprocess and lifecycle safety

- [x] Drain stdout and stderr concurrently in search-mcp and report ripgrep exit
  code 2 as an error, including for file listings.
- [x] Bound shell concurrency, validate timeouts, and kill all running process
  groups during server shutdown.
- [x] Add spill-log retention/removal when jobs are pruned.
- [x] Make incremental output decoding retain partial multibyte characters
  across polls.
- [x] Standardize subprocess spawn, timeout, and decoding failures as structured
  results.

Acceptance: no child can deadlock on a full pipe, survive server shutdown, grow
retained state without bound, or report a failed command as empty success.

## Phase 5 — gateway and setup

- [x] Add a supported gateway installation mode that stamps absolute `uv` and
  project paths, `--no-sync`, workspace environment, and downstream config.
- [x] Extend the doctor through the gateway-to-downstream handshake.
- [x] Decide whether gateway startup should degrade when one downstream server
  is unavailable and test the chosen behavior.
- [x] Add clean-wheel installation and console-script smoke tests to CI.

Acceptance: gateway users receive the same PATH-independent, offline-at-launch
setup guarantees as direct-server users.

## Phase 6 — unified packaging and shared infrastructure

Decision: distribute the toolkit as one Python project with one lockfile and
virtual environment. Preserve separate MCP processes and console entry points,
and keep `--only` as registration selection rather than package selection.
Installed-but-unregistered code has no runtime authority, while a single package
removes coordinated releases and generated-helper drift.

- [x] Replace the per-server projects, lockfiles, and environments with one root
  `continue-mcp` distribution exposing every existing console script.
- [x] Add policy-neutral common modules for bounded configuration, workspace
  paths/containment, Unicode path variants, and standard results.
- [x] Make direct and gateway installation stamp the shared project and perform
  one dependency sync while preserving selective registration.
- [x] Update wheel smoke tests, CI commands, audit tooling, documentation, and
  the new-tool scaffold for the unified project.

Acceptance: one clean wheel install provides every server entry point; the
installer performs one sync; `--only` exposes only the requested direct or
gateway servers; shared safety behavior has one implementation.

## Phase 7 — documentation and release hygiene

- [x] Create a concise current-state architecture guide and move superseded
  design exploration into ADRs/history.
- [x] Generate inventories and token examples from machine-readable metadata or
  audit output so counts cannot drift.
- [x] Update the site for fs, SQL, and notes and reconcile gateway measurements.
- [x] Make the new-tool scaffold update installer, CI, audit, documentation, and
  gateway/direct registration consistently.
- [x] Add a dependency-update process that excludes releases published within
  the last seven days.

Acceptance: a new contributor can install the toolkit, activate one server, run
its checks, understand its trust boundary, and add another server without
discovering hidden registries.
