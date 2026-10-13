# CodexCast — Codex Usage

See your Codex quota, credit balance, usage rates, and estimated time remaining
without leaving your chat. CodexCast is the repository name; the installed plugin
is called **Codex Usage**, so you can find it by typing `@` in Codex.

One local collector updates every 30 seconds while the desktop app is running.
Your chats share that collector. Asking for stats reads the latest saved sample;
it does not start another collector or spend a model request on each poll.

## Install

You need **macOS**, **Python 3.11 or newer**, and an updated Codex desktop app or
Codex in the ChatGPT desktop app, signed in with your ChatGPT account. No OpenAI
API key is needed. Check your Python version with `python3 --version`.

Open Terminal and run:

```sh
git clone https://github.com/digitdash/codexcast.git
cd codexcast
python3 scripts/install-local-plugin.py
```

The installer finds the desktop app's Codex executable, creates a separate Python
runtime, installs the MCP dependency, and installs **Codex Usage** through Codex's
plugin system. It preserves existing usage history and unrelated marketplace
entries. You can also download and unzip the repository with **Code → Download
ZIP**, open Terminal in that folder, and run the same Python command.

After the installer prints **Installed Codex Usage**:

1. Restart the desktop app and open a **new local Codex chat**.
2. Type `@`, select **Codex Usage**, and request **start**.
3. Request **@Codex Usage stats** to see your first reading.

For automatic startup, review and trust the plugin's **SessionStart** hook using
Codex's normal hook controls. Installing the plugin or enabling full access does
not grant hook trust. You can keep using **@Codex Usage start** when automatic
startup is unavailable. Trusted hooks start or attach to the same collector.

## Commands

Select **Codex Usage** from the `@` picker and use any of these requests:

| Request | What it does |
| --- | --- |
| **@Codex Usage stats** | Show collector health, sample age, quota, balances, usage rates, and available forecasts. |
| **@Codex Usage start** | Start the collector, or attach to it if it is already running. |
| **@Codex Usage reset current** | Restart averages and forecasts for this chat's calculation baseline. |
| **@Codex Usage reset all** | Restart calculation baselines for every chat using this plugin's database. |
| **@Codex Usage stop** | Stop the collector shared by your chats. |

**Resets preserve your history.** They do not reset account quota, change your
credit balance, or redeem account resets. Quota and credit measurements are
account-wide; “current” changes the calculation window used in this chat, rather
than attributing all account spending to it. The token summary covers the last
six hours; credit rates and quota forecasts use their own history windows. Reset
cutoffs further restrict the calculation evidence.

Collection continues between requests and after closing a chat. It stops when
all attached desktop processes exit. A later trusted session-start hook can start
it again, including after reopening the app; **stop** is not a permanent disable.

### Tool reference

The routing skill calls these tools for you. You do not need to enter a session ID.

| Function | Purpose |
| --- | --- |
| `usage_start()` | Start or attach; return collector identity, startup cause, and health. |
| `usage_stats(session_id)` | Read saved account statistics using the requesting Codex thread's baseline. |
| `usage_reset_current(session_id)` | Save a new reset cutoff for the requesting thread. |
| `usage_reset_all()` | Save a new shared cutoff for all plugin threads. |
| `usage_stop()` | Stop the shared collector and its provider subprocess. |

## Understanding the numbers

- **Sample age** tells you how fresh the reading is. Normal polls are roughly
  30 seconds apart; network errors retry with backoff and show an error/stale state.
- **Active-hour rates** describe periods of inferred activity, not elapsed clock
  time. Account balance changes may include other devices or features.
- **Collecting / unknown / low confidence** are normal while forecasts gather
  enough evidence, especially after a reset. Estimates are not billing guarantees.
- **Tokens and responses** come from local Codex session counters. Missing local
  sessions or activity on other devices limit what these counters can explain.

## Update or remove

To update, request **@Codex Usage stop**, then run this from your checkout:

```sh
git pull --ff-only
python3 scripts/install-local-plugin.py
```

Restart the desktop app afterwards. Review a changed hook again if Codex requests
it. Existing history and calculation cutoffs are retained.

To stop using the plugin, request **@Codex Usage stop**, then disable or uninstall
**Codex Usage** in the desktop app's Plugins controls. Saved history remains local.

## Troubleshooting

| Problem | What to do |
| --- | --- |
| Python is older than 3.11 | Install a newer Python and run the installer with that executable, for example `python3.11 scripts/install-local-plugin.py`. |
| Codex executable was not found | Open/update the desktop app, or run the installer with `--codex-bin /absolute/path/to/codex`. |
| “Does not support local plugins” | Update the desktop app. An older standalone CLI on your PATH may lack plugin support. |
| Codex Usage is missing from `@` | Confirm setup finished successfully, restart the app, open a new local chat, and check Plugins for **Codex Usage**. |
| Collector is stopped | Request **@Codex Usage start**. Review hook trust if you want automatic startup. |
| Authentication error | Complete Codex's normal ChatGPT sign-in, then request start. The installer does not sign you in. |
| Database already has a collector | Stop the existing terminal `codex-usage watch` for that database, then request start. |
| Sample is stale or collection has an error | Check the connection and sample age; retries are automatic. A fresh quota percentage can remain unchanged between successful polls. |

For advanced paths and a different Codex home/database, see:

```sh
python3 scripts/install-local-plugin.py --help
```

Use a separate database for each Codex account. Stop collection before switching
accounts. macOS desktop operation is supported; Windows, mobile, and cloud-only
chats are not supported by this local collector.

## Local data and privacy

History is stored in:

```text
~/Library/Application Support/codex-usage-estimator/usage.sqlite3
```

The existing estimator database is reused when present. Reset cutoffs live in a
separate metadata file. The plugin stores numeric usage counters and bounded
model/speed metadata; it does not store conversation text or copy credentials.
Codex manages authentication and makes its normal account-usage backend requests.
The plugin has no developer-hosted endpoint. No login service is installed.

This repository provides manual local installation. It is separate from OpenAI's
public plugin directory. See [validation](docs/validation.md) for tested behavior
and limitations, and [OpenAI's plugin packaging guide](https://developers.openai.com/plugins/build/plugins)
for the local plugin and hook model.

## Development

The estimator's standard-library CLI remains available. The MCP SDK is an optional
dependency used by the plugin wrapper.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[plugin]'
.venv/bin/python -m unittest discover -s tests -v
```

Tests use synthetic telemetry and local subprocesses; they require no account
credentials or model turns. Use private databases for manual reset testing.
