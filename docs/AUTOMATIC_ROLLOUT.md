# Native onboarding and automatic rollout

## Release configuration

Set the GitHub repository (or inherited organization) Actions variable
**`MIDDLEWARE_SERVICE_BASE_URL`** to the deployment's complete HTTPS URL ending
in **`/api/middleware/v1`**. The release workflow forwards it under the same
environment-variable name. There is no inferred production host, dotenv lookup,
or fallback to legacy endpoints.

`installer/src/cli/setup.py` requires this setting on every invocation and
generates `output/release-resources/middleware-service.json`:

```json
{"version": 1, "base_url": "<explicit deployment HTTPS URL>/api/middleware/v1"}
```

The generated file is frozen as `resources/middleware-service.json` and installed
through the explicit Inno asset allowlist. The checked-in resource instead uses
`https://deployment-required.invalid/api/middleware/v1`; it is a generic,
non-operational source placeholder and **cannot pass a release build**.

Validation requires a public-style DNS hostname, HTTPS, the exact path (no final
slash), and an optional valid port. Missing values, IP literals, local/reserved
or example domains, credentials, whitespace, queries and fragments fail closed.
Validation is offline; deployment owners supply the correct host and qualify its
TLS, identity-provider configuration and production routing.

The service's public `/bootstrap` supplies native public auth configuration.
Release assets contain no merchant, connection, installation ID, credentials,
inventory, or migration package. Loopback HTTP is a runtime test capability, not
an allowed release setting.

## Fresh installation interface

The actual installer template is **`resources/config_example.yml`**:

```yaml
onboarding: native_required
target_system: "{{ TARGET_SYSTEM }}"
folder_to_watch: "{{ FOLDER_TO_WATCH }}"
```

It deliberately omits both `auth` and `api`. Inno fills the ERP placeholders,
appends Farmax settings when selected, and writes `resources/config.yml` only for
a genuinely fresh installation. Fresh installs require the interactive ERP
wizard; a silent fresh install fails instead of generating unusable defaults.

The runtime must recognize the top-level scalar `onboarding: native_required`
before constructing any legacy service graph or calling the legacy migration
consumer. It obtains native authentication independently, lets the user select
an authorized merchant, and creates or explicitly reconnects a source connection.
A replacement PC must reconnect and recover native source identities before
intake. Successful native setup atomically replaces the marker with the validated
target and installation state. No `native-upgrade.json` is required for fresh setup.

## Updates and recovery

- Existing `resources/config.yml` is preserved, including pending onboarding,
  legacy settings, profile pointers and source configuration.
- `/UPDATE=1` without config fails in `PrepareToInstall`, before copying files.
  Ordinary reinstall/repair also checks existing executables, uninstall evidence,
  and `resources`/`installer` directories. Missing config in those destinations
  requires restoration, never automatic fresh onboarding.
- Freshness is captured before file installation. If existing config disappears
  during the install, the post-install step fails instead of synthesizing it.
- The automatic migration helper still runs synchronously for existing
  installations, including silent updates, after runtime prerequisites and before
  application launch. Fresh installations and preserved pending-onboarding
  configurations skip it. `ConfigNeedsAutomaticUpgrade` returns false for the
  template's top-level `onboarding: native_required` scalar (also accepting simple
  quoted scalars/keys and inline comments). Nested/commented markers do not skip
  migration. The application resumes setup and validates the full configuration;
  Inno does not modify or adopt the pending installation.
- Tokens, journals, ownership ledgers, rollout state, staged packages, receipts,
  and backups are installation-owned. The installer neither copies release
  defaults over them nor deletes them. The self-updater's corrected first-hop
  batch and restart-on-copy-failure behavior remain in place.

Current coordinator sidecars beside config include `config.yml.rollout.json`,
`config.yml.automatic-native-upgrade.json`, and
`config.yml.prepared-native-token.txt`; these are never release resources.

`main.py` enters `NativeOnboardingDialog` before adoption or service construction
when the explicit marker is present. Successful setup hands the independent
session to the native profile and opens the existing courier-mapping workflow.
The server-bound setup installation UUID is retained in config and the journal.
All native profiles gate source intake and dispatch until remote identity recovery
has durably completed, including every page after an interrupted recovery.

Existing legacy installations continue operating until their authenticated
installation rollout is enabled. Native login uses an independent grant. The
coordinator verifies access, pauses writers for a bounded final inventory,
retrieves the exact installation/attempt/digest-bound exporter package, and uses
the existing atomic migration consumer. Timeout/cancellation resumes legacy and
invalidates that attempt. A committed native handoff is recovered from local
receipts and acknowledged without repeating ownership transfer.

The tray's **Configuração / atualização do novo Velide** action exposes discovery,
independent device login, preparation, final inventory, waiting, application,
cancellation and recovery messages. Discovery runs automatically when the legacy
session becomes available and repeats while rollout is disabled. Native login
does not pause legacy work. Immediately before the final pause, its refresh owner
is paused/drained, its latest persisted bundle is checked and the exact target
is verified again.

