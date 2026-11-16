"""Native setup tests use real config/token persistence and offline doubles."""
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import threading
import time
import uuid

import jwt
import pytest
import yaml

from native_onboarding import (
    NativeOnboarding, NativeOnboardingCancelled, NativeOnboardingError,
)
from utils.token_storage import read_token_from_file


MERCHANT = "11111111-1111-4111-8111-111111111111"
CONNECTION = "22222222-2222-4222-8222-222222222222"
OTHER = "33333333-3333-4333-8333-333333333333"
AUTH = dict(domain="native-login.example", client_id="native-client",
            audience="native-api", scope="openid offline_access")
TARGET = dict(
    auth=AUTH,
    api=dict(velide_server="https://native.example/graphql",
             velide_websockets_server="wss://native.example/graphql",
             use_ssl=True,
             native=dict(merchant_id=MERCHANT, connection_id=CONNECTION,
                         timezone="America/Sao_Paulo")))
FARMAX = dict(host="localhost", file="D:/ERP/store.fdb", user="SYSDBA",
              password="test-only-erp-password")


def grant(subject="native-user"):
    token = jwt.encode(
        {"iss": "https://native-login.example/", "sub": subject,
         "aud": AUTH["audience"], "azp": AUTH["client_id"],
         "scope": "openid", "exp": time.time() + 3600},
        "test-only-signing-key", algorithm="HS256")
    return dict(access_token=token, refresh_token="test-only-native-refresh",
                middleware_auth=deepcopy(AUTH))


class Transport:
    def __init__(self):
        self.calls = []
        self.connections = []
        self.target = deepcopy(TARGET)
        self.verify_response = None
        self.on_verify = None
        self.lose_connect_response = False

    def bootstrap(self):
        self.calls.append(("bootstrap",))
        return {"version": 1, "auth": deepcopy(AUTH)}

    def options(self, token):
        self.calls.append(("options", token))
        return {"merchants": [dict(merchant_id=MERCHANT, name="Loja autorizada",
                                   timezone="America/Sao_Paulo",
                                   connections=deepcopy(self.connections))]}

    def connect(self, token, installation_id, merchant_id, source, mode, connection_id=None):
        self.calls.append(("connect", token, installation_id, merchant_id, source, mode, connection_id))
        self.connections = [dict(connection_id=CONNECTION, source=source, enabled=True)]
        if self.lose_connect_response:
            self.lose_connect_response = False
            raise OSError("lost response")
        return dict(installation_id=installation_id, mode=mode, target=deepcopy(self.target))

    def verify(self, token, installation_id, merchant_id, connection_id):
        self.calls.append(("verify", token, installation_id, merchant_id, connection_id))
        if self.on_verify:
            self.on_verify()
        return deepcopy(self.verify_response or dict(authorized=True, target=self.target))


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    import requests
    def forbidden(*args, **kwargs):
        pytest.fail("onboarding tests must never make network requests")
    monkeypatch.setattr(requests.sessions.Session, "request", forbidden)


@pytest.fixture
def installation(tmp_path):
    resources = tmp_path / "resources"
    resources.mkdir()
    path = resources / "config.yml"
    path.write_text(yaml.safe_dump(dict(
        onboarding="native_required", sqlite_days_retention=45,
        local_extension={"preserved": True})), encoding="utf-8")
    return tmp_path, path


def prepared(installation, transport=None, source="Farmax", mode="create"):
    root, path = installation
    transport = transport or Transport()
    flow = NativeOnboarding(path, root, transport)
    bundle = grant()
    flow.bootstrap()
    flow.options(bundle["access_token"])
    flow.connect(bundle["access_token"], MERCHANT, source, mode,
                 CONNECTION if mode == "reconnect" else None)
    return flow, transport, bundle


