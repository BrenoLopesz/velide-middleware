# Middleware reliability and native integration

Implementation handoff for the 2026-11-11 reliability/native migration plan.

> **Independent-review correction:** the original automatic ERP write-back and cancellation assumptions were not justified by the retained native evidence. Farmax now runs as a read-only observer; legacy automatic dispatch is paused because authoritative store binding is unavailable. The detailed finding/disposition report is `MIDDLEWARE_INDEPENDENT_REVIEW.md` in the companion server PR. The automation described in the original plan is not yet a qualified Farmax workflow.

## Delivered implementation

- Exact Farmax ID normalization, polling recovery, one WebSocket business-event path, and corrected departure reconciliation.
- Sale-level discrepancy classification. Retained all-C and partial C/D states never authorize automatic delivery cancellation. Removed I/V and mixed S/C cases are review candidates; the branchless log cannot establish an unconditional identity/intent guarantee.
- A versioned SQLite journal for source observations, original payloads, frozen requests, import acceptance, cancellation, remote state, and ordered ERP effects. Acknowledgements follow commit.
- Restart recovery, hydration before Farmax monitoring, bounded log reads with overlap, and a periodic current-pending scan. A fixed overlap is not treated as a commit-order guarantee.
- Farmax observation validates branch, multiplicity, original source provenance, dates, and retained courier mapping. It recognizes existing persisted fields and never writes departure, arrival, conclusion, sale status, or cashier fields. Unknown/custom schema objects are inspected, never installed or replaced at startup.
- Native import jobs, exact source-state reconciliation, paginated reinstall recovery, source-scoped notification hints, explicit retry, and terminal/unknown identity retention.
- CDS startup scans, move/modify handling, incomplete-file retry, deterministic identity, and durable acceptance.
- TLS verification and timeouts for authentication, refresh-token rotation, scoped database binding, and stopped-session callback guards.
- A packaged offline migration utility, including backups and a read-only validation mode.

Full cashier finalization and courier-return exception workflows remain deferred. Source IDs are preserved; native merchant/connection scope is additional API ownership, not a Farmax business-key redesign.

## Backend dependency and activation

The companion `velide-server` branch is `feature/middleware-native-integration`.

Deploy the backend's `0057_integration_orders.sql` and `0058_native_integration_permissions.sql`, compatible import workers, and the native server before activating this client.

Provision an `integration_connections` record bound to the merchant, logical ERP source, and internal Auth0-backed user. A dedicated integration role needs:

- `integration.import`
- `integration.read`
- `integration.cancel`
- `integration.retry`
- `courier.read`

General delivery cancellation/read authority is not required by the source integration operations. Configure the actual native Auth0 issuer, audience, and client before authenticating.

Example native configuration fragment:

```yaml
api:
  velide_server: "https://native.example.invalid/graphql"
  velide_websockets_server: "wss://native.example.invalid/graphql"
  use_ssl: true
  native:
    merchant_id: "00000000-0000-0000-0000-000000000001"
    connection_id: "00000000-0000-0000-0000-000000000002"
    timezone: "America/Sao_Paulo"
    reconcile_interval_ms: 15000
farmax:
  # Supply the installation's existing host/file/user/password.
  timezone: "America/Sao_Paulo"
```

Merchant and source timezone must agree. Naive source order times are localized explicitly; ambiguous local times are rejected. Retries reuse the frozen request and original timestamp.

Omitting `api.native` retains compatibility documents but **does not enable automatic legacy mutation**. The retained legacy API authenticates users internally yet exposes no authoritative store-ID operation to bind durable work; successful authentication, a location ID, or a client token claim is insufficient. Its deployed idempotency is not in doubt. Each local database is bound to one endpoint/source scope. Switching to native requires the migration manifest, rather than just changing a URL. `VELIDE_CONFIG_PATH` can select a configuration file for rehearsal.

## Operational behavior

`Processando no Velide` means a job has been accepted, not that a delivery has been created or delivered. Errors remain visible in the delivery table/log and in journal `last_error` fields. In native mode, the row context menu offers **Tentar sincronização novamente** for explicit retries.

Job notifications only accelerate reconciliation. Periodic authoritative reads recover missed notifications and offline route completion. Scope recovery uses pages of at most 100 references; its progress is durable. Source references missing from the server remain unresolved.

`SyncOrders` separates submission, cancellation, remote state, and mapping. `SyncEffects` now records **read-only comparison obligations**, tied to authoritative revisions/generations. Cancellation, deletion, undo, or changed source evidence supersedes obsolete observations; late results cannot acknowledge a new generation. Completed native deliveries remain monitored for undo/redo. A remote completion remains unresolved until persisted Farmax delivery fields match or the discrepancy is reviewed. An existing matching V can satisfy both departure/completion observations without replaying a departure. This is not a cashier-settlement acknowledgement.

## Existing-installation migration

Finish legacy routes and reconcile all outstanding ERP work first. Stop Middleware while preparing and applying the manifest. Rehearse on a SQLite copy.

