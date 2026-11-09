# Architecture, access model and limits

Current release: **v0.2.0** for Hermes 0.22.5. See [README](../README.md) for
install, configuration and privacy summary.

This feature means an emoji prefix in a stored session title, NOT a
platform-specific custom topic icon.

## Behavior

- The first session hook schedules one delayed action for that session, default
  60 seconds, configurable as `delay_seconds`.
- At execution, read the current title. Missing session, missing title or an
  existing leading emoji: skip, without retry/backfill.
- Give only that title to the selector and obtain one relevant Unicode emoji
  without a fixed catalog, preserving the title text after the prefix.
- The tool supports “set icons for the last N sessions”. Last means last activity.
  Select N first, then skip already decorated or untitled sessions, without
  filling from older sessions. Manual titles are included but their text is
  preserved. Maximum per-call limit: 200.
- Return updated/skipped/stale/error counts. No title or model-output logging.
- Do not start timers in a transient worker. Repeated hooks must not add timers.
- An explicit rename produces `user` provenance, preventing later native title
  upgrades. This is not an automatic-title provenance update.

## Modules

`session_title_icons/` is a standalone installable plugin directory with its own
`plugin.yaml`. It supports the Linux composed gateway, default profile only, and
requires confirmation that the profile has one trusted human owner.

- `plugin.py`: `on_session_start` only enqueues an ID and due time. No model work
  or timer runs in the hook callback. Managed workers never start the consumer.
- `runtime.py`: one locked gateway-resident background loop, canceled on unload.
- `queue.py`: durable deduplication in the plugin's own
  `<HERMES_HOME>/session-title-icons/jobs.sqlite3`; NOT Hermes' session DB. No
  titles, messages or tokens are saved here. Claimed jobs interrupted by restart
  are marked outcome-unknown and NOT retried. Jobs overdue by >300 seconds skip.
  Capacity is 10,000 retained jobs; overflow refuses new work rather than evicting
  deduplication records. Do not casually delete the queue to rearm old sessions.
- `store.py`: public read-only SessionDB metadata methods, immediate reread, then
  official `session.mutate` RPC with request ID, revision and generation. No SQL
  or direct writes to the Hermes DB. Refused/unknown writes are not retried.
- `selector.py`: host-owned `ctx.llm` structured facade, current default model,
  title-only JSON input, no provider/model override or credential file reads.
- `unicode_emoji.py`: vendored Unicode Emoji 16.0 conformance data with SHA-256
  verification and the Unicode license in `data/`. This validates standard
  sequences; it is NOT a fixed semantic selection catalog. Unsupported newer
  emoji skip.
- `confirmation.py`: host human-prompt queue or CLI human callback, exact `once`
  answer only. Unique request and pattern keys prevent one approval from covering
  parallel batches; the prompt states the captured N. The native prompt is
  withdrawn on cancellation and late responses cannot schedule work.
- `tools.py`: `session_title_icons` tool. `recent` enqueues a batch and returns a
  job ID; `status` returns counts when complete. Queued is never reported as done.

Visible historical/manual/archived titles can be decorated. Hidden/canonical and
internal/cron/kanban/API-server sessions are excluded. Normal title text is never
rewritten or truncated. The 100-character host limit can cause a skip after adding
the prefix. Under load the configured delay is a due time, not an exact deadline.

## Access model

The plugin does not implement a second authentication or owner-allowlist layer.
It relies on Hermes admission and adds one requirement: a fresh, once-only human
confirmation for every batch.

- Routing comes from exact task-local session ContextVars, with no process
  environment fallback. Unbound, foreign-profile and conflicting contexts fail.
- Explicit service/cron/kanban/oneshot callers and delegated child processes are
  refused. A cron marker always denies, even when a local platform is set.
- API callers are supported, but API transcripts remain ineligible batch targets.
- The model cannot choose a profile, an arbitrary session target, or a native
  principal. Consent and caller binding are rechecked after confirmation.
- New job status is profile-scoped, so other clients in the same default profile
  can inspect it. Old jobs are not migrated or replayed.
- No core edits, host method wrapping, log observers or log parsing are used.

Anyone Hermes admits to the trusted default profile can request and confirm a
batch and read its job counts. Do not deploy this policy on a shared or untrusted
profile.

## Known limitation: non-atomic rename

The installed dashboard supports `PATCH /api/sessions/{id}` with `request_id`,
`expected_revision` and `expected_generation`. However, direct native
`SessionDB.set_session_title()` and `set_auto_title()` change the title WITHOUT
advancing `runtime_revision` or `runtime_generation`. This was reproduced against
synthetic storage in the exact pinned Hermes image, not inferred from mocks.

Consequently:

1. Read title T and fences R/G.
2. Even a second read can still return T/R/G.
3. A direct manual/native title write can change T to U without changing R/G.
4. PATCH with old R/G does not itself express “only if the title is still T”.

The REST rename handler forwards no expected-title condition. A read immediately
before PATCH reduces the window but is NOT an atomic title compare-and-set. Do not
label that implementation race-proof. No private SQL writer, method replacement,
private worker receipts or core patch has been introduced to hide the gap.

Native metadata mutation emits `session.updated`; a stored-session watcher can
also signal list changes. No synthetic client event or client/core modification is
added. Immediate repaint of every open client header is NOT guaranteed.

## Tests

Run `scripts/Test-Local.ps1` on Windows with Docker Desktop. The runner uses
hidden child processes, the pinned image from `baseline.json`, network disabled,
read-only source and root, a synthetic temporary DB, and no production
home/credentials/data mounts. See [test isolation](TESTING.md).

Coverage includes durable queue concurrency, resident lifecycle, actual directory
loader, task-local routing, Unicode validation, real read-only metadata, host LLM
trust/schema/parser, real native receipt and authority authorization/publication,
once-only approval queues and callbacks, and a 50-session batch through the real
picker with a synthetic selector. Hook → durable queue → resident → host facade →
receipt is exercised with synthetic model and mutation transport. No cloud
endpoint, production state or credential is used.

This is a locally tested experimental plugin, not a deployed/live success claim.
