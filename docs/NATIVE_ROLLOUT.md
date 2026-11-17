# Native production rollout and `native-upgrade.json`

## Readiness boundary

The companion PRs implement the native integration API, recovery protocol,
installation adoption, package exporter/consumer, and reversible new-order intake.
Passing these tests supports a controlled staging/store pilot; it is not proof
that a production deployment or legacy-data conversion has been completed.

In particular:

- The exporter is **read-only**. It does not create users, merchants, connections,
  deliveries, jobs or couriers, and does not convert the legacy server's database.
- The identity-linking utility links verified existing native pending deliveries
  or retains explicit historical outcomes. It is not a general business-data
  migration or a delivery/geocoding importer.
- The Middleware consumes a local package. **Automatic inventory upload, network
  discovery and package download are not implemented by this file-based flow.**
  A controlled rollout/remote-management channel must orchestrate those steps.
- A package is bound to one installation and its retained state. It must not be
  bundled into the generic release or copied unchanged to every store.
- Native requires an independent user login. Customers need no YAML edits, but
  the first cutover is not completely zero-interaction.

## 1. Prepare the production backend

For the recommended same-Auth0-tenant rollout, the new production server uses:

```dotenv
AUTH0_DOMAIN=login.velide.com.br
AUTH0_AUDIENCE=https://velide.com.br/
AUTH0_ALGORITHM=RS256
```

These are the audited production identity settings, not an instruction to change
the development `.env`. Verify the actual deployed environment. Use the custom
domain consistently for token requests and server issuer verification; the default
tenant domain has a different issuer string even though it belongs to the same
tenant.

The complete deployment also needs:

1. The intended production PostgreSQL database and prerequisite migrations,
   including `server/drizzle/0057_integration_orders.sql` and
   `0058_native_integration_permissions.sql` in the companion Server repository.
2. Compatible import workers and their configured queue/broker dependencies, then
   the native server. Verify that an accepted import job actually completes;
   an HTTP/GraphQL service accepting requests is not sufficient.
3. Actual HTTPS GraphQL and WSS subscription endpoints ending in `/graphql`,
   including reverse-proxy WebSocket support. These may have the same hostname
   and path with different schemes.
4. The existing production Auth0 subjects linked to the correct native
   `users.external_auth_subject`. Equal email addresses are not identity proof.
5. Merchant membership with `integration.import`, `integration.read`,
   `integration.cancel`, `integration.retry` and `courier.read`.
6. An enabled `integration_connections` record for the intended merchant and ERP
   source (`CDS` or `Farmax`), owned by the correct native internal user.
7. The actual merchant timezone, consistent with the installation's bound source
   timezone, and native couriers with authoritative old-to-new ID mappings.
8. A production Native/public Middleware client in the existing Auth0 tenant:
   either an explicitly reviewed reuse of the current client, or a separate app.
   Device Code/Refresh Token grants and the intended user connections must work.
   No client secret belongs in the installed application.

See [the account-specific Auth0 audit](AUTH0_TRANSITION.md) for current values and
the existing Post Login Action restrictions affecting new web clients.

## 2. What the package contains

`native-upgrade.json` is generated centrally for one stopped installation. It
provides the Middleware with both the target configuration and evidence for safely
handing over its retained orders.

| Section | Purpose |
| --- | --- |
| `version` | Package format version, currently `1` |
| `binding` | Installation UUID, original endpoint/source scope, physical-source configuration fingerprint and exact installed config hash |
| `inventory_state_sha256` | Retained-work fingerprint; rejects stale packages if local work changed after export |
| `authority` | Export provenance and review diagnostics; not a cryptographic signature |
| `configuration.api` | Actual GraphQL/WebSocket endpoints and native merchant UUID, connection UUID and timezone |
| `configuration.auth` | Optional complete public auth configuration; omission preserves installed auth settings |
| `manifest` | Exact old/new scopes, original ERP references and native delivery/job/history/courier mappings |
| `cds_resolutions` | Authoritative, original-byte-bound CDS receipt resolutions, when applicable |

