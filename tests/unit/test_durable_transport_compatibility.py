import json

import httpx
import pytest

from api.sqlite_manager import SQLiteManager
from api.sync_journal import SyncJournal
from api.velide_gateway import VelideGateway
from config import ApiConfig, NativeApiConfig, TargetSystem
from models.velide_delivery_models import Order
from services.durable_delivery_dispatcher import DurableDeliveryDispatcher
from workers.legacy_sync_worker import LegacySyncWorker
from workers.native_api_worker import NativeApiWorker


class ImmediateJournal:
    """Real SQLite transactions, with explicit callback timing and no GUI race."""
    def __init__(self, path, scope):
        self.journal = SyncJournal(str(path), scope)
        self.calls, self.errors = [], []

    def submit(self, method, *args, success=None, failure=None):
        self.calls.append((method, args))
        try:
            result = getattr(self.journal, method)(*args)
        except Exception as error:
            self.errors.append(error)
            if failure:
                failure(str(error))
            else:
                raise
        else:
            if success:
                success(result)


class ControlledPool:
    def __init__(self):
        self.jobs = []

    def start(self, job):
        self.jobs.append(job)

    def run_next(self):
        job = self.jobs.pop(0)
        job.run()
        return job


@pytest.fixture
def stack(tmp_path, qtbot, monkeypatch):
    pool = ControlledPool()
    monkeypatch.setattr("services.durable_delivery_dispatcher.QThreadPool.globalInstance", lambda: pool)
    settings = ApiConfig(velide_server="https://dispatch.invalid/graphql",
        velide_websockets_server="wss://dispatch.invalid/graphql", add_delivery_max_retries=3,
        add_delivery_initial_delay=0.25, add_delivery_backoff_factor=3)
    gateway = VelideGateway(settings, TargetSystem.CDS)
    gateway.update_token("Bearer installed-token")
    journal = ImmediateJournal(tmp_path / "legacy.db", settings.velide_server + "|CDS")
    dispatchers = []
    def create():
        dispatcher = DurableDeliveryDispatcher(gateway, journal)
        dispatchers.append(dispatcher)
        return dispatcher
    yield gateway, journal, pool, create
    for dispatcher in dispatchers:
        dispatcher.stop()


def source():
    return Order(internal_id="order-123", customerName="João", address="Rua A, 1",
                 createdAt="2026-11-13T12:00:00-03:00", neighbourhood="Centro")


def transport(monkeypatch, handle):
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(handle), **kwargs))


def add_response(external="remote-123"):
    return httpx.Response(200, json={"data": {"addDeliveryFromIntegration": {
        "id": external, "createdAt": "2026-11-13T15:00:00Z"}}})


def test_legacy_import_is_automatic_and_mapping_commits_before_success(stack, monkeypatch):
    gateway, journal, pool, create = stack
    calls, successes = [], []
    transport(monkeypatch, lambda request: calls.append(request) or add_response())
    dispatcher = create()
    dispatcher.delivery_success.connect(lambda *result: successes.append((result, journal.journal.get_order(result[0]))))
    dispatcher.start()
    assert dispatcher._authorized and not pool.jobs  # No native/store-ID request.
    dispatcher.queue_add(source().internal_id, source())
    assert isinstance(pool.jobs[0], LegacySyncWorker)
    assert journal.journal.get_order(source().internal_id)["external_id"] is None
    pool.run_next()
    assert not journal.errors
    assert successes[0][0] == ("order-123", "remote-123")
    assert successes[0][1]["external_id"] == "remote-123"
    with SQLiteManager(journal.journal.path) as database:
        assert database.get_delivery_by_internal_id("order-123")[0] == "remote-123"
    assert len(calls) == 1
    body = json.loads(calls[0].content)
    assert "addDeliveryFromIntegration" in body["query"]
    assert body["variables"]["idempotencyKey"] == "order-123"
    assert body["variables"]["timestamp"] == 1794582000000


def test_uncertain_legacy_cancel_restarts_replays_same_add_then_deletes(stack, monkeypatch):
    gateway, journal, pool, create = stack
    calls, server = [], {}
    def handle(request):
        body = json.loads(request.content)
        calls.append(body)
        if "addDeliveryFromIntegration" in body["query"]:
            key = body["variables"]["idempotencyKey"]
            server.setdefault(key, "remote-123")
            if len(calls) == 1:
                raise httpx.ReadTimeout("Response lost after creation", request=request)
            return add_response(server[key])
        assert body["variables"] == {"deliveryId": "remote-123"}
        return httpx.Response(200, json={"data": {"deleteDelivery": True}})
    transport(monkeypatch, handle)
    dispatcher = create()
    dispatcher.start()
    dispatcher.queue_add("order-123", source())
    pool.run_next()
    row = journal.journal.get_order("order-123")
    assert row["external_id"] is None and row["submission"] == "RETRY"
    frozen = row["request"]
    dispatcher.stop()
    journal.journal.cancel("order-123")
    assert journal.journal.get_order("order-123")["cancellation"] == "REQUESTED"
    monkeypatch.setattr("api.sync_journal.time.time", lambda: row["retry_at"] + 1)
    restarted = create()
    deleted = []
    restarted.deletion_success.connect(lambda *result: deleted.append(result))
    restarted.start()
    assert pool.jobs[0].operation == "ADD"
    pool.run_next()
    assert pool.jobs[0].operation == "DELETE"
    assert not deleted
    pool.run_next()
    assert deleted == [("order-123", "remote-123")]
    assert len(server) == 1 and calls[0] == calls[1]
    assert journal.journal.get_order("order-123")["request"] == frozen
    assert journal.journal.get_order("order-123")["cancellation"] == "ACKNOWLEDGED"
    assert not journal.errors


