"""Cancellation lifetime, recovery identity, and queued callback regressions."""
import sqlite3

import pytest
from sqlalchemy import text

from api.sync_journal import SyncJournal
from api.sync_schema import migrate_sync_schema
from connectors.farmax.cancellation import cancellation_progress
from test_restored_write_fences import seeded


def progress(repo):
    return cancellation_progress(repo.fetch_sales_statuses_by_id((123,)))


def test_v5_migration_preserves_native_cancellation_receipt_identity():
    # Minimal v5 relations touched by the additive v6 migration.
    with sqlite3.connect(":memory:") as conn:
        conn.executescript("""
            CREATE TABLE SyncOrders(scope TEXT,internal_id TEXT,cancellation TEXT,effect_authority TEXT);
            CREATE TABLE SyncEffects(scope TEXT,state TEXT);
            INSERT INTO SyncOrders VALUES('https://native.invalid|Farmax|merchant|source','123','REQUESTED',NULL);
            INSERT INTO SyncOrders VALUES('https://legacy.invalid|Farmax','456','REQUESTED',NULL);
            PRAGMA user_version=5;
        """)
        migrate_sync_schema(conn, ":memory:")
        assert conn.execute("SELECT internal_id,cancellation_request_id,cancellation_evidence FROM SyncOrders ORDER BY internal_id").fetchall() == [
            ("123", "legacy", "RECOVERED_UNKNOWN"), ("456", None, None)]


def test_pre_upgrade_request_retries_server_legacy_receipt(tmp_path, farmax_stack, remote_state):
    repo, _ = farmax_stack
    journal = seeded(tmp_path, repo)
    journal.native_states([remote_state("PENDING", execution=None)])
    journal.cancel("123")
    with sqlite3.connect(journal.path) as conn:
        conn.execute("UPDATE SyncOrders SET cancellation_request_id=NULL,cancellation_evidence=NULL")
    claimed = journal.claim(native=True)
    assert claimed["cancellation_request_id"] == "legacy"
    journal.native_states([remote_state("PENDING", revision="2", execution=None,
        cancellation="SUPERSEDED", cancellationRequestId="legacy")])
    assert not journal.dispatch_is_current("123", claimed["attempts"], "DELETE")


@pytest.mark.parametrize("disposition", ["REQUESTED", "CONFLICT", "ACKNOWLEDGED", "SUPERSEDED"])
def test_inventory_restores_original_request_and_suppresses_unknown_history(
        tmp_path, farmax_stack, remote_state, disposition):
    repo, engine = farmax_stack
    journal = seeded(tmp_path, repo)
    with engine.begin() as conn:
        conn.execute(text("UPDATE VENDAS SET STATUS='C'"))
    journal.native_states([remote_state("PENDING", execution=None,
        cancellation=disposition, cancellationRequestId="server-request")])
    journal = SyncJournal(journal.path, journal.scope)
    assert journal.get_order("123")["cancellation_request_id"] == "server-request"
    if disposition == "REQUESTED":
        claimed = journal.claim(native=True)
        assert claimed["cancellation_request_id"] == "server-request"
    journal.native_states([remote_state("PENDING", revision="2", execution=None,
        cancellation="SUPERSEDED", cancellationRequestId="server-request")])
    assert not journal.record_farmax_progress("123", progress(repo))["request_cancel"]
    assert journal.claim(native=True) is None
    # Even a delayed same-revision snapshot cannot reopen a consumed receipt.
    journal.native_states([remote_state("PENDING", revision="2", execution=None,
        cancellation="REQUESTED", cancellationRequestId="server-request")])
    assert journal.get_order("123")["cancellation"] == "NONE"
    assert journal.claim(native=True) is None


def test_superseded_lifetime_ignores_metadata_but_allows_observed_new_cancellation(
        tmp_path, farmax_stack, remote_state):
    repo, engine = farmax_stack
    journal = seeded(tmp_path, repo)
    journal.native_states([remote_state("PENDING", execution=None)])
    with engine.begin() as conn:
        conn.execute(text("UPDATE VENDAS SET STATUS='C'"))
    intent = journal.record_farmax_progress("123", progress(repo))
    original = intent["cancellation_request_id"]
    journal.native_states([remote_state("PENDING", revision="2", execution=None,
        cancellation="SUPERSEDED", cancellationRequestId=original)])
    with engine.begin() as conn:
        conn.execute(text("UPDATE VENDAS SET TEMPREFERENCIA='Revised reference'"))
    assert not journal.record_farmax_progress("123", progress(repo))["request_cancel"]
    assert journal.get_order("123")["cancellation_evidence"] == intent["cancellation_evidence"]
    with engine.begin() as conn:
        conn.execute(text("UPDATE VENDAS SET STATUS='S'"))
    assert not journal.record_farmax_progress("123", progress(repo))["request_cancel"]
    # Re-reading the old receipt must not erase the newly observed active baseline.
    journal.native_states([remote_state("PENDING", revision="2", execution=None,
        cancellation="SUPERSEDED", cancellationRequestId=original)])
    with engine.begin() as conn:
        conn.execute(text("UPDATE VENDAS SET STATUS='C'"))
    new_intent = journal.record_farmax_progress("123", progress(repo))
    assert new_intent["request_cancel"]
    assert new_intent["cancellation_request_id"] != original
    assert journal.cancel("123", intent["external_id"], original, intent["cancellation_evidence"]) is None
    assert journal.claim(native=True)["cancellation_request_id"] == new_intent["cancellation_request_id"]
    with sqlite3.connect(journal.path) as conn:
        assert conn.execute("SELECT request_id FROM SyncCancellationReceipts WHERE disposition='SUPERSEDED'").fetchall() == [(original,)]