This schematic shows the shape only. It is **not an installable package**; all
`<...>` values stand for exporter-derived data, and the empty arrays are valid only
when there is genuinely no corresponding retained work:

```json
{
  "version": 1,
  "binding": {
    "installation_id": "<from the installation inventory>",
    "old_scope": "<installed legacy endpoint>|<CDS or Farmax>",
    "source_identity": "<inventory source SHA-256>",
    "config_sha256": "<inventory config SHA-256>"
  },
  "inventory_state_sha256": "<inventory retained-work SHA-256>",
  "authority": {
    "kind": "server-upgrade-export",
    "exported_at": "<export time>",
    "merchant_id": "<actual native merchant UUID>",
    "connection_id": "<actual native connection UUID>",
    "courier_export_id": "<authoritative courier conversion run>",
    "inventory_sha256": "<exported inventory SHA-256>",
    "retained_unknown_ids": [],
    "unreadable_cds_files": []
  },
  "configuration": {
    "api": {
      "velide_server": "https://<actual-native-host>/graphql",
      "velide_websockets_server": "wss://<actual-native-host>/graphql",
      "native": {
        "merchant_id": "<actual native merchant UUID>",
        "connection_id": "<actual native connection UUID>",
        "timezone": "<actual merchant timezone>"
      }
    },
    "auth": {
      "domain": "login.velide.com.br",
      "client_id": "<reviewed production Native/public client ID>",
      "audience": "https://velide.com.br/",
      "scope": "openid profile email offline_access"
    }
  },
  "manifest": {
    "version": 1,
    "old_scope": "<inventory old scope>",
    "new_scope": "<GraphQL endpoint>|<ERP>|<merchant UUID>|<connection UUID>",
    "deliveries": [],
    "couriers": []
  },
  "cds_resolutions": []
}
```

Generate the file with the exporter; do not invent hashes, guess identities,
replace the delivery list with `[]`, or match couriers by name. The actual exporter
derives delivery/job/history outcomes and fails if the required links are missing.
Uncertain history remains `NEEDS_REVIEW`, not permission to submit a duplicate.
Pending local ERP effects also prevent package export until reconciled.

The package contains no passwords, access tokens, refresh tokens, client secrets
or production database connection strings. Inventory may contain customer/order
business evidence, so both inventory and package need access-controlled handling.
Never commit real store exports to either repository.

## 3. Generate and deliver it

These commands describe operator/rollout automation, not tasks for the customer.
Use actual paths and IDs; keep credentials in the administrative environment.

### A. Stop and export the installation

Take a coherent backup, stop Middleware and retain that pause through final
publication. If the new helper is not installed yet, first distribute the tested
helper/update using the rollout process; an ordinary update can remain on legacy.

```powershell
& "$InstallRoot\migrate-native.exe" --export-upgrade-inventory `
  --config "$InstallRoot\resources\config.yml" `
  --bundle-dir "$InstallRoot" `
  --output "$InventoryOutput"
```

The output path must be new. This command can adopt old local SQLite storage, but
does not consume a waiting native package or change installed API configuration.
Transfer the inventory securely to the central migration/export process.

### B. Prepare authoritative native data

The centralized conversion process must already have provisioned the target
identities and exact order/delivery/job/history/courier links. Preserve original
ERP order references and original legacy remote IDs. The companion
`server/scripts/migrate-integration-identities.ts` can link supported verified
records; its default dry-run rolls back and `--apply` is an administrative write.
This guide does not authorize running that write against production.

The courier-conversion output has this shape:

```json
{
  "version": 1,
  "merchant_id": "<actual native merchant UUID>",
  "connection_id": "<actual native connection UUID>",
  "export_id": "<conversion run identifier>",
  "mappings": [
    {
      "legacy_id": "<exact original remote courier ID>",
      "native_id": "<actual converted courier UUID>"
    }
  ]
}
```

An empty courier map is valid only when no corresponding couriers are retained.

### C. Export the final package

From the companion Server repository's `server/` directory, with `DATABASE_URL`
set to the intended dedicated read-only administrative connection:

```powershell
bun scripts/export-native-upgrade-package.ts `
  --inventory "$InventoryInput" `
  --merchant-id "$NativeMerchantId" `
  --connection-id "$NativeConnectionId" `
  --graphql "$NativeGraphqlEndpoint" `
  --websocket "$NativeWebsocketEndpoint" `
  --courier-map "$CourierMigrationOutput" `
  --auth-config "$NativeAuthConfig" `
  --output "$PackageOutput"