@pytest.mark.parametrize("source", ["Farmax", "CDS"])
def test_native_config_commit_persists_token_then_verifies_exact_target(
        installation, monkeypatch, source):
    import native_onboarding
    flow, transport, bundle = prepared(installation, source=source)
    root, path = installation
    original = path.read_bytes()
    replacements = []
    replace = native_onboarding.os.replace

    def observe_replace(staging, destination):
        if Path(destination) == path:
            replacements.append("config")
            assert path.read_bytes() == original
            assert read_token_from_file(flow.token_path) == bundle
            assert transport.calls[-1] == (
                "verify", bundle["access_token"], flow.installation_id, MERCHANT, CONNECTION)
            assert json.loads(flow.state_path.read_text())["target"] == TARGET
            candidate = yaml.safe_load(Path(staging).read_text(encoding="utf-8"))
            assert candidate["installation_id"] == flow.installation_id
        return replace(staging, destination)

    monkeypatch.setattr(native_onboarding.os, "replace", observe_replace)
    settings = flow.complete(bundle["access_token"], farmax=FARMAX,
                             folder_to_watch=str(root), token_bundle=bundle)
    published = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert replacements == ["config"]
    assert "onboarding" not in published
    assert published["native_recovery_required"] is True
    assert published["installation_id"] == flow.installation_id
    assert published["auth"] == TARGET["auth"]
    assert published["api"] == TARGET["api"]
    assert published["local_extension"] == {"preserved": True}
    assert settings.sqlite_days_retention == 45
    assert settings.installation_id == flow.installation_id
    assert settings.token_path == str(flow.token_path)
    assert settings.api.native.connection_id == CONNECTION
    assert not (root / "resources/token.txt").exists()
    assert not flow.database_path.exists()
    if source == "Farmax":
        assert settings.farmax.timezone == "America/Sao_Paulo"
    else:
        assert settings.folder_to_watch == str(root)
    assert [call[0] for call in transport.calls] == ["bootstrap", "options", "connect", "verify"]


def test_explicit_reconnect_uses_authorized_existing_connection(installation):
    transport = Transport()
    transport.connections = [dict(connection_id=CONNECTION, source="Farmax", enabled=True)]
    flow, transport, bundle = prepared(installation, transport, mode="reconnect")
    flow.complete(bundle["access_token"], farmax=FARMAX, token_bundle=bundle)
    assert transport.calls[2][5:] == ("reconnect", CONNECTION)
    assert yaml.safe_load(installation[1].read_text())["native_recovery_required"]


@pytest.mark.parametrize("source", ["Farmax", "CDS"])
def test_published_installation_id_survives_config_reload_and_native_db_adoption(
        installation, source):
    from config import Settings
    from local_upgrade import upgrade_installation
    flow, _, bundle = prepared(installation, source=source)
    root, path = installation
    flow.complete(bundle["access_token"], farmax=FARMAX,
                  folder_to_watch=str(root), token_bundle=bundle)
    assert Settings.from_yaml(str(path)).installation_id == flow.installation_id
    adopted = upgrade_installation(path, root)
    assert adopted.installation_id == flow.installation_id
    connection = sqlite3.connect(adopted.database_path)
    try:
        assert connection.execute(
            "SELECT installation_id FROM UpgradeInstallation WHERE id=1"
        ).fetchone() == (flow.installation_id,)
    finally:
        connection.close()
    assert upgrade_installation(path, root).installation_id == flow.installation_id


@pytest.mark.parametrize("merchant,source,mode,connection_id", [
    (OTHER, "Farmax", "create", None),
    (MERCHANT, "OtherERP", "create", None),
    (MERCHANT, "Farmax", "automatic", None),
    (MERCHANT, "Farmax", "create", CONNECTION),
    (MERCHANT, "Farmax", "reconnect", None),
    (MERCHANT, "Farmax", "reconnect", OTHER),
    (MERCHANT, "CDS", "reconnect", CONNECTION),
])
def test_cannot_claim_an_unselected_or_unauthorized_connection(
        installation, merchant, source, mode, connection_id):
    root, path = installation
    transport = Transport()
    transport.connections = [dict(connection_id=CONNECTION, source="Farmax", enabled=True)]
    flow = NativeOnboarding(path, root, transport)
    token = grant()["access_token"]
    flow.bootstrap()
    flow.options(token)
    with pytest.raises(NativeOnboardingError):
        flow.connect(token, merchant, source, mode, connection_id)
    assert not any(call[0] == "connect" for call in transport.calls)


