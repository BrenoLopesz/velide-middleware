from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from PyQt5.QtCore import QObject, pyqtSignal

from api.sqlite_manager import DeliveryStatus
from config import ApiConfig, TargetSystem
from connectors.farmax.farmax_delivery_ingestor import FarmaxDeliveryIngestor
from connectors.farmax.identity import normalize_farmax_id
from models.velide_delivery_models import Order
from repositories.deliveries_repository import DeliveryRepository
from services.deliveries_service import DeliveriesService
from services.reconciliation_service import ReconciliationService
from workers.velide_worker import VelideWorker


def api_config():
    return ApiConfig(velide_server="https://api.invalid/",
                     velide_websockets_server="wss://api.invalid/")


def make_order(internal_id="123"):
    return Order(internal_id=internal_id, customerName="Fixture", address="Rua A, 1",
                 createdAt=datetime(2026, 11, 11, 12))


@pytest.mark.parametrize("raw", [123, 123.0, "123.0", "00123"])
def test_farmax_identity(raw):
    assert normalize_farmax_id(raw) == "123"


def test_identity_does_not_round_large_integers():
    assert normalize_farmax_id("9007199254740993") == "9007199254740993"
    for raw in ["123.5", "NaN", "inf", None]:
        with pytest.raises(ValueError):
            normalize_farmax_id(raw)


def test_cancel_float_id_removes_queued_order_and_repository(qtbot):
    service = DeliveriesService(MagicMock(), api_config(), TargetSystem.FARMAX,
                                DeliveryRepository())
    service._dispatcher._is_processing = True
    service._on_order_normalized(make_order("123.0"))
    assert service.get_order("123") is not None
    service._on_internal_order_cancelled("123.0", None)
    assert not service._dispatcher._queue
    assert service.get_order("123") is None


def test_cds_string_identity_is_unchanged(qtbot):
    service = DeliveriesService(MagicMock(), api_config(), TargetSystem.CDS,
                                DeliveryRepository())
    order = make_order("00123")
    service._sanitize_order_id(order)
    assert order.internal_id == "00123"


def test_successful_detail_retry_resumes_real_poll_timer(qtbot, monkeypatch):
    ingestor = FarmaxDeliveryIngestor(MagicMock(), MagicMock())
    ingestor._is_running = True
    ingestor._config.poll_interval_ms = 10
    ingestor._on_fetch_details_error("temporary", (123.0,))
    ingestor._retry_timer.stop()
    poll = MagicMock()
    monkeypatch.setattr(ingestor._thread_pool, "start", poll)
    ingestor._on_details_retrieved([])
    qtbot.waitUntil(lambda: poll.call_count == 1)
    ingestor.stop()


def test_reconciled_departure_emits_effect_and_missing_stays_active(qtbot):
    tracking = MagicMock()
    tracking.get_active_cache_snapshot.return_value = [
        ("123", "remote", DeliveryStatus.ADDED),
        ("124", "absent", DeliveryStatus.IN_PROGRESS),
    ]
    service = ReconciliationService(MagicMock(), tracking, api_config(), TargetSystem.FARMAX)
    received = []
    service.delivery_in_route.connect(lambda *args: received.append(args))
    service._handle_snapshot_results({"remote": ("ROUTED", "courier")})
    assert received == [("123", "courier")]
    tracking.mark_as_missing.assert_not_called()


def test_unavailable_client_emits_error_before_finished(qtbot):
    gateway = MagicMock()
    gateway.get_client.return_value = None
    worker = VelideWorker.for_snapshot(gateway)
    outcomes = []
    worker.signals.error.connect(lambda error: outcomes.append("error"))
    worker.signals.finished.connect(lambda: outcomes.append("finished"))
    worker.run()
    assert outcomes == ["error", "finished"]


def test_presenter_uses_only_fsm_business_signal(qtbot):
    from presenters.dashboard_presenter import DashboardPresenter
    from states.websocket_fsm import WebSocketFSM
    from models.velide_websockets_models import LatestAction

    class Socket(QObject):
        action_received = pyqtSignal(LatestAction)
        sig_connecting = pyqtSignal()
        sig_connected = pyqtSignal()
        sig_disconnected = pyqtSignal()
        sig_error = pyqtSignal()

    services = MagicMock()
    services.websockets = Socket()
    fsm = WebSocketFSM(services)
    presenter = DashboardPresenter(MagicMock(), services,
                                  SimpleNamespace(websockets_state=fsm,
                                                  logged_in_state=MagicMock()),
                                  MagicMock(capabilities=set()))
    qtbot.waitUntil(lambda: fsm.machine.isRunning())
    services.websockets.sig_connected.emit()
    qtbot.waitUntil(lambda: fsm.s_connected in fsm.machine.configuration())
    action = LatestAction.model_construct()
    services.websockets.action_received.emit(action)
    services.velide_action_handler.handle_action.assert_called_once_with(action)
    fsm.machine.stop()
