---
name: pm-launch
description: Use when bringing a client PM seat up on the always-on host — after a reboot, a dead or missing tmux session, a crashed claude, or when unsure whether a PM session is already running and healthy. Also for the first launch after instantiating a new client from templates/pm-project.
---

# pm-launch — start (or verify) a client's PM session on the always-on host

**Usage:** `/pm-launch <client>` (e.g. `/pm-launch acme`); no argument → every
dir under `~/PM/` that holds a `CLAUDE.md`.

Conventions: client `<c>` ⇒ session dir `~/PM/<c>`, watchtower profile `pm-<c>`,
tmux window `pm:<c>`. Watchtower daemons are machine-wide launchd jobs — RunAtLoad
brings them back after a reboot on its own; the tmux claude session does NOT come
back by itself, and restoring it is exactly this skill's job.

## Steps

1. **Preconditions.** `~/PM/<c>/CLAUDE.md` exists, and the daemons are healthy:
   ```sh
   cd ~/Development/Agent-Watchtower && just status
   ```
   Every source profile `pm-<c>` configures must show a loaded
   `com.watchtower.<source>` job and a fresh watermark; `just install <source>`
   for any missing one. Never start a session onto dead daemons. (In the
   launchd block, the second column is the job's LAST EXIT STATUS, not health —
   a `-15` beside a live pid just means it was once restarted; the fresh
   watermark is the decisive signal.)

2. **Single-owner check — never double-launch.** Two PM sessions on one profile
   double-process every wake: duplicate tickets, duplicate acks.
   ```sh
   tmux has-session -t pm 2>/dev/null && tmux list-windows -t pm -F '#{window_name} #{pane_current_command}'
   ps -axo pid,command | grep "[w]atchtower/pm-<c>-"
   ```
   - Window `<c>` whose `pane_current_command` is NOT a bare shell
     (fish/zsh/bash/sh) → something is running there. ⚠ claude reports its
     VERSION STRING (e.g. `2.1.235`) as the command, never the word "claude" —
     do not conclude the seat is down because "claude" isn't in the list.
     Confirm with `tmux capture-pane -t pm:<c> -p`: the claude status bar
     (`N monitors`, `❯` prompt) means the seat is live — report its state and
     **stop; send it nothing.**
   - No live window but profile tails show in `ps` → another session owns the
     watch, or tails survived a crash; report to the operator instead of launching.

3. **Launch.**
   ```sh
   tmux new-session -d -s pm -n <c> -c ~/PM/<c>    # or: tmux new-window -t pm -n <c> -c ~/PM/<c>
   tmux send-keys -t pm:<c> "claude" Enter
   ```
   Wait until the pane shows the input prompt (the banner takes ~5–10 s) before
   typing anything into it.

4. **Kickoff — then verify it actually submitted.**
   ```sh
   tmux send-keys -t pm:<c> "Session start — follow your CLAUDE.md start sequence." Enter
   ```
   Capture the pane: if the text is still sitting in the input box, the TUI's
   paste handling swallowed the Enter (observed live 2026-08-19) — send one more
   bare `Enter`. Never re-send the text itself; that queues the kickoff twice.

5. **Verify bootstrap.** Poll the pane until the status bar shows armed Monitors
   (`N monitors`), then capture the session's own start-up summary. A source that
   won't arm is the seat's FIRST report to the operator — surface it, don't
   route around it.

6. **Report.** Per client: daemons ok/repaired, seat `already-live` or `started`,
   monitor count, anomalies. After downtime spanning a schedule entry's time, the
   first wake is a catch-up cron (same-day, at-most-once) — an immediate digest
   after a reboot is by design, not a bug.