@pytest.mark.parametrize("enabled", [False, True])
def test_create_requires_explicit_reconnect_when_source_already_exists(installation, enabled):
    root, path = installation
    transport = Transport()
    transport.connections = [dict(connection_id=CONNECTION, source="Farmax", enabled=enabled)]
    flow = NativeOnboarding(path, root, transport)
    token = grant()["access_token"]
    flow.bootstrap()
    flow.options(token)
    with pytest.raises(NativeOnboardingError, match="Reconectar"):
        flow.connect(token, MERCHANT, "Farmax", "create")
    if not enabled:
        with pytest.raises(NativeOnboardingError, match="ativa"):
            flow.connect(token, MERCHANT, "Farmax", "reconnect", CONNECTION)
    assert not any(call[0] == "connect" for call in transport.calls)


@pytest.mark.parametrize("change", ["auth", "endpoint", "timezone", "connection", "revoked"])
def test_verification_change_prevents_activation(installation, change):
    flow, transport, bundle = prepared(installation)
    original = installation[1].read_bytes()
    response = dict(authorized=True, target=deepcopy(TARGET))
    if change == "auth":
        response["target"]["auth"]["scope"] += " another-scope"
    elif change == "endpoint":
        response["target"]["api"]["velide_websockets_server"] += "/changed"
    elif change == "timezone":
        response["target"]["api"]["native"]["timezone"] = "UTC"
    elif change == "connection":
        response["target"]["api"]["native"]["connection_id"] = OTHER
    else:
        response["authorized"] = False
    transport.verify_response = response
    with pytest.raises(NativeOnboardingError, match="acesso"):
        flow.complete(bundle["access_token"], farmax=FARMAX, token_bundle=bundle)
    assert installation[1].read_bytes() == original
    assert not list(installation[1].parent.glob("config.yml.*.tmp"))


@pytest.mark.parametrize("failure", ["token_write", "config_replace"])
def test_publication_failure_is_restartable_with_the_same_installation(
        installation, monkeypatch, failure):
    import native_onboarding
    import utils.token_storage
    flow, transport, bundle = prepared(installation)
    original = installation[1].read_bytes()
    identity = flow.installation_id
    with monkeypatch.context() as patcher:
        if failure == "token_write":
            def fail(*args):
                raise OSError("disk full")
            patcher.setattr(utils.token_storage, "store_token_at_file", fail)
        else:
            replace = native_onboarding.os.replace
            def fail(source, destination):
                if Path(destination) == installation[1]:
                    raise OSError("config locked")
                return replace(source, destination)
            patcher.setattr(native_onboarding.os, "replace", fail)
        with pytest.raises(OSError):
            flow.complete(bundle["access_token"], farmax=FARMAX, token_bundle=bundle)
    assert installation[1].read_bytes() == original
    if failure == "token_write":
        assert not any(call[0] == "verify" for call in transport.calls)
    retried, _, _ = prepared(installation, transport)
    assert retried.installation_id == identity
    retried.complete(bundle["access_token"], farmax=FARMAX, token_bundle=bundle)
    published = yaml.safe_load(installation[1].read_text())
    assert published["api"] == TARGET["api"]
    assert published["installation_id"] == identity


def test_lost_connect_response_reuses_uuid_even_if_connection_now_exists(installation):
    root, path = installation
    transport = Transport()
    transport.lose_connect_response = True
    flow = NativeOnboarding(path, root, transport)
    token = grant()["access_token"]
    flow.bootstrap()
    flow.options(token)
    with pytest.raises(OSError, match="lost"):
        flow.connect(token, MERCHANT, "Farmax", "create")
    restarted, _, _ = prepared(installation, transport)
    assert restarted.installation_id == flow.installation_id
    calls = [call for call in transport.calls if call[0] == "connect"]
    assert calls[0][2:] == calls[1][2:]


@pytest.mark.parametrize("mutation", ["scope", "missing_metadata", "different_token", "missing_refresh"])
def test_wrong_or_unbound_token_bundle_is_never_published(installation, mutation):
    flow, transport, bundle = prepared(installation)
    token = bundle["access_token"]
    if mutation == "scope":
        bundle["middleware_auth"]["scope"] = "other"
    elif mutation == "missing_metadata":
        bundle.pop("middleware_auth")
    elif mutation == "different_token":
        bundle["access_token"] = grant("other-user")["access_token"]
    else:
        bundle.pop("refresh_token")
    original = installation[1].read_bytes()
    with pytest.raises(NativeOnboardingError, match="sessão"):
        flow.complete(token, farmax=FARMAX, token_bundle=bundle)
    assert installation[1].read_bytes() == original
    assert not any(call[0] == "verify" for call in transport.calls)


