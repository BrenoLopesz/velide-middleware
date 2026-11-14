"""Real Farmax/SQLite/Qt composition, with only HTTP and unused UI peers doubled.

The ERP is the disposable relevant-schema SQLite fixture, not a customer DB.
Firebird dialect/trigger qualification belongs to test_firebird_dialect.py.
Timers are driven explicitly so restart and pre-response assertions are causal,
while repository workers, journal commits and Qt signal delivery remain real.
"""
import asyncio
import json
import logging
import sqlite3
from threading import Event
from unittest.mock import MagicMock

import httpx
import pytest
import yaml
from PyQt5.QtCore import QThreadPool
from sqlalchemy import text

from api.sqlite_manager import DeliveryStatus, SQLiteManager
from api.velide_gateway import VelideGateway
from config import ApiConfig, FarmaxConfig, NativeApiConfig, TargetSystem
from connectors.farmax.farmax_mapper import FarmaxMapper
from connectors.farmax.farmax_repository import FarmaxRepository
from local_upgrade import upgrade_installation
from models.velide_delivery_models import Order
from services.durable_delivery_dispatcher import DurableDeliveryDispatcher
from services.native_reconciliation_service import NativeReconciliationService
from services.sqlite_service import SQLiteService
from services.strategies.farmax_strategy import FarmaxStrategy
from services.sync_journal_service import SyncJournalService
from services.tracking_persistence_service import TrackingPersistenceService
from utils.source_identity import configured_scope, configured_source_identity
from utils.fix_asyncio import apply_asyncio_fix


ZONE = "America/Sao_Paulo"
ENDPOINT = "https://farmax-e2e.invalid/graphql"
MERCHANT = "00000000-0000-0000-0000-000000000001"
CONNECTION = "00000000-0000-0000-0000-000000000002"
COURIER = "00000000-0000-0000-0000-000000000005"
SECOND_COURIER = "00000000-0000-0000-0000-000000000007"


def external_id(sale):
    return "00000000-0000-0000-0000-{:012d}".format(int(sale))


def native_state(sale, status="PENDING", revision="1", courier=COURIER,
                 started="2026-11-11T15:00:00Z", completed=None):
    return dict(externalOrderId=str(sale), found=True, revision=revision,
                jobId="job-" + str(sale), importStatus="COMPLETED",
                deliveryId=external_id(sale), deliveryStatus=status, deleted=False,
                cancellation="NONE", execution=None if status == "PENDING" else dict(
                    id="route-" + str(sale), courierId=courier, startedAt=started,
                    completedAt=completed))


class NativeHttp:
    """Keep the actual NativeVelide client, headers, GraphQL and workers in play."""
    def __init__(self):
        self.states = {}
        self.requests = []
        self.cancel_received = Event()
        self.release_cancel_response = Event()
        self.cancel_response = {"success": True}

    def handle(self, request):
        body = json.loads(request.content)
        self.requests.append((request, body))
        query, variables = body["query"], body["variables"]
        if "myMerchants" in query:
            data = {"myMerchants": [{"id": MERCHANT, "settings": {"timezone": ZONE},
                    "permissions": ["integration.import", "integration.read", "integration.cancel",
                                    "integration.retry", "courier.read"]}]}
        elif "integrationOrderStates" in query:
            data = {"integrationOrderStates": [self.states[key] for key in variables["refs"]]}
        elif "cancelIntegrationOrder" in query:
            self.cancel_received.set()
            if not self.release_cancel_response.wait(15):
                raise AssertionError("Test did not release the simulated server response")
            data = {"cancelIntegrationOrder": self.cancel_response}
        else:
            raise AssertionError("Unexpected HTTP operation: " + query)
        return httpx.Response(200, json={"data": data})

    @property
    def cancellations(self):
        return [(request, body) for request, body in self.requests if "cancelIntegrationOrder" in body["query"]]


