# Native Farmax undo/redo synchronization

Stack: Middleware PR #4 → this change; server PR #250 → companion
[PR #271](https://github.com/BrenoLopesz/velide-server/pull/271).
Deploy the companion server migration/API
before this Middleware version. Legacy transport retains its existing behavior.

## Authority and representation

After adoption, Velide owns delivery logistics. Middleware reconciles **current
state**, not an inverse command for each historical action:

| Current state | Projection |
|---|---|
| Pending, including undo-start/abort | `S`; clear courier/departure/arrival |
| In transit, including paused route/undo-complete/undo-abort | `R`; actual courier/start; clear arrival |
| Completed, including redo | `V`; actual courier/start/completion |
| Pending on hold | Waiting fields; explicit partial representation (Farmax cannot enforce hold) |
| Cancelled or soft deleted | Suppress effects/intake; retain identity; explicit unsupported native representation |
| Restored | Resume projection/tracking for the original identity |
| Missing/unresolved state | Invalidate effects and reconcile; never infer cancellation |

All direct and same-status transitions work, including `S → V`, `V → S`, changed
courier/execution/times and drift. Farmax native return fields are captured as
evidence before being reconciled to Velide. Original timezone-aware instants are
retained locally; Firebird TIME stores installation-local clocks at 100µs precision.
Business dates and checkout timestamps are unchanged.

Synchronization outcome is independent of delivery lifecycle. The UI retains
partial/unsupported rows; its tooltip shows authoritative state, hold, projection
outcome and reason. Current address edits are exposed by the server contract but
address mirroring is not enabled. No address is inferred from original import data.

## Transactional ERP receipts and owned item restoration

The native writer installs these integration-owned objects on first use:

- `VELIDE_PROJECTION_APPLY`, indexed by source scope/order: immutable application
  IDs, generation/revision/execution, before/after managed delivery fields,
  actual item deltas and resulting item write versions.
- `VELIDE_ITEM_GUARD`: versions for the exact item keys touched by Middleware.
- `VELIDE_ITEM_VERSION` on Firebird: after insert/update/delete, increments the
  version of matching watched keys, including same-value native writes.

DDL is individually committed/restartable, as required by Firebird metadata
dependencies. Existing object names/schema/trigger bodies are checked, never
silently replaced. The integration account needs the same installation DDL
capability used for existing capture objects, plus access to these objects.
An incompatible or disabled provenance trigger blocks item-bearing projection
with an explicit synchronization error rather than inventing ownership.

Receipts commit in the **same ERP transaction** as delivery/item writes. A crash
before local acknowledgement therefore cannot lose the original before-image.
Retries read current fields and retained receipts. No-op audits skip ERP updates
and do not append receipts. Repaired drift receives a new immutable receipt.

Forward completion changes eligible `S` items to `V`. Reverse projection changes
only exact owned items back to `S` when their current value **and write version**
still match the receipt. A later native `V → V` write consumes that ownership.
Pre-existing native returns and uncertain historical items remain unchanged and
produce a partial outcome. Intervening `C/D/I` and merchandise-return items are
never revived. Native triggers still run; in particular `VENDAS` updates can
clear `CHEQUE`. This is logistics compensation, not byte-for-byte transaction undo.

Cashier transfer, payment, stock, fiscal state, sale conclusion, terminal and
financial records are outside these writes. Missing/duplicate/source-incarnation
conflicts fail targeted identity checks. An all-cancelled/returned source cannot
be made eligible by reversing commercial records.

## Notification and recovery

`integrationOrderChanges` is a source-authorized cursor query and WebSocket
subscription. Each event contains an external order ID and minimum history
revision. Feed sequence and history revision are separate domains.

Middleware persists cursor and dirty work atomically. New notifications invalidate
old effects. Reads older than the dirty watermark or accepted state are rejected.
Each read carries its dirty invalidation identity; a notification received during
that read remains pending even when the history revision is unchanged. Unfenced
periodic reads defer notified identities to the feed worker.
Each order has one replaceable target; acknowledgements are generation-fenced.
Writes are serialized and check authority before/after source changes.

WebSockets accelerate a bounded worker: one replay page (100), one state batch
(100), or one resync inventory page per turn. Bursts coalesce; errors retain work
and use the reconciliation interval for retry. Reconnect resumes the durable
cursor. Initial/expired-cursor recovery persists the server's boundary and its
inventory cursor, includes terminal/retained identities, then replays changes
after the boundary. Known local identities absent from inventory remain dirty
until explicitly resolved. Each dirty batch reserves a durable 30-second retry
turn before its HTTP read, so unresolved, lagging, empty or failed reads cannot
starve later identities. New notifications make their order immediately eligible
while preserving a separate durable FIFO position. Served orders move to the
tail, so continuous notifications cannot monopolize the front of the queue.

The existing PR #4 reconciliation scheduler remains the periodic recovery path.
Acknowledged projections are eligible for managed-field audit after 60 seconds,
even without changed Velide facts. Relevant state reads wake the ERP writer
immediately. Network, server and ERP latency can exceed timer intervals.

If a server correction is still unknown when an older ERP transaction commits,
the next reconciliation compensates toward the latest state. No distributed
transaction or instantaneous cross-system visibility is claimed.

## Cancellation evidence

Source cancellation uses a durable request UUID tied to a correlated evidence
lifetime. Repeated identical evidence does not create a new cancellation after
Velide supersedes the old request. An observed active source followed by a new
cancellation starts another lifetime. Ancillary metadata and item churn within
an already cancelled lifetime cannot create a new intent. Queued callbacks carry
the original request/evidence identity and can only resume that committed intent.
Inventory recovery preserves the server request ID; unknown historical evidence
stays consumed until a complete active source snapshot establishes a new baseline.
Request receipts and current remote state are separate. Delayed mutation responses
cannot replace restored local state.
Schema-v5 native cancellation intent migrates with the server's reserved
`legacy` request ID, so startup retries also respect earlier supersession.

The companion server fences cancellation inside its business transaction and
retains `ACKNOWLEDGED`/`SUPERSEDED` receipts. A successful superseded receipt means
the request is resolved, not that the delivery is currently cancelled.

## Verification

New regression suites:

- `tests/integration/test_farmax_undo_redo.py`: committed forward/backward/skipped
  transitions, same-value native interference, crash/retry provenance, rapid
  replacement, stale acknowledgements, drift, holds, cancellation restoration.
- `tests/integration/test_native_change_feed.py`: cursor/dirty durability,
  notification during reads, source resync restart and replay, invalid pages,
  durable fairness for missing/lagging/empty/failed reads.
- `tests/integration/test_cancellation_recovery.py`: recovered request dispositions,
  consumed-evidence suppression, metadata changes and new cancellation lifetimes.
- `tests/unit/test_native_change_service.py`: burst coalescing, shutdown/restart,
  session fencing and outage backoff.

Set `VELIDE_TEST_FIREBIRD=1` for the disposable localhost Firebird fixture. It
creates and drops its own database; no customer database is opened. This covers
real SQL, receipt/guard DDL and retained vendor trigger effects. Native Farmax
screen caching/date filters still govern visibility; exact vendor UI equivalence
and financial reversal are not guarantees of this projection.
