# Middleware reliability and native integration

## Current status — user-directed restoration (2026-11-14)

**Automatic native and legacy imports and Velide-led Farmax departure/completion are restored.** Ordinary updates automatically adopt the installed database and configuration. The earlier observer-only reduction was rejected by the user and is superseded; it is not the current operating contract.

Final verification: **478 Middleware tests passed, including disposable Firebird 2.5; server typecheck and 229 selected tests passed**, including real Python → Bun → Python inventory/export/consumer interoperability. Two independent read-only reviewers approved their scopes with no outstanding findings. The final Main x64 cx_Freeze build and `migrate-native.exe --help` passed. Frozen automatic CLI/inventory checks and the updater full freeze are still pending.

[Middleware PR #3](https://github.com/BrenoLopesz/velide-middleware/pull/3) and [server PR #250](https://github.com/BrenoLopesz/velide-server/pull/250) remain unmerged. Full PR CI for the restoration after commits has not run yet. Signed installer release, x86 qualification, and a live pilot are not complete.

## Restored delivery behavior

- Both transports import automatically. Configurations without `api.native` continue using the installed legacy endpoint and authenticated session; a new store-discovery endpoint is not required. Frozen requests retain their identity and original order time across retries.
- Velide route facts drive each related Farmax delivery: departure projects courier, departure time and `R`; completion projects `V`, fills arrival when absent, and updates eligible active sale items to `V`. Existing arrival is preserved. An already returned `V` delivery retains populated courier/departure fields.
- `C`/`D`/`I` item rows are never revived. `CONCLUIDO`, `HORAFINAL`, business dates and cashier ownership are preserved. Cashier transfer/finalization remains future work and is not a prerequisite for these ordinary writes.
- Exact source, branch, item and courier identities are checked. Retained `source_context` also binds original adopted mappings without an order payload, preventing later observations from silently retargeting them.
- `SyncOrders` retains imports, cancellation intent, source evidence and remote state; `SyncEffects` holds durable ordered ERP writes. A shared journal/ERP lock and an authority check before commit prevent writes already known to be superseded. Acknowledgements follow committed, verified fields; obsolete callbacks cannot acknowledge newer work. This is not a transaction spanning Velide and Farmax, and cannot fence remote changes that have not yet been observed.
- Native notifications accelerate periodic authoritative reconciliation. Missed notifications, offline completion and paginated reinstall recovery use retained identities and route revisions. Completed native deliveries remain monitored for undo/redo.

### Cancellation policy

The user explicitly chose these rules:

| Source/route condition | Current behavior |
|---|---|
| All retained item rows are `C` | The item-row count reaches 100% and creates durable whole-delivery cancellation intent, **even if `ENTREGAS` remains**. This is the user's integration policy, not a claim that Farmax always deletes such deliveries. |
| Partial cancellation | Portuguese progress logging only; partial cancellation itself creates no hold and does not stop ordinary delivery work. |
| Repeated or incomplete snapshots | Counts are correlated by retained item identity, never cumulatively summed across polls. Missing historical `C` rows remain cancellation evidence. Missing active/unconfirmed rows become unknown and cannot produce 100%. |
| Physical delivery removal | Correlate retained sale identity, a complete sale snapshot and DELETE evidence. Qualifying removal requests cancellation. Branchless-log limitations are logged; general, unconditional removal attribution is not claimed. |
| Native delivery in transit | Keep cancellation `REQUESTED` with `CANCELLATION_DEFERRED`; do not acknowledge it or exhaust the retry budget while waiting for the active route. |
| Route aborted and delivery returns to `PENDING` | Retry the retained cancellation. |
| Route completed | Keep an explicit lifecycle conflict; do not retroactively change delivery history or finances. |

`DELIVERYLOG` lacks branch, row-incarnation and transaction identity. Reused IDs, source restores and ambiguous evidence still need reconciliation; a DELETE entry alone is insufficient.

### Recovery, CDS and operator messages

`Processando no Velide` means import-job acceptance, not delivery completion. Errors and unresolved work remain visible in the dashboard and journal. **Tentar sincronização novamente** provides explicit retry; source-review holds require resolution rather than being silently cleared. A nonretryable legacy recovery failure becomes `CONFLICT`, preventing a hot retry loop.

CDS scans are bounded and recover incomplete/new/changed files. Original unknown receipts are saved in the upgrade inventory, held visibly as `NEEDS_REVIEW`, and never blindly resubmitted. New files continue flowing. Stable identity uses the lowercase filename and original `horario_pedido`; receipt evidence survives upgrades. Authoritative package resolutions can link or classify historical receipts; local filename/customer similarity cannot recover the old release's random-ID mapping.

Dashboard messages are plain Portuguese. Original errors and tracebacks remain in diagnostic logs. The native client respects `use_neighbourhood`, `use_ssl` and configured retries. Authentication context (issuer, audience, client and scope) is persisted and checked before cached-token publication/refresh, including the token path used with external configuration. Compatible old JWT sessions remain usable on ordinary updates; changed authentication cannot reuse a mismatched cached session.

## Ordinary automatic update

The auto-installer/startup adopts the old SQLite database and configuration automatically, retaining mappings, pending effects and a WAL-aware backup. **No handwritten manifest, native package or completed-route requirement applies to an ordinary update.** Existing legacy configuration keeps working until a valid native package is provisioned. An optional package validation/publication failure leaves legacy service available.

The first-hop installer directly installs the corrected update batch before the old self-updater executes. Release allowlists exclude local databases and tokens. Real CMD/robocopy execution and a small Inno compile have been tested; this does not establish a full signed installer release.

The packaged automatic entry point, also used by the installer, is:

```powershell
.\migrate-native.exe --automatic `
  --config "$InstallRoot\resources\config.yml" --bundle-dir "$InstallRoot"
```

It takes the application instance lock; application/database writers must be stopped while adoption or cutover runs. Configuration can also be selected through `VELIDE_CONFIG_PATH`.

## Planned native cutover

Exact flags, package fields and publication rules are in [installer/UPGRADE_CONTRACT.md](installer/UPGRADE_CONTRACT.md). Its command/package contract applies here; the final restoration status above supersedes older intermediate verification/dependency notes.

1. Deploy server migrations `0057_integration_orders.sql` and `0058_native_integration_permissions.sql`, compatible import workers and the native server. Provision the actual merchant/connection and internal Auth0-backed user. The integration role needs `integration.import`, `integration.read`, `integration.cancel`, `integration.retry` and `courier.read`. Generic `getJobStatus`/`jobStatusUpdated` retain merchant authentication without an added `delivery.read` requirement.
2. Pause Middleware for the final inventory and package publication. Export automatically from the installation; the output path must be new:

   ```powershell
   .\migrate-native.exe --export-upgrade-inventory `
     --config "$InstallRoot\resources\config.yml" --bundle-dir "$InstallRoot" `
     --output "$InventoryOutput"
   ```

   Source equivalent: `python src/native_migration.py` with the same flags. The inventory includes retained work and sanitized business evidence, excluding credentials/tokens. Export can adopt old storage but does not consume a waiting package or change configuration.
3. The centralized server migration provides exact native identities and its courier-ID translation. It retains original order IDs, legacy delivery links, jobs, immutable customer/address/time data, and authoritative historical outcomes. Missing server rows do not prove an order was never submitted.
4. From the companion worktree's `server/` directory, using a dedicated read-only administrative `DATABASE_URL`, generate the bound package:

   ```powershell
   bun scripts/export-native-upgrade-package.ts `
     --inventory "$InventoryInput" `
     --merchant-id "$NativeMerchantId" --connection-id "$NativeConnectionId" `
     --graphql "$NativeGraphqlEndpoint" --websocket "$NativeWebsocketEndpoint" `
     --courier-map "$CourierMigrationOutput" --output "$PackageOutput"
   ```

   Output must be new. Add `--auth-config "$NativeAuthConfig"` only for a complete authentication change. The exporter derives timezone from merchant settings and checks existing source timezone. Missing/ambiguous/conflicting links produce an issue report and exit **2**, with no partial package. It does not create native deliveries or invent IDs.
5. Provision the generated file as `native-upgrade.json` alongside the installed config through the trusted rollout channel. Atomic publication follows the contract:

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

   Normal auto-installer/startup consumes it through `--automatic`; the same command above can be invoked for a stopped-installation rollout. No store-side configuration or manifest editing is required.

The package is bound to installation/config/source identity and the retained-work digest. If work changed after export, regenerate inventory/package. The consumer validates binding and consistency, not a server signature. It prepares a separate database and atomically activates database/endpoint through config replacement; failures retain the old usable installation and repeat application is a no-op. Active routes can cut over with exact native mappings; pending ERP effects defer cutover while legacy service remains available. These are planned-cutover conditions, not ordinary-update requirements.

## Verification and remaining release work

| Check | Latest result |
|---|---|
| Full Middleware suite | **478 passed**, including disposable Firebird **2.5** |
| Server | Typecheck and **229 selected tests passed**, including real Python → Bun → Python export/consumer interoperability |
| Independent read-only reviews | Both scopes approved, no outstanding findings: Farmax final **92** tests; upgrades final **36**, plus the previous **111** checks (separate runs, not an additive suite total) |
| Installer checks | Actual CMD/robocopy and small Inno compile passed |
| Final x64 cx_Freeze | Main application and update installer built successfully |
| Frozen migration CLI | `--help`, automatic original-installation adoption/native cutover/repeat, and inventory export passed (2 executable-level tests) |
| Frozen application startup | Disposable offline CDS installation reached automatic database adoption and remained running |
| Companion server CI | [Full CI passed at `6dc4b2a`](https://github.com/BrenoLopesz/velide-server/actions/runs/43605095759) |
| Pending release checks | Full signed installer release/upgrade, x86 and live pilot |

Middleware suite command: set `QT_QPA_PLATFORM=offscreen`, `PYTHONDONTWRITEBYTECODE=1`, then run `python -m pytest -q -p no:cacheprovider tests`. `VELIDE_TEST_FIREBIRD=1` enables the disposable localhost Firebird fixture; `VELIDE_TEST_FIREBIRD_PASSWORD` selects its service password. No customer database is used by that fixture.

Operational qualification still needs the actual Farmax/Firebird version, custom triggers, date/time behavior and deployed native services/authentication, followed by a selected-store rehearsal and pilot. Configuration fingerprints do not prove database incarnation. Delivery synchronization does not settle cashier accounts; rollback after native business writes requires reconciling those facts, not just restoring old SQLite. No live store has been switched by this work.
