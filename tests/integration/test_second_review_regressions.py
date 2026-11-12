import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy import text
from PyQt5.QtCore import QObject, pyqtSignal, QThreadPool

from api.sqlite_manager import SQLiteManager
from api.sync_journal import SyncJournal
from api.sync_schema import migrate_sync_schema
from api.velide_gateway import VelideGateway
from config import ApiConfig, AuthenticationConfig, NativeApiConfig, TargetSystem
from models.velide_delivery_models import Order
from models.delivery_table_model import DeliveryRowStatus, DeliveryTableModel
from repositories.deliveries_repository import DeliveryRepository
from services.deliveries_service import DeliveriesService
from services.native_reconciliation_service import NativeReconciliationService
from services.sync_journal_service import SyncJournalService
from services.auth_service import AuthService
from connectors.farmax.farmax_delivery_updater import FarmaxDeliveryUpdater


def api_config():
    return ApiConfig(velide_server="https://api.invalid/graphql",velide_websockets_server="wss://api.invalid/graphql",
        native=NativeApiConfig(merchant_id="00000000-0000-0000-0000-000000000001",connection_id="00000000-0000-0000-0000-000000000002",timezone="America/Sao_Paulo"))


def payload(created="2026-11-11T11:00:00"):
    return dict(internal_id="123",customerName="Fixture",address="Rua A, 1",createdAt=created)


@pytest.mark.parametrize("submission,external,error,expected",[
    ("QUEUED",None,None,DeliveryRowStatus.SENDING),
    ("ACCEPTED",None,None,DeliveryRowStatus.PROCESSING),
    ("IMPORTED","remote",None,DeliveryRowStatus.ADDED),
    ("NEEDS_REVIEW",None,"source changed",DeliveryRowStatus.ERROR),
    ("TERMINAL","remote","source changed",DeliveryRowStatus.ERROR),
])
def test_r1_real_restoration_signals_and_table_are_enum_typed(tmp_path,qtbot,submission,external,error,expected):
    journal = SyncJournalService(str(tmp_path/"restore.db"),"test",source_timezone="America/Sao_Paulo")
    gateway = MagicMock(config=api_config(),generation=1)
    gateway.is_ready.return_value = False
    service = DeliveriesService(gateway,gateway.config,TargetSystem.FARMAX,DeliveryRepository(),journal=journal)
    table = DeliveryTableModel()
    service.delivery_acknowledged.connect(table.add_delivery_acknowledge)
    service.delivery_update.connect(lambda internal,status: table.update_delivery(internal,service.get_order(internal),status))
    row = dict(payload=json.dumps(payload()),submission=submission,external_id=external,cancellation="NONE",last_error=error)
    dispatcher = service._dispatcher
    dispatcher._running = True
    dispatcher._restored([row],dispatcher._ticket())
    assert table._data[0].status == expected
    reconciler = NativeReconciliationService(gateway,journal,TargetSystem.FARMAX)
    reconciler.order_restored.connect(service._on_order_restored)
    state = dict(externalOrderId="123",found=True,deliveryStatus="PENDING",cancellation="NONE",deleted=False)
    reconciler._publish([state],[dict(row,internal_id="123",remote_state=json.dumps(state))])
    assert table._data[0].status == expected
    # Normal Orders have an explicitly null hint. The real slot must be safe too.
    service._on_order_restored(Order.model_validate(payload()),external)
    assert isinstance(table._data[0].status,DeliveryRowStatus)
    service.stop_listening()
    journal.pool.waitForDone()


def test_r2_observer_conflict_latches_but_waiting_for_return_does_not(tmp_path,farmax_stack,remote_state,qtbot):
    repo,engine = farmax_stack
    context = repo.fetch_deliveries_by_id((123,),pending_only=False)[0].source_context
    journal = SyncJournalService(str(tmp_path/"conflict.db"),"test",source_timezone="America/Sao_Paulo")
    journal.journal.accept("123",dict(payload(),source_context=context))
    with SQLiteManager(journal.journal.path) as db:
        db.add_mapping("courier","5")
    journal.journal.native_states([remote_state("IN_TRANSIT")])
    with engine.begin() as conn:
        conn.execute(text("UPDATE VENDAS SET TEMPENDERECO='changed'"))
    updater = FarmaxDeliveryUpdater(repo,MagicMock(),journal)
    errors = []
    updater.update_failed.connect(lambda *args: errors.append(args))
    updater.start()
    qtbot.waitUntil(lambda: bool(errors))
    assert journal.journal.restore()[0]["source_review"] is not None
    with engine.begin() as conn:
        conn.execute(text("UPDATE VENDAS SET TEMPENDERECO='Rua A, 1'"))
    journal.journal.native_states([remote_state("IN_TRANSIT",revision="2")])
    assert journal.journal.next_effect() is None
    updater.stop()
    QThreadPool.globalInstance().waitForDone()
    journal.pool.waitForDone()
    # A separate, unconflicted order may wait and later match native completion.
    clean = SyncJournal(str(tmp_path/"waiting.db"),"test",source_timezone="America/Sao_Paulo")
    clean.accept("123",dict(payload(),source_context=context))
    with SQLiteManager(clean.path) as db:
        db.add_mapping("courier","5")
    clean.native_states([remote_state()])
    departure = clean.next_effect()
    clean.effect_result(departure["sequence"],generation=departure["generation"],observation="DEPARTURE_FIELDS_MATCH")
    pending = clean.next_effect()
    with pytest.raises(ValueError) as caught:
        repo.observe_delivery_effect(123,json.loads(pending["payload"]),"America/Sao_Paulo")
    assert caught.value.kind == "WAITING_FOR_SOURCE"
    clean.effect_result(pending["sequence"],str(caught.value),pending["generation"],error_kind=caught.value.kind)
    assert clean.restore()[0]["source_review"] is None


