import httpx
import pytest

from api.native_velide import NativeVelide, NativeApiError
from test_transport_compatibility import native_config
from config import TargetSystem


@pytest.mark.asyncio
@pytest.mark.parametrize("code,retryable", [
    ("INTERNAL_SERVER_ERROR", True), ("AUTHORIZATION_UNAVAILABLE", True),
    ("TEMPORARY_FAILURE", True), ("GRAPHQL_VALIDATION_FAILED", False),
    ("FORBIDDEN", False), ("SOURCE_FORBIDDEN", False),
])
async def test_graphql_execution_error_classification(code, retryable):
    async with NativeVelide("token", native_config(), TargetSystem.CDS) as client:
        await client._client.aclose()
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200,
            json={"errors": [{"message": "simulated", "extensions": {"code": code}}]})))
        with pytest.raises(NativeApiError) as error:
            await client.validate_scope()
        assert error.value.retryable is retryable


@pytest.mark.asyncio
async def test_masked_temporary_error_and_explicit_retry_hint():
    for error in ({"message": "Unexpected error."},
                  {"message": "busy", "extensions": {"code": "DATABASE_BUSY", "retryable": True}}):
        async with NativeVelide("token", native_config(), TargetSystem.CDS) as client:
            await client._client.aclose()
            client._client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"errors": [error]})))
            with pytest.raises(NativeApiError) as captured:
                await client.validate_scope()
            assert captured.value.retryable


@pytest.mark.parametrize("transient", [True, False])
def test_scope_failure_recovers_on_retry_or_new_credentials(qtbot, monkeypatch, transient):
    from unittest.mock import MagicMock
    from services.durable_delivery_dispatcher import DurableDeliveryDispatcher
    from workers.native_api_worker import NativeSignals
    gateway = MagicMock()
    gateway.config.native = True
    gateway.generation = 1
    class Journal:
        def submit(self, method, *args, success=None, failure=None):
            if success:
                success([] if method == "restore" else None)
    workers = []
    def make_worker(*args, **kwargs):
        worker = MagicMock(signals=NativeSignals())
        workers.append(worker)
        return worker
    monkeypatch.setattr("services.durable_delivery_dispatcher.NativeApiWorker", make_worker)
    monkeypatch.setattr("services.durable_delivery_dispatcher.QThreadPool.globalInstance", lambda: MagicMock())
    dispatcher = DurableDeliveryDispatcher(gateway, Journal())
    try:
        dispatcher.start()
        workers[0].signals.failed.emit({"message": "simulated", "retryable": transient})
        assert dispatcher._running and not dispatcher._authorized
        dispatcher._pump()
        if not transient:
            assert len(workers) == 1
            gateway.generation = 2
            dispatcher._pump()
        assert len(workers) == 2
        workers[1].signals.result.emit({})
        assert dispatcher._authorized
    finally:
        dispatcher.stop()
