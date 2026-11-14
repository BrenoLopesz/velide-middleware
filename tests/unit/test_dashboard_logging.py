import logging
import threading
from datetime import datetime

import pytest
from PyQt5.QtCore import QObject, QThread, pyqtSlot

from models.log_table_model import LogTableModel
from utils.log_handler import QLogHandler, PackageFilter
from utils.log_messages import humanize_error, log_operation, log_sync_error
from utils.logger import setup_logging


class DashboardReceiver(QObject):
    def __init__(self):
        super().__init__()
        self.model = LogTableModel(self)
        self.threads = []

    @pyqtSlot(str, str, str)
    def add_row(self, timestamp, level, message):
        self.threads.append(QThread.currentThread())
        self.model.add_log_entry(timestamp, level, message)


@pytest.fixture
def configured_logs(tmp_path, qtbot, monkeypatch):
    monkeypatch.setattr("utils.logger.BUNDLE_DIR", str(tmp_path))
    root = logging.getLogger()
    previous, level = list(root.handlers), root.level
    # Isolate setup without disturbing pytest's capture or another test's logging.
    for handler in previous:
        root.removeHandler(handler)
    receiver = DashboardReceiver()
    handler = QLogHandler(receiver)
    handler.new_log.connect(receiver.add_row)
    try:
        setup_logging(handler)
        yield logging.getLogger("test.dashboard"), handler, receiver, tmp_path / "resources" / "logs"
    finally:
        for current in list(root.handlers):
            root.removeHandler(current)
            current.close()
        for current in previous:
            root.addHandler(current)
        root.setLevel(level)


def messages(receiver):
    return [receiver.model.data(receiver.model.index(row, 2)) for row in range(receiver.model.rowCount())]


def test_real_qhandler_translates_journal_exception_and_keeps_traceback_in_file(configured_logs):
    logger, handler, receiver, folder = configured_logs
    try:
        raise ValueError("Source payload conflicts with an already accepted order; durable review hold recorded")
    except ValueError:
        logger.exception("Durable synchronization operation failed")
    assert receiver.model.rowCount() == 1
    assert "evitar duplicidade" in messages(receiver)[0]
    assert "Traceback" not in messages(receiver)[0]
    assert "durable review hold" not in messages(receiver)[0]
    assert receiver.model.data(receiver.model.index(0, 1)) == "Erro"
    diagnostic = (folder / "error.log").read_text(encoding="utf-8")
    assert "Durable synchronization operation failed" in diagnostic
    assert "Traceback (most recent call last)" in diagnostic
    assert "ValueError: Source payload conflicts" in diagnostic


def test_threaded_operation_signals_compose_with_dashboard_model(configured_logs, qtbot):
    logger, handler, receiver, folder = configured_logs
    def run():
        log_operation(logger, "cancellation_requested", "Pedido 123", detail={"id": "technical-id"})
        log_operation(logger, "cancellation_confirmed", "Pedido 123")
        log_operation(logger, "route_started", "Maria")
        log_operation(logger, "route_completed", "Maria")
    thread = threading.Thread(target=run)
    thread.start()
    thread.join(5)
    qtbot.waitUntil(lambda: receiver.model.rowCount() == 4)
    assert all(thread is receiver.thread() for thread in receiver.threads)
    assert messages(receiver) == [
        "Cancelamento solicitado (Pedido 123). Aguardando confirmação do Velide.",
        "Cancelamento confirmado (Pedido 123).",
        "Entrega saiu para a rota (Maria).",
        "Entrega concluída (Maria).",
    ]
    diagnostic = (folder / "app.log").read_text(encoding="utf-8")
    assert "technical-id" in diagnostic
    assert "event=route_completed" in diagnostic
    assert all("technical-id" not in message for message in messages(receiver))


def test_existing_main_screen_pipeline_translates_wrapped_failure(configured_logs):
    logger, handler, receiver, folder = configured_logs
    logger.error("Sincronização pendente (%s): %s", "123", "database is locked")
    assert messages(receiver)[0].startswith("Sincronização pendente (123): O armazenamento local está ocupado.")
    assert "database is locked" in (folder / "error.log").read_text(encoding="utf-8")