def test_configured_retry_budget_and_delays_are_persisted_once_per_http_attempt(stack, monkeypatch):
    gateway, journal, pool, create = stack
    calls, now = [], [1000.0]
    monkeypatch.setattr("api.sync_journal.time.time", lambda: now[0])
    def unavailable(request):
        calls.append(request)
        raise httpx.ReadTimeout("Response lost", request=request)
    transport(monkeypatch, unavailable)
    dispatcher = create()
    dispatcher.start()
    dispatcher.queue_add("order-123", source())
    for attempt, delay in ((1, 0.25), (2, 0.75), (3, None)):
        assert len(pool.jobs) == 1
        pool.run_next()
        row = journal.journal.get_order("order-123")
        assert len(calls) == attempt
        assert not journal.errors
        failed = [args for method, args in journal.calls if method == "failed"][-1]
        assert failed[4:] == (3, 0.25, 3.0)
        if delay is not None:
            assert row["retry_at"] == pytest.approx(now[0] + delay)
            dispatcher._pump()
            assert not pool.jobs
            now[0] = row["retry_at"] + 0.001
            dispatcher._pump()
        else:
            assert row["submission"] == "NEEDS_REVIEW"
            now[0] += 1000
            dispatcher._pump()
            assert not pool.jobs


def test_delete_gets_its_own_retry_budget_after_successful_import(stack, monkeypatch):
    gateway, journal, pool, create = stack
    deletes, now = [], [1000.0]
    monkeypatch.setattr("api.sync_journal.time.time", lambda: now[0])
    def handle(request):
        if "addDeliveryFromIntegration" in json.loads(request.content)["query"]:
            return add_response()
        deletes.append(request)
        raise httpx.ReadTimeout("Delete response lost", request=request)
    transport(monkeypatch, handle)
    dispatcher = create()
    dispatcher.start()
    dispatcher.queue_add("order-123", source())
    pool.run_next()
    dispatcher.request_cancel("order-123")
    for count, delay in ((1, 0.25), (2, 0.75), (3, None)):
        assert len(pool.jobs) == 1
        pool.run_next()
        assert len(deletes) == count
        row = journal.journal.get_order("order-123")
        assert row["cancellation"] != "ACKNOWLEDGED"
        if delay is not None:
            assert row["retry_at"] == pytest.approx(now[0] + delay)
            now[0] = row["retry_at"] + 0.001
            dispatcher._pump()
        else:
            assert row["cancellation"] == "CONFLICT"
            assert not pool.jobs


def test_stopping_before_scheduled_worker_sends_cannot_create_an_unwanted_order(stack, monkeypatch):
    gateway, journal, pool, create = stack
    calls = []
    transport(monkeypatch, lambda request: calls.append(request) or add_response())
    dispatcher = create()
    dispatcher.start()
    dispatcher.queue_add("order-123", source())
    dispatcher.stop()
    gateway.invalidate_session()
    pool.run_next()
    assert not calls
    assert journal.journal.get_order("order-123")["external_id"] is None


def test_cancellation_recovers_an_exhausted_uncertain_legacy_submission(stack, monkeypatch):
    gateway, journal, pool, create = stack
    gateway.config.add_delivery_max_retries = 1
    calls = []
    def handle(request):
        body = json.loads(request.content)
        calls.append(body)
        if len(calls) == 1:
            raise httpx.ReadTimeout("Created remotely, response lost", request=request)
        if "addDeliveryFromIntegration" in body["query"]:
            return add_response()
        return httpx.Response(200, json={"data": {"deleteDelivery": True}})
    transport(monkeypatch, handle)
    dispatcher = create()
    dispatcher.start()
    dispatcher.queue_add("order-123", source())
    pool.run_next()
    assert journal.journal.get_order("order-123")["submission"] == "NEEDS_REVIEW"
    dispatcher.request_cancel("order-123")
    # A cancellation starts a separate recovery budget, keeping the original key.
    assert pool.jobs[0].operation == "ADD"
    pool.run_next()
    assert pool.jobs[0].operation == "DELETE"
    pool.run_next()
    assert calls[0] == calls[1]
    assert journal.journal.get_order("order-123")["cancellation"] == "ACKNOWLEDGED"