def test_r2_conflicting_intake_commits_hold_before_error(tmp_path):
    journal = SyncJournal(str(tmp_path/"intake.db"),"test",source_timezone="America/Sao_Paulo")
    journal.accept("123",payload())
    claimed = journal.claim(native=True)
    with pytest.raises(ValueError,match="conflicts"):
        journal.accept("123",dict(payload(),address="different"))
    assert journal.restore()[0]["source_review"] is not None
    assert not journal.dispatch_is_current("123",claimed["attempts"],"ADD")
    assert journal.claim(native=True) is None
    journal.accept("123",payload())
    assert journal.restore()[0]["source_review"] is not None


@pytest.mark.parametrize("original",["2026-11-11T11:00:00","2026-11-11T11:00:00-03:00","2026-11-11T14:00:00Z"])
def test_r3_bootstrap_then_unchanged_receipt_is_equivalent(tmp_path,original):
    journal = SyncJournal(str(tmp_path/"times.db"),"test",source_timezone="America/Sao_Paulo")
    journal.native_states([dict(externalOrderId="123",found=True,revision="1",jobId="job",importStatus="PENDING",deleted=False,cancellation="NONE",
        source={"customerName":"Fixture"},request={"rawAddress":"Rua A, 1","createdAtIntent":"2026-11-11T14:00:00.000Z"})],farmax=False)
    row = journal.accept("123",payload(original))
    assert json.loads(row["payload"])["createdAt"] == "2026-11-11T14:00:00+00:00"
    assert row["source_review"] is None
    with pytest.raises(ValueError,match="conflicts"):
        journal.accept("123",payload("2026-11-11T14:01:00Z"))


def test_r3_existing_naive_payload_uses_bound_zone_not_host_zone(tmp_path):
    journal = SyncJournal(str(tmp_path/"old-time.db"),"test",source_timezone="America/Sao_Paulo")
    journal.accept("123",payload())
    with SQLiteManager(journal.path) as db:
        db._get_conn().execute("UPDATE SyncOrders SET payload=?",(json.dumps(payload()),))
    assert journal.accept("123",payload("2026-11-11T14:00:00Z"))["source_review"] is None


@pytest.mark.parametrize("original",["2026-11-11T11:00:00","2026-11-11T11:00:00-03:00","2026-11-11T14:00:00Z"])
def test_r3_cds_rescan_acknowledges_unchanged_original_file_after_bootstrap(tmp_path,qtbot,original):
    from services.strategies.cds_strategy import CdsStrategy
    from models.cds_order_model import CdsOrder
    service = SyncJournalService(str(tmp_path/"cds.db"),"test",source_timezone="America/Sao_Paulo")
    strategy = CdsStrategy(api_config(),"unused",service)
    strategy._running = True
    strategy._file_listener_worker = MagicMock()
    source = dict(nome_cliente="Fixture",endereco="Rua A, 1",horario_pedido=original,contato_cliente="555",valor_pedido="10,00",_source_file="ent1.json",_source_observation=("ent1.json",1,1,1))
    internal = strategy._transform(CdsOrder.model_validate(source),"ent1.json").internal_id
    service.journal.native_states([dict(externalOrderId=internal,found=True,revision="1",jobId="job",importStatus="PENDING",deleted=False,cancellation="NONE",
        source={"customerName":"Fixture","customerContact":"555"},request={"rawAddress":"Rua A, 1","createdAtIntent":"2026-11-11T14:00:00.000Z"})],farmax=False)
    strategy._on_new_file_found(source)
    qtbot.waitUntil(lambda: strategy._file_listener_worker.acknowledge.called)
    assert service.journal.restore()[0]["internal_id"] == internal
    assert service.journal.restore()[0]["source_review"] is None
    strategy._running = False
    service.pool.waitForDone()


