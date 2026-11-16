# Auth0 transition and reversible Middleware connections

## What was actually verified

Initial source/configuration inspection and public OIDC discovery were performed
on 2026-11-15. A subsequent user-authorized, read-only inspection of the logged-in
Auth0 dashboard is recorded below. No settings were saved, Actions deployed,
credentials rotated, tokens requested, or live store actions performed.

| Local component | Configured issuer domain | Configured API audience |
| --- | --- | --- |
| Legacy MapLog server | `login.velide.com.br` | `https://velide.com.br/` |
| Installed legacy Middleware | `login.velide.com.br` | `https://velide.com.br/` |
| New server development configuration | `velide-dev.us.auth0.com` | `https://velide.com.br/` |

Both public `/.well-known/openid-configuration` endpoints responded. They advertise
their respective, different issuer URLs and `/oauth/device/code` endpoints. Their
public JWKS signing-key ID sets were disjoint at inspection time. Thus a legacy
token is **not compatible with the inspected native configuration**, despite the
matching audience: native JWT verification requires its configured issuer and
signing keys too.

Discovery alone did not establish the production tenant, app-specific grants, or
custom-domain ownership. The dashboard audit now establishes those legacy facts;
it does not prove the deployed native server's environment or database mappings.
Development `.env` files remain distinct from production deployment evidence.

## Verified in the live Auth0 dashboard

The user authorized browser navigation and foreground control for this read-only
audit. Only the relevant tenant/application/API/Action pages were inspected; no
customer directory or password/token export was performed. Public client IDs below
are configuration identifiers, not secrets.

| Item | Observed configuration |
| --- | --- |
| Live legacy tenant | `dev-b6bioprv4weafg2d`, **PRODUCTION**, region US-4 |
| Default tenant domain | `dev-b6bioprv4weafg2d.us.auth0.com` |
| Custom domain | `login.velide.com.br`, **VERIFIED**, attached to that Production tenant |
| Legacy Middleware application | `Middleware`, type **Native**, client ID `S89fszfvMnU3WrVulijNCO7FokD1Lktz` — matches installed configuration |
| Middleware grants | **Device Code** and **Refresh Token** enabled; Authorization Code and Implicit also enabled; Password disabled |
| Middleware user connections | `Username-Password-Authentication` and `google-oauth2` both enabled |
| Middleware refresh policy | Rotation disabled; idle and maximum refresh-lifetime switches disabled. The displayed 15/30-day numeric fields were disabled defaults, not active expiration policies |
| Production resource API | `Velide`, identifier `https://velide.com.br/`, **RS256**, **Allow Offline Access enabled** |
| Production API policy | Auth0 RBAC and permissions-in-token enabled; user-delegated access **All apps allowed**; machine-to-machine client access **Per-app authorization** |
| Development tenant | `velide-dev`, **DEVELOPMENT**, region US-5; separate from the Production tenant |
| Development API | `velide-dev`, same identifier `https://velide.com.br/`, RS256 and offline access enabled; user-delegated access **Per-app authorization**; Auth0 RBAC disabled |
| Development applications | Default App; Velide Courier Admin CLI (Native); velide-dev Test Application (M2M); VelideDev (Regular Web). No dedicated Middleware application was listed |
| Third tenant named `velide` | Labelled **DEVELOPMENT**; its Applications page listed only a Default App. It is not the inspected legacy deployment |

The Production tenant's `dev-...` name is misleading: its verified domain and
installed Middleware client establish that it is the existing live identity
namespace. Do not select a tenant merely because its name sounds like production.
The development API's RBAC difference is not, by itself, a native integration
permission defect: native merchant/integration authorization is database-backed.

### Existing Production Post Login Actions

The live Post Login flow contained **Is from Device Flow**, followed by
**Block Developers**. Both were deployed; the editors reported **Action is up to
date**, with Save Draft / Deploy disabled throughout inspection.

