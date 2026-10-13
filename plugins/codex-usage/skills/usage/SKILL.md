---
name: usage
description: Start or stop the local Codex usage collector, show usage statistics, or reset current-session or all-session averages. Use when Codex Usage is mentioned.
---

Use the plugin's dedicated MCP tools. Do not implement another collector or poll
in a shell loop. Collection runs on its own timer after startup.

- For stats or reset current, obtain the actual requesting Codex thread ID from
  verified thread context. If unavailable, read `CODEX_THREAD_ID` with the shell
  tool once. Never use an MCP connection ID or invent a session ID. If no verified
  ID exists, explain that current-session statistics/reset cannot be attributed.
- Stats: call `usage_stats(session_id)`. If stopped, say that explicit
  `usage_start()` is available. Do not silently start collection for a stats read.
- Start: call `usage_start()`. Stop: call `usage_stop()`; it affects all sessions.
- Reset current: call `usage_reset_current(session_id)` only when requested.
- Reset all: call `usage_reset_all()` only when all-session reset is requested.
  An ambiguous reset means current-session reset.

Report collector state, sample age, quota remaining, and available forecasts or
credit rates. Keep collecting/unknown/stale states explicit. These are account-wide
measurements; a current-session reset changes the calculation baseline, not account
quota attribution. Both resets preserve history and never redeem account resets.

An enabled and trusted SessionStart hook can start the collector automatically.
Installation or mention alone is not proof it ran. The collector stops when its
attached desktop clients exit; closing a chat does not stop it.