def test_explicit_user_text_does_not_mutate_record_or_depend_on_handler_order(qtbot, tmp_path):
    handler = QLogHandler()
    received = []
    handler.new_log.connect(lambda *row: received.append(row))
    record = logging.LogRecord("test", logging.ERROR, __file__, 1, "Technical cause: %s", ("raw detail",), None)
    record.created = datetime(2026, 11, 13, 10, 20, 30).timestamp()
    record.user_message = "Confira o pedido no Velide."
    formatter = logging.Formatter("%(message)s")
    before = formatter.format(record)
    handler.handle(record)
    assert received == [("13/11 10:20:30", "Erro", "Confira o pedido no Velide.")]
    assert formatter.format(record) == before == "Technical cause: raw detail"
    assert record.args == ("raw detail",)
    handler.close()


def test_unknown_errors_and_third_party_warnings_remain_visible(configured_logs):
    logger, handler, receiver, folder = configured_logs
    handler.addFilter(PackageFilter(["httpx"]))
    third_party = logging.getLogger("httpx.test_dashboard")
    third_party.setLevel(logging.DEBUG)
    third_party.info("Routine request detail")
    third_party.warning("Unexpected proxy response ZX-91")
    log_sync_error(logger, "Unknown remote condition ZX-92", "Pedido 123")
    logger.error("Falha no cadastro do entregador. Confira os dados.")
    assert receiver.model.rowCount() == 3
    assert "ZX-91" in messages(receiver)[0]
    assert "ZX-92" in messages(receiver)[1]
    assert "consulte o suporte" in messages(receiver)[1]
    assert "Falha no cadastro" in messages(receiver)[2]
    assert "Unknown remote condition ZX-92" in (folder / "error.log").read_text(encoding="utf-8")


def test_setup_replaces_owned_handlers_without_duplicate_dashboard_rows(configured_logs):
    logger, handler, receiver, folder = configured_logs
    setup_logging(handler)
    logger.info("Conectado ao Velide.")
    assert messages(receiver) == ["Conectado ao Velide."]
    assert (folder / "app.log").read_text(encoding="utf-8").count("Conectado ao Velide.") == 1


def test_actual_log_table_accepts_qhandler_signal_from_background_thread(qtbot, monkeypatch):
    from PyQt5.QtGui import QFont
    from visual.components.log_table import LogTable
    monkeypatch.setattr("visual.components.log_table.get_fonts", lambda: {"regular_small": QFont()})
    table = LogTable()
    qtbot.addWidget(table)
    handler = QLogHandler(parent=table)
    handler.new_log.connect(table.add_row)  # Exact main.configure_logging composition.
    logger = logging.Logger("isolated.dashboard", level=logging.INFO)
    logger.addHandler(handler)
    thread = threading.Thread(target=lambda: log_operation(logger, "route_started", "Pedido 123"))
    thread.start()
    thread.join(5)
    qtbot.waitUntil(lambda: table.model().rowCount() == 1)
    assert table.model().data(table.model().index(0, 2)) == "Entrega saiu para a rota (Pedido 123)."
    logger.removeHandler(handler)
    handler.close()


def test_translated_signal_is_not_wrapped_again_on_its_way_to_dashboard(configured_logs):
    logger, handler, receiver, folder = configured_logs
    explanation = humanize_error("Source payload conflicts with an already accepted order")
    logger.error("Sincronização pendente (%s): %s", "123", explanation)
    assert messages(receiver) == ["Sincronização pendente (123): " + explanation]


def test_cancellation_failure_is_an_error_with_separate_diagnostics(configured_logs):
    logger, handler, receiver, folder = configured_logs
    log_operation(logger, "cancellation_failed", "Pedido 123", detail="HTTP_409: delivery in route")
    assert receiver.model.data(receiver.model.index(0, 1)) == "Erro"
    assert "Confira a entrega no Velide" in messages(receiver)[0]
    assert "HTTP_409" not in messages(receiver)[0]
    assert "HTTP_409" in (folder / "error.log").read_text(encoding="utf-8")


@pytest.mark.parametrize("technical,expected", [
    ("Physical source configuration is unverified or changed", "loja configurada"),
    ("Legacy mappings require a native migration manifest", "atualização"),
    ("A bound source timezone is required for a naive original receipt", "fuso horário"),
    ("database or disk is full", "Libere espaço"),
    ("Cancellation is unresolved", "cancelamento"),
    ({"code": "UNAUTHENTICATED", "message": "Denied"}, "Conecte novamente"),
    ("ConnectError: All connection attempts failed", "internet"),
])
def test_actionable_known_errors(technical, expected):
    assert expected in humanize_error(technical)