class Session:
    def __init__(self, qtbot, engine, path, server, api_config=None, source_identity=None):
        self.qtbot, self.engine, self.path, self.server = qtbot, engine, str(path), server
        self.api_config = api_config or ApiConfig(
            velide_server=ENDPOINT, velide_websockets_server="wss://farmax-e2e.invalid/graphql",
            source_timezone=ZONE,
            native=NativeApiConfig(merchant_id=MERCHANT, connection_id=CONNECTION, timezone=ZONE))
        self.scope = self.api_config.velide_server + "|Farmax"
        if self.api_config.native:
            self.scope += "|" + MERCHANT + "|" + CONNECTION
        self.service = SyncJournalService(self.path, self.scope, source_identity, ZONE)
        self.journal = self.service.journal
        self.repo = FarmaxRepository(engine, journal=self.journal)
        self.sqlite = SQLiteService(self.path)
        self.persistence = TrackingPersistenceService(self.sqlite, journal=self.service)
        self.gateway = VelideGateway(self.api_config, TargetSystem.FARMAX)
        self.gateway.update_token("e2e-token")
        self.reconciler = (NativeReconciliationService(self.gateway, self.service, TargetSystem.FARMAX)
                           if self.api_config.native else MagicMock())
        # These peers are not exercised by repository/updater/cancellation work.
        self.strategy = FarmaxStrategy(
            FarmaxConfig(host="fixture.invalid", file="disposable-only.fdb", user="fixture", password="fixture"),
            self.repo, self.persistence, MagicMock(), self.reconciler, MagicMock())
        self.dispatcher = DurableDeliveryDispatcher(self.gateway, self.service)
        self.errors, self.progress, self.intents, self.acks, self.applied = [], [], [], [], []
        self.service.error.connect(self.errors.append)
        self.sqlite.error_occurred.connect(lambda error: self.errors.append(str(error)))
        self.dispatcher.task_failed.connect(lambda key, error: self.errors.append(error))
        self.strategy.synchronization_failed.connect(lambda key, error: self.errors.append(error))
        self.strategy._status_tracker.error_occurred.connect(self.errors.append)
        self.strategy._status_tracker.cancellation_progress.connect(lambda key, value: self.progress.append((key, value)))
        self.strategy._updater.update_success.connect(lambda *args: self.applied.append(args))
        # Same production-facing signal composition: strategy emits only AFTER
        # record_farmax_progress commits, then the dispatcher owns HTTP/ACK.
        self.strategy.order_cancelled.connect(self._record_intent)
        self.strategy.order_cancelled.connect(self.dispatcher.request_cancel)
        self.dispatcher.deletion_success.connect(self._acknowledged)
        self.dispatcher.scope_ready.connect(self.strategy.on_scope_verified)
        self.dispatcher.scope_invalidated.connect(self.strategy.on_scope_invalidated)
        self.closed = False

    def _record_intent(self, internal, external):
        # Capture the committed state at the exact signal boundary.
        self.intents.append((internal, external, self.journal.get_order(internal)["cancellation"]))

    def _acknowledged(self, internal, external):
        self.acks.append((internal, external, self.journal.get_order(internal)["cancellation"]))
        self.strategy.on_cancellation_acknowledged(internal)

    def wait(self, predicate):
        self.qtbot.waitUntil(lambda: bool(self.errors) or bool(predicate()), timeout=10000)
        assert not self.errors, self.errors

    def seed(self, sales):
        # Commit scope before inserting mappings; these are native imports, not
        # unbound legacy rows requiring an upgrade manifest.
        self.journal.restore()
        with SQLiteManager(self.path) as db:
            assert db.add_mapping(COURIER, "5")
            assert db.add_mapping(SECOND_COURIER, "7")
        for sale in sales:
            source = self.repo.fetch_deliveries_by_id((sale,), pending_only=False)[0]
            order = FarmaxMapper.to_order(source)
            data = order.model_dump(mode="json", by_alias=True)
            data["internal_id"] = str(sale)
            self.journal.accept(str(sale), data)
            self.journal.imported(str(sale), external_id(sale))
            self.server.states[str(sale)] = native_state(sale)
        self.journal.bootstrap_progress("", True)

    def start(self):
        # Hydrate the real persistence service before enabling strategy's live
        # callbacks. New-order ingestion and socket reconnection are out of scope.
        with self.qtbot.waitSignal(self.persistence.hydrated, timeout=10000):
            self.persistence.initialize()
        self.persistence._cleanup_service.stop_routine()
        self.strategy._listening = True
        self.dispatcher.start()
        self.wait(lambda: self.dispatcher._authorized and not self.strategy._updater._busy)
        self.dispatcher._timer.stop()
        self.strategy._updater._timer.stop()
        self.wait(lambda: not self.service._callbacks)

    def poll(self):
        tracker = self.strategy._status_tracker
        before = len(self.progress)
        if tracker._is_running:
            tracker._execute_poll_cycle()
        else:
            tracker.start()
        tracker._poll_timer.stop()
        self.wait(lambda: len(self.progress) > before and not tracker._is_processing)
        self.wait(lambda: not self.service._callbacks and self.service.pool.activeThreadCount() == 0)

    def route(self, states):
        self.server.states.update({state["externalOrderId"]: state for state in states})
        self.reconciler.start_service()
        self.wait(lambda: all(self.journal.get_order(state["externalOrderId"])["remote_revision"] == state["revision"] for state in states))
        self.wait(lambda: not self.reconciler._busy)
        self.reconciler.stop_service()
        self.strategy._updater._pump()  # deterministic firing of its ordinary timer
        self.wait(lambda: all(self.journal.get_order(state["externalOrderId"])["submission"] == "TERMINAL"
                             if state["deliveryStatus"] == "COMPLETED" else
                             (state["externalOrderId"], "IN_ROUTE") in self.applied for state in states))
        self.wait(lambda: not self.strategy._updater._busy and not self.service._callbacks)

    def local_rows(self, sql, params=()):
        with sqlite3.connect(self.path) as conn:
            return conn.execute(sql, params).fetchall()

    def evidence(self, sale=123):
        return json.loads(self.journal.get_order(str(sale))["farmax_progress"])

    def assert_active(self, sale=123):
        row = self.journal.get_order(str(sale))
        assert row["cancellation"] == "NONE" and row["source_review"] is None
        assert not self.persistence.is_archived(str(sale))
        assert self.intents == [] and self.acks == [] and self.server.cancellations == []

    def assert_cancel_waiting_for_server(self, sale=123):
        self.wait(self.server.cancel_received.is_set)
        self.wait(lambda: bool(self.intents))
        assert self.intents[-1] == (str(sale), external_id(sale), "REQUESTED")
        assert self.journal.get_order(str(sale))["cancellation"] == "REQUESTED"
        assert not self.persistence.is_archived(str(sale)) and self.acks == []
        request, body = self.server.cancellations[-1]
        assert body["variables"] == dict(merchantId=MERCHANT, connectionId=CONNECTION, externalOrderId=str(sale))
        assert request.headers["Authorization"] == "Bearer e2e-token"
        assert request.headers["X-Merchant-ID"] == MERCHANT
        assert request.url == ENDPOINT

    def release_and_assert_ack(self, sale=123):
        self.server.release_cancel_response.set()
        self.wait(lambda: bool(self.acks))
        assert self.acks == [(str(sale), external_id(sale), "ACKNOWLEDGED")]
        assert self.persistence.is_archived(str(sale))
        assert self.journal.get_order(str(sale))["submission"] == "TERMINAL"
        assert self.local_rows("SELECT status FROM DeliveryMapping WHERE internal_delivery_id=?", (str(sale),)) == [(DeliveryStatus.CANCELLED.value,)]

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.server.release_cancel_response.set()
        self.dispatcher.stop()
        self.strategy.stop_listening()
        self.persistence._cleanup_service.stop_routine()
        # Drain queued Qt completions before any service/signal receiver dies.
        self.qtbot.waitUntil(lambda: QThreadPool.globalInstance().activeThreadCount() == 0
                            and self.service.pool.activeThreadCount() == 0 and not self.service._callbacks,
                            timeout=15000)


