from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock
import json
import threading

import pytest

from services.strategies.cds_strategy import CdsStrategy
from workers.cds_logs_listener_worker import CdsLogsListenerWorker, _NewFileHandler, prepare_cds_observation_baseline
from models.cds_order_model import CdsOrder
from api.sync_journal import SyncJournal


def test_cds_identity_distinguishes_orders_and_preserves_reprints():
    source = CdsOrder(nome_cliente="Fixture", endereco="Rua A", horario_pedido=datetime(2026,1,1,12),
                      contato_cliente="555", valor_pedido="10,00")
    strategy = CdsStrategy(MagicMock(), "unused")
    assert strategy._transform(source, "ent1.json").internal_id == strategy._transform(source, "ENT1.JSON").internal_id
    assert strategy._transform(source, "ent1.json").internal_id != strategy._transform(source, "ent2.json").internal_id


def test_watcher_handles_move_and_modify():
    enqueue = MagicMock()
    handler = _NewFileHandler(enqueue)
    handler.on_moved(SimpleNamespace(is_directory=False, dest_path="ent1.json"))
    handler.on_modified(SimpleNamespace(is_directory=False, src_path="ent2.json"))
    assert enqueue.call_count == 2


def test_cds_partial_file_is_retried_and_preserves_provenance(tmp_path, qtbot, monkeypatch):
    path = tmp_path / "ent1.json"
    path.write_text('{"nome_cliente":', encoding="windows-1252")
    worker = CdsLogsListenerWorker(str(tmp_path))
    calls = []
    def wait(_):
        calls.append(1)
        if len(calls) == 2:
            path.write_text('{"nome_cliente":"Fixture"}', encoding="windows-1252")
        return False
    monkeypatch.setattr(worker._stop, "wait", wait)
    received = []
    worker.signals.new_order.connect(received.append)
    worker._read_file(str(path))
    assert received[0]["nome_cliente"] == "Fixture"
    assert received[0]["_source_file"] == "ent1.json"
    assert received[0]["_source_observation"][0] == str(path)
    worker.stop()  # stop-before-start is also valid


def test_thousands_of_unchanged_files_do_not_delay_new_events(monkeypatch):
    worker = CdsLogsListenerWorker("unused")
    monkeypatch.setattr("workers.cds_logs_listener_worker.os.stat", lambda _: SimpleNamespace(st_size=10, st_mtime_ns=1))
    for n in range(5000):
        path = "ent{}.json".format(n)
        worker.acknowledge((path,10,1))
        worker.enqueue(path, priority=False)
    assert worker._next_path() is None
    for n in range(5000,10000):
        worker.enqueue("ent{}.json".format(n), priority=False)
    assert len(worker._scan) <= 1024
    assert not worker._scan_has_capacity()
    worker.enqueue("ent-new.json")
    assert worker._next_path() == "ent-new.json"
    worker.enqueue("ent0.json")  # A real modified event overrides an unchanged stat.
    assert worker._next_path() == "ent0.json"


def test_old_file_ack_does_not_hide_a_new_observation(monkeypatch):
    worker = CdsLogsListenerWorker("unused")
    monkeypatch.setattr("workers.cds_logs_listener_worker.os.stat", lambda _: SimpleNamespace(st_size=10,st_mtime_ns=1))
    worker.enqueue("ent1.json")
    worker._next_path()
    old_ticket = worker._inflight["ent1.json"][2]
    worker.enqueue("ent1.json")
    worker._next_path()
    worker.acknowledge(("ent1.json",10,1,old_ticket))
    assert "ent1.json" not in worker._seen


def write_order(path, **changes):
    data = dict(nome_cliente="João", endereco="Rua A - 1 - Centro - Cidade - UF - CEP",
                horario_pedido="2026-11-13T12:00:00-03:00", contato_cliente="555", valor_pedido="10,00")
    data.update(changes)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="windows-1252")
    return data


