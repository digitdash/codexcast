# Validation and support

This repository distributes the existing local estimator and plugin wrapper.
The collector and calculation modules were transferred unchanged. Private
telemetry captures and personal runtime configuration were excluded.

## Automated validation

On 2026-10-13, all 252 tests passed locally, including the 237 inherited estimator
and plugin tests and 15 installer regressions. The installer tests cover
desktop-binary discovery and supported-client fallback, explicit paths with spaces,
missing executables, older clients without plugin support, unsupported platforms,
older Python versions, fresh Codex homes, marketplace layout, and collector paths.
Prerequisite failures are checked before creating installation files. Long home
paths use a shorter socket directory; existing working baseline locations are
retained, and explicit relative directories are resolved during installation.

The inherited coverage includes independent timer collection, simultaneous starts,
singleton locking, crash/error recovery, stale/stopped stats, both reset scopes,
preserved telemetry, response boundaries, MCP schema errors/reconnects, packaged
launcher independence, and owner-process exit cleanup. Tests use synthetic data
and local subprocesses rather than account credentials or model turns.

## Installed-package validation

A fresh temporary home and Codex profile were used with the actual bundled
desktop client, `codex-cli 0.162.0-alpha.17.2`, and Python MCP SDK 1.30.0:

- The README's installer command completed without a manually supplied executable.
- Native plugin discovery reported **Codex Usage**, installed and enabled, with
  its routing skill and connected `codex_usage` MCP server.
- All five tools were exposed. Stats returned a structured stopped result without
  starting collection; an invalid current-reset session ID was rejected.
- Reinstallation retained a synthetic history file, reset-cutoff file, and an
  unrelated marketplace entry, and created a backup of the existing catalog.
- A long home path and a previously nonexistent custom Codex home installed
  successfully and exposed all five tools. The shortened socket path was below
  the macOS limit, and reinstallation preserved its data and runtime location.
- No model turns were submitted by these installation checks.

A built wheel also installed in a fresh environment without dependencies, and
the standard-library CLI returned its version without the MCP SDK installed.

The reused implementation was also exercised in the physical desktop on
2026-10-09. User-confirmed mention/stats requests worked; collector samples
advanced during a 96-second gap without stats calls. Two native protocol sessions
shared one collector/provider. Physical desktop quit/reopen stopped the old
collector/provider and started a new collector through the trusted hook.

These are separate kinds of evidence: installation and MCP operation were checked
in the fresh profile; authenticated collection and physical hook/lifecycle behavior
were checked with the reused implementation in the original desktop setup.

## Limits

macOS desktop operation is supported. Local session counters cannot explain all
activity on other devices. Account quotas/balances remain account-wide; resetting
a chat changes its calculation baseline. Forecasts retain their existing evidence
thresholds and may show collecting or low confidence. No public-directory
eligibility is claimed by distributing the plugin on GitHub.
