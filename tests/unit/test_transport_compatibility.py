import asyncio
import json
from datetime import datetime
from unittest.mock import AsyncMock

import httpx
import pytest

from api.native_velide import NativeVelide
from api.velide import Velide
from api.velide_gateway import VelideGateway
from config import ApiConfig, NativeApiConfig, TargetSystem
from models.velide_delivery_models import Order
from workers.legacy_sync_worker import LegacySyncWorker
from workers.velide_worker import VelideWorker


def config(**changes):
    return ApiConfig(velide_server="https://transport.invalid/graphql",
                     velide_websockets_server="wss://transport.invalid/graphql", **changes)


def native_config(**changes):
    return config(native=NativeApiConfig(merchant_id="00000000-0000-0000-0000-000000000001",
                  connection_id="00000000-0000-0000-0000-000000000002", timezone="America/Sao_Paulo"), **changes)


def order(**changes):
    values = dict(internal_id="cds-file-1", customerName="João", address="Rua A, 1",
                  createdAt="2026-11-13T12:00:00-03:00", neighbourhood="Centro")
    values.update(changes)
    return Order.model_validate(values)


def gateway(settings=None):
    instance = VelideGateway(settings or config(), TargetSystem.CDS)
    instance.update_token("Bearer installed-token")
    return instance


def install_transport(monkeypatch, handle):
    original = httpx.AsyncClient
    options = []
    def client(**kwargs):
        options.append(dict(kwargs))
        return original(transport=httpx.MockTransport(handle), **kwargs)
    monkeypatch.setattr(httpx, "AsyncClient", client)
    return options


def added_response():
    return httpx.Response(200, json={"data": {"addDeliveryFromIntegration": {
        "id": "legacy-remote-1", "createdAt": "2026-11-13T15:00:00Z"}}})


def test_no_journal_worker_still_adds_and_deletes_with_existing_credentials(qtbot, monkeypatch):
    seen = []
    def handle(request):
        seen.append(request)
        body = json.loads(request.content)
        if "addDeliveryFromIntegration" in body["query"]:
            return added_response()
        assert "deleteDelivery" in body["query"]
        return httpx.Response(200, json={"data": {"deleteDelivery": True}})
    install_transport(monkeypatch, handle)
    connection = gateway()
    added, deleted, errors = [], [], []
    add = VelideWorker.for_add_delivery(connection, order())
    add.signals.delivery_added.connect(added.append)
    add.signals.error.connect(errors.append)
    add.run()
    remove = VelideWorker.for_delete_delivery(connection, added[0]["id"])
    remove.signals.delivery_deleted.connect(deleted.append)
    remove.signals.error.connect(errors.append)
    remove.run()
    assert not errors
    assert deleted == ["legacy-remote-1"]
    assert len(seen) == 2
    assert all(request.headers["Authorization"] == "Bearer installed-token" for request in seen)
    assert json.loads(seen[0].content)["variables"]["idempotencyKey"] == "cds-file-1"
    assert json.loads(seen[1].content)["variables"] == {"deliveryId": "legacy-remote-1"}


@pytest.mark.parametrize("durable", [False, True])
def test_queued_worker_never_acquires_replacement_session_credentials(qtbot, monkeypatch, durable):
    seen = []
    install_transport(monkeypatch, lambda request: seen.append(request) or added_response())
    connection = gateway()
    worker = LegacySyncWorker(connection, "ADD", order()) if durable else VelideWorker.for_add_delivery(connection, order())
    errors = []
    (worker.signals.failed if durable else worker.signals.error).connect(errors.append)
    connection.update_token("Bearer replacement-token")
    worker.run()
    assert not seen
    assert errors


def test_durable_worker_preflight_rechecks_after_client_context_opens(qtbot, monkeypatch):
    seen = []
    install_transport(monkeypatch, lambda request: seen.append(request) or added_response())
    connection = gateway()
    calls = []
    worker = LegacySyncWorker(connection, "ADD", order(), preflight=lambda: calls.append(True) or False)
    errors = []
    worker.signals.failed.connect(errors.append)
    worker.run()
    assert calls == [True] and not seen
    assert errors[0]["code"] == "LOCAL_PRECONDITION"


@pytest.mark.parametrize("operation", ["ADD", "DELETE"])
def test_durable_worker_performs_one_http_attempt_without_inner_retries(qtbot, monkeypatch, operation):
    attempts = []
    def handle(request):
        attempts.append(request)
        raise httpx.ReadTimeout("Response lost", request=request)
    install_transport(monkeypatch, handle)
    sleep = AsyncMock()
    monkeypatch.setattr("utils.async_retry.asyncio.sleep", sleep)
    worker = LegacySyncWorker(gateway(config(add_delivery_max_retries=4)), operation,
                              order() if operation == "ADD" else "remote", preflight=lambda: True)
    errors, results = [], []
    worker.signals.failed.connect(errors.append)
    worker.signals.result.connect(results.append)
    worker.run()
    assert len(attempts) == 1 and not results
    assert errors[0]["retryable"] is True
    sleep.assert_not_awaited()


