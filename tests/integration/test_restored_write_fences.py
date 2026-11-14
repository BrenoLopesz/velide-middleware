import json
import pytest
from sqlalchemy import event, text

from api.sqlite_manager import SQLiteManager, DeliveryStatus
from api.sync_journal import SyncJournal
from connectors.farmax.cancellation import cancellation_progress
from connectors.farmax.errors import FarmaxEvidenceError, FarmaxWaitingError
from connectors.farmax.farmax_delivery_updater import _ObservationRequest


def seeded(tmp_path, repo, scope="review"):
    journal = SyncJournal(str(tmp_path / "fence.db"), scope, source_timezone="America/Sao_Paulo")
    source = repo.fetch_deliveries_by_id((123,), pending_only=False)[0].source_context
    journal.accept("123", dict(internal_id="123", customerName="Fixture", address="Rua A, 1",
                               createdAt="2026-11-11T11:00:00-03:00", source_context=source))
    with SQLiteManager(journal.path) as db:
        db.add_mapping("courier", "5")
    return journal


@pytest.mark.parametrize("phase", ["read", "write"])
def test_superseded_completion_cannot_commit_source_changes(tmp_path, farmax_stack, remote_state, phase):
    repo, engine = farmax_stack
    journal = seeded(tmp_path, repo)
    journal.native_states([remote_state("COMPLETED", revision="1")])
    departure = journal.next_effect()
    observation = _ObservationRequest(journal, repo, departure, "America/Sao_Paulo", lambda: True).observe()
    journal.effect_result(departure["sequence"], generation=departure["generation"], observation=observation)
    done = journal.next_effect()
    changed = []
    def invalidate(conn, cursor, statement, parameters, context, many):
        match = statement.startswith("SELECT") and "FROM ENTREGAS" in statement if phase == "read" else statement.startswith("UPDATE ENTREGAS")
        if not changed and match:
            changed.append(True)
            journal.native_states([remote_state("IN_TRANSIT", revision="2")])
    event.listen(engine, "before_cursor_execute", invalidate)
    with pytest.raises(FarmaxWaitingError):
        _ObservationRequest(journal, repo, done, "America/Sao_Paulo", lambda: True).observe()
    event.remove(engine, "before_cursor_execute", invalidate)
    assert changed and not journal.effect_is_current(done["sequence"], done["generation"])
    with engine.connect() as conn:
        assert tuple(conn.execute(text("SELECT STATUS,HORA_CHEGADA FROM ENTREGAS")).fetchone()) == ("R", None)
        assert conn.execute(text("SELECT STATUS FROM VENDAS ORDER BY ORDEM")).fetchall() == [("S",), ("S",)]
        assert conn.execute(text("SELECT COUNT(*) FROM DELIVERYLOG")).scalar() == 0


def test_original_mapping_retains_first_source_identity_even_without_order_payload(tmp_path, farmax_stack):
    repo, engine = farmax_stack
    journal = SyncJournal(str(tmp_path / "original.db"), "https://legacy.invalid/|Farmax")
    journal.restore()
    with SQLiteManager(journal.path) as db:
        db._get_conn().execute("CREATE TABLE UpgradeInstallation(id INTEGER PRIMARY KEY,scope TEXT,legacy_adopted INTEGER)")
        db._get_conn().execute("INSERT INTO UpgradeInstallation VALUES(1,?,1)", (journal.scope,))
        db.add_delivery_mapping("remote", "123", DeliveryStatus.ADDED)
        db.add_mapping("courier", "5")
    journal.restore()
    assert journal.record_farmax_progress("123", cancellation_progress(repo.fetch_sales_statuses_by_id((123,))))["accepted"]
    retained = journal.get_order("123")["source_context"]
    assert retained and journal.get_order("123")["payload"] is None
    with engine.begin() as conn:
        conn.execute(text("UPDATE ENTREGAS SET CD_CLIENTE=20"))
        conn.execute(text("UPDATE VENDAS SET CD_CLIENTE=20,TEMPENDERECO='Changed sale'"))
    journal = SyncJournal(journal.path, journal.scope)
    restored = repo.fetch_deliveries_by_id((123,), pending_only=False)[0].source_context
    assert not journal.record_farmax_progress("123", cancellation_progress(repo.fetch_sales_statuses_by_id((123,))))["accepted"]
    journal.enqueue_effect("123", "route:DONE", "DONE", dict(execution_id="route", occurred_at="2026-11-11T16:00:00Z",
        started_at="2026-11-11T15:00:00Z", completed_at="2026-11-11T16:00:00Z", courier="courier", source_context=restored))
    effect = journal.next_effect()
    assert json.loads(journal.prepare_effect(effect["sequence"], effect["generation"])["payload"])["source_context"] == json.loads(retained)
    with pytest.raises(FarmaxEvidenceError):
        _ObservationRequest(journal, repo, effect, "America/Sao_Paulo", lambda: True).observe()
    with engine.connect() as conn:
        assert conn.execute(text("SELECT STATUS FROM ENTREGAS")).scalar() == "R"
