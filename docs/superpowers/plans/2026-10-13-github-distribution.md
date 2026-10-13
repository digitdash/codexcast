# GitHub distribution

Publish the existing local Codex Usage plugin in digitdash/codexcast. Keep its
collector, telemetry history, reset scopes, forecasts, and Codex display name.
The README must make installation and the five supported operations easy to use.

## Task 1: Reuse the tested implementation

Copy the existing Python source, plugin template, tests, and CI configuration into
the new repository without private history, database files, telemetry captures,
or machine-specific evidence. The empty repository requires an empty initial
main commit; product work stays in an isolated orchestrator branch with a draft PR.

Expected: the full inherited suite passes and the MCP tools keep their names.

## Task 2: Make setup one command

Keep the existing packaging installer and make it discover the desktop's bundled
Codex executable, validate prerequisites before writes, install the plugin through
the native CLI, and explain the next steps. Explicit path/config options remain.
Use tests before new installer behavior; validate a private installation through
the actual desktop client protocol without resetting the user's history.

Expected: a clean installation exposes Codex Usage and all five tools, and a
repeat installation preserves existing marketplace entries and telemetry.

## Task 3: Document, review, and publish

Write a concise README with copy/paste install instructions, hook trust and
explicit-start behavior, stats, both resets, stop, updates, and troubleshooting.
Document account-wide measurements and history-preserving resets. Run the full
suite and package checks, obtain independent review and the required GitHub
thumbs-up signal, then merge and verify the public main branch.

Expected: the public repository contains a working installer and readable guide,
the plugin stays named Codex Usage, and all required checks pass.

## Review focus

Check public-file hygiene, copied-code fidelity, setup paths with spaces,
fresh-install discovery, existing-user updates, custom Codex homes, prerequisite
failures before writes, and README commands against real installed behavior.