@pytest.mark.parametrize("claim,value", [
    ("iss", "https://different-login.example/"),
    ("aud", "different-api"),
    ("aud", ["different-api", "userinfo"]),
    ("azp", "different-client"),
    ("scope", "profile"),
])
def test_matching_cache_metadata_cannot_relabel_actual_jwt_context(
        installation, claim, value):
    from utils.token_storage import store_token_at_file
    root, path = installation
    original = path.read_bytes()
    transport = Transport()
    flow = NativeOnboarding(path, root, transport)
    bundle = grant()
    claims = jwt.decode(bundle["access_token"], options={"verify_signature": False})
    claims[claim] = value
    bundle["access_token"] = jwt.encode(claims, "test-only-signing-key", algorithm="HS256")
    flow.bootstrap()
    # The test server deliberately accepts the token, isolating the local
    # context check from principal continuity and server authorization checks.
    flow.options(bundle["access_token"])
    flow.connect(bundle["access_token"], MERCHANT, "Farmax", "create")
    store_token_at_file(bundle, flow.token_path)
    with pytest.raises(NativeOnboardingError, match="sessão nativa não corresponde"):
        flow.prepared_session()
    with pytest.raises(NativeOnboardingError, match="sessão nativa não corresponde"):
        flow.complete(bundle["access_token"], farmax=FARMAX, token_bundle=bundle)
    assert bundle["middleware_auth"] == AUTH
    assert path.read_bytes() == original
    assert not any(call[0] == "verify" for call in transport.calls)


def test_jwt_expiry_and_permission_checks_remain_server_authoritative(installation):
    from utils.token_storage import store_token_at_file
    root, path = installation
    transport = Transport()
    flow = NativeOnboarding(path, root, transport)
    bundle = grant()
    claims = jwt.decode(bundle["access_token"], options={"verify_signature": False})
    claims.update(exp=time.time() - 60, aud=[AUTH["audience"], "userinfo"], scope="openid")
    bundle["access_token"] = jwt.encode(claims, "untrusted-signature", algorithm="HS256")
    flow.bootstrap()
    flow.options(bundle["access_token"])
    flow.connect(bundle["access_token"], MERCHANT, "Farmax", "create")
    store_token_at_file(bundle, flow.token_path)
    assert flow.prepared_session() == bundle
    transport.verify_response = dict(authorized=False, target=deepcopy(TARGET))
    with pytest.raises(NativeOnboardingError, match="acesso"):
        flow.complete(bundle["access_token"], farmax=FARMAX)
    assert transport.calls[-1][0] == "verify"
    assert yaml.safe_load(path.read_text())["onboarding"] == "native_required"


def test_account_switch_cannot_reuse_an_incomplete_installation(installation):
    flow, transport, _ = prepared(installation)
    with pytest.raises(NativeOnboardingError, match="conta original"):
        flow.options(grant("another-user")["access_token"])
    assert transport.calls[-1][0] == "connect"


def test_account_can_change_before_any_connection_is_requested(installation):
    root, path = installation
    transport = Transport()
    flow = NativeOnboarding(path, root, transport)
    flow.bootstrap()
    flow.options(grant("wrong-account")["access_token"])
    token = grant("selected-account")["access_token"]
    flow.options(token)
    flow.connect(token, MERCHANT, "Farmax", "create")
    assert transport.calls[-1][1] == token


@pytest.mark.parametrize("interruption", ["cancel", "config_changed", "retained_data", "token_changed"])
def test_last_moment_interruption_cannot_publish(installation, interruption):
    from utils.token_storage import store_token_at_file
    flow, transport, bundle = prepared(installation)
    original = installation[1].read_bytes()
    def interrupt():
        if interruption == "cancel":
            flow.cancel()
        elif interruption == "config_changed":
            installation[1].write_bytes(original + b"\n# changed externally\n")
        elif interruption == "retained_data":
            database = sqlite3.connect(str(flow.database_path))
            database.execute("CREATE TABLE SyncOrders(id TEXT)")
            database.execute("INSERT INTO SyncOrders VALUES ('existing-order')")
            database.commit()
            database.close()
        else:
            store_token_at_file(grant("other-user"), flow.token_path)
    transport.on_verify = interrupt
    with pytest.raises(NativeOnboardingError):
        flow.complete(bundle["access_token"], farmax=FARMAX, token_bundle=bundle)
    assert yaml.safe_load(installation[1].read_text())["onboarding"] == "native_required"


