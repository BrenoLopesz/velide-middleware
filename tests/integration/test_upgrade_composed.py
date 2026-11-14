"""Original installation -> main's service graph -> real local HTTP transport."""
from datetime import datetime
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading

import pytest
from PyQt5.QtCore import QThreadPool
from PyQt5.QtWidgets import QApplication
import yaml

from config import Settings, install_effective_settings
from local_upgrade import upgrade_installation
from test_automatic_upgrade import installed, rows
from utils.source_identity import cds_file_identity, configured_scope, configured_source_identity


@pytest.fixture
def pre_boot(monkeypatch):
    import main as application
    from utils import instance_lock

    previous_policy = asyncio.get_event_loop_policy()
    owns_lock = []

    def start(root):
        monkeypatch.setattr(instance_lock, "LOCK_FILE_PATH", str(root / "resources" / "velide.lock"))
        # This includes the production Python 3.8 Windows asyncio policy fix.
        application.perform_pre_boot_checks()
        owns_lock.append(True)

    try:
        yield start
    finally:
        if owns_lock:
            instance_lock.release_lock()
        asyncio.set_event_loop_policy(previous_policy)


@pytest.fixture
def legacy_http(monkeypatch):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
            if "addDeliveryFromIntegration" in body.get("query", ""):
                response = {"data": {"addDeliveryFromIntegration": {
                    "id": "new-legacy-delivery", "createdAt": "2026-11-13T15:34:56Z"}}}
            else:
                response = {"errors": [{"message": "Unexpected operation on the original legacy endpoint"}]}
            encoded = json.dumps(response).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.05), daemon=True)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    thread.start()
    try:
        yield "http://127.0.0.1:{}/original-api/".format(server.server_port), requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def write_receipt(folder, filename):
    value = {"nome_cliente": "Cliente de teste", "contato_cliente": "555-1234", "valor_pedido": "12,50",
             "endereco": "Rua de teste, 10 - bloco - Centro - Cidade - UF - CEP", "horario_pedido": "2026-11-13T12:34:56"}
    path = folder / filename
    path.write_bytes(json.dumps(value).encode("windows-1252"))
    return path, cds_file_identity(filename, datetime.fromisoformat(value["horario_pedido"]))


