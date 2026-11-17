"""Runtime wiring checks, with transport/identity HTTP explicitly injected."""
import threading
import time
from types import SimpleNamespace

import jwt
import pytest
import yaml
from PyQt5.QtCore import QObject, QTimer, pyqtSignal
from PyQt5.QtWidgets import QDialog, QMenu

from config import Settings
from local_upgrade import upgrade_installation
from middleware_transport import MiddlewareError
from services.rollout_controller import QtMiddlewareTransport, RolloutController
from test_automatic_upgrade import installed
from test_middleware_rollout import Graph, Server
from utils.token_storage import read_token_from_file


def test_fresh_marker_runs_setup_before_any_legacy_graph(qtbot, monkeypatch):
    import main
    monkeypatch.setattr(main, "config", Settings(onboarding="native_required"))
    monkeypatch.setattr(main, "perform_pre_boot_checks", lambda: None)
    monkeypatch.setattr(main, "handle_updates", lambda: False)
    monkeypatch.setattr(main, "create_application", lambda: qtbot)
    monkeypatch.setattr(main, "MainView", lambda: object())
    monkeypatch.setattr(main, "configure_logging", lambda view: None)
    entered = []
    class Setup:
        Accepted = QDialog.Accepted
        def __init__(self, path, root):
            entered.append(True)
        def exec_(self):
            return QDialog.Rejected
    monkeypatch.setattr("visual.screens.native_onboarding_screen.NativeOnboardingDialog", Setup)
    monkeypatch.setattr(main, "build_services", lambda *args: pytest.fail("Legacy graph built during setup"))
    main.main()
    assert entered == [True]


def test_actual_native_service_builder_connects_recovery_barrier(tmp_path, qtbot):
    from main import build_services
    from config import config
    settings = config.model_copy(deep=True)
    settings.sqlite_path = str(tmp_path / "native.db")
    from config import NativeApiConfig
    settings.api.native = NativeApiConfig(merchant_id="10000000-0000-0000-0000-000000000001",
                                         connection_id="10000000-0000-0000-0000-000000000002",
                                         timezone="America/Sao_Paulo")
    settings.api.velide_server = "https://native.invalid/graphql"
    services = build_services(settings)
    assert services.deliveries._recovery is services.reconciliation
    assert services.deliveries._dispatcher._requires_recovery
    assert not services.deliveries._dispatcher._recovery_ready
    services.tracking_persistence._cleanup_service.stop_routine()


def test_missing_deployment_resource_is_visible_native_setup_state(tmp_path, qtbot):
    from visual.screens.native_onboarding_screen import NativeOnboardingDialog
    dialog = NativeOnboardingDialog(tmp_path / "config.yml", tmp_path)
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitUntil(lambda: not dialog._busy and "middleware-service.json" in dialog.status.text())
    assert dialog.auth is None
    assert not dialog.finish_button.isEnabled()
    dialog.reject()


def test_qt_transport_cancellation_returns_before_slow_http(qtbot, monkeypatch):
    from middleware_transport import MiddlewareTransport
    entered, release = threading.Event(), threading.Event()
    def slow(*args, **kwargs):
        entered.set()
        release.wait(3)
        return {"late": True}
    monkeypatch.setattr(MiddlewareTransport, "request", slow)
    client = QtMiddlewareTransport("https://middleware.invalid/api/middleware/v1")
    QTimer.singleShot(40, lambda: setattr(client, "cancel_requested", True))
    start = time.monotonic()
    try:
        with pytest.raises(MiddlewareError, match="cancelada"):
            client.request("GET", "/bootstrap")
        assert entered.is_set() and time.monotonic() - start < .5
    finally:
        release.set()


