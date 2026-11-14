# Automatic upgrade integration contract

## Startup wiring contract

Run after the application instance lock, before `build_services`, source scans,
authentication, or any persistent database connection:

```python
from config import config, config_path, install_effective_settings
from local_upgrade import upgrade_installation

upgrade = upgrade_installation(config_path, BUNDLE_DIR)
install_effective_settings(config, upgrade.settings)
# Display/log upgrade.instructions, including package_error and unresolved_cds.
# Now construct services using config and upgrade.database_path.
```

`install_effective_settings` updates the existing object, so modules which already
imported `config` see the effective authentication and endpoint settings too.
Do not continue building services after an adoption exception. Optional package
validation/publication errors return `package_error` and the still-usable legacy
settings; they do not fail an ordinary update. A native config already committed
by the installer is loaded normally and repeating its package is a no-op.

`UpgradeResult` also contains `installation_id`, `adopted`, `native_applied`,
`backups`, `unresolved_cds`, and `cds_baseline_path`.

For CDS pass `baseline_path=upgrade.cds_baseline_path` to `CdsStrategy`. This
sidecar has an empty **watcher exclusion list**: the SQLite inventory and journal
holds are the baseline. A separate startup exclusion snapshot would hide
unresolved receipts and files created between installation and startup.

The packaged command is:

```text
migrate-native.exe --automatic --config <installed-config.yml> --bundle-dir <application-root>
```

It acquires the same `resources/velide.lock` as the application. The Python API
expects its caller already holds that lock. Both serialize upgrades through an
additional config-local lock. No application/database writers may run during
copy-on-write adoption/cutover.

## Journal integration hooks

1. `SyncJournal.bind_farmax_branch`: inside its existing write transaction,
   before rejecting historical rows with missing branch provenance, call:

   ```python
   if adopt_observed_farmax_branch(conn, self.scope, branch):
       return
   ```

   Import from `local_upgrade`. The branch must be the normal live branch query
   result. The helper permits original installation continuity, including after
   transport cutover, but rejects conflicting explicit historical branches or a
   changed bound branch. It neither guesses a branch nor queries/writes Firebird.

2. Extend `SyncJournal.accept(internal_id, payload, source_file=None)` with the
   optional third positional argument. CDS should pass `delivery_data['_source_file']`
   through `SyncJournalService.submit`. Do not add filename to the immutable
   business payload: that would conflict with already accepted older payloads.
   Inside the acceptance transaction, before returning a dispatchable row, call:

   ```python
   hold = cds_upgrade_hold(conn, self.scope, internal_id, source_file)
   ```

   Import from `upgrade_cds`. If non-null, persist `source_review` and
   `submission='NEEDS_REVIEW'` together with the accepted payload, and return the
   held row. Do not raise before committing or clear another source-review reason.
   This is essential for original incomplete files whose deterministic ID could
   not be computed during the inventory. Without a filename, their fallback hold
   is conservative across unknown CDS IDs. Parseable original receipts already
   have durable holds seeded automatically.

3. Preserve naive original receipts in **legacy** mode when an old CDS config has
   no timezone. Legacy journal canonicalization now permits these receipts;
   the legacy HTTP client freezes their Windows-local instant once per request.
   Native mode uses the authoritative package timezone. Preserve raw receipt
   representations used in deterministic CDS identities in both modes.

4. Retain the restored legacy transport for configs without `api.native`.
   Auto-adoption binds the existing installed endpoint/source without inventing
   a native merchant, connection, or legacy store-discovery result. Authentication
   must use the effective settings, including checking cached tokens against any
   changed issuer/audience/client at cutover.

## Optional installation-bound native package

Default location: `native-upgrade.json` alongside `config.yml`. An explicit
`package_path` / `--package` is also supported. Ordinary updates need no package.
Supply it through a trusted provisioning channel; the local consumer validates
the binding, configuration, and mapping consistency, not a server signature.