`GraphQuiescence` pauses the complete graph, drains worker pools **and queued Qt
completion callbacks**, rejects pending ERP effects, then holds journal locks.
The coordinator holds the installation upgrade lock across inventory/application.
Cancelled or expired attempts are durably invalidated before legacy resumes;
slow in-flight workers finish draining before a fresh generation may start.
If invalidation cannot be persisted (for example, disk full), writers remain
blocked for recovery rather than claiming success.

REST requests reject redirects, enforce 8 MiB bounds, use connect/read limits and
a whole-request deadline no longer than the remaining pause budget. A trickling
or abandoned network response cannot activate a late package. Package bytes are
stored only under the coordinator-owned non-generic filename. Generic startup,
including `migrate-native.exe --automatic`, runs offline recovery before package
consumption. It quarantines an unfinished automatic package or recognizes the
actual consumer's committed database receipt. Lost acknowledgements reuse the
durable receipt ID; they never repeat ownership transfer.

## Headless integration interface

These modules are used by the GUI and are also injectable for isolated labs:

```python
from middleware_transport import MiddlewareTransport
from middleware_rollout import RolloutCoordinator, recover_local_rollout

transport = MiddlewareTransport(base_url, allow_loopback=True)  # lab only
coordinator = RolloutCoordinator(
    config_path, bundle_dir, transport, bootstrap_token, graph,
)
coordinator.discover()
coordinator.prepare(independent_native_bundle)
coordinator.begin(pause_seconds=120)
# Drive poll() with bounded caller scheduling until it returns UpgradeResult.
# cancel() invalidates first; acknowledge(native_token) retries a durable commit.
```

`graph` supplies `quiesce(monotonic_deadline)`, `resume()` and `release()`.
The Qt adapter returns false when it cannot drain safely; the coordinator rejects
that result. The caller owns the application instance lock. `clock`, `wall_clock`
and `notify` are optional coordinator injections. Actual inventory export,
copy-on-write migration and installation-wide ownership transfer are not replaced.

`NativeOnboarding(config_path, bundle_dir, transport)` exposes `bootstrap()`,
`options(native_token)`, `connect(native_token, merchant_id, source, mode,
connection_id=None)` and `complete(native_token, farmax=...,
folder_to_watch=..., token_bundle=...)`. The native bundle must have its own grant,
the exact `middleware_auth` context and actual matching issuer/audience/client/
scope claims; the server remains responsible for cryptographic token verification
and current merchant/connection authorization. Headless callers must finish any
refresh owner before publication, just as the Qt adapter does.