- **Is from Device Flow**
  - Uses namespace `https://velide.com.br` and writes the `/roles` custom claim.
  - Its device-flow/unrecognised-client branch permits only
    `offline_access`, `openid`, `sub`, `email`, `profile`; other requested scopes
    cause access denial.
  - That branch supplies the `integration` role. The explicitly allowed legacy web
    client instead receives the user's existing Auth0 roles.
  - Its allowed-client list contained the existing Velide web client,
    `z2uN1ubAAucgnj1QJE2HnlW9HJ12oj6A`, not every future web client.
  - **Implication:** the Middleware's current OIDC scopes fit the allowlist.
    Do not request native database permissions such as `integration.import` as
    additional Auth0 scopes. A new web application's client ID needs explicit
    review of this Action's policy; otherwise it follows the integration branch.
- **Block Developers**
  - Denies login when `event.authorization.roles` includes `developer` and the
    transaction is not `oauth2-device-code`.
  - **Implication:** a developer account used to test the new production web app
    can be denied before Velide's own permissions are consulted. Device-flow
    Middleware login and web login must be tested separately.
- Both editors displayed **Node 18 (Not Recommended)** / outdated-runtime warnings.
  Plan a separately tested upgrade to an Auth0-supported runtime; do not make an
  untested global Action/runtime change during the store cutover.

No Action was edited, tested/executed, upgraded, or deployed. The code paths above
were inspected in the editor, not established through a newly issued token.

## Recommended migration path for this account

**Migrate the Velide application/backend, not the Auth0 tenant.** Keep the existing
Production tenant, users, connections and `login.velide.com.br` issuer. Leave
`velide-dev` for development. This is a recommendation, not an applied change.

1. **Choose production authentication before building rollout packages.**
   - Keep `login.velide.com.br` and audience `https://velide.com.br/`.
   - Configure the new **production** server with `AUTH0_DOMAIN=login.velide.com.br`,
     `AUTH0_AUDIENCE=https://velide.com.br/`, and RS256. Leave development's
     `velide-dev.us.auth0.com` configuration in development.
   - Verify the actual deployment configuration, not just a workstation `.env`.
     The custom domain and default tenant domain share a tenant but have different
     issuer strings; server and token-request domain must agree exactly.
   - Do not repoint the legacy custom domain/DNS to the Development tenant.
2. **Establish the production native Middleware client.**
   - Prefer a separately named **Native/public** app in the same Production tenant
     to isolate new-client policies, while retaining the old Middleware app.
     Reusing the existing app is possible, but must be an explicit reviewed choice.
   - For a new app, enable Device Code and Refresh Token grants, public-client
     token-endpoint authentication (no embedded secret), and the intended existing
     user connections. Use `openid profile email offline_access`.
   - The existing production API already permits offline access and all-app
     user-delegated access. Do not confuse its M2M per-app policy with the
     Middleware's user/device flow, or broaden any policy merely to fix a failed
     login. Recheck the selected client's actual access during the pilot.
   - Do not repurpose the development Courier Admin CLI or a test M2M app as the
     production Middleware client.
3. **Review web-client and Action behavior without breaking legacy.**
   - Configure any new production web client with its actual application flow,
     callback/logout/origin URLs and the production issuer.
   - Review `Is from Device Flow`'s explicit legacy-web-client allowlist and
     `Block Developers`' non-device restriction for that new client/account.
   - Preserve the legacy roles claim and login behavior during coexistence.
     Native authorization remains in Velide's database; do not remove Actions
     globally or disable RBAC in an attempt to grant native merchant access.
4. **Provision native database identities and ownership.**
   - Link each intended **production Auth0 subject** to the correct native
     `users.external_auth_subject`, merchant memberships/permissions and
     `integration_connections` owner.
   - Development subjects/memberships are not production mappings. Equal email
     addresses are not authoritative identity proof.
   - Keeping the same tenant and user connection normally preserves those
     users' `sub` values; no password/tenant migration is needed for this path.
     Verify the actual subject mapping before permitting imports.
