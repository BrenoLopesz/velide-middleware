"""Run both provisioned connections; select intake without abandoning either."""
from types import SimpleNamespace
import os

from PyQt5.QtCore import QObject, QTimer
from PyQt5.QtWidgets import QAction, QActionGroup, QMessageBox

from backend_profiles import prepare_profile_state, ownership_for, select_backend
from local_upgrade import UpgradeResult, _cds_observation_options
from presenters.app_presenter import AppPresenter
from utils.tray_manager import AppTrayIcon
from utils.tray_guard import TrayGuard
from visual.main_view import MainView


class ProfileApplication(QObject):
    def __init__(self, app, settings, upgrade, config_path, bundle_dir, primary_view,
                 service_builder=None, strategy_builder=None):
        super().__init__()
        if service_builder is None or strategy_builder is None:
            from main import build_services, setup_strategy
            service_builder = service_builder or build_services
            strategy_builder = strategy_builder or setup_strategy
        self.app, self.settings = app, settings
        self.config_path, self.bundle_dir = config_path, bundle_dir
        self.registry, profiles = prepare_profile_state(settings, bundle_dir)
        self.runtimes = {}
        selected = self.registry.selected()
        for name, profile in profiles.items():
            view = primary_view if name == selected else MainView()
            view.setWindowTitle("Velide Middleware — " + ("legado" if name == "legacy" else "novo Velide"))
            local = UpgradeResult(profile, profile.sqlite_path, upgrade.installation_id)
            _cds_observation_options(local)
            services = service_builder(profile, local, ownership_for(self.registry, name))
            strategy = strategy_builder(profile, services)
            presenter = AppPresenter(view, services, strategy)
            self.runtimes[name] = SimpleNamespace(settings=profile, services=services,
                                                 strategy=strategy, presenter=presenter, view=view)
        self.tray = AppTrayIcon(os.path.join(bundle_dir, "resources", "velide_white.png"),
                                primary_view, self.show_selected, self.stop,
                                "Velide Middleware — conexões de integração")
        TrayGuard.register(self.tray)
        for runtime in self.runtimes.values():
            runtime.view.set_tray_icon(self.tray)
        self._selection_actions = {}
        group = QActionGroup(self)
        group.setExclusive(True)
        menu = self.tray.contextMenu()
        for name in self.runtimes:
            label = "legado" if name == "legacy" else "novo Velide"
            show = QAction("Abrir conexão: " + label, menu)
            show.triggered.connect(lambda checked=False, name=name: self.show_profile(name))
            menu.addAction(show)
            if len(self.runtimes) > 1:
                action = QAction("Novos pedidos: " + label, group)
                action.setCheckable(True)
                action.setChecked(name == selected)
                action.triggered.connect(lambda checked=False, name=name: self.select(name))
                menu.addAction(action)
                self._selection_actions[name] = action
        self.app.aboutToQuit.connect(self.shutdown)
        self.rollout = None

    def start(self):
        self.app.setQuitOnLastWindowClosed(False)
        for name, runtime in self.runtimes.items():
            runtime.presenter.run(show=name == self.registry.selected())
            # Intake is durable even while a tenant needs fresh login. No API
            # mutation/ERP effect runs until the profile scope is authenticated.
            runtime.services.deliveries.start_listening()
            runtime.services.auth.load_stored_token()

    def show_profile(self, name):
        view = self.runtimes[name].view
        view.showNormal()
        view.activateWindow()

    def show_selected(self):
        self.show_profile(self.registry.selected())

    def select(self, name):
        try:
            if self.rollout is not None and self.rollout.coordinator is not None and (
                    self.rollout.coordinator._paused or
                    getattr(self.rollout.coordinator.graph, "state", "running") != "running"):
                raise ValueError("Aguarde a conclusão ou cancele a atualização antes de trocar a conexão.")
            from config import install_effective_settings
            selected = select_backend(self.config_path, self.bundle_dir, name,
                                      registry=self.registry, runtime_settings=self.settings)
            install_effective_settings(self.settings, selected)
        except Exception as error:
            QMessageBox.warning(self.runtimes[name].view, "Conexão não alterada", str(error))
        for backend, action in self._selection_actions.items():
            action.setChecked(backend == self.registry.selected())
        self.show_selected()

    def shutdown(self):
        if self.rollout is not None:
            rollout, self.rollout = self.rollout, None
            rollout.shutdown()
        for runtime in self.runtimes.values():
            runtime.services.deliveries.stop_listening()
            runtime.services.auth.invalidate_session()
            runtime.services.websockets.stop_service()
            runtime.services.tracking_persistence._cleanup_service.stop_routine()
        self.tray.cleanup()

    def stop(self):
        if self.rollout is not None and self.rollout.coordinator is not None and (
                self.rollout.busy or (
                    getattr(self.rollout.coordinator.graph, "state", "running") != "running"
                    and self.rollout.coordinator.state["state"] not in {"ACK_PENDING", "APPLIED"})):
            self.rollout.cancel()
            QTimer.singleShot(100, self.stop)
            return
        self.shutdown()
        self.app.quit()
