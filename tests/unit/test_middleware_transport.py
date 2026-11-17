import json
import threading
import time
from unittest.mock import Mock

import pytest

from middleware_transport import MiddlewareTransport, MiddlewareError, MAX_BYTES, service_url, sha256_text


def transport_response(payload, status=200):
    response = Mock(status_code=status)
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.iter_content.return_value = [json.dumps(payload).encode("utf-8")]
    session = Mock()
    session.request.return_value = response
    return MiddlewareTransport("https://middleware.invalid/api/middleware/v1", session=session), session, response


def test_public_bootstrap_clears_credentials_and_protected_calls_do_not_redirect():
    client, session, _ = transport_response({"version": 1, "auth": {
        "domain": "auth.invalid", "client_id": "client", "audience": "api", "scope": "openid"}})
    client.bootstrap()
    assert session.request.call_args.kwargs["headers"]["Authorization"] is None
    assert session.request.call_args.kwargs["allow_redirects"] is False
    assert session.request.call_args.kwargs["timeout"] == (5, 15)
    client.request("GET", "/test", "native-token")
    assert session.request.call_args.kwargs["headers"]["Authorization"] == "Bearer native-token"


@pytest.mark.parametrize("url", ["http://production.invalid/api/middleware/v1",
                               "https://user:password@host.invalid/api/middleware/v1",
                               "https://host.invalid/api/middleware/v1?token=value",
                               "https://host.invalid/guessed",
                               "http://127.0.0.1/api/middleware/v1"])
def test_bad_deployment_urls_rejected(url):
    with pytest.raises(MiddlewareError):
        MiddlewareTransport(url)


def test_loopback_requires_explicit_opt_in():
    assert MiddlewareTransport("http://127.0.0.1:4000/api/middleware/v1", allow_loopback=True)
    with pytest.raises(MiddlewareError):
        MiddlewareTransport("http://10.1.1.1/api/middleware/v1", allow_loopback=True)


def test_missing_resource_is_actionable(tmp_path):
    with pytest.raises(MiddlewareError, match="middleware-service.json"):
        service_url(tmp_path)


def test_unconfigured_source_placeholder_is_deployment_state(tmp_path):
    resources = tmp_path / "resources"
    resources.mkdir()
    (resources / "middleware-service.json").write_text(
        '{"version":1,"base_url":"https://deployment-required.invalid/api/middleware/v1"}')
    with pytest.raises(MiddlewareError, match="middleware-service.json"):
        service_url(tmp_path)


def test_redirect_and_oversized_response_are_rejected():
    client, _, response = transport_response({}, 302)
    with pytest.raises(MiddlewareError, match="redirects"):
        client.request("GET", "/test", "token")
    response.status_code = 200
    response.iter_content.return_value = [b"x" * (MAX_BYTES + 1)]
    with pytest.raises(MiddlewareError, match="8 MiB"):
        client.request("GET", "/test", "token")


def test_inventory_hash_is_exact_utf8_string():
    client, session, _ = transport_response({})
    inventory = '{ "name": "São Paulo" }\n'
    client.submit_inventory("legacy", "10000000-0000-0000-0000-000000000001",
                            "10000000-0000-0000-0000-000000000002", inventory)
    body = json.loads(session.request.call_args.kwargs["data"].decode("utf-8"))
    assert body["inventory_json"] == inventory
    assert body["inventory_sha256"] == sha256_text(inventory)
    assert body["inventory_sha256"] != sha256_text(json.dumps(json.loads(inventory)))


def test_trickling_response_has_total_bound_and_late_result_is_discarded():
    client, _, response = transport_response({})
    client.total_timeout = .05
    release = threading.Event()
    def trickle(_size):
        release.wait(2)
        yield b'{"late":true}'
    response.iter_content.side_effect = trickle
    start = time.monotonic()
    try:
        with pytest.raises(MiddlewareError, match="deadline"):
            client.request("GET", "/test", "token")
        assert time.monotonic() - start < .5
    finally:
        release.set()


def test_paused_deadline_overrides_general_transport_timeout():
    client, _, response = transport_response({})
    client.total_timeout = 10
    client.deadline = time.monotonic() + .03
    release = threading.Event()
    response.iter_content.side_effect = lambda size: (release.wait(2), b"{}")[1:]
    try:
        with pytest.raises(MiddlewareError, match="deadline"):
            client.request("GET", "/test", "token")
    finally:
        release.set()
