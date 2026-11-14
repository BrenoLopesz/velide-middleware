"""One frozen, preflighted legacy mutation; the journal owns all durable retries."""
import asyncio
import logging

import httpx
from PyQt5.QtCore import QObject, QRunnable, pyqtSignal
from pydantic import ValidationError

from api.velide_gateway import freeze_gateway
from models.velide_delivery_models import GraphQLRequestError, GraphQLResponseError, GraphQLParseError
from utils.log_messages import humanize_error


class LegacySignals(QObject):
    result = pyqtSignal(object)
    failed = pyqtSignal(dict)


class LegacySyncError(Exception):
    def __init__(self, message, code, retryable):
        super().__init__(message)
        self.code, self.retryable = code, retryable


class LegacySyncWorker(QRunnable):
    def __init__(self, gateway, operation, argument, preflight=None):
        super().__init__()
        self.gateway = freeze_gateway(gateway)
        self.operation, self.argument, self.preflight = operation, argument, preflight
        self.signals = LegacySignals()

    def run(self):
        try:
            self.signals.result.emit(asyncio.run(self._run()))
        except Exception as error:
            if isinstance(error, LegacySyncError):
                code, retryable = error.code, error.retryable
            elif isinstance(error, GraphQLRequestError):
                code = "HTTP_{}".format(error.status_code)
                retryable = error.status_code in (401, 408, 429) or error.status_code >= 500
            elif isinstance(error, (httpx.RequestError, GraphQLParseError, GraphQLResponseError)):
                code, retryable = "TRANSPORT_ERROR", True
            else:
                code, retryable = "INVALID_REQUEST", not isinstance(error, (ValueError, ValidationError, TypeError))
            if code != "LOCAL_PRECONDITION":
                logging.getLogger(__name__).exception("Legacy %s failed: %s", self.operation, error,
                    extra={"user_message": humanize_error(error)})
            self.signals.failed.emit({"message": str(error), "code": code, "retryable": retryable})

    async def _run(self):
        client = self.gateway.get_client()
        if client is None:
            raise LegacySyncError("A sessão mudou antes do envio. Aguarde a conexão atual.", "AUTH_REQUIRED", True)
        async with client:
            if not self.gateway.is_ready() or (self.preflight is not None and not self.preflight()):
                raise LegacySyncError("A situação do pedido mudou antes do envio.", "LOCAL_PRECONDITION", False)
            if self.operation == "ADD":
                # No retry-reconciliation heuristics: recover ambiguous responses
                # only by replaying the same deployed idempotencyKey and request.
                response = await client.add_delivery(self.argument, retry=False)
                if not response or not response.id:
                    raise LegacySyncError("API returned success but no ID found.", "INVALID_RESPONSE", True)
                return response.id
            if self.operation == "DELETE":
                if not self.argument:
                    raise LegacySyncError("O cancelamento ainda não tem identificação da entrega no Velide.", "MISSING_DELIVERY_ID", False)
                if await client.delete_delivery(self.argument, retry=False) is not True:
                    raise LegacySyncError("O Velide ainda não confirmou o cancelamento da entrega.", "CANCEL_NOT_CONFIRMED", True)
                return self.argument
            raise ValueError("Operação legada desconhecida: " + self.operation)
