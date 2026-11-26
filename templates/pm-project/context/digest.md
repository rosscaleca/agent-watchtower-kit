# {{CLIENT}} — digest playbook

Runs on the `morning-digest` schedule wake. The charter's contract
("The digest is verifiable") governs; this file is the mechanics.

## Gather (counts + ids, never vibes)

1. **Board delta** since the last digest: tickets created / moved / done in
   `{{JIRA_PROJECT}}`, each by id. Query Jira REST directly — the digest
   re-derives from source, never from memory of wake lines.
2. **Mail**: inbound count per sender tier (allowlisted / known / other),
   ids of anything held (auth=FAIL, unlisted sender, gate-failed).
3. **Watchers**: any `error:` lines in `{{PROFILE}}`'s logs since the last
   digest, and every watchers-dark window (daemon down, machine asleep,
   network out) with start/end — a quiet log that was dark is not a quiet
   day, and the digest must not let the two look alike.
4. **Blocked + stale**: tickets in blocked states, and anything untouched
   past the board's staleness convention.

## Compose

- Lead with the one-line state of the engagement.
- Then the four sections above, ids inline. Empty section → say "none",
  never omit — the reader must be able to distinguish "nothing happened"
  from "nothing was checked".
- The digest SENDS even when everything is "none". The missing digest is
  the alarm; a sent-empty digest is the proof the watch was kept.

## Send

To the recipients {{OPERATOR}} designated in `client.md`, from
`{{INBOX_ADDRESS}}` via `{{INBOX_CLI}}`. The digest is the one standing
outbound that needs no per-send word — {{OPERATOR}}'s word for it is this
playbook. Anything else outbound stays draft-first.

## After sending

Append one line to `context/client.md`'s Log: date, digest sent, headline
counts. If the schedule wake ever fires and this playbook cannot complete
(Jira unreachable, inbox-cli dead), send what you have with the failure
named in the first line — a degraded digest beats a missing one.