def test_no_journal_add_keeps_configured_attempt_count_and_delays(monkeypatch):
    seen = []
    def handle(request):
        seen.append(request)
        if len(seen) < 3:
            raise httpx.ReadTimeout("Response lost", request=request)
        return added_response()
    install_transport(monkeypatch, handle)
    sleep = AsyncMock()
    monkeypatch.setattr("utils.async_retry.asyncio.sleep", sleep)
    async def run():
        async with Velide("token", config(add_delivery_max_retries=3, add_delivery_initial_delay=0.25,
                          add_delivery_backoff_factor=3), TargetSystem.CDS) as client:
            return await client.add_delivery(order())
    assert asyncio.run(run()).id == "legacy-remote-1"
    assert len(seen) == 3
    assert [call.args[0] for call in sleep.await_args_list] == [0.25, 0.75]


def test_no_journal_delete_keeps_existing_three_attempts(monkeypatch):
    seen = []
    def handle(request):
        seen.append(request)
        raise httpx.ReadTimeout("Response lost", request=request)
    install_transport(monkeypatch, handle)
    sleep = AsyncMock()
    monkeypatch.setattr("utils.async_retry.asyncio.sleep", sleep)
    async def run():
        async with Velide("token", config(), TargetSystem.CDS) as client:
            await client.delete_delivery("remote")
    with pytest.raises(httpx.ReadTimeout):
        asyncio.run(run())
    assert len(seen) == 3
    assert [call.args[0] for call in sleep.await_args_list] == [1.0, 2.0]


def test_naive_legacy_receipt_uses_windows_local_instant_then_replays_frozen_request():
    receipt = datetime(2026, 1, 13, 12, 34, 56)
    source = order(createdAt=receipt)
    client = Velide("token", config(), TargetSystem.CDS)
    frozen = client._build_variables_to_add_delivery(source).model_dump(mode="json", by_alias=True)
    assert frozen["timestamp"] == round(receipt.timestamp() * 1000)
    assert frozen["offset"] == 0
    source.request_variables = frozen
    source.created_at = datetime(2030, 1, 1)
    source.address = "Edited later"
    client._api_config.source_timezone = "Asia/Tokyo"
    assert client._build_variables_to_add_delivery(source).model_dump(mode="json", by_alias=True) == frozen


def test_configured_legacy_timezone_still_overrides_windows_local_interpretation():
    client = Velide("token", config(source_timezone="America/Sao_Paulo"), TargetSystem.CDS)
    value = client._build_variables_to_add_delivery(order(createdAt="2026-01-13T12:00:00"))
    assert value.timestamp == round(datetime.fromisoformat("2026-01-13T12:00:00-03:00").timestamp() * 1000)


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("ssl", [False, True])
def test_ssl_setting_is_respected_by_both_transports(monkeypatch, native, ssl):
    options = install_transport(monkeypatch, lambda _: httpx.Response(200, json={}))
    settings = native_config(use_ssl=ssl) if native else config(use_ssl=ssl)
    async def run():
        async with (NativeVelide if native else Velide)("token", settings, TargetSystem.CDS):
            pass
    asyncio.run(run())
    assert options[0]["verify"] is ssl
    assert options[0]["headers"]["Authorization"] == ("Bearer token" if native else "token")
    if native:
        assert options[0]["headers"]["X-Merchant-ID"] == settings.native.merchant_id


@pytest.mark.parametrize("use_neighbourhood", [False, True])
def test_native_neighbourhood_option_and_frozen_payload(use_neighbourhood):
    settings = native_config(use_neighbourhood=use_neighbourhood)
    client = NativeVelide("token", settings, TargetSystem.CDS)
    source = order()
    frozen = client._build_variables_to_add_delivery(source).model_dump()
    assert frozen["input"]["rawNeighbourhood"] == ("Centro" if use_neighbourhood else None)
    source.request_variables = frozen
    settings.use_neighbourhood = not use_neighbourhood
    assert client._build_variables_to_add_delivery(source).model_dump() == frozen


def test_durable_worker_does_not_report_false_delete_as_success(qtbot, monkeypatch):
    install_transport(monkeypatch, lambda _: httpx.Response(200, json={"data": {"deleteDelivery": False}}))
    worker = LegacySyncWorker(gateway(), "DELETE", "remote", preflight=lambda: True)
    errors, results = [], []
    worker.signals.failed.connect(errors.append)
    worker.signals.result.connect(results.append)
    worker.run()
    assert not results
    assert errors[0]["code"] == "CANCEL_NOT_CONFIRMED"