@pytest.mark.parametrize("artifact", [
    "database", "other_database", "token", "orphan_native_token",
    "wal", "migration", "ownership", "missing_config", "installed_config",
])
def test_retained_installation_is_never_treated_as_new(installation, artifact):
    root, path = installation
    if artifact in ("database", "other_database", "ownership"):
        name = {"database": "velide.db", "other_database": "old.sqlite3",
                "ownership": "velide.db.ownership.db"}[artifact]
        connection = sqlite3.connect(str(path.parent / name))
        connection.execute("CREATE TABLE Retained(id TEXT)")
        connection.execute("INSERT INTO Retained VALUES ('must-not-claim')")
        connection.commit()
        connection.close()
    elif artifact == "missing_config":
        path.unlink()
    elif artifact == "installed_config":
        path.write_text(yaml.safe_dump(dict(auth=AUTH, api=TARGET["api"])))
    else:
        names = dict(token="token.txt", orphan_native_token="native-onboarding-token.txt",
                     wal="velide.db-wal", migration="native-upgrade.json")
        (path.parent / names[artifact]).write_bytes(b"retained")
    transport = Transport()
    with pytest.raises(NativeOnboardingError):
        NativeOnboarding(path, root, transport)
    assert not transport.calls
    assert not (path.parent / "native-onboarding.json").exists()


def test_empty_packaged_database_is_allowed_but_never_written(installation):
    root, path = installation
    db = path.parent / "velide.db"
    connection = sqlite3.connect(str(db))
    connection.execute("CREATE TABLE DeliveryMapping(id TEXT)")
    connection.close()
    original = db.read_bytes()
    flow, _, bundle = prepared(installation)
    flow.complete(bundle["access_token"], farmax=FARMAX, token_bundle=bundle)
    assert db.read_bytes() == original


def test_state_uuid_is_durable_and_corrupt_state_is_not_reset(installation):
    root, path = installation
    flow = NativeOnboarding(path, root, Transport())
    assert str(uuid.UUID(flow.installation_id)) == flow.installation_id
    restarted = NativeOnboarding(path, root, Transport())
    assert restarted.installation_id == flow.installation_id
    flow.state_path.write_text("{}")
    with pytest.raises(NativeOnboardingError, match="estado"):
        NativeOnboarding(path, root, Transport())
    assert flow.state_path.read_text() == "{}"


def test_source_validation_precedes_publication(installation):
    flow, _, bundle = prepared(installation)
    with pytest.raises(NativeOnboardingError, match="Firebird"):
        flow.complete(bundle["access_token"], farmax=dict(FARMAX, password=""), token_bundle=bundle)
    with pytest.raises(NativeOnboardingError, match="fuso"):
        flow.complete(bundle["access_token"], farmax=dict(FARMAX, timezone="UTC"), token_bundle=bundle)


def fake_auth_factory(bundle):
    from PyQt5.QtCore import QObject, pyqtSignal
    from utils.token_storage import store_token_at_file

    class FakeAuth(QObject):
        device_code = pyqtSignal(dict)
        access_token = pyqtSignal(str)
        error = pyqtSignal(str, str)

        def __init__(self, auth_config, gateway, parent=None, token_path=None):
            super().__init__(parent)
            self.auth_config = auth_config
            self.token_path = token_path
            self.invalidations = 0

        def load_stored_token(self):
            self.error.emit("No cache", "")

        def load_device_flow(self):
            self.device_code.emit(dict(user_code="TEST-CODE",
                                       verification_uri="https://native-login.example/activate"))
            store_token_at_file(bundle, self.token_path)
            self.access_token.emit(bundle["access_token"])

        def invalidate_session(self):
            self.invalidations += 1

        def pause_for_rollout(self):
            pass

        def resume_after_rollout(self):
            pass
    return FakeAuth