Top-level format:

```text
version: 1
authority: nonempty server export/provenance record
binding: installation_binding(config_path, database_path)
inventory_state_sha256: exact retained-work digest from the exported inventory
configuration:
  api:
    velide_server: actual native GraphQL endpoint
    velide_websockets_server: actual native WebSocket endpoint
    native: actual merchant_id, connection_id, timezone, optional interval
    source_timezone: optional authoritative timezone
  auth: optional complete actual domain/client_id/scope/audience
manifest:
  version: 1
  old_scope: exact installed scope
  new_scope: exact proposed native scope
  deliveries: every retained internal ID with its exact legacy/native outcome
  couriers: every retained local courier with exact legacy/native IDs
cds_resolutions: optional byte-bound receipt outcomes
```

`installation_binding` exports installation ID, exact old scope, source
fingerprint, and SHA-256 of the complete installed config. The only generated UUID
is a **local installation identifier**, never a merchant, connection or delivery
identifier. Fingerprints detect configuration changes, not database incarnation.

The server exporter also copies the inventory's retained-work digest into the
package. If attempts, cancellation, observations, mappings, effects, or receipt
inventory change after export, automatic consumption defers cutover and requests
a fresh inventory/package. Repeating an already applied package remains a no-op.

Configuration changes are limited to authoritative API/auth fields and the new
SQLite pointer. Source paths/credentials, neighbourhood, TLS, timeouts, retry and
reconciliation settings, retention, and unknown YAML keys survive. An omitted
auth section retains the existing one; changed auth must be complete. Native
cutover cannot reinterpret an already bound timezone.

Native mapping requires exact IDs. UUID shape alone never authorizes adoption of
an unbound native database. Active routes can migrate with their exact native
delivery mapping; completing every route is not an ordinary-update prerequisite.
Pending ERP effects currently defer **native cutover**, retaining legacy service.
Uncertain submissions cannot become `QUEUED`. Cancellation intent must retain its
authoritative native delivery mapping. Original requests/states remain in the
old database, backup, and `SyncMigrationOrders` audit.

### CDS outcomes

The original release generated random order IDs and did not persist filename
correlation. There is no sound local-only automatic classification of all old
files. Every unknown original `ent*.json` is inventoried with its exact bytes,
SHA-256, and (when parseable) deterministic receipt identity. Unknown is visible
`NEEDS_REVIEW`, never implicitly processed. New files are not re-baselined later.

Each `cds_resolutions` item supplies `filename`, `sha256`, `outcome`, and nonempty
authoritative `evidence`:

- `PROCESSED`: confirmed terminal receipt; manifest retains a terminal identity.
- `NOT_SUBMITTED`: authoritative never-submitted receipt; manifest may queue it.
- `LINKED`: manifest maps this deterministic identity to its native delivery.
  Optionally supply `retained_internal_id` to identify the original random-ID
  order. That original mapping remains active; the deterministic receipt gets a
  separate `TERMINAL` tombstone with no duplicate delivery mapping.

Every retained identity still needs a manifest outcome. Omitted resolutions stay
held as `NEEDS_REVIEW`, including through cutover. Automatic packages reject the
older unbound `processed_cds_files` list. Incomplete original receipts stay held
until their receipt identity can be authoritatively resolved; this consumer does
not implement that external discovery/repair process.

## Transaction and recovery behavior

Ordinary adoption migrates a private SQLite copy, preserving pending effects,
then publishes through SQLite's transactional backup API. A WAL-aware pre-upgrade
backup is retained and the ordinary configuration remains byte-for-byte intact.

Native cutover migrates a separate database file, checks it, prepares all support
files, backs up config, and atomically replaces config to activate that database
and endpoint together. Failed attempts leave the original config/database usable.
Handled failures remove their unpublished working copies. A hard process/power
interruption can leave an unused copy; retry does not depend on deleting it.
No source files are deleted. Repeating an already published package is a no-op.

