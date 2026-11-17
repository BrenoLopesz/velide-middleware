# Reversible intake profiles — implementation scope

Base: Middleware PR #3 `dd8db29`, Server PR #250 `a3fe74f`.
The existing PR worktrees contain unrelated uncommitted work and are untouched.
No release, signing, deployment, live ERP writes, or Auth0 changes are authorized.

## Contract

- Existing automatic installation adoption remains compatible with old config.
- The centrally generated, installation-bound native package provisions both
  profiles, their exact mappings and authentication. Users do not edit config.
- The selected backend owns **new intake**, not every historical order.
- A separate installation-wide SQLite ownership ledger assigns each ERP reference
  once, before submission. Both backend journals/tokens remain separate.
- Explicit authoritative migration packages may transfer existing ownership at
  stopped-installation cutover; a selector never performs that transfer.
- Both profiles keep recovering their own jobs, cancellations and ERP obligations.
  If native is unavailable, selecting legacy does not resend native-owned orders.
- Native cutover always obtains an independent login/refresh grant; cloning a
  rotating legacy refresh token into two live profiles is unsafe. Later ordinary
  updates preserve compatible sessions. Legacy work has a principal check; native
  merchant/connection authorization remains authoritative.
- The installer and packaged migration CLI must preserve profile databases,
  tokens and the ownership ledger on the old-updater first hop and later updates.
- Address importer changes and Farmax accounting changes are out of scope.

## Verification

Cover automatic adoption/cutover/repeat, legacy/native/legacy selection, durable
ownership and response-loss recovery, dual-profile service composition, isolated
sessions, wrong-account login, transient GraphQL errors, updater resources and
frozen executable entry points. Use disposable files and mocked/local APIs only.

## Authentication evidence

The local native server config uses `velide-dev.us.auth0.com`, while the local
legacy server and Middleware use `login.velide.com.br`; the API audience matches.
These are local development files, not proof of deployed tenant configuration.
Public discovery returned different issuer URLs and disjoint signing-key sets;
the inspected legacy token contract is not accepted by that native configuration.
The subsequent read-only dashboard audit confirmed Production tenant
`dev-b6bioprv4weafg2d`, its verified `login.velide.com.br` domain, the installed
Middleware client, required grants/connections and API offline access. The final
native client/deployed environment and native user/merchant/source provisioning
still need verification. See `docs/AUTH0_TRANSITION.md`; never share token or
client-secret values in reports.

## Integration status

- Middleware follow-up: `feature/middleware-backend-profiles` in
  `C:\Users\Breno\AppData\Local\Temp\opencode\velide-middleware-profiles`.
- Server follow-up: `feature/middleware-profile-transition` in
  `C:\Users\Breno\AppData\Local\Temp\opencode\velide-profile-transition`.
- Both are based on the published PR heads, without the dirty original worktrees'
  unrelated Farmax/address/source-context changes.
- The user authorized including this follow-up in the existing PRs. Publication
  uses non-forced `HEAD:<existing PR head>` pushes from the isolated worktrees;
  it does not move the checked-out local PR branches or touch their dirty files.
- The separate original Farmax/source-context edits are not included in these
  commits. Their worktrees remain at the old local tips and must be reconciled
  explicitly before any later combined release. Do not blindly pull or apply
  overlapping patches over that in-progress work.
- No release, live installer execution, ERP writes, or Auth0 management changes.

## Release gates (not performed by this local implementation)

1. Reconcile the dirty original PR worktrees and test the combined changes. The
   follow-up does not include their additional Farmax/source-context work.
2. Confirm the production Auth0 settings and native subject/merchant/connection
   access using `docs/AUTH0_TRANSITION.md`. Preserve legacy access for fallback.
3. Generate and centrally provision installation-bound packages with real rollout
   inputs. Customers need no configuration edits, but native needs its own login.
4. Qualify the final signed installer through the actual internal updater,
   including its old-updater first hop, installation-owned state and restart.
5. Qualify x86 separately and run actual Firebird tests / a controlled store pilot.
   A frozen x64 offline CDS smoke is not equivalent to these deployment checks.

`docs/NATIVE_ROLLOUT.md` separates implemented API/export/consumer behavior from
production deployment, centralized business-data conversion and trusted package
distribution. A store-bound package is not a generic release asset or a server
database migration.

## Final local verification (2026-11-15)

| Check | Result |
| --- | --- |
| Complete Python 3.8 Middleware suite | **507 passed, 14 skipped** |
| Server upgrade-package export + real Python → Bun → Python interoperability | **11 passed**, 41 assertions |
| Old-updater first-hop and installer resource/compile checks | **5 passed** (also included in full suite) |
| cx_Freeze x64 application + migration helper | Rebuilt successfully from final application sources |
| Final frozen GUI offline dual-profile intake + automatic CLI + selection CLI | **3 passed** |
| Ruff `F` checks on all changed/new Python files | Passed |
| `git diff --check`, both follow-up worktrees | Passed |

The source suite's skips comprise the optional frozen-GUI smoke (run separately
above) and environment-dependent Firebird checks. No actual Firebird qualification
was performed. Frozen GUI readiness is checked by durable intake, not mere process
survival: cold imports/endpoint protection exceeded the former fixed 25-second
deadline. A disposable console-base diagnostic confirmed both listeners start; its
temporary binary/module aliases were removed. The final GUI smoke used the actual
Win32GUI `main.exe`, not that diagnostic.

The freezer still reports optional/platform imports and old-CRT dependency warnings;
successful offline x64 smoke does not waive the release gates above. Build output
is local under `output/middleware/`; no signed installer or release was published.

Reproduction commands (from the corresponding follow-up worktree):

```powershell
$env:QT_QPA_PLATFORM = 'offscreen'
$env:PYTHONDONTWRITEBYTECODE = '1'
& 'C:\Users\Breno\Dev\velide-middleware\.venv38\Scripts\python.exe' -m pytest -q -p no:cacheprovider tests
& 'C:\Users\Breno\Dev\velide-middleware\.venv38\Scripts\python.exe' installer/src/cli/setup.py build_exe
```

For frozen tests, set `VELIDE_UPGRADE_EXE` to the resulting `migrate-native.exe`
and `VELIDE_PROFILE_APPLICATION_EXE` to `main.exe`; run
`tests/integration/test_frozen_profile_startup.py` plus the two CLI tests named
`test_automatic_cli_uses_selected_config_without_importing_workstation_defaults`
and `test_selection_cli_is_packaged_entrypoint_and_does_not_transfer_orders`.

For Server interoperability, set `VELIDE_UPGRADE_PYTHON` to that Python executable,
`VELIDE_MIDDLEWARE_WORKTREE` to this worktree and `VELIDE_UPGRADE_TEST_TEMP` to
`C:\Users\Breno\AppData\Local\Temp\opencode`; run
`bun test tests/deliveries/upgrade-package-export.test.ts` from the Server follow-up's
`server/` directory.