@pytest.fixture
def worker_event_loop_policy():
    # Mirror main.perform_pre_boot_checks rather than masking NativeApiWorker:
    # Python 3.8.0's Windows Proactor loop cannot initialize in a QRunnable.
    previous = asyncio.get_event_loop_policy()
    apply_asyncio_fix()
    try:
        yield
    finally:
        asyncio.set_event_loop_policy(previous)


@pytest.fixture
def composition(farmax_stack, tmp_path, qtbot, monkeypatch, worker_event_loop_policy):
    _, engine = farmax_stack
    server = NativeHttp()
    actual_client = httpx.AsyncClient

    def http_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(server.handle)
        kwargs["trust_env"] = False
        return actual_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", http_client)
    with engine.begin() as conn:
        conn.execute(text("UPDATE ENTREGAS SET STATUS='S',CD_ENTREGADOR=NULL,HORA_SAIDA=NULL"))
        # The same product and ID_PRODUTO appears twice, with distinct ORDEM.
        conn.execute(text("""INSERT INTO VENDAS SELECT CD_VENDA,CD_FILIAL,CD_CLIENTE,ID_PRODUTO,3,
            CD_PRODUTO,ID_VENDA,DATA_CAIXA,HORA,TEMPENDERECO,TEMPREFERENCIA,STATUS,CONCLUIDO,ENTREGA,
            TIPO_VENDA,TROCA,PRECO_TOTAL,HORAFINAL,CHEQUE FROM VENDAS WHERE ORDEM=1"""))
        # SQLite equivalent of the native DELETE capture only; Firebird's real
        # sequence/trigger behavior is tested separately on disposable Firebird.
        conn.execute(text("""CREATE TRIGGER E2E_CAPTURE_DELETE AFTER DELETE ON ENTREGAS BEGIN
            INSERT INTO DELIVERYLOG(ID,CD_VENDA,ACTION,LOGDATE)
            SELECT COALESCE(MAX(ID),0)+1,OLD.CD_VENDA,'DELETE',CURRENT_TIMESTAMP FROM DELIVERYLOG; END"""))
    sessions = []

    def make(sales=(123,), restart=None, path=None, api_config=None, source_identity=None, seed=True):
        if restart:
            restart.close()
            server.release_cancel_response.clear()
            path = restart.path
            seed = False
        session = Session(qtbot, engine, path or tmp_path / "journal.db", server, api_config, source_identity)
        sessions.append(session)
        if seed:
            session.seed(sales)
        session.start()
        return session

    make.engine, make.server = engine, server
    try:
        yield make
    finally:
        for session in reversed(sessions):
            session.close()


