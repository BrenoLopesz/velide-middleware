import json
from unittest.mock import MagicMock

import httpx
import pytest

from api.native_velide import NativeVelide, NativeApiError
from api.sync_journal import SyncJournal
from config import ApiConfig, NativeApiConfig, TargetSystem
from models.velide_delivery_models import Order


MERCHANT = "00000000-0000-0000-0000-000000000001"
SOURCE = "00000000-0000-0000-0000-000000000002"


def client():
    config = ApiConfig(velide_server="https://api.invalid/graphql", velide_websockets_server="wss://api.invalid/graphql",
        native=NativeApiConfig(merchant_id=MERCHANT, connection_id=SOURCE, timezone="America/Sao_Paulo"))
    return NativeVelide("Bearer token", config, TargetSystem.FARMAX)


def test_native_request_has_stable_identity_and_explicit_source_time():
    order = Order(internal_id="123", customerName="Fixture", address="Rua A", createdAt="2026-01-01T12:00:00")
    api = client()
    first = api._build_variables_to_add_delivery(order).model_dump()
    assert first["input"]["createdAtIntent"] == "2026-01-01T12:00:00-03:00"
    assert first["input"]["integration"]["externalOrderId"] == "123"
    assert api._build_variables_to_add_delivery(order).model_dump() == first


@pytest.mark.asyncio
async def test_native_acceptance_is_a_job_not_a_delivery():
    api = client()
    seen = []
    def handle(request):
        seen.append(request)
        return httpx.Response(200, json={"data": {"importIntegrationOrder": {"success": True, "jobId": "job"}}})
    async with api:
        await api._client.aclose()
        api._client = httpx.AsyncClient(transport=httpx.MockTransport(handle), headers={"Authorization": "Bearer token", "X-Merchant-ID": MERCHANT})
        assert await api.submit_import({"input": {}}) == {"success": True, "jobId": "job"}
    assert seen[0].headers["Authorization"] == "Bearer token"


@pytest.mark.asyncio
async def test_native_partial_lookup_and_graphql_error_are_not_success():
    api = client()
    async with api:
        await api._client.aclose()
        api._client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"data": {"integrationOrderStates": []}})))
        with pytest.raises(NativeApiError, match="Incomplete"):
            await api.order_states(["123"])


def test_offline_start_and_complete_produces_ordered_durable_effects(tmp_path):
    journal = SyncJournal(str(tmp_path / "native.db"), "native")
    journal.accept("123", {"internal_id": "123"})
    state = {"externalOrderId": "123", "found": True, "jobId": "job", "importStatus": "COMPLETED",
             "deliveryId": "remote", "deliveryStatus": "COMPLETED", "deleted": False, "cancellation": "NONE",
             "execution": {"id": "route", "courierId": "courier", "startedAt": "2026-01-01T15:00:00Z", "completedAt": "2026-01-01T16:00:00Z"}}
    journal.native_states([state])
    first = journal.next_effect()
    assert first["operation"] == "IN_ROUTE"
    journal.effect_result(first["sequence"])
    second = journal.next_effect()
    assert json.loads(second["payload"])["occurred_at"] == state["execution"]["completedAt"]
    journal.effect_result(second["sequence"])
    journal.native_states([state])
    assert journal.next_effect() is None
    assert journal.restore()[0]["submission"] == "TERMINAL"


def test_native_cancellation_can_precede_delivery_creation(tmp_path):
    journal = SyncJournal(str(tmp_path / "native.db"), "native")
    journal.accept("123", {"internal_id": "123"})
    journal.claim(native=True)
    journal.accepted_job("123", "job")
    journal.cancel("123")
    assert journal.claim(native=True)["operation"] == "DELETE"


def test_database_cannot_silently_switch_sources(tmp_path):
    path = str(tmp_path / "native.db")
    SyncJournal(path, "first").accept("123", {"internal_id": "123"})
    with pytest.raises(ValueError, match="migration manifest"):
        SyncJournal(path, "second").restore()


def test_retained_legacy_outcomes_do_not_invent_delivery_or_erp_effects(tmp_path):
    journal = SyncJournal(str(tmp_path / "retained.db"), "native")
    journal.native_states([{"externalOrderId": "123", "found": True, "retainedOutcome": "COMPLETED", "deleted": False, "cancellation": "NONE"},
                           {"externalOrderId": "124", "found": True, "retainedOutcome": "UNKNOWN", "deleted": False, "cancellation": "NONE", "error": "Legacy outcome unresolved"}])
    rows = {row["internal_id"]: row for row in journal.restore()}
    assert rows["123"]["submission"] == "TERMINAL"
    assert rows["124"]["submission"] == "NEEDS_REVIEW"
    assert journal.next_effect() is None
    assert journal.claim(native=True) is None


def test_native_bootstrap_recovers_source_payload_before_ui_notification(tmp_path, qtbot, monkeypatch):
    from services.sync_journal_service import SyncJournalService
    from services.native_reconciliation_service import NativeReconciliationService
    from workers.native_api_worker import NativeSignals
    journal = SyncJournalService(str(tmp_path / "bootstrap.db"), "native")
    gateway = MagicMock(config=client().config)
    workers = []
    def worker_factory(gateway, operation, *args):
        worker = MagicMock(signals=NativeSignals(), operation=operation)
        workers.append(worker)
        return worker
    monkeypatch.setattr("services.native_reconciliation_service.NativeApiWorker", worker_factory)
    monkeypatch.setattr("services.native_reconciliation_service.QThreadPool.globalInstance", lambda: MagicMock())
    service = NativeReconciliationService(gateway, journal, TargetSystem.FARMAX)
    received = []
    service.order_restored.connect(lambda order, external: received.append((order, external)))
    service.start_service()
    qtbot.waitUntil(lambda: len(workers) == 1)
    assert workers[0].operation == "list_orders"
    workers[0].signals.result.emit([{"externalOrderId": "123", "found": True, "jobId": "job", "importStatus": "PENDING",
        "deleted": False, "cancellation": "NONE", "source": {"customerName": "Fixture"},
        "request": {"rawAddress": "Rua A", "createdAtIntent": "2026-01-01T15:00:00Z"}}])
    qtbot.waitUntil(lambda: len(received) == 1)
    assert received[0][0].internal_id == "123"
    assert received[0][1] is None
    assert journal.journal.restore()[0]["submission"] == "ACCEPTED"
    service.stop_service()
    journal.pool.waitForDone()