5. **Pilot one controlled store before general release.**
   - Back up the installed config, both profile journals and their matching
     ownership ledger together.
   - Generate the installation-bound `native-upgrade.json` using real native
     merchant/connection/courier mappings. If using a new client, supply the full
     reviewed `--auth-config` (`domain`, `client_id`, `audience`, `scope`).
   - Exercise the final signed internal updater. The customer does not edit YAML;
     native performs its own device login and legacy retains its old session.
   - Verify login, refresh, restart, `myMerchants`, source-connection reads, courier
     mapping, a disposable import, reconciliation, cancellation and the appropriate
     ERP acknowledgement. Also test rejection of a wrong account/merchant.
   - Test web login separately, especially accounts with the Auth0 `developer`
     role and any newly created production client.
   - Select legacy again for **new intake**, restart, and confirm already
     native-owned work keeps recovering through native rather than being resent.
6. **Roll out gradually, retaining rollback capability.**
   - Keep the legacy API, Auth0 app/connections/Actions and required user access
     alive during the agreed fallback window. Monitor grant/refresh failures and
     import/job/reconciliation outcomes before expanding the cohort.
   - Selecting legacy is an intake switch in the new Middleware, not a binary
     downgrade or an automatic transfer of existing native-owned orders.
   - Retire legacy-specific resources only after retained orders, cancellations
     and ERP obligations finish and rollback closure is explicitly approved.
     Do not delete the shared Production tenant or shared user connections.

### If a separate production Auth0 tenant is intentionally required

Treat that as a **second migration project**, not a requirement of the new Velide
API. Design a production tenant/domain, supported user/password/social-account
migration, verified issuer/subject mappings, client grants, Actions and concurrent
legacy access. Do not move `login.velide.com.br` away from legacy during overlap,
copy refresh tokens across tenants, assume equal emails mean equal accounts, or
use `velide-dev` as the replacement merely because development currently uses it.

## What changed in code

- The legacy server verifies its configured issuer/audience, finds the Auth0 `sub`
  in its MongoDB user records, and uses that user's `storeId` and integration role.
- The native server verifies one configured issuer/audience with JWKS. It resolves
  `sub` through `users.external_auth_subject`, then native merchant memberships and
  database permissions. An authenticated but unmapped subject is not authorized.
- Native integration operations additionally require an enabled
  `integration_connections` record bound to that internal user and merchant.
- The Middleware still uses OAuth **Device Authorization Flow** plus refresh
  tokens. It is a public/native application; it must not ship a client secret.
- Profiles now have separate token paths and auth configurations. Initial native
  cutover requires an independent login, even if the settings match: copying a
  rotating refresh token into two live profiles could revoke its token family.
  Old legacy credentials remain available only to legacy. Later ordinary updates
  do not invalidate compatible sessions.
- Old queued workers cannot acquire another profile/session's credentials. Legacy
  retained work is also pinned to its original issuer/subject continuity hint.
  That local hint grants no API access: the server must verify every token.
- Back up the configuration, both profile journals and the matching ownership
  ledger together. The cutover copy has its own identity; substituting the older
  pre-cutover ledger is rejected rather than restoring obsolete backend ownership.

Do not change the server to accept every issuer merely to avoid a login prompt.
The new and legacy APIs can keep their own trust configurations and run together.

## Auth0 dashboard checklist (administrator, before release)

1. **Confirm tenants and domains**
   - Use the tenant picker and record the intended production tenant.
   - Under **Branding → Custom Domains** (the route observed in this dashboard), establish which tenant owns
     `login.velide.com.br` and whether the new API will use a custom or tenant domain.
   - Compare the exact discovery `issuer` and `jwks_uri` with the deployed native
     server's `AUTH0_DOMAIN`. A custom-domain alias is not automatic issuer equality.
2. **Confirm the Middleware application**
   - Under **Applications → Applications**, select/create the native Middleware
     application, using a **Native** public-client configuration.
   - Record its actual **Domain** and **Client ID**. These are public configuration;
     never put a Client Secret in the Middleware/package.
   - Under **Advanced Settings → Grant Types**, enable **Device Code** and
     **Refresh Token**, as required by this application's flow.
   - Under the app's **Connections** tab, enable the intended database/social/
     enterprise user connections.
