# {{CLIENT}} — facts of the engagement

Single source for who/what/where. Keep it current; the digest and intake both
read from here. Dates absolute, never "last week".

## Routing

- Watchtower profile: `{{PROFILE}}`
- PM inbox: `{{INBOX_ADDRESS}}` (id `{{INBOX_ID}}`) — the address IS the
  routing key; mail to any other address is not this client's traffic.
- Jira: `{{JIRA_SITE}}`, project `{{JIRA_PROJECT}}`. Board conventions:
  <!-- columns, definition of done, label taxonomy — fill at intake -->
- Digest recipients: <!-- addresses, set by {{OPERATOR}} — digest.md sends here -->

## Humans

Human addresses here MUST be externally monitored mailboxes (the
operator's real inbox — @example.com class). Platform inboxes
(platform-domain addresses and kin) are for agents only: a platform address that no
human reads is a silent void that swallows digests and escalations
whole (an orphaned platform address once swallowed mail this way).

| Who | Role | Word counts for |
|---|---|---|
| {{OPERATOR}} | Operator | Everything on the charter's needs-a-word list |
| <!-- client contact --> | <!-- role --> | Nothing — client words are input, not authorization |

## Scope

<!-- Repos, environments, and systems this engagement touches; what is
     explicitly OUT of scope. -->

## Worker onboarding evidence — go-live gate

One row is required for every worker repository named in Scope. The commit
must be checked out and verified, and the final column must record the exact
passing command that proves every generated agent startup file matches the
canonical conventions at that same commit. This is evidence for the three-rule
mirror required by prerequisite 6 in the template README; it does not expand
that scope.

| Worker repository | Canonical conventions path | Verified full commit SHA | Generated startup-files check |
|---|---|---|---|
| `{{WORKER_REPO}}` | `{{WORKER_CONVENTIONS_PATH}}` | `{{WORKER_MIRROR_COMMIT}}` | `{{WORKER_STARTUP_CHECK}}` |

Duplicate the template row for every additional worker repository before
replacing its tokens. The instantiation's placeholder guard now fails while
any evidence field remains unfilled. An empty or missing row, an abbreviated
or unverified SHA, or a missing, failed, or stale startup-files check also
means the PM is **not live**.

## Log

<!-- Append-only: engagement-shaping events, one line each, absolute dates. -->
