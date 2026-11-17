import time
import jwt
import pytest

from config import AuthenticationConfig
from workers.stored_token_retriever_worker import StoredTokenRetrieverWorker, auth_context


@pytest.mark.parametrize("expired", [False, True])
@pytest.mark.parametrize("changed", ["domain", "client_id", "audience", "scope"])
def test_cutover_does_not_publish_or_refresh_previous_auth_context(qtbot, monkeypatch, expired, changed):
    old = AuthenticationConfig(domain="login.example", client_id="client", audience="api", scope="openid")
    new = old.model_copy(update={changed: "changed"})
    token = jwt.encode({"iss": "https://login.example/", "aud": "api", "azp": "client",
                        "exp": time.time() + (-100 if expired else 1000)}, "test-only-secret", algorithm="HS256")
    monkeypatch.setattr("workers.stored_token_retriever_worker.read_token_from_file",
                        lambda: {"access_token": token, "refresh_token": "old-refresh", "middleware_auth": auth_context(old)})
    worker = StoredTokenRetrieverWorker(new)
    published, refreshes, errors = [], [], []
    worker.signals.token.connect(lambda *args: published.append(args))
    worker.signals.expired.connect(refreshes.append)
    worker.signals.error.connect(errors.append)
    worker.run()
    assert not published and not refreshes
    assert errors == ["A conexão foi atualizada. Entre novamente para continuar usando o Velide."]


@pytest.mark.parametrize("expired", [False, True])
def test_original_auth0_token_survives_ordinary_application_upgrade(qtbot, monkeypatch, expired):
    config = AuthenticationConfig(domain="login.example", client_id="client", audience="api", scope="openid")
    token = jwt.encode({"iss": "https://login.example/", "aud": ["api", "userinfo"], "azp": "client",
                        "exp": time.time() + (-100 if expired else 1000)}, "test-only-secret", algorithm="HS256")
    monkeypatch.setattr("workers.stored_token_retriever_worker.read_token_from_file",
                        lambda: {"access_token": token, "refresh_token": "same-refresh"})
    worker = StoredTokenRetrieverWorker(config)
    published, refreshes = [], []
    worker.signals.token.connect(lambda *args: published.append(args))
    worker.signals.expired.connect(refreshes.append)
    worker.run()
    assert published == ([] if expired else [(token, "same-refresh")])
    assert refreshes == (["same-refresh"] if expired else [])


@pytest.mark.parametrize("expired", [False, True])
def test_old_bundle_with_explicit_scopes_cannot_authorize_a_new_scope(qtbot, monkeypatch, expired):
    config = AuthenticationConfig(domain="login.example", client_id="client", audience="api", scope="openid offline_access write:deliveries")
    token = jwt.encode({"iss": "https://login.example/", "aud": "api", "azp": "client",
                        "scope": "openid", "exp": time.time() + (-100 if expired else 1000)}, "test-only-secret", algorithm="HS256")
    monkeypatch.setattr("workers.stored_token_retriever_worker.read_token_from_file",
                        lambda: {"access_token": token, "refresh_token": "old-refresh"})
    worker = StoredTokenRetrieverWorker(config)
    published, refreshed = [], []
    worker.signals.token.connect(lambda *args: published.append(args))
    worker.signals.expired.connect(refreshed.append)
    worker.run()
    assert not published and not refreshed