1. Create/verify native pending deliveries and source identities in staging. The backend identity migration utility links existing native pending records and preserves explicit historical outcomes; it does not manufacture a pending delivery or infer its address.
2. Map every retained local order and courier. Use exact source/legacy/native identifiers, never customer/address matching.
3. Prepare SQLite without starting ingestion, validate the manifest, then apply it:

```powershell
.\migrate-native.exe "velide.db" "manifest.json" --prepare
.\migrate-native.exe "velide.db" "manifest.json"
.\migrate-native.exe "velide.db" "manifest.json" --apply
```

The source equivalent is `python src/native_migration.py ...`. Preparation upgrades the SQLite schema and retains existing mappings. Application creates a WAL-aware backup, checks outstanding effects/routes, applies mappings transactionally, and stores the full manifest audit. Repeating an already applied manifest is idempotent.

Existing unbound work also requires a qualified `source_identity` configuration fingerprint and, for Farmax, `farmax_branch_id` during offline preparation. Calculate the fingerprint using `utils.source_identity.configured_source_identity` with the actual installation settings; it detects host/file/folder changes but does not prove database incarnation. Do not guess a branch or adopt data from an unverified source. A legacy order with attempts, a frozen request, a job, or cancellation uncertainty cannot be declared never submitted merely because its delivery mapping is missing.

SQLite v4 also persists the original `source_timezone`. Qualify that timezone when adopting older naive timestamps; `--prepare` accepts it in the manifest. Payload comparison uses canonical UTC instants and retains raw local receipt evidence separately. The CDS identity key still uses the original file receipt representation, so upgrading/recovering does not regenerate order IDs. A conflicting immutable payload commits a review hold before reporting the error, preventing the old queued payload from being dispatched.

Example local manifest:

```json
{
  "version": 1,
  "old_scope": "https://legacy.example.invalid/|Farmax",
  "new_scope": "https://native.example.invalid/graphql|Farmax|00000000-0000-0000-0000-000000000001|00000000-0000-0000-0000-000000000002",
  "deliveries": [
    {
      "internal_id": "123",
      "legacy_delivery_id": "retained-legacy-id",
      "native_delivery_id": "00000000-0000-0000-0000-000000000003",
      "state": "IMPORTED"
    }
  ],
  "couriers": [
    {
      "local_id": "5",
      "legacy_id": "retained-legacy-courier",
      "native_id": "00000000-0000-0000-0000-000000000004"
    }
  ]
}
```

Possible local outcomes are `QUEUED`, `ACCEPTED` (requires `native_job_id`), `IMPORTED` (requires `native_delivery_id`), `TERMINAL`, and `NEEDS_REVIEW`. Previously submitted orders cannot simply be declared queued. Retain unknown legacy outcomes explicitly and seed their identities on the backend too.

### CDS file baseline

CDS identity is the lowercase filename plus the original `horario_pedido`, hashed under the configured source. A repeated observation is the same order. Two filenames can represent two legitimate identical orders; changing data under the same filename/timestamp is a conflict, not a new order.

Before upgrading an existing CDS installation, inventory files that the old release already consumed: that release generated volatile random IDs. Include reviewed completed files in the local manifest's optional `processed_cds_files` array, and run `--prepare` while the old application is stopped:

```json
"processed_cds_files": [
  { "filename": "ent1.json", "created_at": "2026-01-01T12:00:00" }
]
```

These become durable terminal source identities, preventing the new startup scan from importing historical files. List only confirmed processed files; preserve pending/unknown files for reconciliation. Existing mapped orders still require explicit delivery entries. Files are never deleted by this tool.

## Verification and remaining release work

Local verification covers Python 3.8.0, Qt, real SQLite, native HTTP fixtures, cancellation/restart/response-loss scenarios, and a disposable Firebird **2.5** database with a delivery-log trigger. The Windows x64 cx_Freeze build and packaged `migrate-native.exe --help` were exercised. The build includes timezone and certificate data and a build-only workaround for Python 3.8.0 wildcard-path handling.

The earlier **178-pass** result predates independent review and did not establish ERP correctness. Consult the independent-review report and subsequent PR checks for corrected verification results.

Run the suite with `QT_QPA_PLATFORM=offscreen`, `PYTHONDONTWRITEBYTECODE=1`, and `python -m pytest -q -p no:cacheprovider tests`. Setting `VELIDE_TEST_FIREBIRD=1` enables the disposable localhost Firebird test; `VELIDE_TEST_FIREBIRD_PASSWORD` selects the test service password. No customer database is used by that fixture.

Still required before live cutover:

- Qualify the actual deployed Farmax/Firebird version and source address/date rules, including midnight boundaries and representative triggers/index plans.
- Rehearse against the deployed native API, broker, geocoding dependencies, real authentication, and copied store data; verify migrated coordinates and immutable order times.
- Qualify the x86 build and signed installer upgrades on supported Windows installations. A successful x64 freeze is not an installer-upgrade rehearsal.
- Select the pilot store, supply its exact migration manifest/exports, drain legacy work, migrate and reconcile before resuming one backend, and observe its backlog to completion.
- A rollback after native business writes requires reconciliation of those new facts; restoring an old SQLite file alone is insufficient.

The implementation has not deployed migrations or switched a live store. Phase 6 remains an operational staging/pilot task.