The server repository's shared
[`MIDDLEWARE_ROLLOUT_PROTOCOL.md`](https://github.com/BrenoLopesz/velide-server/blob/8b0e73373dccd3c54fed8aedc84824525cf1011e/MIDDLEWARE_ROLLOUT_PROTOCOL.md)
defines the v1 wire contract. The same server change contains the
[acceptance plan](https://github.com/BrenoLopesz/velide-server/blob/8b0e73373dccd3c54fed8aedc84824525cf1011e/MIDDLEWARE_AUTOMATIC_ROLLOUT_PLAN.md),
[deployment/converter instructions](https://github.com/BrenoLopesz/velide-server/blob/8b0e73373dccd3c54fed8aedc84824525cf1011e/MIDDLEWARE_AUTOMATIC_ROLLOUT_OPERATIONS.md)
and reusable `scripts/middleware_lab/` rehearsal tooling.
`installer/UPGRADE_CONTRACT.md` describes the underlying migration consumer and
file-based administrative path; its historical “network discovery not
implemented” note describes that earlier path, not this automatic coordinator.
Customer conversion remains the external converter's responsibility.

## Packaging boundaries and local checks

`MAIN_RESOURCES` and `UPDATER_RESOURCES` in
`installer/src/cli/release_resources.py` are exact asset allowlists. The main
application's root `resources` directory is never recursively included. Inno
copies only named executables, root DLL/PYD libraries, the frozen license, known
runtime directories, and allowlisted assets. Nested dependency resources, SQLite,
Qt plugins and timezone data remain available. Customer state/packages at the
application root, in arbitrary directories, or in application resources cannot
enter the installer. Known state filenames are also excluded from runtime trees.
The updater's own generic `resources/config.yml` and verification key are required
release assets; they are distinct from application configuration.

With Python 3.8 and build dependencies installed, in PowerShell:

```powershell
$env:TEMP = 'D:\velide-middleware-lab\packaging-temp'
$env:TMP = $env:TEMP
New-Item -ItemType Directory -Force $env:TEMP | Out-Null
$python = 'C:\Users\Breno\Dev\velide-middleware\.venv38\Scripts\python.exe'
# Supply the actual release setting explicitly; do not copy the .invalid resource.
$env:MIDDLEWARE_SERVICE_BASE_URL = '<deployment HTTPS URL>/api/middleware/v1'
& $python -m pytest tests/unit/test_rollout_packaging.py `
  tests/integration/test_upgrade_installer_resources.py `
  --basetemp D:\velide-middleware-lab\packaging-pytest
& $python installer/src/cli/setup_installer.py build
& $python installer/src/cli/setup.py build
& $python installer/src/cli/release_resources.py check output/middleware
& $python installer/src/cli/release_resources.py check output/installer --updater
```

Checks reject extra frozen resources, unexpected root payloads, a missing helper,
and service settings that differ from the explicit build setting. The focused
tests compile the real Inno scripts with disposable payloads when ISCC is
available. A separate payload-free probe executes the production helper predicate
and exits from `InitializeSetup` before installation, checking that pending setup
skips migration and installed connections retain it without changing config/tokens.
Checks do not contact production, sign, publish, deploy, tag, or commit.
Full Auth0/live-store qualification and signed x86/x64
release execution are separate from these local packaging checks.

### Local verification (2026-11-16)

- Python 3.8: **89 passed** across the focused rollout-packaging tests and existing
  installer-resource integration tests, including real Inno x64 compilation and
  13 payload-free Pascal predicate cases for pending-onboarding updates.
- Interim main and updater x64 cx_Freeze packaging builds completed; both
  frozen-payload checks passed. Those builds ran during concurrent runtime edits
  and do not qualify the final runtime. The integration owner will rebuild after
  runtime helpers stabilize. Installer predicate follow-up changes also require
  refreshed installer resources.
- Frozen `migrate-native.exe --help` succeeded. Ruff for the new packaging code
  and tests, and the owned-file `git diff --check`, passed.
- Builds used only the synthetic syntax fixture
  `https://middleware.release-fixture.org/api/middleware/v1`, with no requests to
  that hostname. Those local outputs are test artifacts, not configured releases.
- TEMP/TMP, pytest basetemp and build artifacts were on D:. Build logs:
  `D:\velide-middleware-lab\packaging-main-build.log` and
  `D:\velide-middleware-lab\packaging-updater-build.log`.

### Final runtime verification

- **127 passed** in the final focused run covering onboarding, grant binding,
  transport, graph quiescence, coordinator, real startup/controller wiring and
  prior retry/source-hold regressions.
- The cancellation integration test posts real Qt mouse events to the controller
  Cancel button while a real journal worker is blocked. It observes durable
  invalidation before any inventory upload, prompt cancellation, disabled runtime
  views and complete delayed-callback persistence before legacy resumes.
- The graph-focused regression selection passed **136 tests**, including
  **20 quiescence/recovery tests**. Packaging's final selection passed **89**,
  including real Inno compilation and executable Pascal marker predicates.
- Broader source sweeps exposed old test fixtures that bypassed recovery or omitted
  new-grant scope evidence. Those fixtures were updated; all failures were covered
  by the final passing focused run. Skipped external tests require explicit frozen
  executable/Firebird environment settings.
- Six new runtime modules passed focused mypy (imports skipped, unavailable
  third-party stubs excluded) and Pyflakes/Ruff checks. `git diff --check` passed.
- Final main/updater freezes and both resource checks completed successfully.
  **16 frozen-executable tests passed** on an isolated copy, covering application
  startup with preserved legacy offline intake/native recovery gating, actual
  automatic-consumer CLI execution and profile migration/selection regressions.
  The final output directories remain free of test-created installation state.
  Final build logs use `D:\velide-middleware-lab\rollout-final-*.log`.
- JWT/provider operations are injected in local unit/UI tests. Actual signature
  verification against the production identity provider, production routing and
  signed release execution are deployment qualification boundaries.

### Fresh interactive installer follow-up

- Fixed pre-wizard `{app}` expansion by restricting `ShouldSkipPage` installation
  evidence checks to the custom configuration pages. `PrepareToInstall` retains
  the existing repair/missing-configuration protections.
- **91 packaging tests passed**, including two real non-silent Inno startup cases
  with production Settings/Tasks/Files/logic and inert payloads. Each uses a unique
  AppId and cancels at the first page without installation. The default directory
  behavior and `UsePreviousAppDir=no` are both covered; the latter triggers the
  early built-in-page query observed in the isolated rehearsal.
- Reverting only the condition ordering in an owned negative-control copy
  reproduced `Runtime error (at 7:88)` / `{app}` before initialization. Its owned
  process tree was terminated. Evidence:
  `D:\velide-middleware-lab\pytest-inno-initialization-reset\test_real_interactive_inno_ini1\negative-control\old-startup.log`.
  Final passing tests:
  `D:\velide-middleware-lab\pytest-inno-startup-final`.
- The updater was rebuilt and its frozen-resource check passed. Logs:
  `D:\velide-middleware-lab\rollout-final-updater-inno-startup.log` and
  `D:\velide-middleware-lab\rollout-final-updater-inno-startup-resources.log`.

## Reconciliation stack compatibility

The rollout branch includes the refresh scheduler and reversible Farmax projection
changes. Merge the refresh fix and reversible projection into the native integration
branch before merging this rollout PR.

Reconciliation uses one queued refresh/retry timer and lifecycle/pass/gateway
tickets. Rollout pause stops that scheduler and its correction feed; graph quiescence
drains outstanding workers and journal callbacks before inventory export. Recovery
opens native intake only after the final bootstrap checkpoint is committed. An
invalidated checkpoint is reloaded from SQLite, and retired callbacks cannot release
a replacement pass or announce recovery for it.