@pytest.mark.parametrize("new_access",["new-access","old-access"])
def test_r4_delayed_stored_bundle_cannot_undo_refresh(qtbot,monkeypatch,new_access):
    class Signals(QObject):
        token = pyqtSignal(str,str)
        expired = pyqtSignal(str)
        error = pyqtSignal(str)
    workers = []
    def factory():
        worker = SimpleNamespace(signals=Signals())
        workers.append(worker)
        return worker
    monkeypatch.setattr("services.auth_service.StoredTokenRetrieverWorker",factory)
    gateway = VelideGateway(api_config(),TargetSystem.FARMAX)
    auth = AuthService(AuthenticationConfig(domain="auth.invalid",client_id="test",scope="openid",audience="test"),gateway)
    auth._thread_pool = MagicMock()
    auth._on_access_token_received("old-access","old-refresh")
    auth.load_stored_token()
    monkeypatch.setattr("services.auth_service.store_token_at_file",MagicMock())
    response = MagicMock(status_code=200,text=json.dumps({"access_token":new_access,"refresh_token":"rotated-refresh"}))
    monkeypatch.setattr("workers.refresh_token_worker.requests.post",MagicMock(return_value=response))
    auth._manual_refresh_token("old-refresh")
    refresh = auth._thread_pool.start.call_args.args[0]
    refresh.run()
    workers[0].signals.token.emit("old-access","old-refresh")
    workers[0].signals.expired.emit("old-refresh")
    assert gateway.snapshot().token == new_access
    assert auth._current_refresh_token == "rotated-refresh"
    assert auth._thread_pool.start.call_count == 2  # one read + one refresh, no retired-token refresh
    auth.invalidate_session()


def test_r5_mutation_during_bootstrap_does_not_lose_periodic_recovery(qtbot,monkeypatch):
    journal = MagicMock()
    deferred = []
    calls = []
    def submit(method,*args,**kwargs):
        calls.append(method)
        if method == "bootstrap_status":
            deferred.append(kwargs["success"])
        elif method == "active_orders":
            kwargs["success"]([])
    journal.submit.side_effect = submit
    gateway = MagicMock(config=api_config(),generation=1)
    service = NativeReconciliationService(gateway,journal,TargetSystem.FARMAX)
    service.timer.setInterval(10)
    service.start_service()
    service.invalidate_pending_reads()
    deferred[0]({"after":"","complete":True})
    qtbot.waitUntil(lambda: calls.count("active_orders") >= 2)
    assert service.timer.isActive()
    service.stop_service()


@pytest.mark.parametrize("version",[0,1,2])
def test_r6_concurrent_first_opens_migrate_once(tmp_path,version):
    path = str(tmp_path/"concurrent.db")
    conn = sqlite3.connect(path)
    conn.executescript("""
      CREATE TABLE DeliverymenMapping(velide_id TEXT PRIMARY KEY,local_id TEXT UNIQUE);
      CREATE TABLE DeliveryMapping(external_delivery_id TEXT PRIMARY KEY,internal_delivery_id TEXT UNIQUE,status TEXT,deliveryman_id TEXT,create_at TEXT,updated_at TEXT);
    """)
    if version >= 1:
        conn.executescript("""
          CREATE TABLE SyncOrders(scope TEXT,internal_id TEXT,payload TEXT,request TEXT,submission TEXT,cancellation TEXT,external_id TEXT,job_id TEXT,attempts INTEGER,retry_at REAL,last_error TEXT,remote_state TEXT,PRIMARY KEY(scope,internal_id));
          CREATE TABLE SyncEffects(scope TEXT,internal_id TEXT,effect_key TEXT,operation TEXT,payload TEXT,state TEXT,attempts INTEGER,retry_at REAL,last_error TEXT,sequence INTEGER PRIMARY KEY,UNIQUE(scope,internal_id,effect_key));
          CREATE TABLE SyncSource(scope TEXT,source_id TEXT,payload TEXT,resolved INTEGER,last_error TEXT,PRIMARY KEY(scope,source_id));
          CREATE TABLE SyncCheckpoints(scope TEXT PRIMARY KEY,cursor INTEGER);
        """)
    if version == 2:
        conn.execute("CREATE TABLE SyncScope(id INTEGER PRIMARY KEY,scope TEXT)")
    conn.execute("PRAGMA user_version="+str(version))
    conn.execute("INSERT INTO DeliveryMapping(external_delivery_id,internal_delivery_id,status) VALUES('legacy','123','ADICIONADO')")
    conn.commit()
    conn.close()
    start = Barrier(2)
    def opened():
        current = sqlite3.connect(path,timeout=10)
        start.wait()
        migrate_sync_schema(current,path)
        result = current.execute("PRAGMA user_version").fetchone()[0]
        current.close()
        return result
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(lambda _: opened(),range(2))) == [4,4]
    with SQLiteManager(path) as db:
        assert [row[1] for row in db._get_conn().execute("PRAGMA table_info(SyncSource)")].count("attempts") == 1
        assert db.get_delivery_by_internal_id("123")[0] == "legacy"