def test_dialog_is_interactive_and_network_work_runs_off_the_gui_thread(
        installation, qtbot, monkeypatch):
    from PyQt5.QtCore import QThread, Qt
    from PyQt5.QtWidgets import QApplication, QDialog
    from visual.screens.native_onboarding_screen import NativeOnboardingScreen
    from api.velide_gateway import VelideGateway

    def forbid_gateway(*args, **kwargs):
        pytest.fail("native onboarding must not construct a runtime gateway")
    monkeypatch.setattr(VelideGateway, "__init__", forbid_gateway)
    transport = Transport()
    threads = []
    for name in ("bootstrap", "options", "connect", "verify"):
        original = getattr(transport, name)
        def checked(*args, original=original, **kwargs):
            threads.append(QThread.currentThread() != QApplication.instance().thread())
            return original(*args, **kwargs)
        monkeypatch.setattr(transport, name, checked)
    root, path = installation
    screen = NativeOnboardingScreen(path, root, transport, auth_service_factory=fake_auth_factory(grant()))
    qtbot.addWidget(screen)
    screen.show()
    qtbot.waitUntil(lambda: screen.auth is not None and not screen._busy)
    qtbot.mouseClick(screen.login_button, Qt.LeftButton)
    qtbot.waitUntil(lambda: screen.merchant.count() == 2 and not screen._busy)
    screen.merchant.setCurrentIndex(1)
    screen.source.setCurrentText("CDS")
    screen.mode.setCurrentIndex(screen.mode.findData("create"))
    screen.cds_folder.setText(str(root))
    qtbot.mouseClick(screen.finish_button, Qt.LeftButton)
    qtbot.waitUntil(lambda: screen.result() == QDialog.Accepted)
    assert screen.settings.api.native.connection_id == CONNECTION
    assert screen.settings.target_system.value == "CDS"
    assert screen.auth.invalidations >= 1
    assert threads == [True, True, True, True]


def test_dialog_cancellation_during_verification_waits_without_activating(installation, qtbot):
    from PyQt5.QtCore import Qt
    from PyQt5.QtWidgets import QDialog
    from visual.screens.native_onboarding_screen import NativeOnboardingScreen
    entered, release = threading.Event(), threading.Event()
    transport = Transport()
    def hold_verify():
        entered.set()
        assert release.wait(5)
    transport.on_verify = hold_verify
    root, path = installation
    original = path.read_bytes()
    screen = NativeOnboardingScreen(path, root, transport, auth_service_factory=fake_auth_factory(grant()))
    qtbot.addWidget(screen)
    screen.show()
    try:
        qtbot.waitUntil(lambda: screen.auth is not None and not screen._busy)
        qtbot.mouseClick(screen.login_button, Qt.LeftButton)
        qtbot.waitUntil(lambda: screen.merchant.count() == 2 and not screen._busy)
        screen.merchant.setCurrentIndex(1)
        screen.mode.setCurrentIndex(screen.mode.findData("create"))
        screen.farmax_file.setText(FARMAX["file"])
        screen.farmax_password.setText(FARMAX["password"])
        qtbot.mouseClick(screen.finish_button, Qt.LeftButton)
        qtbot.waitUntil(entered.is_set)
        qtbot.mouseClick(screen.cancel_button, Qt.LeftButton)
        assert screen.isVisible()  # GUI remains responsive; worker is fenced.
        release.set()
        qtbot.waitUntil(lambda: not screen.isVisible())
        assert screen.result() == QDialog.Rejected
        assert screen.settings is None
        assert path.read_bytes() == original
    finally:
        release.set()
        qtbot.waitUntil(lambda: not screen._busy)


def test_cancelled_headless_setup_never_contacts_transport(installation):
    root, path = installation
    transport = Transport()
    flow = NativeOnboarding(path, root, transport)
    flow.cancel()
    with pytest.raises(NativeOnboardingCancelled):
        flow.bootstrap()
    assert not transport.calls