@pytest.mark.parametrize("failed_optional_package", [False, True])
def test_original_cds_auto_upgrade_main_services_and_real_legacy_http(tmp_path, qtbot, monkeypatch, legacy_http, failed_optional_package, pre_boot):
    import main as application

    endpoint, requests = legacy_http
    config, database, folder, document = installed(tmp_path, "CDS")
    document.pop("farmax")  # Original CDS installer emitted no Farmax/native/timezone section.
    document["api"]["velide_server"] = endpoint
    config.write_text(yaml.safe_dump(document), encoding="utf-8")
    old_config = config.read_bytes()
    existing_mappings = rows(database, "SELECT * FROM DeliveryMapping")
    existing_couriers = rows(database, "SELECT * FROM DeliverymenMapping")
    _, historical = write_receipt(folder, "ent-historical.json")
    (folder / "ent-incomplete.json").write_bytes(b'{"horario_pedido":')
    if failed_optional_package:
        config.with_name("native-upgrade.json").write_text("incomplete rollout package", encoding="utf-8")
    current = Settings.from_yaml(str(config))
    pre_boot(tmp_path)
    upgrade = upgrade_installation(config, tmp_path)
    assert bool(upgrade.package_error) == failed_optional_package
    assert upgrade.adopted and upgrade.settings.api.native is None
    assert upgrade.unresolved_cds == 2
    assert current.api.source_timezone is None
    install_effective_settings(current, upgrade.settings)
    # A file arriving after installer adoption but before watcher startup must
    # not disappear into another observation baseline.
    _, fresh = write_receipt(folder, "ent-new.json")
    monkeypatch.setattr(application, "BUNDLE_DIR", str(tmp_path))
    services = application.build_services(current, upgrade)
    strategy = application.setup_strategy(current, services)
    journal = services.tracking_persistence.journal
    failures, successes = [], []
    journal.error.connect(failures.append)
    services.deliveries._dispatcher.task_failed.connect(lambda *args: failures.append(args))
    services.deliveries._dispatcher.delivery_success.connect(lambda *args: successes.append(args))
    try:
        assert strategy._baseline_path == upgrade.cds_baseline_path
        # Authentication is the boundary: use an already authenticated legacy
        # session. Dispatcher, journal, HTTP client and source watcher are real.
        services.deliveries._gateway.update_token("Bearer existing-test-session")
        services.deliveries.start_listening()
        qtbot.waitUntil(lambda: bool(successes) or bool(failures), timeout=20000)
        assert not failures
        assert successes == [(fresh, "new-legacy-delivery")]
        qtbot.waitUntil(lambda: bool(journal.journal.get_order(historical)["payload"]), timeout=5000)
        held = journal.journal.get_order(historical)
        assert held["submission"] == "NEEDS_REVIEW" and held["source_review"]
        assert services.delivery_repository.get_by_internal(historical) is not None
        assert len(requests) == 1  # No native/store-discovery calls or historical imports.
        sent = requests[0]
        assert sent["path"] == "/original-api/"
        assert sent["headers"]["Authorization"] == "Bearer existing-test-session"
        assert not any(key.lower() == "x-merchant-id" for key in sent["headers"])
        variables = sent["body"]["variables"]
        assert variables["idempotencyKey"] == fresh
        assert variables["timestamp"] == round(datetime(2026, 11, 13, 12, 34, 56).astimezone().timestamp() * 1000)
        assert variables["neighbourhood"] == "Centro"
        saved = journal.journal.get_order(fresh)
        assert saved["source_receipt_raw"] == "2026-11-13T12:34:56"
        assert json.loads(saved["payload"])["createdAt"] == "2026-11-13T12:34:56"
        assert saved["submission"] == "IMPORTED" and saved["add_attempts"] == 1
        assert json.loads(saved["request"])["timestamp"] == variables["timestamp"]
        assert current.api.add_delivery_max_retries == 7 and current.api.add_delivery_backoff_factor == 3
        assert rows(database, "SELECT * FROM DeliveryMapping WHERE internal_delivery_id='123'") == existing_mappings
        assert rows(database, "SELECT * FROM DeliverymenMapping") == existing_couriers
        assert config.read_bytes() == old_config
        assert not (config.parent / "manifest.json").exists()
        assert current.api.native is None and current.api.velide_server == endpoint
    finally:
        services.deliveries.stop_listening()
        services.tracking_persistence._cleanup_service.stop_routine()
        services.reconciliation.timer.stop()
        services.websockets.stop_service()
        journal.pool.waitForDone(5000)
        QThreadPool.globalInstance().waitForDone(5000)
        QApplication.processEvents()


def test_main_cds_keeps_naive_time_policy_when_unused_farmax_settings_exist(tmp_path, qtbot, monkeypatch):
    """Production main regression: an inactive ERP's settings are not CDS evidence."""
    import main as application

    config, _, _, document = installed(tmp_path, "CDS")
    document["farmax"]["timezone"] = "Asia/Tokyo"
    config.write_text(yaml.safe_dump(document), encoding="utf-8")
    upgrade = upgrade_installation(config, tmp_path)
    monkeypatch.setattr(application, "BUNDLE_DIR", str(tmp_path))
    services = application.build_services(upgrade.settings, upgrade)
    try:
        assert services.deliveries._api_config.source_timezone is None, "main.build_services must only infer Farmax timezone for TargetSystem.FARMAX"
        assert services.tracking_persistence.journal.journal.source_timezone is None
    finally:
        services.deliveries.stop_listening()
        services.tracking_persistence._cleanup_service.stop_routine()
        services.tracking_persistence.journal.pool.waitForDone(5000)
        QThreadPool.globalInstance().waitForDone(5000)
        QApplication.processEvents()


def test_original_farmax_branch_is_adopted_through_the_real_journal_hook(tmp_path):
    from api.sync_journal import SyncJournal

    config, database, _, _ = installed(tmp_path)
    upgrade = upgrade_installation(config, tmp_path)
    journal = SyncJournal(str(database), configured_scope(upgrade.settings), configured_source_identity(upgrade.settings))
    journal.bind_farmax_branch("7")
    assert rows(database, "SELECT farmax_branch FROM SyncScope") == [("7",)]
    assert journal.restore()[0]["external_id"] == "old-delivery"
    with pytest.raises(ValueError, match="changed"):
        journal.bind_farmax_branch("8")
