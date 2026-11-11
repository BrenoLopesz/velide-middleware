from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

from services.strategies.cds_strategy import CdsStrategy
from workers.cds_logs_listener_worker import CdsLogsListenerWorker, _NewFileHandler
from models.cds_order_model import CdsOrder


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
    assert received == [{"nome_cliente": "Fixture", "_source_file": "ent1.json"}]
    worker.stop()  # stop-before-start is also valid