Journal schema v5's `SyncFarmaxEvents` participates in scope validation, native
scope migration, and the exported state digest. Event sequence/evidence, per-phase
retry counters, and `farmax_progress` are retained. An additional failed source
observation also invalidates a stale package, even if the order row did not change.
The migration audit captures original rows before their scope is changed.

## Centralized package-production procedure (planned server cutover only)

Ordinary updates do **not** run this procedure or require an inventory/package.
For the planned cutover, the rollout pauses Middleware for the final export and
publication. Active routes need not be finished for ordinary adoption, and
native cutover supports active routes with exact native delivery mappings.

### 1. Export the installation automatically

Using the updated packaged utility on the installation, with paths supplied by
the rollout system:

```powershell
.\migrate-native.exe --export-upgrade-inventory `
  --config "$InstallRoot\resources\config.yml" --bundle-dir "$InstallRoot" `
  --output "$InventoryOutput"
```

The output path must be new. Omitting `--output` emits the inventory JSON on
stdout. Source equivalent: `python src/native_migration.py` with the same flags.

Export automatically adopts old SQLite storage if needed, never consumes a
waiting package, and does not edit config. It includes:

- Installation binding, source fingerprint, config hash, and retained-work digest.
- Exact original order/courier IDs, submission/cancellation state, attempts,
  presence of frozen requests, and existing mappings/statuses.
- Allowlisted business payload/request fields, original receipt representation,
  absolute frozen legacy `timestamp`/`offset` and sanitized `metadata`, and
  recursively redacted raw source context/observations and Farmax events.
- CDS receipt filename, original-byte hash/size, deterministic identity when
  readable, sanitized parsed business fields, and unresolved/resolved metadata.
- Pending ERP effect identities/operations for cutover diagnostics.

It excludes source passwords, configuration authentication, stored token files,
request headers, raw GraphQL documents, and uninterpretable raw file bodies.
Customer/order business evidence remains in the inventory for the migration.

Python API: `upgrade_inventory.export_upgrade_inventory(config_path, bundle_dir)`
returns the document. Its caller must hold the application instance lock and
have stopped application writers/services, as with the upgrade API.

### 2. Preserve exact links during the centralized server migration

The existing conversion/migration process must retain each inventory
`internal_id` as `integration_orders.external_order_id` in the intended native
merchant/connection. For locally known remote deliveries, persist the exact
original ID in `migration_snapshot.legacyDeliveryId`; the native ID comes from
`integration_orders.delivery_id`. The existing server
`migrate-integration-identities.ts` accepts and persists those original links.

Retain authoritative historical `COMPLETED`, `CANCELLED`, and `UNKNOWN` outcomes.
An absent server row is **not** evidence of a never-submitted order. Uncertain
orders should be retained explicitly as `UNKNOWN`, not guessed into a new
delivery. In-flight native imports must retain their actual source-bound jobs.

For original CDS files, use the deterministic inventory identity. Proven consumed
receipts can have authoritative terminal source tombstones; unresolved receipts
remain `UNKNOWN`. Existing random-ID orders keep their own original identities.
Never manufacture a receipt-to-random-order link by customer/address/filename
similarity. Unreadable receipts remain in the installation's unresolved inventory.

Because couriers have no legacy-ID column, the **server conversion process**
must emit its actual ID translation as a courier migration export:

```text
version: 1
merchant_id: actual native merchant UUID
connection_id: actual native connection UUID
export_id: centralized migration run identifier
mappings:
  - legacy_id: exact original remote courier ID
    native_id: actual migrated courier UUID
```

This is conversion output, not an installation manifest handwritten at a store.
An empty `mappings` array is valid when the installation has no retained couriers.
No courier names are used for matching.

### 3. Generate the ready-to-consume package at the server

From the companion worktree's `server` directory, using a dedicated read-only
administrative connection via the existing `DATABASE_URL` environment variable:

```powershell
bun scripts/export-native-upgrade-package.ts `
  --inventory "$InventoryInput" `
  --merchant-id "$NativeMerchantId" --connection-id "$NativeConnectionId" `
  --graphql "$NativeGraphqlEndpoint" --websocket "$NativeWebsocketEndpoint" `
  --courier-map "$CourierMigrationOutput" --output "$PackageOutput"
```

Use the actual deployed endpoints. Add `--auth-config "$NativeAuthConfig"` only
when changing auth; that file contains the complete actual
`domain/client_id/scope/audience`. Omitting it preserves installed auth. No auth
URLs, audience, or client are synthesized. Timezone is read from the native
merchant settings and must agree with any already-bound source timezone.

The exporter reads a repeatable, read-only server snapshot and derives exact
delivery/job/history/courier outcomes. It emits no `QUEUED` outcomes. Missing,
ambiguous, conflicting, or cross-source mappings produce a JSON issue report on
stderr and exit code **2**; no partial package is published. Output must be a new
file. Unknown outcomes are retained as `NEEDS_REVIEW`, not resubmitted.

Server API:

```typescript
exportNativeUpgradePackage(
  dedicatedPgClient,
  inventoryDocument,
  { merchantId, connectionId, graphqlEndpoint, websocketEndpoint, auth },
  courierMigrationDocument,
)
```

`auth` is optional/undefined to retain installation auth. Import from
`src/modules/integrations/upgrade-package-export.ts`. Returns the
consumer's complete version-1 package. `UpgradePackageExportError.issues`
contains machine-readable codes and affected local IDs. The function never
provisions connections, creates deliveries, or changes server migration data.

### 4. Publish through rollout and resume automatic update/startup

Transfer the generated package using the trusted rollout channel. Publish it
atomically beside the unchanged installed config:

```powershell
$Destination = "$InstallRoot\resources\native-upgrade.json"
$Staged = "$Destination.pending"
Copy-Item -LiteralPath "$PackageOutput" -Destination "$Staged"
if (Test-Path -LiteralPath "$Destination") {
  [System.IO.File]::Replace($Staged, $Destination, $null)
} else {
  [System.IO.File]::Move($Staged, $Destination)
}
```

The normal auto-installer/startup consumes this package through `--automatic`.
For a stopped-installation rollout, the same operation can be invoked directly:

```powershell
.\migrate-native.exe --automatic `
  --config "$InstallRoot\resources\config.yml" --bundle-dir "$InstallRoot"
