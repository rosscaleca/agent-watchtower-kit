# Atlassian account for the PM agent — setup

**Short answer: yes, it's a normal user seat.** Jira/Confluence Cloud on Free and
Standard plans has no first-class "service account" object — Atlassian reserves
managed service accounts for the enterprise tier (Atlassian Guard). The universal
practice at this scale is a **dedicated ordinary user** that only the agent uses,
plus an **API token** minted under that account. Everything the agent writes then
carries its own identity in history, assignability, and notifications — which is
the entire point (a shared human account cannot carry the agent signal).

## Steps

1. **Mint the mailbox first.** The account's email must receive Atlassian's
   verification mail. Create `pm-<client>@example.com` as an agent-readable
   mailbox (for example on your agent-mail platform) — the PM session itself can
   then read the verification mail, which is a nice closed loop.
2. **Invite the user**: `admin.atlassian.com` → your organization → Directory →
   Users → *Invite user* → `pm-<client>@example.com`. Product access: Jira (and Confluence
   only if the agent will read/write pages). Display name: **`PM Agent
   (claude-pm)`** — the name is what appears in ticket history, so make it
   unambiguous that it's an agent.
3. **Accept + set credentials.** The invite lands in the PM inbox; the accept
   flow needs a browser ONCE (any machine — it's just account setup). Set a
   strong generated password, store it in the password vault. Do not enable
   interactive 2FA on the bot account unless org policy forces it; API tokens
   authenticate independently of the interactive login either way.
4. **Mint the API token** while logged in as the PM account:
   `id.atlassian.com/manage-profile/security/api-tokens` → *Create API token*.
   Label it with host + purpose (`pm-host watchtower`, `pm-host pm-session`).
   Consider one token per consumer so revocation is surgical.
5. **Grant project access.** Team-managed (simplified) projects: project
   → Settings → Access → add `PM Agent` as **Member** (create/edit/comment/label
   — enough for the charter's autonomous set; Viewer is too little,
   Administrator is more than the charter allows it to use). Company-managed
   projects: a `pm-agents` group in the permission scheme, same idea. Only the
   projects this agent serves — profiles are confidentiality boundaries.
6. **Record the accountId** (needed for echo suppression and for signature
   audits):
   ```
   curl -s -u pm-<client>@example.com:$TOKEN https://<site>.atlassian.net/rest/api/3/myself | jq -r .accountId
   ```
7. **Wire it in**: the token goes into `~/.config/watchtower/config.json`
   (chmod 600) as the PM profile's `jira.api_token` with `email: pm-<client>@example.com`,
   and the accountId into that profile's `suppress_account_ids` so the PM's own
   writes don't wake the PM. The PM session's own tooling (REST via token, not
   the interactive claude.ai MCP connector — headless boxes can't re-auth
   interactive connectors) uses the same pair.

## One account per client, not one shared bot

An API token cannot be scoped to a site or project — tokens inherit everything
the account can reach (Atlassian's scoped tokens restrict *operations*, not
*tenants*). A shared `pm@` account in every client's projects therefore puts
every client's Jira behind the credential in every PM session — and PM sessions
ingest untrusted client email, the design's highest-risk input. Separate
accounts are the only Atlassian-native way to make the charter's "profiles are
confidentiality boundaries" true for Jira: a compromised session is capped at
its own client, and offboarding a client is one account deactivation.

Where seats actually live: on a CLIENT-owned site the seat exists per site
regardless (their invite, their bill — per-client identity costs them nothing
extra and reads better in their security review). On YOUR site — where you
track several clients' work yourself — per-client means N seats on YOUR bill (Free covers 10 users total; Standard bills each).

The decision lens for that spend: the PM's autonomous writes are narrow and
reversible, so a shared account's real exposure is READS — a session
compromised through one client's email could read every client's projects and
exfiltrate through the mail capability it inherently has. A per-client seat is
what caps client B's injection at client B's data; weigh it per client by
their project's sensitivity. Two softeners: an "agent PM seat" is a
defensible per-client line item in the engagement (the seat scales with
revenue, not overhead), and the fork only arrives at client #2 — today it is
one seat either way. Mint `pm-<client>@example.com` (born per-client, never
generic `pm@`) and make the shared-vs-dedicated call at each onboarding; a
client that later grows their own Jira site inherits the identity cleanly
(their invite, their bill, history intact).

## Costs and small print

- **The seat is billable like any user.** Jira Free covers 10 users; on Standard
  a bot seat bills at the normal per-user rate. One PM account across all clients
  on YOUR site is one seat; a client's own Jira site means a seat granted on
  *their* side (they invite `pm-<client>@example.com` — same steps, their bill).
- Tokens expire per org policy (Atlassian now defaults new tokens to an expiry,
  max ~1 year). Put rotation on the PM's own schedule source
  (`{"name": "rotate-atlassian-token-check", "at": "09:00"}`-style annual
  reminder) — a silently expired token is exactly the watchdog's
  `stale watermark` signature, but cheaper to rotate on time than diagnose.
- The verify-by-re-read discipline stands: the REST API is the source of truth
  for any write the agent makes, never the write call's response alone.