def change_items(engine, statuses, sale=123):
    with engine.begin() as conn:
        for position, status in statuses.items():
            conn.execute(text("UPDATE VENDAS SET STATUS=:status WHERE CD_VENDA=:sale AND ORDEM=:position"),
                         dict(status=status, sale=sale, position=position))


def test_route_projection_per_delivery_partial_log_and_completion_preservation(composition, caplog):
    engine = composition.engine
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO ENTREGAS SELECT 124,CD_FILIAL,CD_CLIENTE,NOME,BAIRRO,DATA,STATUS,CD_ENTREGADOR,HORA_SAIDA,HORA_CHEGADA,ENVIADO,ID_OPERADOR_CAIXA_ORIGINAL FROM ENTREGAS WHERE CD_VENDA=123"))
        conn.execute(text("INSERT INTO VENDAS SELECT 124,CD_FILIAL,CD_CLIENTE,ID_PRODUTO,ORDEM,CD_PRODUTO,ID_VENDA,DATA_CAIXA,HORA,TEMPENDERECO,TEMPREFERENCIA,STATUS,CONCLUIDO,ENTREGA,TIPO_VENDA,TROCA,PRECO_TOTAL,HORAFINAL,CHEQUE FROM VENDAS WHERE CD_VENDA=123"))
        conn.execute(text("INSERT INTO VENDEDORES VALUES(7,1,'Second courier','E','A')"))
    app = composition(sales=(123, 124))
    app.poll()
    app.route([native_state(123, "IN_TRANSIT"), native_state(124, "IN_TRANSIT", courier=SECOND_COURIER, started="2026-11-11T16:30:00Z")])
    with engine.connect() as conn:
        assert conn.execute(text("SELECT CD_VENDA,STATUS,CD_ENTREGADOR,HORA_SAIDA FROM ENTREGAS ORDER BY CD_VENDA")).fetchall() == [
            (123, "R", 5, "12:00:00"), (124, "R", 7, "13:30:00")]
    change_items(engine, {1: "C"})
    with caplog.at_level(logging.INFO, logger="api.sync_journal"):
        app.poll()
        app.poll()
    assert sum("1/3 itens cancelados (33.3%)" in record.getMessage() for record in caplog.records) == 1
    assert app.evidence()["percentage"] == pytest.approx(100 / 3)
    app.assert_active()
    with engine.begin() as conn:
        conn.execute(text("UPDATE VENDAS SET CONCLUIDO='N' WHERE CD_VENDA=123 AND ORDEM=2"))
        conn.execute(text("UPDATE ENTREGAS SET HORA_CHEGADA='13:05:00' WHERE CD_VENDA=123"))
    app.route([native_state(123, "COMPLETED", revision="2", completed="2026-11-11T16:10:00Z"),
               native_state(124, "COMPLETED", revision="2", courier=SECOND_COURIER,
                            started="2026-11-11T16:30:00Z", completed="2026-11-11T17:00:00Z")])
    with engine.connect() as conn:
        assert conn.execute(text("SELECT CD_VENDA,STATUS,HORA_CHEGADA,ID_OPERADOR_CAIXA_ORIGINAL FROM ENTREGAS ORDER BY CD_VENDA")).fetchall() == [
            (123, "V", "13:05:00", 90), (124, "V", "14:00:00", 90)]
        assert conn.execute(text("SELECT STATUS,CONCLUIDO,HORAFINAL FROM VENDAS WHERE CD_VENDA=123 ORDER BY ORDEM")).fetchall() == [
            ("C", "S", "11:05:00"), ("V", "N", "11:05:00"), ("V", "S", "11:05:00")]
    assert app.local_rows("SELECT DISTINCT status FROM DeliveryMapping") == [(DeliveryStatus.DELIVERED.value,)]
    assert app.local_rows("SELECT DISTINCT state FROM SyncEffects") == [("ACKNOWLEDGED",)]
    assert not app.intents and not app.server.cancellations