def test_first_upgrade_boundary_survives_restart_and_does_not_claim_old_files_delivered(tmp_path, qtbot):
    old = tmp_path / "ent-old.json"
    write_order(old)
    baseline = tmp_path / "installation.cds-observation.json"
    first = CdsLogsListenerWorker(str(tmp_path), str(baseline))
    first._prepare_baseline()
    original = baseline.read_bytes()
    first.enqueue(str(old), priority=False)
    assert first._next_path() is None

    # This may be an order arriving while the app is closed, with the exact same
    # customer, address and receipt. Its distinct filename must stay distinct.
    new = tmp_path / "ent-new.json"
    write_order(new)
    restarted = CdsLogsListenerWorker(str(tmp_path), str(baseline))
    restarted._prepare_baseline()
    restarted.enqueue(str(old), priority=False)
    restarted.enqueue(str(new), priority=False)
    assert restarted._next_path() == str(new)
    received = []
    restarted.signals.new_order.connect(received.append)
    restarted._read_file(str(new))
    assert received[0]["_source_file"] == "ent-new.json"
    assert baseline.read_bytes() == original
    assert old.exists() and new.exists()
    assert "delivered" not in original.decode("utf-8")


def test_installer_boundary_keeps_reviewed_pending_files_and_later_arrivals(tmp_path):
    write_order(tmp_path / "ent-history.json")
    write_order(tmp_path / "ent-pending.json")
    path = tmp_path / "baseline.json"
    baseline = prepare_cds_observation_baseline(str(tmp_path), str(path), pending_files=["ENT-PENDING.JSON"])
    write_order(tmp_path / "ent-during-upgrade.json")
    worker = CdsLogsListenerWorker(str(tmp_path), str(path))
    worker._prepare_baseline()
    for name in ("ent-history.json", "ent-pending.json", "ent-during-upgrade.json"):
        worker.enqueue(str(tmp_path / name), priority=False)
    assert set(worker._scan) == {str(tmp_path / "ent-pending.json"), str(tmp_path / "ent-during-upgrade.json")}
    assert list(baseline["unobserved_files"]) == ["ent-history.json"]


def test_pre_boundary_reprint_stays_excluded_but_new_receipt_is_a_new_order(tmp_path, qtbot, monkeypatch):
    path = tmp_path / "ent1.json"
    write_order(path)
    worker = CdsLogsListenerWorker(str(tmp_path))
    worker._prepare_baseline()
    received = []
    worker.signals.new_order.connect(received.append)
    monkeypatch.setattr(worker._stop, "wait", lambda _: False)
    write_order(path, nome_cliente="Reimpressão com dados corrigidos")
    worker._read_file(str(path))
    assert received == []
    write_order(path, horario_pedido="2026-11-13T12:01:00-03:00")
    worker._read_file(str(path))
    assert len(received) == 1
    assert path.exists()


@pytest.mark.parametrize("contents", ['{', '{"version":1,"folder":"another folder","unobserved_files":{}}'])
def test_invalid_baseline_stops_instead_of_reimporting_or_taking_a_new_snapshot(tmp_path, qtbot, contents):
    write_order(tmp_path / "ent1.json")
    baseline = tmp_path / "baseline.json"
    baseline.write_text(contents, encoding="utf-8")
    worker = CdsLogsListenerWorker(str(tmp_path), str(baseline))
    received, failures, finished = [], [], []
    worker.signals.new_order.connect(received.append)
    worker.signals.error.connect(failures.append)
    worker.signals.finished.connect(lambda: finished.append(True))
    worker.run()
    assert not received
    assert "monitoramento CDS foi interrompido" in failures[0]
    assert finished == [True]
    assert baseline.read_text(encoding="utf-8") == contents


def test_missing_persistence_permission_stops_before_scanning(tmp_path, qtbot, monkeypatch):
    write_order(tmp_path / "ent1.json")
    worker = CdsLogsListenerWorker(str(tmp_path))
    def denied(*args):
        raise PermissionError("Access is denied")
    monkeypatch.setattr("workers.cds_logs_listener_worker.os.replace", denied)
    failures, received = [], []
    worker.signals.error.connect(failures.append)
    worker.signals.new_order.connect(received.append)
    worker.run()
    assert failures and not received
    assert not (tmp_path / ".velide-cds-observation.json").exists()
    assert (tmp_path / "ent1.json").exists()


def test_real_watcher_reads_new_file_after_first_start_and_emits_portuguese_lifecycle(tmp_path, qtbot, caplog):
    write_order(tmp_path / "ent-before.json")
    worker = CdsLogsListenerWorker(str(tmp_path))
    received = []
    worker.signals.new_order.connect(received.append)
    thread = threading.Thread(target=worker.run)
    with caplog.at_level("INFO"):
        thread.start()
        try:
            qtbot.waitUntil(lambda: worker._observer is not None and worker._observer.is_alive(), timeout=5000)
            write_order(tmp_path / "ent-after.json")
            qtbot.waitUntil(lambda: bool(received), timeout=5000)
            assert {row["_source_file"] for row in received} == {"ent-after.json"}
        finally:
            worker.stop()
            thread.join(5)
    assert not thread.is_alive()
    assert "Monitoramento CDS iniciado" in caplog.text
    assert "Monitoramento CDS encerrado" in caplog.text


