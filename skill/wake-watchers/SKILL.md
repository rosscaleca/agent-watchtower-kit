---
name: wake-watchers
description: Arm this session's wake watchers for a client/topic profile (acme, globex, …) — verifies the Watchtower daemons are healthy, then tails that profile's Jira/GitHub/Bitbucket event logs with persistent Monitors so external activity wakes the session. Use at session start in any watcher/coordination session, or when asked to "watch" a project's PRs, tickets, or CI.
---

# wake-watchers — arm a session's external-activity wake-ups

**Usage:** `/wake-watchers <profile>` (e.g. `/wake-watchers acme`). The profile names a key
under `profiles` in `~/.config/watchtower/config.json`.

Watchtower separates *daemons* from *context*: machine-wide launchd daemons poll every source
once for all profiles and write per-profile event logs; a session gains context by tailing only
its profile's logs. This skill does the session half.

## Steps

1. **Resolve the profile.** Read `~/.config/watchtower/config.json`; confirm the requested
   profile exists and note which sources (`github`, `jira`, `bitbucket`, `stalled`) it configures. If the
   argument is missing, infer from the session's project context and say which you picked.

2. **Verify the daemons** (don't arm tails onto dead pipes):
   ```sh
   cd ~/Development/Agent-Watchtower && just status
   ```
   Every configured source should show a loaded `com.watchtower.<source>` job and a recent
   watermark. If a daemon is missing: `just install <source>`. If a Jira profile shows
   `waiting: … placeholder`, the API token still needs pasting into the config (from
   https://id.atlassian.com/manage-profile/security/api-tokens) — the daemon picks it up within
   a cycle, no restart.

3. **Check for an existing tail before arming** (single-owner rule — a duplicate Monitor is
   noise in two sessions):
   ```sh
   ps -axo pid,command | grep "[w]atchtower/<profile>-"
   ```

4. **Arm one persistent Monitor per configured source**, using the Monitor tool with
   `persistent: true`:
   ```sh
   tail -n 0 -F ~/Library/Logs/watchtower/<profile>-github.log    | grep -E --line-buffered '^type=|^error:'
   tail -n 0 -F ~/Library/Logs/watchtower/<profile>-jira.log      | grep -E --line-buffered '^type=|^error:'
   tail -n 0 -F ~/Library/Logs/watchtower/<profile>-bitbucket.log | grep -E --line-buffered '^type=|^error:'
   tail -n 0 -F ~/Library/Logs/watchtower/<profile>-stalled.log   | grep -E --line-buffered '^type=|^error:'
   ```
   Only arm sources the profile actually configures; a tail on a never-written file is harmless
   but pointless. `just arm <profile>` prints these ready to copy.

5. **Confirm to the user** what is now watched and what is deliberately not (see semantics).

## Event semantics (what a wake means)

- `type=pr_opened|pr_push|pr_merged|pr_closed|pr_declined` — PR lifecycle. Never suppressed:
  these fire for your own side's actions too, as confirmation.
- `type=review` / `type=comment` — someone **else** wrote (your own identities are
  echo-suppressed per config; without that, every agent write would wake the agent that wrote it).
- `type=run` — a completed Actions run with its conclusion. A burst of these after silence
  usually means a CI outage just recovered.
- `type=unread_runs` — the runs listing could not be read in full for a window (`reason=page_cap`:
  100 or more runs created after the read's cutoff, which is the last completed cycle's start less 2
  minutes, or, while an earlier failed read is held, the hold's start less 2 minutes, and never
  earlier than the first run's start;
  `reason=hold_expired`: a failed read was held for more than 30 minutes and the next read was
  incomplete too). A run created in `since`…`until` may never be announced: check that window by
  run id.
- `type=created|change` — Jira issue created / fields changed (status, assignee, description…).
- `error:` — the daemon failed 3+ consecutive polls or auth was rejected. Investigate the
  daemon log (`~/Library/Logs/watchtower/daemon-<source>.log`) before assuming the source is down.

## Troubleshooting

- **Silent log ≠ broken** — quiet projects are quiet. `just status` shows watermark freshness;
  a recent `since` means polling is healthy.
- **Restart one daemon:** `launchctl kickstart -k gui/$(id -u)/com.watchtower.<source>`.
- **Auth:** GitHub rides `gh auth login`; Bitbucket rides `bkt`; Jira uses the token in the
  config. None of these ever pass through the session — do not paste tokens into chat.
- **Adding a profile** is config-only: add a key under `profiles` with the sources it needs,
  then arm its Monitors in the relevant session. No daemon changes.