def test_real_auth_refresh_rotation_drains_before_native_handoff(installation, qtbot, monkeypatch):
    """Use the real AuthService/workers, replacing only their HTTP operations."""
    import requests
    from PyQt5.QtCore import Qt
    from PyQt5.QtWidgets import QDialog
    from utils.access_token import AccessToken
    from utils.device_code import DeviceCode
    from visual.screens.native_onboarding_screen import NativeOnboardingScreen

    original_bundle, rotated_bundle = grant(), grant()
    rotated_bundle["refresh_token"] = "rotated-native-refresh"
    monkeypatch.setattr(DeviceCode, "request", lambda _: dict(
        user_code="TEST", device_code="device-test", expires_in=300, interval=1,
        verification_uri="https://native-login.example/activate"))
    monkeypatch.setattr(AccessToken, "request", lambda _: deepcopy(original_bundle))
    entered, release = threading.Event(), threading.Event()

    def refresh(url, **kwargs):
        assert url == "https://native-login.example/oauth/token"
        entered.set()
        assert release.wait(5)
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps(rotated_bundle).encode("utf-8")
        return response
    monkeypatch.setattr(requests, "post", refresh)
    root, path = installation
    original_config = path.read_bytes()
    transport = Transport()
    screen = NativeOnboardingScreen(path, root, transport)
    qtbot.addWidget(screen)
    screen.show()
    try:
        qtbot.waitUntil(lambda: screen.auth is not None and not screen._busy)
        qtbot.mouseClick(screen.login_button, Qt.LeftButton)
        qtbot.waitUntil(lambda: screen.merchant.count() == 2 and not screen._busy)
        screen.merchant.setCurrentIndex(1)
        screen.source.setCurrentText("CDS")
        screen.mode.setCurrentIndex(screen.mode.findData("create"))
        screen.cds_folder.setText(str(root))
        screen.auth._manual_refresh_token(original_bundle["refresh_token"])
        qtbot.waitUntil(entered.is_set)
        qtbot.mouseClick(screen.finish_button, Qt.LeftButton)
        assert screen.auth._rollout_paused
        assert path.read_bytes() == original_config
        assert not any(call[0] == "connect" for call in transport.calls)
        release.set()
        qtbot.waitUntil(lambda: screen.result() == QDialog.Accepted)
        assert read_token_from_file(screen.onboarding.token_path) == rotated_bundle
        assert transport.calls[-1][1] == rotated_bundle["access_token"]
        assert screen._auth_pool.activeThreadCount() == 0
        assert screen.auth._bundle_access_token is None
        assert not screen.auth._refresh_timer.isActive()
    finally:
        release.set()
        qtbot.waitUntil(lambda: not screen._busy)


@pytest.mark.parametrize("field", ["auth", "merchant", "connection", "installation"])
def test_connect_cannot_substitute_the_requested_target(installation, field):
    root, path = installation
    transport = Transport()
    transport.connections = [dict(connection_id=CONNECTION, source="Farmax", enabled=True)]
    flow = NativeOnboarding(path, root, transport)
    token = grant()["access_token"]
    flow.bootstrap()
    flow.options(token)
    if field == "auth":
        transport.target["auth"]["client_id"] = "different-client"
    elif field in ("merchant", "connection"):
        transport.target["api"]["native"][field + "_id"] = OTHER
    else:
        connect = transport.connect
        def wrong_installation(*args, **kwargs):
            result = connect(*args, **kwargs)
            result["installation_id"] = OTHER
            return result
        transport.connect = wrong_installation
    with pytest.raises(NativeOnboardingError):
        flow.connect(token, MERCHANT, "Farmax", "reconnect", CONNECTION)
    assert yaml.safe_load(path.read_text())["onboarding"] == "native_required"
    assert not json.loads(flow.state_path.read_text()).get("target")


def test_parent_dialog_signature_uses_packaged_transport_without_legacy_services(
        installation, qtbot, monkeypatch):
    from middleware_transport import MiddlewareTransport
    from visual.screens.native_onboarding_screen import NativeOnboardingDialog
    root, path = installation
    (path.parent / "middleware-service.json").write_text(json.dumps(dict(
        version=1, base_url="https://native.example/api/middleware/v1")))
    urls = []
    def bootstrap(transport):
        urls.append(transport.base_url)
        return dict(version=1, auth=deepcopy(AUTH))
    monkeypatch.setattr(MiddlewareTransport, "bootstrap", bootstrap)
    dialog = NativeOnboardingDialog(path, root)
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitUntil(lambda: dialog.auth is not None and not dialog._busy)
    assert urls == ["https://native.example/api/middleware/v1"]
    assert dialog.auth._token_path == str(path.parent / "native-onboarding-token.txt")
    dialog.close()
    qtbot.waitUntil(lambda: not dialog.isVisible() and not dialog._busy)
    assert yaml.safe_load(path.read_text())["onboarding"] == "native_required"