def test_incremental_all_c_across_restart_cancels_retained_delivery_only_after_response(composition, caplog):
    app = composition()
    app.poll()
    change_items(app.engine, {1: "C"})
    with caplog.at_level(logging.INFO, logger="api.sync_journal"):
        app.poll()
    assert app.evidence()["cancelled_items"] == 1
    app.assert_active()
    app = composition(restart=app)
    assert app.evidence()["cancelled_items"] == 1
    change_items(app.engine, {2: "C"})
    with caplog.at_level(logging.INFO, logger="api.sync_journal"):
        app.poll()
    assert app.evidence()["percentage"] == pytest.approx(200 / 3)
    app.assert_active()
    change_items(app.engine, {3: "C"})
    with caplog.at_level(logging.INFO, logger="api.sync_journal"):
        app.poll()
    assert app.evidence()["percentage"] == 100
    with app.engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM ENTREGAS WHERE CD_VENDA=123")).scalar() == 1
    # Explicit user policy: all-C cancels even with ENTREGAS retained. This is
    # deliberately broader than the earlier removal-only implementation.
    app.assert_cancel_waiting_for_server()
    app.release_and_assert_ack()
    assert len(app.server.cancellations) == 1
    assert app.journal.get_order("123")["delete_attempts"] == 1
    assert any("2/3 itens cancelados (66.7%)" in record.getMessage() for record in caplog.records)
    assert any("3/3 itens cancelados (100.0%)" in record.getMessage() for record in caplog.records)


def test_disappeared_previously_cancelled_item_retains_denominator_across_restart(composition):
    app = composition()
    app.poll()
    change_items(app.engine, {1: "C"})
    app.poll()
    with app.engine.begin() as conn:
        conn.execute(text("DELETE FROM VENDAS WHERE CD_VENDA=123 AND ORDEM=1"))
    app = composition(restart=app)
    app.poll()
    evidence = app.evidence()
    assert evidence["total_items"] == 3 and evidence["cancelled_items"] == 1
    assert evidence["percentage"] == pytest.approx(100 / 3)
    assert any(item["key"][-1] == "1" and item["status"] == "C" for item in evidence["ledger"])
    app.assert_active()
    change_items(app.engine, {2: "C", 3: "C"})
    app.poll()
    assert app.evidence()["cancelled_items"] == 3
    app.assert_cancel_waiting_for_server()
    app.release_and_assert_ack()


def test_missing_active_item_does_not_turn_remaining_all_c_into_100_percent(composition):
    app = composition()
    app.poll()
    with app.engine.begin() as conn:
        conn.execute(text("DELETE FROM VENDAS WHERE CD_VENDA=123 AND ORDEM=1"))
    change_items(app.engine, {2: "C", 3: "C"})
    app.poll()
    # Repository's current two-row snapshot is all-C; durable correlation must
    # retain the missing active third row instead of forwarding that decision.
    assert app.progress[-1][1]["decision"] == "CANCEL"
    evidence = app.evidence()
    assert evidence["decision"] == "UNRESOLVED" and evidence["unknown_items"] == 1
    assert evidence["total_items"] == 3 and evidence["percentage"] == pytest.approx(200 / 3)
    app.assert_active()