class ImmediateJournal:
    """Keep the real journal/transactions while making callback timing explicit."""
    def __init__(self, path):
        self.journal = SyncJournal(str(path), "cds-test|CDS")

    def submit(self, method, *args, success, failure):
        try:
            result = getattr(self.journal, method)(*args)
        except Exception as error:
            failure(str(error))
        else:
            success(result)


def test_real_journal_keeps_distinct_files_and_conflict_hold_without_reimport(tmp_path, qtbot, monkeypatch):
    journal = ImmediateJournal(tmp_path / "installation.db")
    strategy = CdsStrategy(MagicMock(), str(tmp_path), journal)
    assert strategy._baseline_path == str(tmp_path / "installation.db.cds-observation.json")
    worker = CdsLogsListenerWorker(str(tmp_path), strategy._baseline_path)
    worker._prepare_baseline()
    monkeypatch.setattr(worker._stop, "wait", lambda _: False)
    strategy._file_listener_worker = worker
    strategy._running = True
    worker.signals.new_order.connect(lambda data: strategy._on_new_file_found(data, worker))
    received, failures = [], []
    strategy.order_normalized.connect(received.append)
    strategy.synchronization_failed.connect(lambda internal, message: failures.append(message))
    for name in ("ent1.json", "ent2.json"):
        write_order(tmp_path / name)
        worker._read_file(str(tmp_path / name))
    assert len({order.internal_id for order in received}) == 2
    assert len(journal.journal.restore()) == 2
    first_id = received[0].internal_id

    # An edit is not another order; the durable review hold must commit before ack.
    write_order(tmp_path / "ent1.json", nome_cliente="Cliente corrigido")
    worker._read_file(str(tmp_path / "ent1.json"))
    assert len(received) == 2
    assert "evitar duplicidade" in failures[-1]
    assert journal.journal.get_order(first_id)["source_review"]
    assert str(tmp_path / "ent1.json") in worker._seen
    assert len(journal.journal.restore()) == 2

    # Reverting the source must not clear the held state or emit to dispatch.
    write_order(tmp_path / "ent1.json")
    worker._read_file(str(tmp_path / "ent1.json"))
    assert len(received) == 2
    assert journal.journal.get_order(first_id)["source_review"]


def test_no_journal_acknowledges_unchanged_file_and_reprints(tmp_path, qtbot, monkeypatch):
    path = tmp_path / "ent1.json"
    write_order(path)
    worker = CdsLogsListenerWorker(str(tmp_path))
    monkeypatch.setattr(worker._stop, "wait", lambda _: False)
    strategy = CdsStrategy(MagicMock(), str(tmp_path))
    strategy._file_listener_worker, strategy._running = worker, True
    worker.signals.new_order.connect(lambda data: strategy._on_new_file_found(data, worker))
    received = []
    strategy.order_normalized.connect(received.append)
    worker._read_file(str(path))
    worker.enqueue(str(path), priority=False)
    assert worker._next_path() is None
    worker._read_file(str(path))
    assert len(received) == 1


def test_stale_callbacks_do_not_ack_or_report_in_a_new_session(tmp_path, qtbot):
    strategy = CdsStrategy(MagicMock(), str(tmp_path), MagicMock())
    current, old = MagicMock(), MagicMock()
    strategy._file_listener_worker, strategy._running = current, True
    received, errors = [], []
    strategy.order_normalized.connect(received.append)
    strategy.synchronization_failed.connect(lambda *args: errors.append(args))
    source = CdsOrder.model_validate(write_order(tmp_path / "ent1.json"))
    order = strategy._transform(source, "ent1.json")
    strategy._accepted(order, {"submission": "QUEUED", "cancellation": "NONE"}, ("ent1.json", 1, 2), old)
    strategy._accept_failed(order, "database is locked", ("ent1.json", 1, 2), old)
    strategy._running = False
    strategy._accept_failed(order, "database is locked", ("ent1.json", 1, 2), current)
    assert not received and not errors
    current.acknowledge.assert_not_called()


