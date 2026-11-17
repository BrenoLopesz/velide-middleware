from pathlib import Path
import pytest
from PyQt5.QtCore import QThreadPool
from config import Settings, NativeApiConfig
from main import build_services


@pytest.mark.parametrize("native", [False, True])
def test_application_services_compose_without_live_services(tmp_path, qtbot, native):
    config = Settings.from_yaml(str(Path(__file__).parents[1] / "fixtures" / "test_config.yml"))
    config.sqlite_path = str(tmp_path / "application.db")
    if native:
        config.api.velide_server = "https://api.invalid/graphql"
        config.api.velide_websockets_server = "wss://api.invalid/graphql"
        config.api.native = NativeApiConfig(merchant_id="00000000-0000-0000-0000-000000000001",
            connection_id="00000000-0000-0000-0000-000000000002", timezone="America/Sao_Paulo")
    services = build_services(config)
    assert services.deliveries.supports_native_retry == native
    assert not services.auth._gateway.is_ready()
    services.deliveries.stop_listening()
    services.tracking_persistence._cleanup_service.stop_routine()
    services.deliveries._journal.pool.waitForDone()
    QThreadPool.globalInstance().waitForDone()