```

No store-side config, source fingerprint, order list, or local manifest editing
is needed. If normal work resumed after export, regenerate the inventory/package
instead of bypassing the state-digest check. This pause is for planned cutover,
not an ordinary application-update prerequisite.

## Remaining integration dependencies

- Main/journal startup, branch-adoption, baseline and CDS filename hooks are now
  wired and covered by composed tests. One independently reproduced main issue
  remains: `build_services` must only infer a timezone from `farmax` when the
  active target is Farmax. An unused Farmax configuration currently changes CDS
  naive-time interpretation. The regression is active, not skipped or weakened.
- Cached-auth context validation remains the main/auth owner's work. At review,
  `AuthService.load_stored_token()` still constructed the cache reader without
  passing the effective auth context; publication and refresh both need the
  planned issuer/audience/client/scope fingerprint gate after native auth changes.
- Inventory export and server package production are implemented. The planned
  centralized conversion must still populate authoritative native identities and
  emit its courier map; missing migration evidence is reported rather than guessed.
  Actual merchant/connection/endpoints and any changed auth come from that rollout.
  Automatic network discovery/download is not implemented or needed by this
  file-based rollout path.
- Ambiguous original CDS history cannot be both automatically resent and
  guaranteed duplicate-free without additional authoritative evidence. It is
  retained visibly while new ordinary work continues.
- Full signed Inno Setup upgrade execution and x86 release qualification require
  separate installer verification. No deployment or server migration occurs here.

## Verification performed

### Independent wired-startup and installer check

- Full Python 3.8/offscreen suite: **417 passed, 13 skipped, 1 failed**. The only
  failure was `test_main_cds_keeps_naive_time_policy_when_unused_farmax_settings_exist`
  in `tests/integration/test_upgrade_composed.py`, pointing to `main.build_services`.
- Composed tests use production pre-boot setup (including the Windows 3.8 asyncio
  policy), automatic adoption, effective-settings installation, `build_services`,
  `setup_strategy`, the real CDS watcher/journal/dispatcher, and a real localhost
  HTTP endpoint. Ordinary updates and corrupt-optional-package fallback both
  imported new naive CDS receipts while preserving old mappings/settings and
  holding historical/incomplete files. No manual manifest/native settings are used.
- Malformed legacy scopes now produce an explicit `ValueError`, including when
  they match a malformed persisted `SyncScope`; dry-run, apply and preparation
  regressions verify no mapping changes or migration artifacts.
- Resource allowlists are shared by freeze configuration and checked against
  Inno file entries. A small **real Inno compile**, without freezing/installing,
  retained SQLite DLL/PYD modules, nested runtime resources/config, timezone data,
  Qt plugins, the updater config and verification key, and excluded local DB/WAL,
  tokens, config, inventory packages and test data. Missing `migrate-native.exe`
  now fails installer compilation. Runtime -> helper -> application ordering and
  silent-mode helper execution are checked. Self-updater copy failures still
  attempt to restart the already-installed application.
- Server exporter/Python-consumer interoperability: **11 passing tests**, no
  skipped bridge. Exact environment used:

  ```powershell
  $env:VELIDE_MIDDLEWARE_PYTHON='C:\Users\Breno\Dev\velide-middleware\.venv38\Scripts\python.exe'
  $env:VELIDE_UPGRADE_PYTHON=$env:VELIDE_MIDDLEWARE_PYTHON
  $env:VELIDE_MIDDLEWARE_WORKTREE='C:\Users\Breno\AppData\Local\Temp\opencode\velide-middleware-reliability'
  $env:VELIDE_UPGRADE_TEST_TEMP='C:\Users\Breno\AppData\Local\Temp\opencode'
  bun test tests/deliveries/upgrade-package-export.test.ts
  ```

  The current server test actually reads `VELIDE_UPGRADE_PYTHON`; the requested
  `VELIDE_MIDDLEWARE_PYTHON` variable is configured and explicitly forwarded.

### Earlier helper/package-production checks

- Python 3.8, Qt offscreen: 37 passing focused inventory/upgrade/migration cases in
  `test_upgrade_inventory.py`, `test_automatic_upgrade.py`, and `test_native_migration.py`.
- Server: 11 passing tests in `tests/deliveries/upgrade-package-export.test.ts`,
  using real PostgreSQL-compatible SQL, including a generated Python inventory
  -> SQL exporter -> real automatic Python consumer interoperability test.
  This covers active routes, completed/cancelled history, accepted jobs,
  unresolved work, CDS linked/terminal/unknown receipts, and repeatability.
  Server `bun run typecheck` passed.
- Full x64 cx_Freeze build using the existing `installer/src/cli/setup.py` and
  retained `qt_compat` completed for the preceding automatic-consumer change.
- A fresh frozen build including the new inventory switch was blocked by
  `No space left on device`; incomplete temporary build outputs were removed.
  The new inventory CLI is verified through the Python 3.8 entry point, not a
  newly completed frozen executable.
- Frozen `migrate-native.exe --help`, original-schema automatic adoption,
  provisioned native cutover, and repeated invocation passed. The CLI test can
  target a built executable through `VELIDE_UPGRADE_EXE`.
- Ruff Pyflakes (`--select F`) passed for the owned Python changes/tests;
  tracked owned-file `git diff --check` passed.