def test_unreadable_pre_boundary_file_does_not_block_new_files_and_can_be_explicitly_released(tmp_path, qtbot, monkeypatch):
    old = tmp_path / "ent-old.json"
    old.write_text('{"nome_cliente":', encoding="windows-1252")
    baseline = tmp_path / "baseline.json"
    worker = CdsLogsListenerWorker(str(tmp_path), str(baseline))
    worker._prepare_baseline()
    original = baseline.read_bytes()
    write_order(old)
    new = tmp_path / "ent-new.json"
    write_order(new)
    worker.enqueue(str(old))
    worker.enqueue(str(new))
    assert worker._next_path() == str(new)
    assert worker._next_path() is None

    # The migration owner has subsequently reviewed this specific file. This
    # exception does not silently turn every existing file into a pending order.
    reviewed = CdsLogsListenerWorker(str(tmp_path), str(baseline), pending_files=[old.name])
    reviewed._prepare_baseline()
    monkeypatch.setattr(reviewed._stop, "wait", lambda _: False)
    received = []
    reviewed.signals.new_order.connect(received.append)
    reviewed.enqueue(str(old))
    assert reviewed._next_path() == str(old)
    reviewed._read_file(str(old))
    assert received[0]["_source_file"] == old.name
    assert baseline.read_bytes() == original


def test_failed_accept_retries_unchanged_source_and_restart_preserves_terminal_reprint(tmp_path, qtbot, monkeypatch):
    journal = ImmediateJournal(tmp_path / "installation.db")
    strategy = CdsStrategy(MagicMock(), str(tmp_path), journal)
    worker = CdsLogsListenerWorker(str(tmp_path), strategy._baseline_path)
    worker._prepare_baseline()
    monkeypatch.setattr(worker._stop, "wait", lambda _: False)
    strategy._file_listener_worker, strategy._running = worker, True
    worker.signals.new_order.connect(lambda data: strategy._on_new_file_found(data, worker))
    path = tmp_path / "ent1.json"
    write_order(path)
    original_accept = journal.journal.accept
    attempts = []
    def flaky_accept(*args):
        attempts.append(args)
        if len(attempts) == 1:
            raise OSError("database is locked")
        return original_accept(*args)
    monkeypatch.setattr(journal.journal, "accept", flaky_accept)
    worker._read_file(str(path))
    assert str(path) not in worker._seen
    expires = worker._inflight[str(path)][1]
    monkeypatch.setattr("workers.cds_logs_listener_worker.time.monotonic", lambda: expires + 1)
    worker.enqueue(str(path), priority=False)
    assert worker._next_path() == str(path)
    worker._read_file(str(path))
    assert str(path) in worker._seen
    assert attempts[0] == attempts[1]
    row = journal.journal.restore()[0]
    journal.journal.cancel(row["internal_id"])

    restarted_strategy = CdsStrategy(MagicMock(), str(tmp_path), journal)
    restarted = CdsLogsListenerWorker(str(tmp_path), strategy._baseline_path)
    restarted._prepare_baseline()
    monkeypatch.setattr(restarted._stop, "wait", lambda _: False)
    restarted_strategy._file_listener_worker, restarted_strategy._running = restarted, True
    restarted.signals.new_order.connect(lambda data: restarted_strategy._on_new_file_found(data, restarted))
    received = []
    restarted_strategy.order_normalized.connect(received.append)
    restarted._read_file(str(path))
    assert received == []
    assert journal.journal.restore()[0]["cancellation"] == "ACKNOWLEDGED"
    assert str(path) in restarted._seen


def test_invalid_cds_data_is_still_retried_without_repeating_unchanged_dashboard_errors(tmp_path, qtbot, monkeypatch, caplog):
    path = tmp_path / "ent1.json"
    write_order(path, endereco="")
    strategy = CdsStrategy(MagicMock(), str(tmp_path))
    worker = CdsLogsListenerWorker(str(tmp_path))
    strategy._file_listener_worker, strategy._running = worker, True
    monkeypatch.setattr(worker._stop, "wait", lambda _: False)
    worker.signals.new_order.connect(lambda data: strategy._on_new_file_found(data, worker))
    received = []
    strategy.order_normalized.connect(received.append)
    worker._read_file(str(path))
    worker._read_file(str(path))
    assert str(path) not in worker._seen
    assert caplog.text.count("contém dados incompletos ou inválidos") == 1
    write_order(path)
    worker._read_file(str(path))
    assert len(received) == 1
    assert str(path) in worker._seen