def test_active_route_deferral_keeps_intent_without_exhausting_transport_budget(stack):
    gateway, journal, pool, create = stack
    journal.journal.accept("order-123", dict(source().model_dump(mode="json", by_alias=True), internal_id="order-123"))
    journal.journal.imported("order-123", "remote-123")
    journal.journal.cancel("order-123")
    for _ in range(6):
        with SQLiteManager(journal.journal.path) as db:
            db._get_conn().execute("UPDATE SyncOrders SET retry_at=0")
        row = journal.journal.claim(native=True)
        assert row["operation"] == "DELETE"
        assert journal.journal.defer_cancellation("order-123", row["attempts"])
        state = journal.journal.get_order("order-123")
        assert state["cancellation"] == "REQUESTED" and state["delete_attempts"] == 0
    assert "rota ativa" in state["last_error"]


def test_permanent_recovery_failure_stops_until_explicit_retry(stack, monkeypatch):
    gateway, journal, pool, create = stack
    gateway.config.add_delivery_max_retries = 1
    calls = []
    def handle(request):
        calls.append(json.loads(request.content))
        if len(calls) == 1:
            raise httpx.ReadTimeout("Created remotely, response lost", request=request)
        return httpx.Response(400, json={"errors": [{"message": "Invalid request"}]})
    transport(monkeypatch, handle)
    dispatcher = create()
    dispatcher.start()
    dispatcher.queue_add("order-123", source())
    pool.run_next()
    dispatcher.request_cancel("order-123")
    pool.run_next()
    state = journal.journal.get_order("order-123")
    assert state["cancellation"] == "CONFLICT" and state["submission"] == "NEEDS_REVIEW"
    assert calls[0] == calls[1] and len(calls) == 2
    dispatcher._pump()
    assert not pool.jobs
    journal.journal.retry_work("order-123")
    dispatcher._pump()
    assert pool.jobs[0].operation == "ADD"


def test_durable_new_requests_use_idempotency_even_when_old_optional_flag_is_off(stack, monkeypatch):
    gateway, journal, pool, create = stack
    gateway.config.add_delivery_idempotency_enabled = False
    transport(monkeypatch, lambda _: add_response())
    dispatcher = create()
    dispatcher.start()
    dispatcher.queue_add("order-123", source())
    assert json.loads(journal.journal.get_order("order-123")["request"])["idempotencyKey"] == "order-123"
    pool.run_next()
    assert journal.journal.get_order("order-123")["external_id"] == "remote-123"


def test_configured_native_dispatch_still_validates_scope_and_records_job_not_mapping(tmp_path, qtbot, monkeypatch):
    pool = ControlledPool()
    monkeypatch.setattr("services.durable_delivery_dispatcher.QThreadPool.globalInstance", lambda: pool)
    settings = ApiConfig(velide_server="https://native.invalid/graphql", velide_websockets_server="wss://native.invalid/graphql",
        native=NativeApiConfig(merchant_id="00000000-0000-0000-0000-000000000001",
            connection_id="00000000-0000-0000-0000-000000000002", timezone="America/Sao_Paulo"))
    gateway = VelideGateway(settings, TargetSystem.CDS)
    gateway.update_token("Bearer scoped-token")
    journal = ImmediateJournal(tmp_path / "native.db", settings.velide_server + "|CDS|" + settings.native.merchant_id + "|" + settings.native.connection_id)
    calls = []
    def handle(request):
        body = json.loads(request.content)
        calls.append(body)
        assert request.headers["X-Merchant-ID"] == settings.native.merchant_id
        if "myMerchants" in body["query"]:
            return httpx.Response(200, json={"data": {"myMerchants": [{"id": settings.native.merchant_id,
                "permissions": ["integration.import", "integration.read", "integration.cancel", "integration.retry", "courier.read"],
                "settings": {"timezone": settings.native.timezone}}]}})
        if "integrationOrderStates" in body["query"]:
            return httpx.Response(200, json={"data": {"integrationOrderStates": []}})
        assert "importIntegrationOrder" in body["query"]
        return httpx.Response(200, json={"data": {"importIntegrationOrder": {"success": True, "jobId": "native-job"}}})
    transport(monkeypatch, handle)
    dispatcher = DurableDeliveryDispatcher(gateway, journal)
    try:
        dispatcher.start()
        assert isinstance(pool.jobs[0], NativeApiWorker)
        assert not dispatcher._authorized
        pool.run_next()
        assert dispatcher._authorized
        dispatcher.queue_add("order-123", source())
        pool.run_next()
        row = journal.journal.get_order("order-123")
        assert row["job_id"] == "native-job" and row["external_id"] is None
        assert len(calls) == 3
        assert not journal.errors
    finally:
        dispatcher.stop()