3. **Confirm the resource API**
   - Under **Applications → APIs**, select the native Velide resource API.
   - Confirm its exact **Identifier** (audience) and **RS256** signing algorithm.
   - Under **Settings**, enable **Allow Offline Access** for refresh tokens.
   - Review any consent/client-access policy which may prohibit the native client
     from requesting this API.
4. **Confirm scopes and Actions**
   - The Middleware requests `openid profile email offline_access` (or the actual
     reviewed equivalent). `offline_access` is necessary for unattended renewal.
   - The legacy `https://velide.com.br/roles` integration claim may depend on
     legacy tenant Rules/Actions. Keep it working for the legacy profile.
   - Native `integration.*` permissions are **Velide database permissions**; adding
     identically named Auth0 scopes does not provision native access.
5. **Confirm refresh behavior**
   - Review app refresh-token rotation, reuse interval and absolute/inactivity
     expiration. Test the actual device-code grant and renewal, including rotated
     refresh tokens and renewal after application restart.
   - Retain the legacy tenant, legacy application, legacy API and user access
     during the rollback window. Deleting/revoking them defeats legacy fallback.
6. **Map users and merchant/source access**
   - In **User Management → Users**, identify the intended native user and actual
     User ID (`sub`). A tenant replacement may produce different IDs.
   - Administrators must link those subjects to the intended existing native
     internal users, merchant membership and source-connection ownership.
   - Required native capabilities: `integration.import`, `integration.read`,
     `integration.cancel`, `integration.retry`, `courier.read`.
   - Never infer an authoritative account/store mapping solely from equal email,
     display name, or a similarly shaped ID. Invalidate affected native auth caches
     when changing subject mappings.
7. **Produce the automatic rollout package**
   - Supply the complete actual native `domain`, `client_id`, `audience`, `scope`
     through the server exporter's `--auth-config` input when authentication changes.
   - Provision the installation-bound `native-upgrade.json` through the controlled
     rollout channel. The internal update helper installs both connection profiles;
     the customer does not edit configuration.
   - Do not replace example/default config on an existing installation to force
     cutover, invent merchant/connection IDs, or reuse a legacy refresh token with
     the new tenant/client.
8. **Rehearse with a test store**
   - After initial cutover, confirm the native profile prompts for its own login
     and the retained legacy session still works. Repeat an ordinary update to
     verify that both independently established sessions remain usable.
   - Verify `myMerchants`, the intended merchant/connection, a source-state read,
     courier reads, then a disposable import and its eventual job/delivery.
   - Switch **new orders** back to legacy and confirm native-owned work is still
     monitored rather than resent. Verify a wrong legacy account cannot claim the
     original journal and that restarting preserves the selection.
   - Inspect Auth0 **Monitoring → Logs** for successful device grants/refreshes or
     explicit grant/audience/client errors. Do not paste tokens into support chats,
     public JWT-decoding websites, PRs, logs or test fixtures.

## Remaining external inputs

The existing Production tenant/domain, legacy Middleware grants/connections and
API offline-access policy have now been verified in the dashboard. Still required:
the final production native client choice/configuration; deployed native server
issuer/audience; production subject-to-native-user/merchant/connection mappings;
Action policy for new web clients; and successful device-login/refresh plus store
pilot evidence. None of those were changed or established by this read-only audit.
They are rollout inputs, not customer-side manual configuration.

## Limits

The legacy schema has no authenticated store-ID discovery operation. Consequently
its local safety fence uses original issuer/subject continuity plus a successful
authenticated API read, not a claim that the local cache proves server-side store
ownership. Reassigning that same legacy subject to a different store on the server
must be treated as an administrative migration. If original retained work exists
but no usable historical identity can be recovered, it remains blocked for review;
a new login must not silently adopt it.
