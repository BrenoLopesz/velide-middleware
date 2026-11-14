from unittest.mock import MagicMock

import pytest

from config import ApiConfig, NativeApiConfig, TargetSystem
from models.delivery_table_model import DeliveryTableModel, DeliveryRowStatus
from repositories.deliveries_repository import DeliveryRepository
from services.deliveries_service import DeliveriesService
from services.native_reconciliation_service import NativeReconciliationService
from services.sync_journal_service import SyncJournalService


@pytest.fixture
def stack(tmp_path,qtbot):
    config = ApiConfig(velide_server="https://api.invalid/graphql",velide_websockets_server="wss://api.invalid/graphql",
        native=NativeApiConfig(merchant_id="00000000-0000-0000-0000-000000000001",connection_id="00000000-0000-0000-0000-000000000002",timezone="America/Sao_Paulo"))
    journal = SyncJournalService(str(tmp_path/"review.db"),"test",source_timezone="America/Sao_Paulo")
    gateway = MagicMock(config=config,generation=1)
    gateway.is_ready.return_value = False
    service = DeliveriesService(gateway,config,TargetSystem.CDS,DeliveryRepository(),journal=journal)
    service._dispatcher._running = True
    table = DeliveryTableModel()
    service.delivery_acknowledged.connect(table.add_delivery_acknowledge)
    service.delivery_update.connect(lambda internal,status: table.update_delivery(internal,service.get_order(internal),status) if service.get_order(internal) else None)
    reconciler = NativeReconciliationService(gateway,journal,TargetSystem.CDS)
    reconciler.order_restored.connect(service._on_order_restored)
    reconciler.order_state.connect(service.on_native_state)
    journal.journal.accept("cds-1",dict(internal_id="cds-1",customerName="Fixture",address="Original",createdAt="2026-11-11T11:00:00"))
    journal.journal.claim(native=True)
    journal.journal.accepted_job("cds-1","job")
    yield journal,service,reconciler,table,gateway
    service.stop_listening()
    reconciler.stop_service()
    journal.pool.waitForDone()


def hold(journal):
    with pytest.raises(ValueError,match="conflicts"):
        journal.journal.accept("cds-1",dict(internal_id="cds-1",customerName="Fixture",address="Changed",createdAt="2026-11-11T11:00:00"))


@pytest.mark.parametrize("status",["PENDING","IN_TRANSIT","COMPLETED","CANCELLED",None])
def test_t2_both_publication_signals_keep_source_hold_visible(stack,status):
    journal,service,reconciler,table,_ = stack
    hold(journal)
    state = dict(externalOrderId="cds-1",found=True,revision="1",jobId="job",importStatus="PROCESSING" if status is None else "COMPLETED",
                 deliveryId=None if status is None else "remote",deliveryStatus=status,deleted=False,cancellation="NONE",error=None)
    for _ in range(2):  # normal publication and a restart-equivalent reconstruction
        rows = journal.journal.native_states([state],farmax=False)
        reconciler._publish([state],rows)
        assert table._data[0].status == DeliveryRowStatus.ERROR
        assert service.get_order("cds-1") is not None
        assert "conflicts" in journal.journal.get_order("cds-1")["last_error"]
    service._dispatcher._restored(journal.journal.restore(),service._dispatcher._ticket())
    assert table._data[0].status == DeliveryRowStatus.ERROR


def test_t1_hold_blocks_retry_before_any_worker_is_created(stack,qtbot,monkeypatch):
    journal,service,_,_,_ = stack
    hold(journal)
    pool = MagicMock()
    monkeypatch.setattr("services.deliveries_service.QThreadPool.globalInstance",lambda: pool)
    failed = []
    service.delivery_failed.connect(lambda *args: failed.append(args))
    service.retry_native_order("cds-1")
    qtbot.waitUntil(lambda: bool(failed))
    pool.start.assert_not_called()
    assert journal.journal.get_order("cds-1")["source_review"] is not None


@pytest.mark.parametrize("conflict",[True,False])
def test_t1_retry_preflight_rechecks_hold_before_transport(stack,qtbot,monkeypatch,conflict):
    journal,service,_,_,gateway = stack
    calls = []
    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self,*args): pass
        async def retry_order(self,internal):
            calls.append(internal)
            return {"success":True,"jobId":"job"}
    gateway.get_client.return_value = Client()
    workers = []
    monkeypatch.setattr("services.deliveries_service.QThreadPool.globalInstance",lambda: MagicMock(start=workers.append))
    service.retry_native_order("cds-1")
    qtbot.waitUntil(lambda: len(workers) == 1)
    if conflict:
        hold(journal)
    workers[0].run()
    assert calls == ([] if conflict else ["cds-1"])
    journal.pool.waitForDone()


def test_acceptance_receipt_does_not_hide_hold(stack,qtbot):
    journal,service,_,table,_ = stack
    hold(journal)
    row = journal.journal.get_order("cds-1")
    service._dispatcher._restored([row],service._dispatcher._ticket())
    service._on_import_accepted("cds-1","job")
    journal.pool.waitForDone()
    qtbot.wait(10)
    assert table._data[0].status == DeliveryRowStatus.ERROR
