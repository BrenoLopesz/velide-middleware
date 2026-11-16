import time

import jwt
import pytest

from api.backend_ownership import BackendOwnership, ProfileOwnership
from api.velide_gateway import VelideGateway
from backend_profiles import token_principal
from config import ApiConfig, AuthenticationConfig, TargetSystem
from services.auth_service import AuthService
from utils.token_storage import read_token_from_file
from workers.stored_token_retriever_worker import StoredTokenRetrieverWorker


def token(subject, domain="original.invalid"):
    return jwt.encode({"iss": "https://" + domain + "/", "sub": subject, "aud": "audience",
                       "azp": "client", "exp": int(time.time()) + 3600}, "disposable-test-key", algorithm="HS256")


def auth_config(domain="original.invalid"):
    return AuthenticationConfig(domain=domain, client_id="client", audience="audience", scope="openid offline_access")


def test_wrong_account_is_rejected_before_token_storage_and_ui_publication(tmp_path, qtbot):
    registry = BackendOwnership(tmp_path / "owners.db", "source")
    registry.register("legacy", "https://legacy.invalid|CDS")
    registry.seed("legacy", ["123"], token_principal(token("A")))
    gateway = VelideGateway(ApiConfig(velide_server="https://legacy.invalid", velide_websockets_server="wss://legacy.invalid"), TargetSystem.CDS)
    gateway.ownership = ProfileOwnership(registry, "legacy")
    path = tmp_path / "legacy.token.txt"
    auth = AuthService(auth_config(), gateway, token_path=str(path))
    published, errors, invalidated = [], [], []
    auth.access_token.connect(published.append)
    auth.error.connect(lambda *args: errors.append(args))
    auth.session_invalidated.connect(lambda: invalidated.append(True))
    try:
        auth._commit_token(0, {"access_token": token("A"), "refresh_token": "A-refresh"})
        stored = path.read_bytes()
        with pytest.raises(ValueError, match="conta original"):
            auth._commit_token(0, {"access_token": token("B"), "refresh_token": "B-refresh"})
        assert path.read_bytes() == stored
        auth._on_access_token_received(token("B"), "B-refresh")
        assert not published and errors and invalidated and not gateway.is_ready()
    finally:
        auth.invalidate_session()


def test_changed_auth_cache_cannot_publish_or_refresh_old_tenant(tmp_path, qtbot):
    gateway = VelideGateway(ApiConfig(velide_server="https://native.invalid/graphql", velide_websockets_server="wss://native.invalid/graphql"), TargetSystem.CDS)
    path = tmp_path / "separate-native.token.txt"
    old = AuthService(auth_config(), gateway, token_path=str(path))
    old._commit_token(0, {"access_token": token("user"), "refresh_token": "old-tenant-refresh"})
    worker = StoredTokenRetrieverWorker(auth_config("new.invalid"), str(path))
    published, refreshed, errors = [], [], []
    worker.signals.token.connect(lambda *args: published.append(args))
    worker.signals.expired.connect(refreshed.append)
    worker.signals.error.connect(errors.append)
    worker.run()
    assert errors and not published and not refreshed
    assert read_token_from_file(path)["refresh_token"] == "old-tenant-refresh"
    old.invalidate_session()
