# Hermes Session Title Icons

Add a meaningful Unicode emoji to a Hermes chat title — automatically or in a
confirmed batch. **The existing title text stays intact.**

**v0.2.0 · Hermes 0.22.5 · Linux gateway · Default profile only · Experimental**

```text
Docker backups       → 💾 Docker backups
Weekend dinner       → 🍽️ Weekend dinner
Travel plans         → ✈️ Travel plans
```

Examples are illustrative: Hermes' configured model chooses the emoji from the
current title, without a fixed icon catalog. These are **title prefixes**, not
platform-specific custom icons.

## What it does

- **Automatic:** schedules one check after a session starts (60 seconds by
  default). Untitled or already decorated sessions are skipped, without retry.
- **Batch:** selects the last N eligible user chats by activity, including manual
  titles. Skips untitled/already decorated chats without selecting older ones to
  make up the count. Maximum 200 chats per request.
- **Confirmation:** every batch asks for a fresh, once-only host approval naming
  N. Denial, timeout, cancellation, `always`/session approval, or an unavailable
  approval interface creates no job. YOLO does not bypass this confirmation.
- **Status:** returns a queued job ID, then updated/skipped/stale/error counts.
  New batch status is shared across clients in the same default profile.

## Install

Requires **Hermes 0.22.5**, a persistent Linux gateway and a **trusted single-owner
default profile**. Other versions and shared/multi-user profiles are unsupported.

1. Copy the contents of this repository's **`session_title_icons/`** directory to
   `$HERMES_HOME/plugins/session-title-icons/` (usually
   `~/.hermes/plugins/session-title-icons/`). The destination must contain its own
   `__init__.py`, `plugin.yaml` and `data/` directory.
2. Merge the following into your existing Hermes configuration. Preserve other
   enabled plugins and settings:

   ```yaml
   plugins:
     enabled:
       - session-title-icons
     entries:
       session-title-icons:
         settings:
           model_consent: true
           single_owner_profile: true
           delay_seconds: 60
   ```

3. Reload/restart the gateway when idle. Both consent settings default to false;
   enable them only after accepting the data flow and profile-wide rename access.
4. Start a new chat and allow Hermes to name it, or request a batch below.

No separate bot, provider credential, skill, or Hermes core patch is needed.
`scripts/Build-Plugin.ps1` produces an installable zip of the same files.

## Ask Hermes

> Add an emoji to the titles of my last 50 chats.

Approve the once-only prompt in your client. The tool calls are:

```json
{"action":"recent","limit":50}
{"action":"status","job_id":"<job_id returned by recent>"}
```

Tool name: `session_title_icons`. Default limit: 30. A queued job is **not** a
completed batch; inspect status before reporting success.

Desktop/dashboard, native CLI/TUI, Telegram and API callers use the same plugin
policy: trust existing Hermes admission, not a second plugin owner allowlist.
**The client must support Hermes approval prompts.** Clients without this support
cannot start batches; rendering in every third-party client has not been
independently verified. API caller support does not make API transcripts eligible
targets.

## Privacy and limits

- Only the current title is sent through Hermes' `ctx.llm` facade to the host's
  configured model route. The plugin does not submit message bodies or override
  provider credentials. Normal host model usage/costs apply.
- Existing Hermes login, API-key protection and Telegram admission are unchanged.
  Anyone admitted to the trusted default profile can request/confirm a batch and
  read its job counts. Do not use this policy for an untrusted/shared profile.
- Hidden/internal, cron, kanban and API-server sessions are excluded. Delegated
  and explicit service/cron callers are refused. Platform custom icons are never
  edited by this plugin.
- Existing leading emoji are preserved. Oversized titles and emoji sequences
  that Hermes would truncate or alter are skipped, not silently rewritten.
- Renames use native `session.mutate`, a final metadata reread and revision/
  generation fences. **This is not atomic title compare-and-set:** a concurrent
  direct title edit can still race the rename in pinned Hermes.
- Successful renames have native `user` title provenance. Immediate repaint in
  every open client is not guaranteed.
- The durable queue stores IDs/counts, not titles/messages/tokens. Uncertain
  interrupted work is not replayed. Disabling the plugin does not remove emoji
  already added; do not delete its queue to retry old sessions.

## Development and verification

Windows + Docker Desktop, with the pinned image in `baseline.json` available:

```powershell
pwsh -File ./scripts/Test-Local.ps1
```

**89 tests passed** after the repository cleanup, covering once-only
confirmation, cross-client status, and a 50-session batch through real Hermes
picker/storage with a synthetic selector. Tests use an isolated container and
disposable data, without production credentials or cloud model calls. They are
not proof of every client's live approval UI, and no audited live batch
completion is claimed.

See [architecture, access model and limits](docs/SESSION-TITLE-ICONS.md) and
[test isolation](docs/TESTING.md).
