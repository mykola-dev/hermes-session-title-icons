# Local test isolation

Run the suite on Windows with Docker Desktop:

```powershell
pwsh -File ./scripts/Test-Local.ps1
```

The runner starts one short-lived Docker container on the developer PC.
`baseline.json` records the pinned registry digest, platform and image ID. The
runner validates the digest and platform; image ID representation differs between
classic Docker and containerd. The container entrypoint is overridden, so the
image's normal gateway startup never executes.

## Boundaries

- `--network none`; no ports; no bot polling process.
- Repository mounted read-only at `/project`; no other host mounts.
- Read-only container root; private tmpfs at `/tmp` and `/opt/data`.
- UID/GID 10000, all capabilities dropped, no-new-privileges.
- Memory 512 MiB, CPU 0.5, PID limit 128; in-container Python deadline 180
  seconds, with a 20-second host-side startup/exit allowance.
- Container removed normally with `--rm`; timeout cleanup addresses only the
  unique container name created by this invocation. Cleanup retries and reports
  unresolved daemon errors.
- `HOME` and `HERMES_HOME` point to the empty `/opt/data`; no host environment or
  secret files are forwarded. No model provider is contacted.
- `PYTHONDONTWRITEBYTECODE=1` and `python -B` prevent source cache writes.

Docker/WSL is process and filesystem isolation, not a separate physical machine.
The pinned third-party image is trusted to the same extent as a deployed image.

## Coverage

- Engine: delayed actions, deduplication, bounded scheduling, concurrency,
  cancellation, consent gating, one-shot skips and batch counts.
- Host contracts: pinned Hermes fence behavior and title sanitization, on
  synthetic storage only.
- Integration: real Hermes manifest parsing, plugin loader, hook dispatch,
  resident lifecycle, real read-only metadata picker, native authority and receipt
  transaction, Unicode validation, once-only approval queues/callbacks, and
  cross-client job status.
- A 50-session test checks preserved title text and title-only model input.

Model and network boundaries are synthetic. Synthetic UI bridges are not live
WebSocket login or client-rendering tests; the plugin intentionally relies on
Hermes admission instead of implementing its own authentication policy.

## Host compatibility and the multiplex regression

The 95-test suite targets Hermes 0.23.1 (`baseline.json`). Earlier Hermes versions
are not supported by this release; no compatibility fallback is maintained.

The former 89-test suite passed on 0.23.1 but did not model the gateway activating
profile multiplexing. An empty background thread consequently reached the native
secret reader without a profile scope and failed with `UnscopedSecretError`.
Relaxing the manifest alone did not fix that runtime failure.

The regression now enables the native multiplex guard and checks hook → resident
→ real host LLM facade → synthetic native receipt with a synthetic profile secret.
It fails on the old runtime and passes with per-job host profile scopes. Separate
tests cover auto/batch scopes, foreign-home refusal, scope refresh, exception and
cancellation cleanup, and diagnostics that never log exception text or titles.
No real credentials or cloud endpoint are involved in these tests.

The tests use the current native `SessionActor` contract. A minimum version is
an admission constraint, not a guarantee that future host versions preserve these
native contracts.