```

`$NativeAuthConfig` contains only the four public `auth` fields illustrated above.
Omit `--auth-config` to preserve the installation's existing auth configuration;
it is not necessary to create a new Auth0 tenant. Even with unchanged settings,
native obtains its own login/refresh grant rather than cloning legacy's cache.
The timezone comes from native merchant settings, not a guessed package default.

The exporter refuses missing/ambiguous/conflicting evidence with an issue report
and exit code **2**, and publishes no partial package. Resolve those issues before
continuing; this is not a reason to remove the checks.

### D. Publish to that installation and apply

The trusted rollout channel publishes the completed file **atomically** at:

```text
<InstallRoot>\resources\native-upgrade.json
```

That is beside the normal installed `config.yml`; an external config override
uses its own config directory. Stage to another filename first, then rename/replace
on the same volume after the transfer is complete. The full publication example is
in [the installer contract](../installer/UPGRADE_CONTRACT.md#4-publish-through-rollout-and-resume-automatic-updatestartup).

The internal installer/startup automatically consumes the package. An operator can
invoke the same operation while Middleware is stopped:

```powershell
& "$InstallRoot\migrate-native.exe" --automatic `
  --config "$InstallRoot\resources\config.yml" `
  --bundle-dir "$InstallRoot"
```

Inspect the JSON result: a successful native handoff requires
`native_applied: true` and no `package_error` on its first application. A valid
repeat is a no-op (`native_applied: false`), so also verify the installed profiles,
selection and prior application receipt. Exit code zero alone is **not** proof of
native activation: optional package rejection intentionally does not fail an
otherwise valid ordinary update.

Missing or rejected package → retain the current legacy configuration for an
initial legacy installation, with diagnostics. Valid package → prepare private
native database/ownership copies, retain legacy profile/session, atomically
publish the new profile configuration, then request native login. Repeated updates
do not transfer orders again or override a later legacy intake selection.

If the local config or retained work changed since inventory, regenerate the
inventory and package. The final paused window needs operational orchestration;
do not silently bypass freshness checks to make a long-running conversion pass.

## 4. Pilot, rollback and production sign-off

Verify the final signed updater on a controlled installation; applicable x86,
Firebird/custom-trigger, timezone and network conditions still need qualification.
Test native device login/refresh/restart, new web-client login/Action behavior,
merchant/source authorization, accepted-job completion, reconciliation,
cancellation and ERP acknowledgements, including wrong-account denial.

Use the tray's new-order selector or, with Middleware stopped:

```powershell
& "$InstallRoot\migrate-native.exe" --select-backend legacy `
  --config "$InstallRoot\resources\config.yml" --bundle-dir "$InstallRoot"
```

This changes new intake, not existing ownership. Native-owned work stays on native
even while new orders go to legacy. A complete reversal of server business writes
is a separate reconciliation exercise, not something this selector promises.

Preserve the legacy service, identity access and retained local state throughout
the rollback window. Close that window only after outstanding orders,
cancellations and ERP obligations are reconciled and production sign-off is given.

## Trust boundary

The binding hashes detect the wrong installation or stale work; `authority` is
provenance, **not signature verification**. The application release's signature
does not automatically authenticate a separately provisioned store package.
Controlled authenticated distribution, integrity protection and local filesystem
permissions are required. An unattended fleet-download design must explicitly
address authorization, package authenticity, freshness, pause/resume, safe retry
and audit records before replacing this operator-managed file-based rollout.