def test_rollout_controller_prepares_independent_login_while_legacy_continues(tmp_path, qtbot, monkeypatch):
    config_path, database, _, document = installed(tmp_path)
    document["api"]["use_ssl"] = True
    config_path.write_text(yaml.safe_dump(document), encoding="utf-8")
    adopted = upgrade_installation(config_path, tmp_path)
    server, graph = Server(config_path, database, adopted), Graph()
    monkeypatch.setattr("services.rollout_quiescence.GraphQuiescence", lambda runtimes: graph)
    class LegacyAuth(QObject):
        access_token = pyqtSignal(str)
        _bundle_access_token = "legacy-access"
    runtime = QObject()
    menu = QMenu()
    runtime.tray = SimpleNamespace(contextMenu=lambda: menu)
    runtime.runtimes = {"legacy": SimpleNamespace(services=SimpleNamespace(auth=LegacyAuth()))}
    controller = RolloutController(runtime, config_path, tmp_path, transport=server)
    qtbot.addWidget(controller.dialog)
    controller.timer.stop()
    controller.discover()
    assert controller.login.isEnabled()
    token = jwt.encode(dict(iss="https://native-auth.invalid/", sub="test-subject", aud="native-api",
                            azp="native-client", scope="openid", exp=int(time.time()) + 3600),
                       "test-key", algorithm="HS256")
    bundle = dict(access_token=token, refresh_token="native-refresh")
    from utils.device_code import DeviceCode
    from utils.access_token import AccessToken
    monkeypatch.setattr(DeviceCode, "request", lambda _: dict(user_code="TEST", device_code="device",
        expires_in=300, interval=1, verification_uri="https://native-auth.invalid/activate"))
    monkeypatch.setattr(AccessToken, "request", lambda _: dict(bundle))
    controller.start_login()
    qtbot.waitUntil(lambda: controller.coordinator.state["state"] == "PREPARED")
    assert not graph.paused and runtime.runtimes["legacy"].services.auth._bundle_access_token == "legacy-access"
    assert controller.finish.isEnabled()
    controller.begin()
    assert graph.paused
    assert controller.native_auth._rollout_paused
    assert not controller.native_auth._refresh_timer.isActive()
    assert read_token_from_file(controller.coordinator.prepared_path)["refresh_token"] == "native-refresh"
    controller.cancel()
    assert graph.resumed == 1
    controller.shutdown()


def test_real_cancel_button_during_writer_drain_invalidates_before_export(tmp_path, qtbot):
    from PyQt5.QtCore import QCoreApplication, QEvent, QPointF, Qt
    from PyQt5.QtGui import QMouseEvent
    from PyQt5.QtWidgets import QWidget
    from main import build_services, setup_strategy
    from middleware_rollout import read_state
    config_path, database, _, document = installed(tmp_path, target="CDS", mappings=False)
    document["api"]["use_ssl"] = True
    config_path.write_text(yaml.safe_dump(document), encoding="utf-8")
    adopted = upgrade_installation(config_path, tmp_path)
    adopted.settings.sqlite_path = str(database)
    adopted.settings.token_path = str(tmp_path / "resources" / "token.txt")
    services = build_services(adopted.settings, adopted)
    strategy = setup_strategy(adopted.settings, services)
    services.auth._bundle_access_token = "legacy-access"
    view, menu = QWidget(), QMenu()
    qtbot.addWidget(view)
    runtime = QObject()
    runtime.tray = SimpleNamespace(contextMenu=lambda: menu)
    runtime.runtimes = {"legacy": SimpleNamespace(services=services, strategy=strategy, view=view)}
    server = Server(config_path, database, adopted)
    controller = RolloutController(runtime, config_path, tmp_path, transport=server)
    qtbot.addWidget(controller.dialog)
    controller.timer.stop()
    controller.discover()
    native_token = jwt.encode(dict(iss="https://native-auth.invalid/", sub="test-subject", aud="native-api",
        azp="native-client", scope="openid", exp=int(time.time()) + 3600), "test-key", algorithm="HS256")
    controller.coordinator.prepare(dict(access_token=native_token, refresh_token="native-refresh"))
    entered, release = threading.Event(), threading.Event()
    journal = services.tracking_persistence.journal
    def delayed_writer():
        entered.set()
        assert release.wait(4)
    journal.submit(delayed_writer)
    journal.submit("observe_source", [("farmax:late-source", {})])
    assert entered.wait(1)
    def post_cancel():
        center = QPointF(controller.cancel_button.rect().center())
        for event, buttons in ((QEvent.MouseButtonPress, Qt.LeftButton), (QEvent.MouseButtonRelease, Qt.NoButton)):
            QCoreApplication.postEvent(controller.cancel_button,
                QMouseEvent(event, center, Qt.LeftButton, buttons, Qt.NoModifier))
    QTimer.singleShot(40, post_cancel)
    start = time.monotonic()
    try:
        controller._perform(lambda: controller.coordinator.begin())
        assert time.monotonic() - start < .75
        assert not release.is_set()
        assert read_state(config_path)["state"] == "INVALIDATED"
        assert read_state(config_path)["cancel_reason"] == "cancelled"
        assert not hasattr(server, "inventory")
        assert not view.isEnabled()
    finally:
        release.set()
        qtbot.waitUntil(lambda: controller.coordinator.graph.state == "running", timeout=3000)
        controller.shutdown()
        services.tracking_persistence._cleanup_service.stop_routine()
        services.auth.invalidate_session()
        services.reconciliation.stop_service()
        strategy.stop_listening()
        journal.pool.waitForDone()
    assert view.isEnabled()
    assert journal.journal.source_state()["pending"][0]["source_id"] == "farmax:late-source"
