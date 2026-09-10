# Continue compatibility and migration

This toolkit is not claimed to be a drop-in-complete Continue replacement and has
not been live-tested in VS Code. Its project installation target is a
saved, local, single-root workspace. For terminal-only project or personal/global
registration and opt-in no-prompt execution, follow the
[shell setup guide](shell-mcp/README.md#setup). Global registration uses a fixed
home-directory default, not automatic active-workspace routing.
For remote work, install on the same host and
filesystem as the workspace and explicitly validate with `--check`. Multi-root
workspaces require manual per-root configuration with distinct server names and
are not automatically supported. Distinct names change the tool prefixes below.
Use filesystem paths, not `file://`/`vscode-remote://` URIs or `~` shorthand.

## Tool mapping

Use these exact mappings when migrating prompts or policy:

| Continue/tool purpose | MCP tool |
|---|---|
| filesystem read | `fs_read` |
| directory list | `fs_list` |
| content grep | `search_grep` |
| file search | `search_files` |
| foreground shell | `shell_run` |
| background shell lifecycle | `shell_start`, `shell_output`, `shell_poll`, `shell_kill`, `shell_list_jobs`, `shell_send` |
| optional file mutation | `edit_edit`, `edit_create_file` |

Retain Continue's `read_currently_open_file` and `read_file` for dirty editor
buffers, which disk-based MCP reads cannot see. Retain the interactive
`edit_existing_file`, `single_find_and_replace`, `multi_edit`, and
`create_new_file` tools. Only after verifying the mapped MCP tools in your actual
environment should you consider disabling Continue's `ls`, `grep_search`,
`file_glob_search`, and `run_terminal_command`.

Continue's reviewed MCP call path does not pass its cancellation signal at
[the pinned source baseline](https://github.com/continuedev/continue/blob/5522c6f44ca0ac3528b37244818fbfa39b5af470/core/tools/callTool.ts#L102-L109).
Do not rely on built-in **Stop**; explicitly call `shell_kill` for a
running background job. Do not infer permission behavior from MCP annotations:
annotations are not Continue policy. Configure shell and edit as **Ask First**
by default. For intentionally unattended terminal execution, explicitly set the
shell tools to **Automatic** in Continue's per-user tool settings using the guide
above; installing MCP YAML alone does not remove approval prompts. Shell commands
have your account's full authority. Fs and search may also be **Automatic** if
that matches your threat model.

The installer intentionally makes no automatic changes to user model configuration
or built-in permissions. The default changed from shell/fs/search/edit to
shell/fs/search. Existing edit YAML is warned about and retained, never silently
deleted: choose `--with-edit` to keep edit selected, or manually remove the YAML
after reviewing the migration.

## Before disabling built-ins

1. Record the installed Continue extension version, VS Code version, OS, and
   workspace topology. The source baseline above is not a certified extension
   release; no live extension version is certified by this repository yet.
2. Install into a disposable project, then run the same selection with `--check`.
   For example, `uv run --project continue-mcp --no-sync python
   continue-mcp/install-workspace.py /path/to/project --check`. Include
   `--with-edit`/`--with-sql` if installed. Every selected server must pass an
   actual tool call. This check is not a VS Code integration test.
3. Reload Continue and verify there is one copy of each selected tool, no name
   collision warning, and no accidentally retained old server group. In the
   Agent-mode tool settings, configure each tool's policy explicitly. Decline a
   shell request and confirm it has no side effects before allowing another.
4. Exercise `fs_read`, `fs_list`, `search_grep`, and `search_files` on a known
   fixture. Page through a line longer than 2,000 characters using both returned
   coordinates. Search an invalid regex and confirm the error explains why it
   failed rather than appearing to be an empty successful search.
5. Leave a document dirty. Confirm the retained built-in read sees the unsaved
   text, and the built-in edit preserves it through accept/reject. Do not use
   disk edit against that document. With `--with-edit`, test only saved fixture
   files, including dry-run, a rejected non-unique match, and successful edits.
6. Start a harmless long-running shell job. Recover its job ID and cursors from
   the displayed text, request subsequent output, and explicitly kill it. Verify
   the process and its child exit. Test the server timeout independently of Stop.
   Foreground calls default to 30 seconds; use background tools for long jobs.
7. Verify the actual GUI-launched environment can find project executables and
   credentials it needs. Continue does not necessarily forward your terminal's
   entire environment. Do not copy secrets into committed YAML; use your reviewed
   environment/configuration mechanism. For remote work, verify commands and
   reads actually execute against the remote workspace, not a local directory.
8. Only then disable the four built-ins listed above. Retain their previous
   policy settings for rollback. To roll back, disable the MCP groups and
   re-enable those built-ins; do not delete project data.

## What automated checks establish

The suites test real stdio framing, text-only read pagination, protocol errors,
text-based shell cursors, MCP cancellation while the client stays connected,
spill failures, bounds, and functional installation. They deliberately do not
treat FastMCP `result.data` as evidence that Continue's model sees those fields.
Continue's pinned adapter consumes `content` and handles `isError`, but ignores
`structuredContent`; both result forms are maintained for other clients.

Windows/macOS CI and live extension acceptance are separate evidence. A Linux
test run does not certify PowerShell, cmd, remote routing, UI permissions, or
unsaved-buffer integration. Preserve the results of the acceptance checklist
with the exact extension version before calling a deployment a replacement.

## Optional Windows model acceptance scenarios

Run these in a disposable, saved single-root Windows workspace through the
actual Continue Agent mode, separately from deterministic CI. Record the model,
extension/VS Code versions, PowerShell version, tool policy, actual tool-call
arguments and text results. Use a fresh unpredictable marker per run, and verify
output/side effects rather than trusting the model's claim of success. These
scenarios use model tokens and require a configured Continue host; a standalone
MCP client is not a substitute.

| Scenario | Prompt/task | Evidence required |
|---|---|---|
| Unscripted shell choice | "This is Windows. Report the current directory and value of TEMP, then print `<marker> 雪🚀`." Do not prescribe commands or tool names. | MCP shell selected; correct PowerShell syntax (`$env:TEMP`, quoted paths with `&` when invoking); no redundant interpreter; exact marker/Unicode in actual stdout. |
| Incremental output | Ask it to start a job emitting a numbered marker once per second, retrieve two successive batches, then stop it. | Returned job ID reused; stdout/stderr cursors passed back separately; no duplicate lines; explicit `shell_kill`, verified process/child exit. |
| Truncation recovery | Ask for numbered output larger than the configured memory cap and recovery of a known middle line. | Truncation marker and workspace spill path visible in text; fs/search reads actual spill content using reported encoding; no fabricated missing lines. |
| Timeout and retry | Ask for a harmless command that exceeds a short server timeout, then a short successful replacement. | First result is a timeout error with partial output; second is a new successful call, not a claim that the timed-out job succeeded. |
| Input and errors | Request an interactive line read and send a Unicode marker, then run an explicit `exit 7`. | `interactive=true` and `shell_send`; marker round trip; failure is visible through `isError` and text, not only structured data. |
| Permissions and Stop | Decline a shell request, then separately allow a long background job and press Continue Stop. | Declined request has no side effect; do not assume Stop kills the job—explicitly invoke `shell_kill` and verify exit. |

Repeat relevant scenarios with PowerShell 7 and Windows PowerShell 5.1; the latter
must not receive `&&`/`||`. A prescribed-command test proves transport behavior,
not autonomous dialect selection, so retain the unscripted scenario.

The Pi reference extension can change active tools, intercept `!`, render job
widgets and post asynchronous failure notifications using Pi host APIs. This
MCP server does not provide those host capabilities. Do not automatically
disable Continue tools or claim notification/Stop integration without a tested
Continue adapter; configure built-ins and permissions using the checklist above.