@pytest.mark.parametrize("identity_matches", [True, False])
def test_physical_removal_mixed_i_v_requires_retained_identity(composition, identity_matches):
    app = composition()
    app.poll()
    change_items(app.engine, {1: "I", 2: "V", 3: "I"})
    with app.engine.begin() as conn:
        if not identity_matches:
            conn.execute(text("UPDATE VENDAS SET TEMPENDERECO='Another sale address' WHERE CD_VENDA=123"))
        conn.execute(text("DELETE FROM ENTREGAS WHERE CD_VENDA=123"))
    app.poll()
    assert app.progress[-1][1]["decision"] == "CANCEL"
    assert app.progress[-1][1]["items"][0]["removal_evidence"][0]["action"] == "DELETE"
    if identity_matches:
        assert app.evidence()["decision"] == "CANCEL"
        app.assert_cancel_waiting_for_server()
        app.release_and_assert_ack()
    else:
        app.assert_active()
        rejected = json.loads(app.local_rows("SELECT evidence FROM SyncFarmaxEvents ORDER BY rowid DESC LIMIT 1")[0][0])
        assert rejected["accepted"] is False


def test_original_installation_adoption_without_source_context_writes_normal_routes(composition, tmp_path):
    path = tmp_path / "original.db"
    # Original installation layout: no SyncOrders, SyncEffects, source context,
    # branch qualification or upgrade marker exists before the actual upgrader.
    with sqlite3.connect(str(path)) as conn:
        conn.executescript("""
            CREATE TABLE DeliverymenMapping(velide_id TEXT PRIMARY KEY,local_id TEXT UNIQUE NOT NULL);
            CREATE TABLE DeliveryMapping(external_delivery_id TEXT PRIMARY KEY,internal_delivery_id TEXT UNIQUE NOT NULL,
                status TEXT NOT NULL,deliveryman_id TEXT,create_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP);
        """)
        conn.execute("INSERT INTO DeliverymenMapping VALUES(?,?)", (COURIER, "5"))
        conn.execute("INSERT INTO DeliveryMapping(external_delivery_id,internal_delivery_id,status) VALUES(?,?,?)",
                     (external_id(123), "123", DeliveryStatus.ADDED.value))
    document = dict(target_system="Farmax", sqlite_path=str(path),
                    auth=dict(domain="auth.invalid", client_id="fixture", scope="openid", audience="fixture"),
                    api=dict(velide_server=ENDPOINT, velide_websockets_server="wss://farmax-e2e.invalid/graphql", source_timezone=ZONE),
                    farmax=dict(host="fixture.invalid", file="disposable-only.fdb", user="fixture", password="fixture", timezone=ZONE))
    config_path = tmp_path / "original.yml"
    config_path.write_text(yaml.safe_dump(document), encoding="utf-8")
    upgraded = upgrade_installation(config_path, bundle_dir=tmp_path)
    assert upgraded.adopted and not upgraded.native_applied
    app = composition(path=path, seed=False, api_config=upgraded.settings.api,
                      source_identity=configured_source_identity(upgraded.settings))
    assert app.scope == configured_scope(upgraded.settings)
    assert app.local_rows("SELECT legacy_adopted FROM UpgradeInstallation") == [(1,)]
    assert app.journal.get_order("123")["payload"] is None
    assert app.local_rows("SELECT farmax_branch FROM SyncScope") == [(None,)]
    order = Order(internal_id="123", customerName="Fixture", address="Rua A, 1", createdAt="2026-11-11T11:00:00-03:00")
    order.effect_context = dict(execution_id="original-route", occurred_at="2026-11-11T15:00:00Z",
                                started_at="2026-11-11T15:00:00Z", courier=COURIER)
    app.strategy.on_delivery_route_started_on_velide(order, COURIER)
    app.wait(lambda: ("123", "IN_ROUTE") in app.applied)
    assert app.local_rows("SELECT farmax_branch FROM SyncScope") == [("1",)]
    with app.engine.connect() as conn:
        assert conn.execute(text("SELECT STATUS,CD_ENTREGADOR,HORA_SAIDA FROM ENTREGAS")).fetchone() == ("R", 5, "12:00:00")
    order.effect_context = dict(order.effect_context, occurred_at="2026-11-11T16:00:00Z", completed_at="2026-11-11T16:00:00Z")
    app.strategy.on_delivery_route_ended_on_velide(order)
    app.wait(lambda: ("123", "DONE") in app.applied)
    assert app.journal.get_order("123")["source_review"] is None
    assert app.journal.get_order("123")["submission"] == "TERMINAL"
    with app.engine.connect() as conn:
        assert conn.execute(text("SELECT STATUS,HORA_CHEGADA,ID_OPERADOR_CAIXA_ORIGINAL FROM ENTREGAS")).fetchone() == ("V", "13:00:00", 90)
        assert conn.execute(text("SELECT DISTINCT CONCLUIDO,HORAFINAL FROM VENDAS")).fetchall() == [("S", "11:05:00")]