def test_unknown_original_cds_receipt_reaches_real_dashboard_pipeline_without_dispatch(tmp_path, qtbot, monkeypatch):
    from config import ApiConfig, TargetSystem
    from api.velide_gateway import VelideGateway
    from models.delivery_table_model import DeliveryTableModel
    from repositories.deliveries_repository import DeliveryRepository
    from services.deliveries_service import DeliveriesService
    from utils.log_handler import QLogHandler

    path = tmp_path / "ent-original.json"
    write_order(path)
    accepted = []
    class InventoryJournal:
        def submit(self, method, *args, success=None, failure=None):
            assert method == "accept"
            internal, payload, source_file = args
            accepted.append(source_file)
            success(dict(internal_id=internal, payload=json.dumps(payload), external_id=None,
                         submission="NEEDS_REVIEW", cancellation="NONE",
                         source_review=json.dumps({"kind": "CDS_UPGRADE_UNRESOLVED"}),
                         last_error="Pre-upgrade CDS receipt: server submission outcome is unknown"))

    settings = ApiConfig(velide_server="https://cds.invalid/graphql", velide_websockets_server="wss://cds.invalid/graphql")
    journal = InventoryJournal()
    strategy = CdsStrategy(settings, str(tmp_path), journal, pending_files=[path.name])
    service = DeliveriesService(VelideGateway(settings, TargetSystem.CDS), settings, TargetSystem.CDS,
                                DeliveryRepository(), journal)
    service.set_strategy(strategy)
    dispatch = MagicMock()
    monkeypatch.setattr(service._dispatcher, "queue_add", dispatch)
    model = DeliveryTableModel()
    service.delivery_acknowledged.connect(model.add_delivery_acknowledge)
    service.delivery_update.connect(lambda internal, status: model.update_delivery(internal, service.get_order(internal), status))
    handler = QLogHandler()
    logs = []
    handler.new_log.connect(lambda *row: logs.append(row))
    service.logger.addHandler(handler)
    worker = CdsLogsListenerWorker(str(tmp_path), pending_files=[path.name])
    worker._prepare_baseline()
    monkeypatch.setattr(worker._stop, "wait", lambda _: False)
    strategy._running, strategy._file_listener_worker = True, worker
    worker.signals.new_order.connect(lambda data: strategy._on_new_file_found(data, worker))
    try:
        worker._read_file(str(path))
        assert accepted == ["ent-original.json"]  # Third positional journal argument.
        assert model.rowCount() == 1
        assert model.data(model.index(0, 1)) == "Erro"
        assert model.data(model.index(0, 2)) == "Rua A"
        assert len(logs) == 1 and "envio anterior ainda não foi confirmado" in logs[0][2]
        dispatch.assert_not_called()
        assert str(path) in worker._seen
        worker._read_file(str(path))
        assert model.rowCount() == 1 and len(logs) == 1
    finally:
        service.logger.removeHandler(handler)
        handler.close()


def test_cds_filename_survives_service_dispatcher_second_accept_without_changing_payload(tmp_path, qtbot):
    from config import ApiConfig, TargetSystem
    from api.velide_gateway import VelideGateway
    from repositories.deliveries_repository import DeliveryRepository
    from services.deliveries_service import DeliveriesService

    accepted = []
    class Journal:
        def submit(self, method, *args, success=None, failure=None):
            assert method == "accept"
            accepted.append(args)
            success({"submission": "QUEUED", "cancellation": "NONE"})
    settings = ApiConfig(velide_server="https://cds.invalid/graphql", velide_websockets_server="wss://cds.invalid/graphql")
    journal = Journal()
    strategy = CdsStrategy(settings, str(tmp_path), journal)
    service = DeliveriesService(VelideGateway(settings, TargetSystem.CDS), settings, TargetSystem.CDS,
                                DeliveryRepository(), journal)
    service.set_strategy(strategy)
    strategy._running = True
    data = write_order(tmp_path / "ent-new.json")
    data["_source_file"] = "ent-new.json"
    strategy._on_new_file_found(data)
    assert len(accepted) == 2
    assert accepted[0] == accepted[1]
    assert accepted[0][2] == "ent-new.json"
    assert "_cds_source_file" not in accepted[0][1]
