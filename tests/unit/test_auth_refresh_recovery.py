import json
from unittest.mock import MagicMock

from config import AuthenticationConfig
from workers.refresh_token_worker import RefreshTokenWorker


def test_rotated_refresh_token_is_stored_before_being_emitted(qtbot, monkeypatch):
    config = AuthenticationConfig(domain="auth.invalid", client_id="test", scope="openid", audience="test")
    response = MagicMock(status_code=200, text=json.dumps({"access_token": "access", "refresh_token": "rotated"}))
    post = MagicMock(return_value=response)
    monkeypatch.setattr("workers.refresh_token_worker.requests.post", post)
    outcomes = []
    monkeypatch.setattr("workers.refresh_token_worker.store_token_at_file", lambda bundle: outcomes.append(("stored", bundle["refresh_token"])))
    worker = RefreshTokenWorker("old", config)
    worker.signals.token.connect(lambda access, refresh: outcomes.append(("emitted", refresh)))
    worker.run()
    assert outcomes == [("stored", "rotated"), ("emitted", "rotated")]
    assert post.call_args.kwargs["verify"] is True
    assert post.call_args.kwargs["timeout"] == 15


def test_failed_token_storage_does_not_publish_success(qtbot, monkeypatch):
    config = AuthenticationConfig(domain="auth.invalid", client_id="test", scope="openid", audience="test")
    response = MagicMock(status_code=200, text=json.dumps({"access_token": "access"}))
    monkeypatch.setattr("workers.refresh_token_worker.requests.post", MagicMock(return_value=response))
    monkeypatch.setattr("workers.refresh_token_worker.store_token_at_file", MagicMock(side_effect=OSError("disk full")))
    worker = RefreshTokenWorker("old", config)
    success, errors = [], []
    worker.signals.token.connect(lambda *args: success.append(args))
    worker.signals.error.connect(errors.append)
    worker.run()
    assert not success and len(errors) == 1
