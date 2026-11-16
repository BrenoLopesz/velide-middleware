from pathlib import Path

from PyQt5.QtCore import QThreadPool

from backend_profiles import prepare_profile_state
from config import Settings
from local_upgrade import upgrade_installation
from main import build_services, setup_strategy
from services.profile_application import ProfileApplication
from test_automatic_upgrade import installed, package_for, supply, receipt
from test_backend_profile_upgrade import cache


def dual_cds(tmp_path):
    config, database, folder, _ = installed(tmp_path, "CDS", mappings=False)
    cache(config)
    adopted = upgrade_installation(config, tmp_path)
    supply(config, package_for(config, database, adopted))
    upgraded = upgrade_installation(config, tmp_path)
    assert not upgraded.package_error
    return config, folder, upgraded


def cleanup(services):
    services.deliveries.stop_listening()
    services.websockets.stop_service()
    if hasattr(services.reconciliation, "stop_service"):
        services.reconciliation.stop_service()
    else:
        services.reconciliation.timer.stop()
    services.tracking_persistence._cleanup_service.stop_routine()
    services.deliveries._journal.pool.waitForDone()


def test_both_production_service_stacks_have_isolated_auth_and_journals(tmp_path, qtbot):
    _, _, upgrade = dual_cds(tmp_path)
    registry, profiles = prepare_profile_state(upgrade.settings, tmp_path)
    from backend_profiles import ownership_for
    stacks = {name: build_services(profile, ownership=ownership_for(registry, name)) for name, profile in profiles.items()}
    try:
        for services in stacks.values():
            # Callback-local token paths, not a mutable process-global setting.
            assert services.auth._token_path
        assert stacks["legacy"].auth._token_path != stacks["native"].auth._token_path
        assert stacks["legacy"].deliveries._journal.journal.path != stacks["native"].deliveries._journal.journal.path
        legacy, native = [stacks[name].auth for name in ("legacy", "native")]
        legacy._commit_token(0, {"access_token": cache(Path(profiles["legacy"].token_path)), "refresh_token": "legacy-only"})
        assert not native._gateway.is_ready()
    finally:
        for services in stacks.values():
            cleanup(services)
        QThreadPool.globalInstance().waitForDone()


def test_real_cds_scanners_preserve_owner_across_switch_and_logged_out_intake(tmp_path, qtbot):
    _, folder, upgrade = dual_cds(tmp_path)
    registry, profiles = prepare_profile_state(upgrade.settings, tmp_path)
    from backend_profiles import ownership_for
    from local_upgrade import UpgradeResult, _cds_observation_options
    stacks, strategies = {}, {}
    for name, profile in profiles.items():
        local = UpgradeResult(profile, profile.sqlite_path, upgrade.installation_id)
        _cds_observation_options(local)
        services = build_services(profile, local, ownership_for(registry, name))
        stacks[name] = services
        strategies[name] = setup_strategy(profile, services)
    try:
        for services in stacks.values():
            services.deliveries.start_listening()
        _, first = receipt(folder, "ent-first.json")
        qtbot.waitUntil(lambda: registry.owner(first) == "native", timeout=10000)
        registry.select("legacy")
        _, second = receipt(folder, "ent-second.json")
        qtbot.waitUntil(lambda: registry.owner(second) == "legacy", timeout=10000)
        # Neither profile is authenticated. File receipts and ownership survive
        # nonetheless; no HTTP worker can send to an alternate backend.
        assert all(not stack.auth._gateway.is_ready() for stack in stacks.values())
        for name, internal in (("native", first), ("legacy", second)):
            journal = stacks[name].deliveries._journal.journal
            qtbot.waitUntil(lambda: journal.get_order(internal) is not None or bool(registry.receipts(name)), timeout=10000)
            journal.restore()
            assert journal.get_order(internal)["payload"]
        registry.select("native")
        assert registry.owner(second) == "legacy"
        assert not registry.receipts("legacy") and not registry.receipts("native")
    finally:
        for services in stacks.values():
            cleanup(services)
        QThreadPool.globalInstance().waitForDone()


def test_profile_application_constructs_two_presenters_and_selection_not_order_migration(tmp_path, qtbot, qapp, monkeypatch):
    config, _, upgrade = dual_cds(tmp_path)
    import services.profile_application as module
    views = []
    class View:
        def __init__(self):
            self.title, self.tray_icon = None, None
            views.append(self)
        def setWindowTitle(self, title): self.title = title
        def set_tray_icon(self, tray): self.tray_icon = tray
        def showNormal(self): pass
        def activateWindow(self): pass
    # Real services/strategies are composed; avoid platform monitor/tray geometry
    # and presentation state machines for this lifecycle wiring assertion.
    presenters = []
    class Presenter:
        def __init__(self, view, services, strategy):
            self.view, self.services = view, services
            presenters.append(self)
        def run(self, show=True): self.show = show
    class Tray:
        def __init__(self, *args): pass
        def contextMenu(self):
            from PyQt5.QtWidgets import QMenu
            self.menu = QMenu()
            return self.menu
        def cleanup(self): pass
    monkeypatch.setattr(module, "MainView", View)
    monkeypatch.setattr(module, "AppPresenter", Presenter)
    monkeypatch.setattr(module, "AppTrayIcon", Tray)
    monkeypatch.setattr(module.TrayGuard, "register", lambda _: None)
    runtime = ProfileApplication(qapp, upgrade.settings, upgrade,
                                 config, tmp_path, View(), build_services, setup_strategy)
    try:
        for profile in runtime.runtimes.values():
            monkeypatch.setattr(profile.services.auth, "load_stored_token", lambda: None)
        runtime.start()
        assert len(presenters) == 2 and sum(presenter.show for presenter in presenters) == 1
        assert all(profile.services.deliveries._listening for profile in runtime.runtimes.values())
        runtime.registry.intake("native-owned", {"internal_id": "native-owned"})
        runtime.select("legacy")
        assert runtime.registry.selected() == "legacy"
        assert runtime.registry.owner("native-owned") == "native"
        assert Settings.from_yaml(config).active_backend == "legacy"
    finally:
        runtime.shutdown()
        for profile in runtime.runtimes.values():
            cleanup(profile.services)
        QThreadPool.globalInstance().waitForDone()
        qapp.aboutToQuit.disconnect(runtime.shutdown)
